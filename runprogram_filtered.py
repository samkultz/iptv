"""Filter, export and serve an IPTV playlist with editable inclusion and exclusion rules."""

import argparse
import html
import json
import re
import unicodedata
from pathlib import Path

import runprogram
import stream_tags


DEFAULT_FILTERS = Path(__file__).resolve().with_name("filters.json")
DEFAULT_OUTPUT = runprogram.DEFAULT_PLAYLIST.with_name("filtered.m3u")
DEFAULT_RULES = {
    "include_keywords": [],
    "exclude_languages": [],
    "exclude_non_latin": False,
    "exclude_keywords": [],
    "exclude_unknown_language": False,
    "exclude_porn": True,
}

# Match channel labels, never playback URLs. Avoid broad terms such as "sex"
# (Essex/Sussex) and age ratings (18+ films are not necessarily pornography).
PORN_LABEL = re.compile(
    r"porn|xxx|erotic|erotik|erotico|playboy|hustler|brazzers|penthouse|"
    r"dorcel|red\W*light|private\W*tv|sex\W*prive|sex\W*treme|"
    r"sexy?\W*hot|\badult(?:s|o|os)?\b(?!\W*swim\b)"
)


def normalized_text(value):
    return "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(char))


def language_base(value):
    return (stream_tags.normalize_language(value) or "").split("-")[0].casefold()


def load_rules(path, keywords=(), languages=(), include_keywords=()):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("O arquivo de filtros deve conter um objeto JSON.")
    unknown = data.keys() - DEFAULT_RULES.keys()
    if unknown:
        raise ValueError("Filtros desconhecidos: " + ", ".join(sorted(unknown)))
    rules = DEFAULT_RULES | data
    for key in ("exclude_languages", "exclude_keywords", "include_keywords"):
        if not isinstance(rules[key], list) or any(
            not isinstance(value, str) or not value.strip() for value in rules[key]
        ):
            raise ValueError(f"{key} deve ser uma lista de textos não vazios.")
    for key in ("exclude_non_latin", "exclude_unknown_language", "exclude_porn"):
        if not isinstance(rules[key], bool):
            raise ValueError(f"{key} deve ser true ou false.")
    rules["exclude_languages"] = {language_base(value) for value in [*rules["exclude_languages"], *languages]}
    for key, extra in (("exclude_keywords", keywords), ("include_keywords", include_keywords)):
        terms = [*rules[key], *extra]
        if any(not isinstance(value, str) or not value.strip() for value in terms):
            raise ValueError(f"{key} deve ser uma lista de textos não vazios.")
        rules[key] = [normalized_text(value.strip()) for value in terms]
    return rules


def excluded(entry, rules):
    metadata = entry[0].strip()
    attrs = stream_tags.attributes(metadata)
    _, name = stream_tags.split_extinf(metadata)
    languages = stream_tags.values(
        attrs.get("tvg-language") or attrs.get("language") or attrs.get("audio-language"),
        stream_tags.normalize_language,
    )
    if not languages and rules["exclude_unknown_language"]:
        return True
    if any(language_base(language) in rules["exclude_languages"] for language in languages):
        return True
    labels = [html.unescape(name), attrs.get("tvg-name", ""), attrs.get("group-title", ""),
              attrs.get("tvg-category", ""), attrs.get("category", "")]
    labels.extend(line.strip().partition(":")[2] for line in entry if line.strip().startswith("#EXTGRP:"))
    if rules["exclude_non_latin"] and any(
        char.isalpha() and "LATIN" not in unicodedata.name(char, "")
        for label in labels for char in unicodedata.normalize("NFKC", label)
    ):
        return True
    searchable = [normalized_text(label) for label in [*labels, attrs.get("tvg-id", "")]]
    if rules["exclude_porn"] and any(PORN_LABEL.search(label) for label in searchable):
        return True
    if rules["include_keywords"] and not any(
        keyword in label for keyword in rules["include_keywords"] for label in searchable
    ):
        return True
    return any(keyword in label for keyword in rules["exclude_keywords"] for label in searchable)


def filter_playlist(content, rules):
    lines = content.lstrip("\ufeff").splitlines(keepends=True)
    if not lines or not lines[0].strip().startswith("#EXTM3U"):
        raise ValueError("Arquivo sem cabeçalho #EXTM3U.")
    if any(line.strip().startswith("#EXT-X-") for line in lines):
        raise ValueError("Use uma lista de canais M3U, não um manifesto HLS.")
    result, entry = [], []
    for line in lines:
        if line.strip().startswith("#EXTINF:"):
            if entry and not excluded(entry, rules):
                result.extend(entry)
            entry = [line]
        elif entry:
            entry.append(line)
        elif line.strip() and not line.lstrip().startswith("#"):
            raise ValueError("Canal sem #EXTINF; não é possível aplicar os filtros.")
        else:
            result.append(line)
    if entry and not excluded(entry, rules):
        result.extend(entry)
    return "".join(result)


class FilteredServer(runprogram.PlaylistServer):
    def __init__(self, address, playlist, filters, keywords=(), languages=(), include_keywords=(), output=None):
        self.filters = Path(filters)
        self.keywords = keywords
        self.languages = languages
        self.include_keywords = include_keywords
        if output is not None:
            output = runprogram.output_path(output, self.filters)
        super().__init__(address, playlist, output)

    def read_playlist(self):
        rules = load_rules(self.filters, self.keywords, self.languages, self.include_keywords)
        content = self.playlist.read_text(encoding="utf-8-sig")
        return filter_playlist(content, rules).encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description="Filtra, gera um arquivo M3U e serve a playlist na rede local.")
    parser.add_argument("--port", type=runprogram.port_number, default=8080)
    parser.add_argument("--host", type=runprogram.ipv4_address, default="0.0.0.0")
    parser.add_argument("--playlist", type=Path, default=runprogram.DEFAULT_PLAYLIST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Arquivo M3U de saída (padrão: output/filtered.m3u).")
    parser.add_argument("--filters", type=Path, default=DEFAULT_FILTERS, help="Arquivo JSON de filtros (padrão: filters.json).")
    parser.add_argument("--include-keyword", action="append", default=[], help="Palavra ou trecho a incluir; repetível.")
    parser.add_argument("--exclude-keyword", action="append", default=[], help="Palavra ou trecho a excluir; repetível.")
    parser.add_argument("--exclude-language", action="append", default=[], help="Idioma adicional a excluir; repetível.")
    args = parser.parse_args()
    playlist = args.playlist.expanduser().resolve()
    filters = args.filters.expanduser().resolve()
    try:
        load_rules(filters, args.exclude_keyword, args.exclude_language, args.include_keyword)
        server = FilteredServer((args.host, args.port), playlist, filters,
                                args.exclude_keyword, args.exclude_language, args.include_keyword, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Não foi possível iniciar o servidor: {error}\n")
    address = runprogram.lan_ip() if args.host == "0.0.0.0" else args.host
    with server:
        base_url = f"http://{address}:{server.server_port}"
        runprogram.initial_export(server, base_url, parser)
        print(f"Playlist filtrada: {base_url}/working.m3u", flush=True)
        print(f"Guia EPG: {base_url}/epg.xml", flush=True)
        print(f"Arquivo: {playlist}", flush=True)
        print(f"Filtros: {filters} (relidos a cada acesso)", flush=True)
        if not playlist.is_file():
            print("Aguardando o scanner gerar a playlist.", flush=True)
        print("Servidor ativo. Ctrl+C para encerrar.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServidor encerrado.", flush=True)


if __name__ == "__main__":
    main()
