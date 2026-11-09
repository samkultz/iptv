"""Serve the generated IPTV playlist and XMLTV guide on the local network."""

import argparse
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.request import Request, urlopen


DEFAULT_PLAYLIST = Path(__file__).resolve().parent / "output" / "working.m3u"
DEFAULT_OUTPUT = DEFAULT_PLAYLIST.with_name("runprogram.m3u")
BLOCKED_ADULT_DOMAINS = ("kaamdesi.com",)
PORN_LABEL = re.compile(
    r"porn|xxx|erotic|erotik|erotico|playboy|hustler|brazzers|penthouse|"
    r"dorcel|red\W*light|private\W*tv|sex\W*prive|sex\W*treme|"
    r"sexy?\W*hot|\badult(?:s|o|os)?\b(?!\W*swim\b)"
)
CHANNEL_ATTRIBUTE = re.compile(
    r'''\b(?:tvg-name|tvg-id|group-title|tvg-category|category)\s*=\s*(["'])(.*?)\1''',
    re.IGNORECASE,
)


def _is_porn_entry(entry):
    if any(domain in line.casefold() for domain in BLOCKED_ADULT_DOMAINS for line in entry):
        return True
    metadata = entry[0].strip()
    labels = [metadata.partition(",")[2]]
    labels.extend(match.group(2) for match in CHANNEL_ATTRIBUTE.finditer(metadata))
    searchable = " ".join(labels).casefold()
    searchable = "".join(
        character for character in unicodedata.normalize("NFKD", searchable)
        if not unicodedata.combining(character)
    )
    return bool(PORN_LABEL.search(searchable))


def filter_porn_entries(content):
    """Remove adult channel entries without fetching or inspecting stream URLs."""
    lines = content.decode("utf-8-sig").splitlines(keepends=True)
    result, entry = [], []
    for line in lines:
        if line.strip().startswith("#EXTINF:"):
            if entry and not _is_porn_entry(entry):
                result.extend(entry)
            entry = [line]
        elif entry:
            entry.append(line)
        else:
            result.append(line)
    if entry and not _is_porn_entry(entry):
        result.extend(entry)
    return "".join(result).encode("utf-8")


def _channel_entries(content):
    lines = content.decode("utf-8-sig").splitlines()
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


def _channel_request(entry):
    name = " ".join(entry[0].partition(",")[2].split())[:100] or "Sem nome"
    headers = {"User-Agent": "IPTV-HealthCheck/1.0", "Accept-Encoding": "identity"}
    header_names = {
        "http-user-agent": "User-Agent",
        "user-agent": "User-Agent",
        "http-referrer": "Referer",
        "referer": "Referer",
        "origin": "Origin",
    }
    url = None
    options = ""
    for line in entry[1:]:
        value = line.strip()
        if value.startswith("#EXTVLCOPT:"):
            key, separator, option = value.partition(":")[2].partition("=")
            header = header_names.get(key.casefold())
            if separator and header:
                headers[header] = option
        elif value and not value.startswith("#"):
            url, _, options = value.partition("|")
            break
    for key, value in parse_qsl(options.replace("|", "&"), keep_blank_values=True):
        header = header_names.get(key.casefold())
        if header:
            headers[header] = value
    return name, url, headers


def _fetch_sample(url, headers, timeout, byte_range="bytes=0-65535"):
    request_headers = dict(headers)
    request_headers["Range"] = byte_range
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(65536)
            return response.geturl(), body
    except HTTPError as error:
        raise ValueError(f"HTTP {error.code}") from None
    except URLError as error:
        reason = getattr(error, "reason", error)
        raise ValueError(str(reason)) from None
    except TimeoutError:
        raise ValueError("timeout") from None
    except HTTPException as error:
        raise ValueError(f"resposta HTTP inválida: {error}") from None


def _check_channel(url, headers, timeout):
    parsed = urlsplit(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL não HTTP/HTTPS ou ausente")
    final_url, body = _fetch_sample(url, headers, timeout)
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
        next_uri = None
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                next_uri = line
                break
        if not next_uri:
            raise ValueError("playlist HLS sem segmento")
        final_url, body = _fetch_sample(urljoin(final_url, next_uri), headers, timeout)
        if not body:
            raise ValueError("primeiro segmento HLS vazio")
        if body.lstrip().lower().startswith((b"<html", b"<!doctype html")):
            raise ValueError("HLS apontou para uma resposta HTML")
    raise ValueError("playlist HLS aninhada além do limite")


def check_playlist(path, offset=0, limit=25, timeout=4):
    content = filter_porn_entries(path.read_bytes())
    channels, seen = [], set()
    for entry in _channel_entries(content):
        name, url, headers = _channel_request(entry)
        key = (url, tuple(sorted(headers.items()))) if url else f"invalid:{len(channels)}"
        if key not in seen:
            seen.add(key)
            channels.append((name, url, headers))
    selected = channels[offset:offset + limit]
    print(f"Testando {len(selected)} de {len(channels)} URLs únicas (offset {offset}).")
    failed = 0
    for name, url, headers in selected:
        try:
            result = _check_channel(url, headers, timeout)
            print(f"[RESPONDE] {name}: {result}")
        except (OSError, ValueError) as error:
            failed += 1
            print(f"[FALHA] {name}: {error}")
    print(f"Resultado do lote: {len(selected) - failed} responderam, {failed} falhas.")
    return failed


def output_path(value, *sources):
    path = Path(value).expanduser().resolve()
    for source in sources:
        source = Path(source).resolve()
        if path == source or (path.exists() and source.exists() and path.samefile(source)):
            raise ValueError("O arquivo de saída deve ser diferente da playlist, do guia e dos filtros.")
    return path


def write_playlist(path, body):
    """Replace the export atomically so readers never see a partial playlist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as target:
            temporary = Path(target.name)
            target.write(body)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def lan_ip():
    # UDP connect selects a local route without sending traffic to the destination.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            address = probe.getsockname()[0]
            if not ipaddress.ip_address(address).is_loopback:
                return address
    except OSError:
        pass
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            ip = ipaddress.ip_address(address)
            if not ip.is_loopback and not ip.is_unspecified:
                return address
    except OSError:
        pass
    return "127.0.0.1"


def playlist_header(header, guide_url):
    """Make the local EPG address usable by players on other devices."""
    def replace(match):
        if match[3] in (b"epg.xml", b"./epg.xml", b"/epg.xml"):
            return match[1] + match[2] + guide_url.encode("utf-8") + match[2]
        return match[0]

    return re.sub(rb'''(\b(?:x-tvg-url|url-tvg)\s*=\s*)(["'])(.*?)\2''', replace, header)


class PlaylistServer(ThreadingHTTPServer):
    def __init__(self, address, playlist, output=None):
        self.playlist = Path(playlist)
        self.output = output_path(output, self.playlist, self.playlist.with_name("epg.xml")) if output else None
        super().__init__(address, PlaylistHandler)

    def read_playlist(self):
        return filter_porn_entries(self.playlist.read_bytes())

    def playlist_body(self, guide_url):
        body = self.read_playlist()
        header, separator, rest = body.partition(b"\n")
        body = playlist_header(header, guide_url) + separator + rest
        if self.output is not None:
            write_playlist(self.output, body)
        return body


class PlaylistHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.serve_file(send_body=True)

    def do_HEAD(self):
        self.serve_file(send_body=False)

    def serve_file(self, send_body):
        path = urlsplit(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/working.m3u")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/working.m3u":
            try:
                local_ip, local_port = self.connection.getsockname()[:2]
                body = self.server.playlist_body(f"http://{local_ip}:{local_port}/epg.xml")
            except FileNotFoundError:
                self.send_error(404, "Playlist ou arquivo de filtros ainda nao disponivel")
                return
            except OSError:
                self.send_error(503, "Falha ao ler ou exportar a playlist")
                return
            except ValueError as error:
                self.log_error("Falha na playlist: %s", error)
                self.send_error(503, "Playlist ou filtros invalidos; consulte o terminal")
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return
        elif path == "/epg.xml":
            file_path = self.server.playlist.with_name("epg.xml")
            content_type = "application/xml; charset=utf-8"
        else:
            self.send_error(404, "Arquivo nao encontrado")
            return

        try:
            source = file_path.open("rb")
        except FileNotFoundError:
            self.send_error(404, "Arquivo ainda nao gerado pelo scanner")
            return
        except OSError:
            self.send_error(503, "Arquivo indisponivel")
            return

        # Open on every request so atomic replacements from the scanner are visible.
        with source:
            length = os.fstat(source.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                try:
                    shutil.copyfileobj(source, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use uma porta de 1 a 65535.") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Use uma porta de 1 a 65535.")
    return port


def nonnegative_number(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use um inteiro não negativo.") from None
    if number < 0:
        raise argparse.ArgumentTypeError("Use um inteiro não negativo.")
    return number


def check_limit(value):
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use um limite de 1 a 100 canais.") from None
    if not 1 <= limit <= 100:
        raise argparse.ArgumentTypeError("Use um limite de 1 a 100 canais.")
    return limit


def check_timeout(value):
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use um timeout entre 1 e 15 segundos.") from None
    if not 1 <= timeout <= 15:
        raise argparse.ArgumentTypeError("Use um timeout entre 1 e 15 segundos.")
    return timeout


def ipv4_address(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise argparse.ArgumentTypeError("Use um endereço IPv4.") from None


def initial_export(server, base_url, parser):
    if server.playlist.is_file():
        try:
            server.playlist_body(f"{base_url}/epg.xml")
        except (OSError, ValueError) as error:
            parser.exit(1, f"Não foi possível gerar o M3U: {error}\n")
        print(f"M3U gerado: {server.output}", flush=True)
    else:
        print(f"M3U de saída: {server.output} (aguardando a playlist do scanner)", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Verifica canais IPTV ou serve a playlist e o guia XMLTV.")
    parser.add_argument("--port", type=port_number, default=8080, help="Porta HTTP (padrão: 8080).")
    parser.add_argument("--host", type=ipv4_address, default="0.0.0.0",
                        help="IPv4 de escuta (padrão: todas as interfaces).")
    parser.add_argument("--playlist", type=Path, default=DEFAULT_PLAYLIST,
                        help="Arquivo M3U (padrão: output/working.m3u ao lado do script).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Arquivo M3U de saída.")
    parser.add_argument("--check-streams", action="store_true",
                        help="Testa um lote de canais e encerra sem iniciar o servidor.")
    parser.add_argument("--check-offset", type=nonnegative_number, default=0,
                        help="Índice inicial do lote de verificação (padrão: 0).")
    parser.add_argument("--check-limit", type=check_limit, default=25,
                        help="Máximo de URLs por lote, de 1 a 100 (padrão: 25).")
    parser.add_argument("--check-timeout", type=check_timeout, default=4,
                        help="Timeout por requisição em segundos, de 1 a 15 (padrão: 4).")
    args = parser.parse_args()
    playlist = args.playlist.expanduser().resolve()
    if args.check_streams:
        try:
            failures = check_playlist(playlist, args.check_offset, args.check_limit, args.check_timeout)
        except (OSError, ValueError) as error:
            parser.exit(1, f"Não foi possível verificar a playlist: {error}\n")
        if failures:
            raise SystemExit(1)
        return
    address = lan_ip() if args.host == "0.0.0.0" else args.host
    try:
        server = PlaylistServer((args.host, args.port), playlist, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Não foi possível abrir {args.host}:{args.port}: {error}\n")

    with server:
        base_url = f"http://{address}:{server.server_port}"
        initial_export(server, base_url, parser)
        print(f"IP da rede: {address}", flush=True)
        if address == "127.0.0.1":
            print("Endereço local: acessível somente neste computador.", flush=True)
        print(f"Playlist: {base_url}/working.m3u", flush=True)
        print(f"Guia EPG: {base_url}/epg.xml", flush=True)
        print(f"Arquivo: {playlist}", flush=True)
        if not playlist.is_file():
            print("Aguardando o scanner gerar a playlist.", flush=True)
        if not playlist.with_name("epg.xml").is_file():
            print("Guia EPG ainda não disponível.", flush=True)
        print("Servidor ativo. Ctrl+C para encerrar.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServidor encerrado.", flush=True)


if __name__ == "__main__":
    main()
