"""Serve the ROOT product site on the IPv6 loopback, separate from the workbench."""

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv6Address
from pathlib import Path
from socket import AF_INET6
from urllib.parse import urlsplit

PAGE = Path(__file__).resolve().parent / "design" / "root-public-site.html"
SITE_HOST = "::1"
DEFAULT_WORKBENCH = "http://127.0.0.1:8774/"
POLICY = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class SiteServer(ThreadingHTTPServer):
    address_family = AF_INET6


def workbench_address(value):
    try:
        url = urlsplit(value)
        valid = (
            url.scheme in ("http", "https") and url.hostname
            and url.username is None and url.password is None
            and not any(char.isspace() or ord(char) < 32 or char == "\\" for char in value)
            and (url.port is None or url.port > 0)
        )
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("请输入无凭据的 HTTP 或 HTTPS 工作台地址。")
    hostname = url.hostname
    if hostname.rstrip(".") == "localhost" or (":" in hostname and IPv6Address(hostname).is_loopback):
        raise argparse.ArgumentTypeError("工作台需使用与官网分开的主机地址，本机请使用 127.0.0.1。")
    return value


def serve(port, workbench):
    template = PAGE.read_text(encoding="utf-8")
    body = template.replace(
        f'href="{DEFAULT_WORKBENCH}"', f'href="{escape(workbench, quote=True)}"'
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_page(self, include_body):
            if self.headers.get_all("Host") != [host]:
                self.send_error(400)
                return
            if self.path.partition("?")[0] != "/":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", POLICY)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def do_GET(self):
            self.send_page(include_body=True)

        def do_HEAD(self):
            self.send_page(include_body=False)

    with SiteServer((SITE_HOST, port), Handler) as server:
        host = f"[{SITE_HOST}]:{server.server_port}"
        print(f"ROOT 官网 · http://{host}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--workbench-url", type=workbench_address, default=DEFAULT_WORKBENCH)
    options = parser.parse_args()
    serve(options.port, options.workbench_url)
