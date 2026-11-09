#!/usr/bin/env python3
"""Find public M3U files on GitHub and build allFound.m3u and working.m3u."""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
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
OUTPUT_DIR = ROOT / "output"
DEFAULT_ALL_FOUND = OUTPUT_DIR / "allFound.m3u"
DEFAULT_WORKING = OUTPUT_DIR / "working.m3u"
GITHUB_API = "https://api.github.com"
RAW_GITHUB_BASE_URL = "https://raw.githubusercontent.com"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
GITHUB_API_VERSION = "2022-11-28"
HTTP_USER_AGENT = "iptv-playlist-collector/1.0"
DEFAULT_QUERIES = ("iptv m3u in:readme", "iptv playlist in:readme")
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
MAX_REPOSITORIES = 30
MAX_PLAYLISTS = 100
REQUEST_TIMEOUT = 15
STREAM_TIMEOUT = 4
CHECK_DELAY = 0.1
RESUME = True # starts it all again or 
MAX_WORKERS = 4


class GitHubAPIError(RuntimeError):
    pass


class GitHubRateLimitError(GitHubAPIError):
    pass


def _request(url, timeout, token=None, accept="application/vnd.github+json"):
    headers = {
        "Accept": accept,
        "User-Agent": HTTP_USER_AGENT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
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
                f"GitHub API rate limit reached (Unix reset: {reset}); "
                f"wait or configure {GITHUB_TOKEN_ENV}"
            ) from None
        raise GitHubAPIError(f"HTTP {error.code} while requesting {url}") from None
    except URLError as error:
        raise GitHubAPIError(f"network error while requesting {url}: {error.reason}") from None


def _api_json(path, timeout, token=None):
    url = f"{GITHUB_API}{path}"
    body = _request(url, timeout, token)
    if len(body) > MAX_PLAYLIST_BYTES:
        raise GitHubAPIError(f"API response is too large: {url}")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubAPIError(f"invalid JSON response from {url}: {error}") from None


def _search_query(query, max_repositories, timeout, token=None):
    seen = set()
    repositories = []
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
                repositories.append(repository)
                if len(seen) >= max_repositories:
                    break
        if len(items) < 100:
            break
        page += 1
    return repositories


def search_repositories(queries, max_repositories, timeout, token=None, workers=MAX_WORKERS):
    queries = list(queries)
    if not queries:
        return
    with ThreadPoolExecutor(max_workers=min(workers, len(queries))) as executor:
        futures = [
            executor.submit(_search_query, query, max_repositories, timeout, token)
            for query in queries
        ]
        seen = set()
        for future in as_completed(futures):
            for repository in future.result():
                name = repository.get("full_name")
                if name and name not in seen:
                    seen.add(name)
                    yield repository
                    if len(seen) >= max_repositories:
                        break
            if len(seen) >= max_repositories:
                break


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
        print(f"  Repository tree is truncated; some files may be missing: {full_name}")
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if item.get("type") == "blob" and path.casefold().endswith(".m3u"):
            yield path


def _repository_playlist_paths(repository, timeout, token):
    return list(repository_playlists(repository, timeout, token))


def download_playlist(repository, path, timeout, token=None):
    owner, name = repository["full_name"].split("/", 1)
    branch = quote(repository.get("default_branch") or "main", safe="")
    encoded_path = quote(path, safe="/")
    url = f"{RAW_GITHUB_BASE_URL}/{owner}/{name}/{branch}/{encoded_path}"
    body = _request(url, timeout, token, accept="text/plain")
    if len(body) > MAX_PLAYLIST_BYTES:
        raise ValueError(f"file exceeds {MAX_PLAYLIST_BYTES // (1024 * 1024)} MiB")
    text = body.decode("utf-8-sig")
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError("file does not start with #EXTM3U")
    return url, filter_porn_entries(body)


def _append_entries(path, entries):
    if not entries:
        return
    with path.open("a", encoding="utf-8", newline="") as output:
        output.write(render_playlist(entries, header=""))
        output.flush()


def _channel_key(entry):
    _, url, headers = channel_request(entry)
    return (url, tuple(sorted(headers.items()))) if url else None


def _load_entries(path):
    content = filter_porn_entries(path.read_bytes())
    text = content.decode("utf-8-sig")
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError(f"Invalid playlist without an #EXTM3U header: {path}")
    return parse_entries(content)


def _write_entries(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as target:
            temporary = Path(target.name)
            target.write(render_playlist(entries).encode("utf-8"))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def format_eta(seconds):
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def format_percent(percent):
    return f"{percent:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def discover(
    queries,
    all_found,
    max_repositories,
    max_playlists,
    timeout,
    token=None,
    resume=False,
    workers=MAX_WORKERS,
):
    all_found.parent.mkdir(parents=True, exist_ok=True)
    if resume and all_found.is_file():
        existing_entries = _load_entries(all_found)
        seen_channels = {key for entry in existing_entries if (key := _channel_key(entry))}
        print(f"Resuming allFound with {len(seen_channels)} existing channels.", flush=True)
    else:
        all_found.write_text("#EXTM3U\n", encoding="utf-8")
        seen_channels = set()
    accepted_playlists = 0
    processed_playlists = 0
    repositories = list(search_repositories(queries, max_repositories, timeout, token, workers))
    playlist_jobs = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        tree_futures = {
            executor.submit(_repository_playlist_paths, repository, timeout, token): repository
            for repository in repositories
        }
        for future in as_completed(tree_futures):
            repository = tree_futures[future]
            full_name = repository["full_name"]
            print(f"[REPOSITORY] {full_name}", flush=True)
            try:
                for path in future.result():
                    if processed_playlists >= max_playlists:
                        break
                    playlist_jobs.append((repository, path))
                    processed_playlists += 1
            except GitHubRateLimitError:
                raise
            except (GitHubAPIError, OSError) as error:
                print(f"  [REPOSITORY SKIPPED] {full_name}: {error}", file=sys.stderr, flush=True)

        download_futures = {
            executor.submit(download_playlist, repository, path, timeout, token): (repository, path)
            for repository, path in playlist_jobs
        }
        for future in as_completed(download_futures):
            repository, path = download_futures[future]
            try:
                source_url, content = future.result()
                entries = []
                for entry in parse_entries(content):
                    key = _channel_key(entry)
                    if key and key not in seen_channels:
                        seen_channels.add(key)
                        entries.append(entry)
                if entries:
                    _append_entries(all_found, entries)
                    accepted_playlists += 1
                    print(
                        f"  [M3U] {source_url}: {len(entries)} new channels "
                        f"(total {len(seen_channels)})",
                        flush=True,
                    )
            except GitHubRateLimitError:
                raise
            except (GitHubAPIError, OSError, UnicodeDecodeError, ValueError) as error:
                print(f"  [SKIPPED] {repository['full_name']}:{path}: {error}", file=sys.stderr, flush=True)
    print(
        f"Discovery complete: {len(repositories)} repositories, "
        f"{accepted_playlists} valid playlists out of {processed_playlists} checked, "
        f"{len(seen_channels)} channels in {all_found}.",
        flush=True,
    )
    return len(seen_channels)


def validate_all(all_found, working, timeout, delay, resume=False, workers=MAX_WORKERS):
    entries = _load_entries(all_found)
    unique_entries = []
    all_keys = set()
    for entry in entries:
        key = _channel_key(entry)
        if key and key not in all_keys:
            all_keys.add(key)
            unique_entries.append((key, entry))

    working_entries = []
    seen = set()
    if resume and working.is_file():
        source_by_key = dict(unique_entries)
        for entry in _load_entries(working):
            key = _channel_key(entry)
            if key and key in source_by_key and key not in seen:
                seen.add(key)
                working_entries.append(source_by_key[key])
        print(f"Resuming working with {len(working_entries)} already validated channels.", flush=True)
    _write_entries(working, working_entries)
    working_count = len(working_entries)
    total = len(unique_entries)
    completed = len(seen)
    processed_this_run = 0
    started_at = time.monotonic()
    request_start_lock = threading.Lock()
    last_request_started = None

    def check_with_spacing(url, headers):
        nonlocal last_request_started
        with request_start_lock:
            now = time.monotonic()
            if last_request_started is not None:
                wait_time = delay - (now - last_request_started)
                if wait_time > 0:
                    time.sleep(wait_time)
            last_request_started = time.monotonic()
        return check_channel(url, headers, timeout)

    pending_entries = iter(
        (key, entry, *channel_request(entry))
        for key, entry in unique_entries
        if key not in seen
    )
    pending = {}
    print(f"Starting validation of {total} unique channels...", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        def fill_window():
            while len(pending) < workers:
                try:
                    key, entry, name, url, headers = next(pending_entries)
                except StopIteration:
                    return
                future = executor.submit(check_with_spacing, url, headers)
                pending[future] = (key, entry, name)

        fill_window()
        while pending:
            completed_futures, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed_futures:
                key, entry, name = pending.pop(future)
                try:
                    result = future.result()
                    _append_entries(working, [entry])
                    working_count += 1
                    message = f"[WORKS] {name}: {result}"
                except (OSError, ValueError) as error:
                    message = f"[FAILED] {name}: {error}"
                completed += 1
                processed_this_run += 1
                elapsed = time.monotonic() - started_at
                average = elapsed / processed_this_run
                eta = format_eta(average * (total - completed))
                percent = completed / total * 100 if total else 100
                percent_text = format_percent(percent)
                print(f"[{completed}/{total} {percent_text}% ETA: {eta}] {message}", flush=True)
            fill_window()
    print(f"Validation complete: {working_count}/{total} channels in {working}.", flush=True)
    return working_count


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a positive integer.") from None
    if number < 1:
        raise argparse.ArgumentTypeError("Use a positive integer.")
    return number


def positive_float(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a positive number.") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("Use a positive number.")
    return number


def nonnegative_float(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a non-negative number.") from None
    if number < 0:
        raise argparse.ArgumentTypeError("Use a non-negative number.")
    return number


def worker_count(value):
    number = positive_int(value)
    if number > MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"Use between 1 and {MAX_WORKERS} workers.")
    return number


def main():
    parser = argparse.ArgumentParser(
        description="Search GitHub for public M3U playlists and validate their streams."
    )
    parser.add_argument("--query", action="append", help="Repository search query; may be repeated.")
    parser.add_argument("--all-found", type=Path, default=DEFAULT_ALL_FOUND)
    parser.add_argument("--working", type=Path, default=DEFAULT_WORKING)
    parser.add_argument("--max-repositories", type=positive_int, default=MAX_REPOSITORIES)
    parser.add_argument("--max-playlists", type=positive_int, default=MAX_PLAYLISTS)
    parser.add_argument("--request-timeout", type=positive_float, default=REQUEST_TIMEOUT)
    parser.add_argument("--stream-timeout", type=positive_float, default=STREAM_TIMEOUT)
    parser.add_argument("--check-delay", type=nonnegative_float, default=CHECK_DELAY)
    parser.add_argument(
        "--workers",
        type=worker_count,
        default=MAX_WORKERS,
        help=f"Maximum concurrent tasks (1-{MAX_WORKERS}; default: {MAX_WORKERS}).",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=RESUME,
        help="Preserve existing M3Us and skip completed channels (default; use --no-resume to restart).",
    )
    args = parser.parse_args()

    if args.all_found.resolve() == args.working.resolve():
        parser.error("--all-found and --working must be different files.")
    if not args.resume:
        for path in (args.all_found, args.working):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#EXTM3U\n", encoding="utf-8")
    token = os.environ.get(GITHUB_TOKEN_ENV)
    queries = args.query or list(DEFAULT_QUERIES)
    try:
        discover(
            queries,
            args.all_found,
            args.max_repositories,
            args.max_playlists,
            args.request_timeout,
            token,
            resume=args.resume,
            workers=args.workers,
        )
        validate_all(
            args.all_found,
            args.working,
            args.stream_timeout,
            args.check_delay,
            resume=args.resume,
            workers=args.workers,
        )
    except (GitHubAPIError, OSError, ValueError) as error:
        parser.exit(1, f"Pipeline failed: {error}\n")
    except KeyboardInterrupt:
        print("Interrupted; partial results were preserved. Run again with --resume.", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
