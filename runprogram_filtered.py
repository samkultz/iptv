"""Filter an IPTV M3U with whitelist/blacklist rules and serve it without EPG."""

import argparse
import ipaddress
import json
import socket
import tempfile
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from iptv_playlist import entry_attributes, is_adult_entry, parse_entries, render_playlist


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "output" / "working.m3u"
DEFAULT_OUTPUT = ROOT / "output" / "filtered.m3u"
DEFAULT_FILTERS = ROOT / "filters.json"
DEFAULT_RULES = {
    "whitelist_terms": [],
    "blacklist_terms": [],
    "whitelist_countries": [],
    "blacklist_countries": [],
    "exclude_adult": True,
}


def normalize(value):
    value = value.casefold().strip()
    return "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )


def load_rules(path, cli_rules=None):
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(data, dict):
        raise ValueError("O arquivo de filtros deve conter um objeto JSON.")
    unknown = data.keys() - DEFAULT_RULES.keys()
    if unknown:
        raise ValueError("Filtros desconhecidos: " + ", ".join(sorted(unknown)))
    rules = DEFAULT_RULES | data
    cli_rules = cli_rules or {}
    for key in ("whitelist_terms", "blacklist_terms", "whitelist_countries", "blacklist_countries"):
        if not isinstance(rules[key], list) or any(
            not isinstance(value, str) or not value.strip() for value in rules[key]
        ):
            raise ValueError(f"{key} deve ser uma lista de textos não vazios.")
        additions = cli_rules.get(key, [])
        if any(not isinstance(value, str) or not value.strip() for value in additions):
            raise ValueError(f"Valores inválidos para {key}.")
        rules[key] = [normalize(value) for value in [*rules[key], *additions]]
    if not isinstance(rules["exclude_adult"], bool):
        raise ValueError("exclude_adult deve ser true ou false.")
    return rules


def countries_for(entry, attrs):
    raw = " ".join(
        attrs.get(key, "")
        for key in ("tvg-country", "country", "country-code", "tvg-country-code")
    )
    return {
        normalize(country)
        for country in raw.replace(";", ",").replace("|", ",").split(",")
        if country.strip()
    }


def excluded(entry, rules):
    if rules["exclude_adult"] and is_adult_entry(entry):
        return True
    attrs = entry_attributes(entry)
    labels = [entry[0].partition(",")[2], *attrs.values()]
    labels.extend(line.strip().partition(":")[2] for line in entry if line.strip().startswith("#EXTGRP:"))
    searchable = normalize(" ".join(labels))

    whitelist_terms = rules["whitelist_terms"]
    if whitelist_terms and not any(term in searchable for term in whitelist_terms):
        return True
    if any(term in searchable for term in rules["blacklist_terms"]):
        return True

    countries = countries_for(entry, attrs)
    if rules["whitelist_countries"] and not countries.intersection(rules["whitelist_countries"]):
        return True
    return bool(countries.intersection(rules["blacklist_countries"]))


def filter_playlist(content, rules):
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    if not text.lstrip("\ufeff").splitlines() or not text.lstrip("\ufeff").splitlines()[0].strip().startswith("#EXTM3U"):
        raise ValueError("Arquivo sem cabeçalho #EXTM3U.")
    entries = [entry for entry in parse_entries(text) if not excluded(entry, rules)]
    return render_playlist(entries).encode("utf-8")


def write_atomic(path, body):
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


class FilterServer(ThreadingHTTPServer):
    def __init__(self, address, source, output, rules):
        self.source = Path(source)
        self.output = Path(output)
        self.rules = rules
        super().__init__(address, FilterHandler)

    def filtered_playlist(self):
        body = filter_playlist(self.source.read_bytes(), self.rules)
        write_atomic(self.output, body)
        return body


class FilterHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if self.requestline.startswith("\x16\x03"):
            return
        super().log_message(format, *args)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/filtered.m3u")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path != "/filtered.m3u":
            self.send_error(404, "Arquivo não encontrado")
            return
        try:
            body = self.server.filtered_playlist()
        except FileNotFoundError:
            self.send_error(404, "Playlist de entrada não encontrada")
            return
        except (OSError, ValueError) as error:
            self.send_error(503, str(error))
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use um inteiro positivo.") from None
    if number < 1:
        raise argparse.ArgumentTypeError("Use um inteiro positivo.")
    return number


def ipv4_address(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise argparse.ArgumentTypeError("Use um endereço IPv4.") from None


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            address = probe.getsockname()[0]
            if not ipaddress.ip_address(address).is_loopback:
                return address
    except OSError:
        pass
    return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="Filtra e serve uma M3U reduzida, sem EPG.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="M3U de entrada (padrão: output/working.m3u).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="M3U filtrada (padrão: output/filtered.m3u).")
    parser.add_argument("--filters", type=Path, default=DEFAULT_FILTERS, help="Regras JSON (padrão: filters.json).")
    parser.add_argument("--whitelist-term", "--include-keyword", dest="whitelist_terms", action="append", default=[])
    parser.add_argument("--blacklist-term", "--exclude-keyword", dest="blacklist_terms", action="append", default=[])
    parser.add_argument("--whitelist-country", "--include-country", dest="whitelist_countries", action="append", default=[])
    parser.add_argument("--blacklist-country", "--exclude-country", dest="blacklist_countries", action="append", default=[])
    parser.add_argument("--include-adult", action="store_true", help="Não aplica a exclusão automática de canais adultos.")
    parser.add_argument("--host", type=ipv4_address, default="0.0.0.0")
    parser.add_argument("--port", type=positive_int, default=8082)
    args = parser.parse_args()

    cli_rules = {
        "whitelist_terms": args.whitelist_terms,
        "blacklist_terms": args.blacklist_terms,
        "whitelist_countries": args.whitelist_countries,
        "blacklist_countries": args.blacklist_countries,
    }
    try:
        rules = load_rules(args.filters, cli_rules)
        if args.include_adult:
            rules["exclude_adult"] = False
        preview = filter_playlist(args.input.read_bytes(), rules)
        write_atomic(args.output, preview)
        server = FilterServer((args.host, args.port), args.input, args.output, rules)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Não foi possível iniciar o filtro: {error}\n")

    address = lan_ip() if args.host == "0.0.0.0" else args.host
    with server:
        print(f"M3U filtrada: http://{address}:{server.server_port}/filtered.m3u", flush=True)
        print(f"Arquivo: {args.output.resolve()}", flush=True)
        print("EPG desativado.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServidor encerrado.", flush=True)


if __name__ == "__main__":
    main()
