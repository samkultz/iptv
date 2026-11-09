
#!/usr/bin/env python3

import argparse
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# ==========================================================
# CONFIGURAÇÃO
# ==========================================================

VIDEO = Path(
    "/Users/samkultz/Downloads/QB/"
    "Harry.Potter.and.the.Order.of.the.Phoenix.2007.2160p."
    "UHD.BluRay.x265-DEPTH/"
    "Harry.Potter.and.the.Order.of.the.Phoenix.2007.2160p."
    "UHD.BluRay.x265-DEPTH.mkv"
)

PORT = 8091
VIDEO_BITRATE = "6000k"
TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080
AUDIO_BITRATE = "640k"
AUDIO_CHANNELS = 6

# ==========================================================
# UTILITÁRIOS
# ==========================================================

def probe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_streams",
            "-show_format",
            "-of", "json",
            str(path)
        ],
        capture_output=True,
        text=True,
        check=True
    )
    return json.loads(result.stdout)


def find_audio(info):
    audios = [
        s for s in info["streams"]
        if s["codec_type"] == "audio"
    ]

    for position, stream in enumerate(audios):
        if stream["codec_name"] == "ac3":
            return position

    for position, stream in enumerate(audios):
        if stream["codec_name"] == "dts":
            profile = stream.get("profile", "").upper()
            if "DTS-HD" in profile or "DTS:X" in profile:
                return position

    if audios:
        return 0

    raise RuntimeError("Nenhuma faixa de áudio encontrada.")


def format_time(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def show_progress(position, duration, elapsed):
    if duration > 0:
        fraction = min(position / duration, 1.0)
        percent = fraction * 100
        remaining = (duration - position) / (position / elapsed) if position > 0 and elapsed > 0 else 0
        bar_width = 30
        filled = round(fraction * bar_width)
        bar = "#" * filled + "-" * (bar_width - filled)
        estimate = format_time(remaining) if position > 0 else "--:--:--"
        message = (
            f"[{bar}] {percent:5.1f}%  "
            f"elapsed {format_time(elapsed)}  ETA {estimate}"
        )
    else:
        message = f"Media time {format_time(position)}  elapsed {format_time(elapsed)}"
    print(f"\r{message}", end="", flush=True)


def transcode(source, audio_position, duration):
    output = source.with_name(f".{source.stem}.transcoding.mkv")
    if output.exists():
        raise FileExistsError(f"Temporary output already exists: {output}")

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", f"0:a:{audio_position}",
        "-vf",
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p",
        "-c:v", "h264_videotoolbox",
        "-profile:v", "high",
        "-b:v", VIDEO_BITRATE,
        "-c:a", "ac3",
        "-b:a", AUDIO_BITRATE,
        "-ac", str(AUDIO_CHANNELS),
        "-sn", "-dn",
        "-progress", "pipe:1",
        "-stats_period", "20",
        "-nostats",
        str(output),
    ]

    print("\n=== FAST TV CONVERSION ===")
    print("Video: H.264, up to 1080p (VideoToolbox)")
    print("Audio: AC3, 5.1 channels")
    print("A hidden temporary file will replace the original after validation.")
    print("Temporary output:", output)
    print(flush=True)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    started_at = time.monotonic()
    progress = {}

    try:
        for line in process.stdout or ():
            key, separator, value = line.strip().partition("=")
            if not separator:
                continue
            progress[key] = value
            if key == "progress":
                timestamp = progress.get("out_time", "N/A")
                if timestamp != "N/A":
                    hours, minutes, seconds = timestamp.split(":")
                    position = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                    show_progress(position, duration, time.monotonic() - started_at)
                progress.clear()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        print()
        if process.returncode != 0:
            output.unlink(missing_ok=True)

    converted = probe(output)
    video = next(
        (stream for stream in converted["streams"] if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in converted["streams"] if stream.get("codec_type") == "audio"),
        None,
    )
    if video is None or video.get("codec_name") != "h264" or audio is None or audio.get("codec_name") != "ac3":
        output.unlink(missing_ok=True)
        raise RuntimeError("Converted file failed H.264/AC3 validation; original was kept.")

    output.replace(source)
    print("Replaced original with validated TV-compatible file:", source)
    return process.returncode


# ==========================================================
# STREAMING SEM RECODIFICAR O VÍDEO
# ==========================================================

def stream(source, audio_position, duration):
    print("\n=== STREAMING ===")
    print("Vídeo: H.264 até 1080p, convertido em tempo real")
    print("Aspect ratio: preservado")
    print("FPS: preservado")

    print("Áudio: conversão AC3 em tempo real")
    command = [
        "ffmpeg", "-hide_banner",
        "-re",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", f"0:a:{audio_position}",
    ]

    command += [
        "-vf",
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p",
        "-c:v", "h264_videotoolbox",
        "-profile:v", "high",
        "-b:v", VIDEO_BITRATE,
        "-c:a", "ac3",
        "-b:a", AUDIO_BITRATE,
        "-ac", str(AUDIO_CHANNELS),
        "-progress", "pipe:1",
        "-nostats",
        "-sn", "-dn",
        "-mpegts_flags", "+resend_headers",
        "-f", "mpegts",
        "-listen", "1",
        f"http://0.0.0.0:{PORT}/movie"
    ]

    print(f"\nPorta: {PORT}")
    print("Caminho: /movie")
    print("Aguardando conexão HTTP do UMS...")
    print("Ctrl+C para encerrar.\n", flush=True)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    started_at = None
    progress = {}
    progress_lines = queue.Queue()

    def collect_progress():
        if process.stdout is not None:
            for output_line in process.stdout:
                progress_lines.put(output_line)
        progress_lines.put(None)

    reader = threading.Thread(target=collect_progress, daemon=True)
    reader.start()
    waiting_at = time.monotonic()

    try:
        while True:
            try:
                line = progress_lines.get(timeout=1)
            except queue.Empty:
                if process.poll() is not None:
                    break
                if started_at is None:
                    waiting = format_time(time.monotonic() - waiting_at)
                    print(
                        f"\rWaiting for UMS connection | elapsed {waiting}",
                        end="",
                        flush=True,
                    )
                continue

            if line is None:
                break
            key, separator, value = line.strip().partition("=")
            if not separator:
                continue
            progress[key] = value
            if key == "progress":
                if started_at is None:
                    started_at = time.monotonic()
                timestamp = progress.get("out_time", "N/A")
                if timestamp != "N/A":
                    hours, minutes, seconds = timestamp.split(":")
                    position = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                    show_progress(position, duration, time.monotonic() - started_at)
                progress.clear()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        print()

    return process.returncode


# ==========================================================
# MAIN
# ==========================================================

def main():
    parser = argparse.ArgumentParser(description="Transcode or stream a video for TV playback.")
    parser.add_argument(
        "--transcode",
        action="store_true",
        help="Create a fast .tv.mkv copy for UMS instead of starting the HTTP stream.",
    )
    args = parser.parse_args()

    for program in ("ffmpeg", "ffprobe"):
        if not shutil.which(program):
            raise RuntimeError(
                f"{program} não encontrado no PATH."
            )

    source = VIDEO.expanduser().resolve()

    if not source.is_file():
        raise FileNotFoundError(source)

    info = probe(source)

    print("\n=== ARQUIVO ORIGINAL ===")
    print(source.name)
    print(
        f"Tamanho: {source.stat().st_size / 1024**3:.2f} GiB"
    )

    audio_position = find_audio(info)

    duration = float(info.get("format", {}).get("duration", 0))
    if args.transcode:
        status = transcode(source, audio_position, duration)
    else:
        status = stream(source, audio_position, duration)

    if status != 0:
        raise RuntimeError(
            f"Streaming encerrado com erro: {status}"
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStreaming interrompido.")
    except Exception as error:
        print(f"\nERRO: {error}", file=sys.stderr)
        sys.exit(1)
