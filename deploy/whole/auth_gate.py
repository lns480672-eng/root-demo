"""Limit public credential requests before Python password hashing."""
from argparse import ArgumentParser
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from threading import Lock
from time import monotonic


class AuthGate:
    def __init__(self):
        self.lock = Lock()
        self.events = deque()
        self.counts = Counter()

    def allow(self, client, now):
        with self.lock:
            while self.events and self.events[0][0] <= now - 60:
                _, expired = self.events.popleft()
                self.counts[expired] -= 1
                if not self.counts[expired]:
                    del self.counts[expired]
            if len(self.events) >= 80 or self.counts[client] >= 10:
                return False
            self.events.append((now, client))
            self.counts[client] += 1
            return True


gate = AuthGate()
LIMITED = '{"code":"auth_rate_limit","message":"尝试次数过多，请稍后再试。"}'.encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path != "/check":
            self.send_error(404)
            return
        headers = self.headers.get_all("X-Root-Remote") or []
        try:
            client = ip_address(headers[0]) if len(headers) == 1 else None
        except ValueError:
            client = None
        if client is None:
            self.send_error(403)
            return
        if gate.allow(client, monotonic()):
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(429)
        self.send_header("Retry-After", "60")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(LIMITED)))
        self.end_headers()
        self.wfile.write(LIMITED)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8775)
    with ThreadingHTTPServer(("127.0.0.1", parser.parse_args().port), Handler) as server:
        server.serve_forever()
