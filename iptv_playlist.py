"""Shared M3U parsing, adult filtering, and bounded stream checks."""

import html
import re
import unicodedata
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.request import Request, urlopen


BLOCKED_ADULT_DOMAINS = ("kaamdesi.com",)
PORN_LABEL = re.compile(
    r"porn|xxx|erotic|erotik|erotico|playboy|hustler|brazzers|penthouse|"
    r"dorcel|red\W*light|private\W*tv|sex\W*prive|sex\W*treme|"
    r"sexy?\W*hot|\badult(?:s|o|os)?\b(?!\W*swim\b)"
)
CHANNEL_ATTRIBUTE = re.compile(
    r'''\b([\w-]+)\s*=\s*(["'])(.*?)\2''',
    re.IGNORECASE,
)
HEADER_NAMES = {
    "http-user-agent": "User-Agent",
    "user-agent": "User-Agent",
    "http-referrer": "Referer",
    "referer": "Referer",
    "origin": "Origin",
}


def decode_playlist(content):
    if isinstance(content, bytes):
        return content.decode("utf-8-sig")
    return content.lstrip("\ufeff")


def parse_entries(content):
    lines = decode_playlist(content).splitlines(keepends=True)
    entries, entry = [], []
    for line in lines:
        if line.strip().startswith("#EXTINF:"):
            if entry:
                entries.append(entry)
            entry = [line]
        elif entry:
            entry.append(line)
    if entry:
        entries.append(entry)
    return entries


def entry_attributes(entry):
    return {
        match.group(1).casefold(): html.unescape(match.group(3))
        for match in CHANNEL_ATTRIBUTE.finditer(entry[0])
    }


def normalized_text(value):
    value = html.unescape(value).casefold()
    return "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )


def is_adult_entry(entry):
    if any(domain in line.casefold() for domain in BLOCKED_ADULT_DOMAINS for line in entry):
        return True
    attrs = entry_attributes(entry)
    labels = [entry[0].partition(",")[2], *attrs.values()]
    return any(PORN_LABEL.search(normalized_text(label)) for label in labels)


def filter_porn_entries(content):
    text = decode_playlist(content)
    lines = text.splitlines(keepends=True)
    result, entry = [], []
    for line in lines:
        if line.strip().startswith("#EXTINF:"):
            if entry and not is_adult_entry(entry):
                result.extend(entry)
            entry = [line]
        elif entry:
            entry.append(line)
        else:
            result.append(line)
    if entry and not is_adult_entry(entry):
        result.extend(entry)
    return "".join(result).encode("utf-8")


def channel_request(entry):
    name = " ".join(html.unescape(entry[0].partition(",")[2]).split())[:100] or "Sem nome"
    headers = {"User-Agent": "IPTV-HealthCheck/1.0", "Accept-Encoding": "identity"}
    url = None
    options = ""
    for line in entry[1:]:
        value = line.strip()
        if value.startswith("#EXTVLCOPT:"):
            key, separator, option = value.partition(":")[2].partition("=")
            header = HEADER_NAMES.get(key.casefold())
            if separator and header:
                headers[header] = option
        elif value and not value.startswith("#"):
            url, _, options = value.partition("|")
            break
    for key, value in parse_qsl(options.replace("|", "&"), keep_blank_values=True):
        header = HEADER_NAMES.get(key.casefold())
        if header:
            headers[header] = value
    return name, url, headers


def entry_url(entry):
    return channel_request(entry)[1]


def fetch_sample(url, headers, timeout, byte_range="bytes=0-65535"):
    request_headers = dict(headers)
    request_headers["Range"] = byte_range
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.geturl(), response.read(65536)
    except HTTPError as error:
        raise ValueError(f"HTTP {error.code}") from None
    except URLError as error:
        raise ValueError(str(getattr(error, "reason", error))) from None
    except TimeoutError:
        raise ValueError("timeout") from None
    except HTTPException as error:
        raise ValueError(f"resposta HTTP inválida: {error}") from None


def check_channel(url, headers=None, timeout=4):
    headers = headers or {"User-Agent": "IPTV-HealthCheck/1.0", "Accept-Encoding": "identity"}
    parsed = urlsplit(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL não HTTP/HTTPS ou ausente")
    final_url, body = fetch_sample(url, headers, timeout)
    if not body:
        raise ValueError("resposta vazia")
    if body.lstrip().lower().startswith((b"<html", b"<!doctype html")):
        raise ValueError("servidor respondeu HTML, não um stream")
    manifest_prefix = b"#EXTM3U"
    if not body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(manifest_prefix):
        return "respondeu com dados (formato não confirmado)"
    for _ in range(3):
        if not body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(manifest_prefix):
            return "HLS e primeiro segmento responderam"
        lines = body.decode("utf-8-sig", errors="replace").splitlines()
        next_uri = next((line.strip() for line in lines if line.strip() and not line.startswith("#")), None)
        if not next_uri:
            raise ValueError("playlist HLS sem segmento")
        final_url, body = fetch_sample(urljoin(final_url, next_uri), headers, timeout)
        if not body:
            raise ValueError("primeiro segmento HLS vazio")
        if body.lstrip().lower().startswith((b"<html", b"<!doctype html")):
            raise ValueError("HLS apontou para uma resposta HTML")
    raise ValueError("playlist HLS aninhada além do limite")


def render_playlist(entries, header="#EXTM3U\n"):
    body = header
    if body and not body.endswith("\n"):
        body += "\n"
    for entry in entries:
        body += "".join(entry)
        if entry and not entry[-1].endswith(("\n", "\r")):
            body += "\n"
    return body
