#!/usr/bin/env python3
"""Find public M3U files on GitHub and build allFound.m3u and working.m3u."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from iptv_playlist import (
    channel_request,
    check_channel,
    filter_porn_entries,
    parse_entries,
    render_playlist,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_ALL_FOUND = ROOT / "output" / "allFound.m3u"
DEFAULT_WORKING = ROOT / "output" / "working.m3u"
GITHUB_API = "https://api.github.com"
DEFAULT_QUERIES = ("iptv m3u in:readme", "iptv playlist in:readme")
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024


class GitHubAPIError(RuntimeError):
    pass


class GitHubRateLimitError(GitHubAPIError):
    pass


def _request(url, timeout, token=None, accept="application/vnd.github+json"):
    headers = {
        "Accept": accept,
        "User-Agent": "iptv-playlist-collector/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read(MAX_PLAYLIST_BYTES + 1)
    except HTTPError as error:
        if error.code == 403 and error.headers.get("X-RateLimit-Remaining") == "0":
            reset = error.headers.get("X-RateLimit-Reset", "unknown")
            raise GitHubRateLimitError(
                f"limite da API do GitHub atingido (reset Unix: {reset}); "
                "aguarde ou configure GITHUB_TOKEN"
            ) from None
        raise GitHubAPIError(f"HTTP {error.code} ao acessar {url}") from None
    except URLError as error:
        raise GitHubAPIError(f"falha de rede ao acessar {url}: {error.reason}") from None


def _api_json(path, timeout, token=None):
    url = f"{GITHUB_API}{path}"
    body = _request(url, timeout, token)
    if len(body) > MAX_PLAYLIST_BYTES:
        raise GitHubAPIError(f"resposta muito grande da API: {url}")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubAPIError(f"resposta JSON inválida de {url}: {error}") from None


def search_repositories(queries, max_repositories, timeout, token=None):
    seen = set()
    for query in queries:
        page = 1
        while len(seen) < max_repositories:
            params = urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": 100, "page": page})
            result = _api_json(f"/search/repositories?{params}", timeout, token)
            items = result.get("items", [])
            if not items:
                break
            for repository in items:
                name = repository.get("full_name")
                if name and name not in seen:
                    seen.add(name)
                    yield repository
                    if len(seen) >= max_repositories:
                        break
            if len(items) < 100:
                break
            page += 1


def repository_playlists(repository, timeout, token=None):
    full_name = repository["full_name"]
    branch = repository.get("default_branch") or "main"
    branch_path = quote(branch, safe="")
    tree = _api_json(
        f"/repos/{full_name}/git/trees/{branch_path}?recursive=1",
        timeout,
        token,
    )
    if tree.get("truncated"):
        print(f"  Árvore truncada; alguns arquivos podem não ser encontrados: {full_name}")
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if item.get("type") == "blob" and path.casefold().endswith(".m3u"):
            yield path


def download_playlist(repository, path, timeout, token=None):
    owner, name = repository["full_name"].split("/", 1)
    branch = quote(repository.get("default_branch") or "main", safe="")
    encoded_path = quote(path, safe="/")
    url = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{encoded_path}"
    body = _request(url, timeout, token, accept="text/plain")
    if len(body) > MAX_PLAYLIST_BYTES:
        raise ValueError(f"arquivo ultrapassa {MAX_PLAYLIST_BYTES // (1024 * 1024)} MiB")
    text = body.decode("utf-8-sig")
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError("arquivo não começa com #EXTM3U")
    return url, filter_porn_entries(body)


def _append_entries(path, entries):
    if not entries:
        return
    with path.open("a", encoding="utf-8", newline="") as output:
        output.write(render_playlist(entries, header=""))
        output.flush()


def discover(queries, all_found, max_repositories, max_playlists, timeout, token=None):
    all_found.parent.mkdir(parents=True, exist_ok=True)
    all_found.write_text("#EXTM3U\n", encoding="utf-8")
    seen_channels = set()
    accepted_playlists = 0
    processed_playlists = 0
    checked_repositories = 0

    for repository in search_repositories(queries, max_repositories, timeout, token):
        checked_repositories += 1
        full_name = repository["full_name"]
        print(f"[REPOSITÓRIO] {full_name}", flush=True)
        try:
            paths = repository_playlists(repository, timeout, token)
            for path in paths:
                if processed_playlists >= max_playlists:
                    break
                processed_playlists += 1
                try:
                    source_url, content = download_playlist(repository, path, timeout, token)
                    entries = []
                    for entry in parse_entries(content):
                        _, url, headers = channel_request(entry)
                        key = (url, tuple(sorted(headers.items())))
                        if url and key not in seen_channels:
                            seen_channels.add(key)
                            entries.append(entry)
                    if entries:
                        _append_entries(all_found, entries)
                        accepted_playlists += 1
                        print(
                            f"  [M3U] {source_url}: {len(entries)} canais novos "
                            f"(total {len(seen_channels)})",
                            flush=True,
                        )
                except GitHubRateLimitError:
                    raise
                except (GitHubAPIError, OSError, UnicodeDecodeError, ValueError) as error:
                    print(f"  [IGNORADA] {path}: {error}", file=sys.stderr, flush=True)
            if processed_playlists >= max_playlists:
                break
        except GitHubRateLimitError:
            raise
        except (GitHubAPIError, OSError) as error:
            print(f"  [REPOSITÓRIO IGNORADO] {full_name}: {error}", file=sys.stderr, flush=True)
    print(
        f"Busca concluída: {checked_repositories} repositórios, "
        f"{accepted_playlists} playlists válidas de {processed_playlists} examinadas, "
        f"{len(seen_channels)} canais em {all_found}.",
        flush=True,
    )
    return len(seen_channels)


def validate_all(all_found, working, timeout, delay):
    entries = parse_entries(filter_porn_entries(all_found.read_bytes()))
    working.parent.mkdir(parents=True, exist_ok=True)
    working.write_text("#EXTM3U\n", encoding="utf-8")
    seen, working_count = set(), 0
    total = len(entries)
    print(f"Iniciando validação de {total} canais...", flush=True)
    for index, entry in enumerate(entries, start=1):
        name, url, headers = channel_request(entry)
        key = (url, tuple(sorted(headers.items())))
        if not url or key in seen:
            continue
        seen.add(key)
        try:
            result = check_channel(url, headers, timeout)
            _append_entries(working, [entry])
            working_count += 1
            print(f"[{index}/{total}] [FUNCIONA] {name}: {result}", flush=True)
        except (OSError, ValueError) as error:
            print(f"[{index}/{total}] [FALHA] {name}: {error}", flush=True)
        if delay:
            time.sleep(delay)
    print(f"Validação concluída: {working_count}/{total} canais em {working}.", flush=True)
    return working_count


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use um inteiro positivo.") from None
    if number < 1:
        raise argparse.ArgumentTypeError("Use um inteiro positivo.")
    return number


def positive_float(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use um número positivo.") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("Use um número positivo.")
    return number


def nonnegative_float(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use um número não negativo.") from None
    if number < 0:
        raise argparse.ArgumentTypeError("Use um número não negativo.")
    return number


def main():
    parser = argparse.ArgumentParser(
        description="Busca playlists M3U públicas no GitHub e valida seus canais."
    )
    parser.add_argument("--query", action="append", help="Consulta de repositórios; pode ser repetida.")
    parser.add_argument("--all-found", type=Path, default=DEFAULT_ALL_FOUND)
    parser.add_argument("--working", type=Path, default=DEFAULT_WORKING)
    parser.add_argument("--max-repositories", type=positive_int, default=30)
    parser.add_argument("--max-playlists", type=positive_int, default=100)
    parser.add_argument("--request-timeout", type=positive_float, default=15)
    parser.add_argument("--stream-timeout", type=positive_float, default=4)
    parser.add_argument("--check-delay", type=nonnegative_float, default=0.1)
    args = parser.parse_args()

    if args.all_found.resolve() == args.working.resolve():
        parser.error("--all-found e --working precisam ser arquivos diferentes.")
    token = os.environ.get("GITHUB_TOKEN")
    queries = args.query or list(DEFAULT_QUERIES)
    try:
        discover(
            queries,
            args.all_found,
            args.max_repositories,
            args.max_playlists,
            args.request_timeout,
            token,
        )
        validate_all(args.all_found, args.working, args.stream_timeout, args.check_delay)
    except (GitHubAPIError, OSError, ValueError) as error:
        parser.exit(1, f"Pipeline interrompida: {error}\n")


if __name__ == "__main__":
    main()
