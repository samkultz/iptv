"""Serve the generated IPTV playlist and XMLTV guide on the local network."""

import argparse
import ipaddress
import os
import re
import shutil
import socket
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_PLAYLIST = Path(__file__).resolve().parent / "output" / "working.m3u"
DEFAULT_OUTPUT = DEFAULT_PLAYLIST.with_name("runprogram.m3u")


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
        return self.playlist.read_bytes()

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
    parser = argparse.ArgumentParser(description="Serve a playlist IPTV e o guia por HTTP na rede local.")
    parser.add_argument("--port", type=port_number, default=8080, help="Porta HTTP (padrão: 8080).")
    parser.add_argument("--host", type=ipv4_address, default="0.0.0.0",
                        help="IPv4 de escuta (padrão: todas as interfaces).")
    parser.add_argument("--playlist", type=Path, default=DEFAULT_PLAYLIST,
                        help="Arquivo M3U (padrão: output/working.m3u ao lado do script).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Arquivo M3U de saída.")
    args = parser.parse_args()
    playlist = args.playlist.expanduser().resolve()
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
