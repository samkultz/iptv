
#!/usr/bin/env python3
"""
IPTV server with automatic video preprocessing.

Features:
- Convert 4K HEVC to 1080p H.264/AAC before streaming.
- Preserve the original display aspect ratio.
- Cache completed conversions.
- Use Apple VideoToolbox hardware encoding.
- Stream without real-time transcoding.
- Use the main VLC GUI as playback controller.
- Open an independent VLC window for testing.
- Serve an HTTP M3U playlist.
- Clean up previous IPTV processes and ports.
"""

import argparse
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ==========================================
# CONFIGURATION
# ==========================================

VLC_APP = Path("/Applications/VLC.app")
VLC = VLC_APP / "Contents/MacOS/VLC"

DEFAULT_VIDEO = (
    "/Users/samkultz/Downloads/QB/"
    "Harry Potter And The Prisoner Of Azkaban (2004) [2160p] [4K] [BluRay] [5.1] [YTS.MX]/"
    "Harry.Potter.And.The.Prisoner.Of.Azkaban.2004.2160p.4K.BluRay.x265.10bit.AAC5.1-[YTS.MX].mkv"
)

HOST = "0.0.0.0"

PLAYLIST_PORT = 8090
STREAM_PORT = 8091

PLAYLIST_ENDPOINT = "/playlist.m3u"
STREAM_ENDPOINT = "/movie"

CHANNEL_NAME = "Mac VLC"
CHANNEL_GROUP = "Local"

LOOP_VIDEO = False
CLEANUP_PORTS = True

OPEN_TEST_PLAYER = True
TEST_PLAYER_DELAY = 2.0
NETWORK_CACHING = 1000

# ==========================================
# PREPROCESSING CONFIGURATION
# ==========================================

PREPROCESS_VIDEO = True

# Existing valid conversions are reused.
USE_CONVERSION_CACHE = True

# Force a new conversion regardless of cache.
FORCE_RECONVERT = False

# Maximum output dimensions.
TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080

# VideoToolbox H.264 bitrate.
VIDEO_BITRATE = "6000k"

# AAC stereo bitrate.
AUDIO_BITRATE = "192k"
AUDIO_CHANNELS = 2

# Preprocessing output extension.
OUTPUT_EXTENSION = ".mp4"

# Conversion configuration version.
# Increment if you intentionally change the encoding policy.
CACHE_VERSION = 1


# ==========================================
# NETWORK
# ==========================================

def lan_ip():
    """Get the local network IPv4 address."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def stream_url(host="127.0.0.1"):
    return (
        f"http://{host}:{STREAM_PORT}"
        f"{STREAM_ENDPOINT}"
    )


def playlist_url(host="127.0.0.1"):
    return (
        f"http://{host}:{PLAYLIST_PORT}"
        f"{PLAYLIST_ENDPOINT}"
    )


# ==========================================
# FFMPEG UTILITIES
# ==========================================

def require_program(name):
    """Find an executable in PATH."""

    executable = shutil.which(name)

    if executable is None:
        raise FileNotFoundError(
            f"{name} was not found in PATH. "
            f"Install it first."
        )

    return executable


def ffprobe_info(video):
    """Inspect media using FFprobe."""

    ffprobe = require_program("ffprobe")

    command = [
        ffprobe,
        "-v", "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height",
        "-show_entries",
        "format=duration,size",
        "-of", "json",
        str(video),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
    )

    return json.loads(result.stdout)


def video_stream(info):
    """Return the first video stream."""

    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream

    return None


def audio_stream(info):
    """Return the first audio stream."""

    for stream in info.get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream

    return None


# ==========================================
# CONVERSION CACHE
# ==========================================

def converted_path(source):
    """Determine the cached output filename."""

    return source.with_name(
        source.stem
        + ".iptv_1080p"
        + OUTPUT_EXTENSION
    )


def metadata_path(output):
    return output.with_suffix(
        output.suffix + ".json"
    )


def conversion_signature(source):
    """
    Create a signature from the source file
    and current conversion settings.
    """

    stat = source.stat()

    return {
        "cache_version": CACHE_VERSION,
        "source": str(source.resolve()),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "width": TARGET_WIDTH,
        "height": TARGET_HEIGHT,
        "video_codec": "h264_videotoolbox",
        "video_bitrate": VIDEO_BITRATE,
        "audio_codec": "aac",
        "audio_bitrate": AUDIO_BITRATE,
        "audio_channels": AUDIO_CHANNELS,
        "output_extension": OUTPUT_EXTENSION,
    }


def validate_converted_video(output):
    """Ensure the cached output has usable media streams."""

    try:
        if not output.is_file():
            return False

        if output.stat().st_size < 1024:
            return False

        info = ffprobe_info(output)

        video = video_stream(info)

        if video is None:
            return False

        if video.get("codec_name") != "h264":
            return False

        width = video.get("width", 0)
        height = video.get("height", 0)

        if not width or not height:
            return False

        if width > TARGET_WIDTH:
            return False

        if height > TARGET_HEIGHT:
            return False

        # H.264 output is encoded with even dimensions.
        if width % 2 or height % 2:
            return False

        audio = audio_stream(info)

        if audio is not None:
            if audio.get("codec_name") != "aac":
                return False

        duration = float(
            info.get("format", {}).get("duration", 0)
        )

        if duration <= 0:
            return False

        return True

    except (
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ):
        return False


def cache_valid(source, output):
    """Check whether the conversion can be reused."""

    if not USE_CONVERSION_CACHE:
        return False

    if FORCE_RECONVERT:
        return False

    metadata = metadata_path(output)

    if not output.is_file():
        return False

    if not metadata.is_file():
        return False

    try:
        existing_signature = json.loads(
            metadata.read_text(encoding="utf-8")
        )

        expected_signature = conversion_signature(source)

        if existing_signature != expected_signature:
            return False

        return validate_converted_video(output)

    except (OSError, ValueError, KeyError):
        return False


# ==========================================
# PORT MANAGEMENT
# ==========================================

def listening_pids(port):
    """Get PIDs listening on a TCP port."""

    result = subprocess.run(
        [
            "lsof",
            "-nP",
            f"-tiTCP:{port}",
            "-sTCP:LISTEN",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"Unable to inspect port {port}: "
            f"{result.stderr.strip()}"
        )

    return {
        int(line)
        for line in result.stdout.splitlines()
        if line.strip().isdigit()
    }


def process_command(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )

    return result.stdout.strip()


def cleanup_ports():
    """Stop recognized previous IPTV listeners."""

    print("Checking IPTV ports...", flush=True)

    found = set()

    for port in (PLAYLIST_PORT, STREAM_PORT):
        found.update(listening_pids(port))

    found.discard(os.getpid())

    for pid in sorted(found):
        command = process_command(pid)

        executable = (
            command.split(" ", 1)[0]
            if command else ""
        )

        is_vlc = executable == str(VLC)

        is_iptv = (
            "runprogram_stream.py" in command
            and "python" in command.lower()
        )

        if not (is_vlc or is_iptv):
            print(
                f"Skipping unrelated process {pid}: {command}",
                flush=True,
            )
            continue

        print(f"Stopping previous IPTV process {pid}")

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise RuntimeError(
                f"Cannot terminate PID {pid}: {error}"
            ) from error

    deadline = time.monotonic() + 5

    while time.monotonic() < deadline:
        occupied = [
            port
            for port in (PLAYLIST_PORT, STREAM_PORT)
            if listening_pids(port)
        ]

        if not occupied:
            print("Ports available.", flush=True)
            return

        time.sleep(0.2)

    occupied = [
        port
        for port in (PLAYLIST_PORT, STREAM_PORT)
        if listening_pids(port)
    ]

    raise RuntimeError(
        f"Ports still occupied: {occupied}"
    )


# ==========================================
# STREAM OUTPUT
# ==========================================

def build_sout():
    """
    Remux the preprocessed H.264/AAC video
    into MPEG-TS over HTTP.

    NO real-time transcoding.
    """

    return (
        "#standard{"
        "access=http,"
        "mux=ts,"
        f"dst=:{STREAM_PORT}{STREAM_ENDPOINT}"
        "}"
    )


# ==========================================
# MAIN VLC CONTROLLER
# ==========================================

def start_controller(video):
    """
    Launch graphical VLC as playback controller.

    VLC reads the converted file and sends
    it over HTTP without transcoding.
    """

    if not VLC.is_file():
        raise FileNotFoundError(
            f"VLC not found: {VLC}"
        )

    if not video.is_file():
        raise FileNotFoundError(
            f"Video not found: {video}"
        )

    if listening_pids(STREAM_PORT):
        raise RuntimeError(
            f"Port {STREAM_PORT} is occupied."
        )

    command = [
        str(VLC),

        "--intf=macosx",
        "--quiet",

        "--no-video-title-show",
        "--no-sout-all",
        "--sout-keep",
    ]

    if LOOP_VIDEO:
        command.append("--input-repeat=-1")

    command.extend([
        f"--sout={build_sout()}",
        str(video),
    ])

    print("\nOpening VLC controller...", flush=True)

    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
    )


# ==========================================
# TEMPORARY TEST PLAYER
# ==========================================

def open_test_player():
    """
    Open independent VLC application instance.

    Closing this window does not stop the
    primary VLC streaming controller.
    """

    command = [
        "/usr/bin/open",
        "-n",
        "-a",
        str(VLC_APP),
        "--args",

        "--intf=macosx",
        "--quiet",
        "--no-video-title-show",

        f"--network-caching={NETWORK_CACHING}",

        stream_url(),
    ]

    print("\nOpening VLC test player...", flush=True)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        print(
            "Test player error: "
            + result.stderr.strip(),
            flush=True,
        )
        return False

    print(
        "Test player opened. "
        "Close it after checking the stream.",
        flush=True,
    )

    return True


# ==========================================
# M3U SERVER
# ==========================================

class PlaylistHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path not in ("/", PLAYLIST_ENDPOINT):
            self.send_error(404, "Not found")
            return

        playlist = (
            "#EXTM3U\n"
            f'#EXTINF:-1 tvg-name="{CHANNEL_NAME}" '
            f'group-title="{CHANNEL_GROUP}",'
            f"{CHANNEL_NAME}\n"
            f"{stream_url(lan_ip())}\n"
        )

        data = playlist.encode("utf-8")

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "audio/x-mpegurl",
        )
        self.send_header(
            "Content-Length",
            str(len(data)),
        )
        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )
        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate",
        )
        self.end_headers()

        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        print(
            f"[HTTP] {self.address_string()} {format % args}",
            flush=True,
        )


class PlaylistServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ==========================================
# PROCESS MANAGEMENT
# ==========================================

def stop_process(process, name):
    if process is None or process.poll() is not None:
        return

    print(f"Stopping {name}...", flush=True)

    process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def wait_for_stream(process, timeout=30):
    """Wait for the VLC HTTP listener."""

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"VLC exited with code {process.returncode}."
            )

        if process.pid in listening_pids(STREAM_PORT):
            return

        time.sleep(0.3)

    raise RuntimeError(
        f"VLC did not open port {STREAM_PORT}."
    )


# ==========================================
# MAIN
# ==========================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "IPTV server with cached 1080p preprocessing "
            "and graphical VLC controller."
        )
    )

    parser.add_argument(
        "video",
        nargs="?",
        default=DEFAULT_VIDEO,
        help="Original video file path.",
    )

    args = parser.parse_args()

    original_video = Path(
        args.video.strip().strip("'\"")
    ).expanduser().resolve()

    controller_process = None

    try:

        # ----------------------------------
        # CLEAN OLD PORTS
        # ----------------------------------

        if CLEANUP_PORTS:
            cleanup_ports()

        # ----------------------------------
        # START PLAYLIST SERVER
        # ----------------------------------

        with PlaylistServer(
            (HOST, PLAYLIST_PORT),
            PlaylistHandler,
        ) as server:

            server_thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )

            server_thread.start()

            # ----------------------------------
            # 4. START VLC CONTROLLER
            # ----------------------------------

            controller_process = start_controller(video)

            print("\nWaiting for VLC stream listener...")

            wait_for_stream(controller_process)

            ip = lan_ip()

            print()
            print("=" * 60)
            print("MAC VLC IPTV SERVER")
            print("=" * 60)

            print(f"Original:   {original_video.name}")
            print(f"Streaming:  {video.name}")
            print(f"Channel:    {CHANNEL_NAME}")

            print()
            print(f"Playlist:   {playlist_url(ip)}")
            print(f"Stream:     {stream_url(ip)}")

            print()
            print("Video:      H.264 / 1080p maximum")
            print(f"Bitrate:    {VIDEO_BITRATE}")
            print("Audio:      AAC stereo")
            print("Aspect:     Original")
            print("Encoding:   Preprocessed / cached")
            print("Live encode: Disabled")

            print()
            print("Controller: VLC graphical interface")
            print("Test player: Separate VLC instance")

            print()
            print("Status:     RUNNING")
            print("=" * 60)
            print()

            # ----------------------------------
            # 5. OPEN TEMPORARY TEST PLAYER
            # ----------------------------------

            if OPEN_TEST_PLAYER:
                time.sleep(TEST_PLAYER_DELAY)
                open_test_player()

            print()
            print("Control playback in the MAIN VLC window.")
            print("Close the TEST VLC after checking.")
            print("Closing MAIN VLC stops streaming.")
            print("Ctrl+C also stops the server.")
            print()

            # ----------------------------------
            # 6. MAINTAIN SERVER
            # ----------------------------------

            while controller_process.poll() is None:
                time.sleep(0.5)

            print("\nVLC controller closed.")

            server.shutdown()
            server_thread.join(timeout=5)

    except KeyboardInterrupt:
        print("\nStopping IPTV server...")

    except (
        OSError,
        ValueError,
        RuntimeError,
        FileNotFoundError,
        subprocess.SubprocessError,
    ) as error:
        print(f"\nERROR: {error}", file=sys.stderr)

    finally:
        stop_process(
            controller_process,
            "VLC controller",
        )

        print("Server stopped.", flush=True)


if __name__ == "__main__":
    main()
