"""Filter an IPTV M3U with whitelist/blacklist rules and serve it without EPG."""

import argparse
import ipaddress
import json
import socket
import tempfile
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from iptv_playlist import entry_attributes, is_adult_entry, parse_entries, render_playlist, set_group_title


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
    "exclude_unmapped_non_latin": False,
    "group_script_channels": True,
    "group_unmapped_scripts": True,
    "language_character_groups": {
        "spanish_presumed": ["ñ", "¿", "¡"],
        "islandic_presumed": ["þ", "ð", "æ"],
    },
    "script_country_groups": {
        "HAN": "China",
        "HIRAGANA": "Japan",
        "KATAKANA": "Japan",
        "HANGUL": "South Korea",
        "THAI": "Thailand",
        "ARMENIAN": "Armenia",
        "GEORGIAN": "Georgia",
        "GREEK": "Greece",
        "HEBREW": "Israel",
        "KHMER": "Cambodia",
        "LAO": "Laos",
        "MYANMAR": "Myanmar",
        "SINHALA": "Sri Lanka",
    },
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
    for key in (
        "exclude_adult",
        "exclude_unmapped_non_latin",
        "group_script_channels",
        "group_unmapped_scripts",
    ):
        if not isinstance(rules[key], bool):
            raise ValueError(f"{key} deve ser true ou false.")
    if not isinstance(rules["language_character_groups"], dict) or any(
        not isinstance(group, str) or not group.strip()
        or not isinstance(characters, list)
        or any(not isinstance(char, str) or len(char) != 1 for char in characters)
        for group, characters in rules["language_character_groups"].items()
    ):
        raise ValueError("language_character_groups deve mapear grupos para listas de caracteres.")
    rules["language_character_groups"] = {
        group.strip(): [char.casefold() for char in characters]
        for group, characters in rules["language_character_groups"].items()
    }
    if not isinstance(rules["script_country_groups"], dict) or any(
        not isinstance(script, str) or not script.strip()
        or not isinstance(country, str) or not country.strip()
        for script, country in rules["script_country_groups"].items()
    ):
        raise ValueError("script_country_groups deve mapear scripts para países não vazios.")
    rules["script_country_groups"] = {
        script.strip().upper(): country.strip()
        for script, country in rules["script_country_groups"].items()
    }
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


def entry_labels(entry, attrs):
    labels = [entry[0].partition(",")[2]]
    labels.extend(
        attrs[key]
        for key in ("tvg-name", "tvg-id", "group-title", "tvg-category", "category", "tvg-language")
        if attrs.get(key)
    )
    labels.extend(line.strip().partition(":")[2] for line in entry if line.strip().startswith("#EXTGRP:"))
    return labels


def scripts_in_entry(entry, attrs):
    scripts = set()
    for char in " ".join(entry_labels(entry, attrs)):
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if name.startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")):
            scripts.add("HAN")
        elif "HIRAGANA" in name:
            scripts.add("HIRAGANA")
        elif "KATAKANA" in name:
            scripts.add("KATAKANA")
        elif "HANGUL" in name:
            scripts.add("HANGUL")
        elif "THAI" in name:
            scripts.add("THAI")
        elif "LATIN" not in name:
            scripts.add(name.partition(" ")[0] or "OTHER")
    return scripts


def script_country(entry, attrs, rules):
    scripts = scripts_in_entry(entry, attrs)
    mapping = rules["script_country_groups"]
    for script in ("HIRAGANA", "KATAKANA", "HANGUL", "HAN", "THAI"):
        if script in scripts and script in mapping:
            return mapping[script]
    for script in sorted(scripts):
        if script in mapping:
            return mapping[script]
    return None


def generic_script_group(entry, attrs, rules):
    if not rules["group_unmapped_scripts"]:
        return None
    scripts = scripts_in_entry(entry, attrs) - rules["script_country_groups"].keys()
    return min(scripts).replace("_", " ").title() if scripts else None


def language_character_group(entry, attrs, rules):
    searchable = " ".join(entry_labels(entry, attrs)).casefold()
    for group, characters in rules["language_character_groups"].items():
        if any(char in searchable for char in characters):
            return group
    return None


def excluded(entry, rules):
    if rules["exclude_adult"] and is_adult_entry(entry):
        return True
    attrs = entry_attributes(entry)
    labels = entry_labels(entry, attrs)
    searchable = normalize(" ".join(labels))
    scripts = scripts_in_entry(entry, attrs)
    country_group = script_country(entry, attrs, rules)
    unmapped_scripts = scripts - rules["script_country_groups"].keys()
    if rules["exclude_unmapped_non_latin"] and unmapped_scripts:
        return True

    whitelist_terms = rules["whitelist_terms"]
    if whitelist_terms and not any(term in searchable for term in whitelist_terms):
        return True
    if any(term in searchable for term in rules["blacklist_terms"]):
        return True

    countries = countries_for(entry, attrs)
    if country_group:
        countries.add(normalize(country_group))
    if rules["whitelist_countries"] and not countries.intersection(rules["whitelist_countries"]):
        return True
    return bool(countries.intersection(rules["blacklist_countries"]))


def filter_playlist(content, rules):
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    if not text.lstrip("\ufeff").splitlines() or not text.lstrip("\ufeff").splitlines()[0].strip().startswith("#EXTM3U"):
        raise ValueError("Arquivo sem cabeçalho #EXTM3U.")
    entries = []
    for entry in parse_entries(text):
        if excluded(entry, rules):
            continue
        attrs = entry_attributes(entry)
        country_group = script_country(entry, attrs, rules)
        language_group = language_character_group(entry, attrs, rules)
        script_group = country_group or language_group or generic_script_group(entry, attrs, rules)
        if script_group and rules["group_script_channels"]:
            entry = set_group_title(entry, script_group)
        entries.append(entry)
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
            server = cast(FilterServer, self.server)
            body = server.filtered_playlist()
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
    non_latin = parser.add_mutually_exclusive_group()
    non_latin.add_argument("--include-unmapped-non-latin", action="store_true",
                           help="Mantém alfabetos sem país mapeado e agrupa pelo script.")
    non_latin.add_argument("--exclude-unmapped-non-latin", action="store_true",
                           help="Exclui alfabetos sem país mapeado.")
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
        if args.include_unmapped_non_latin:
            rules["exclude_unmapped_non_latin"] = False
        if args.exclude_unmapped_non_latin:
            rules["exclude_unmapped_non_latin"] = True
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
