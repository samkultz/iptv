
#!/usr/bin/env python3

import json
import shutil
import subprocess
import sys
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

# ==========================================================
# UTILITÁRIOS
# ==========================================================

def run(command):
    return subprocess.run(
        command,
        check=False
    ).returncode


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


# ==========================================================
# STREAMING SEM RECODIFICAR O VÍDEO
# ==========================================================

def stream(source, audio_position):
    print("\n=== STREAMING ===")
    print("Vídeo: cópia direta do original")
    print("Resolução: preservada")
    print("HDR: preservado")
    print("Aspect ratio: preservado")
    print("FPS: preservado")

    print("Áudio: cópia direta da faixa selecionada")
    command = [
        "ffmpeg", "-hide_banner",
        "-re",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", f"0:a:{audio_position}",
    ]

    command += [
        "-c:v", "copy",
        "-c:a", "copy",
        "-sn", "-dn",
        "-mpegts_flags", "+resend_headers",
        "-f", "mpegts",
        "-listen", "1",
        f"http://0.0.0.0:{PORT}/movie"
    ]

    print(f"\nPorta: {PORT}")
    print("Caminho: /movie")
    print("Aguardando conexão HTTP...")
    print("Ctrl+C para encerrar.\n")

    return run(command)


# ==========================================================
# MAIN
# ==========================================================

def main():
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

    status = stream(
        source,
        audio_position
    )

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
