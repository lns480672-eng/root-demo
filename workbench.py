"""Serve ROOT's local workbench using only Python's standard library."""

import argparse
import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import generate

ROOT = Path(__file__).resolve().parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
    "/workbench.js": ("workbench.js", "text/javascript; charset=utf-8"),
}
TOOL_PATH = re.compile(r"/api/tools/(run-[a-z0-9_]{8})")
SHELL_POLICY = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
GENERATION_ERRORS = {
    "incomplete_html": "模型输出不完整，需求已保留。",
    "credential_in_output": "模型输出未通过安全检查，需求已保留。",
    "request_failed": "模型请求未完成，请检查网络后重试。",
}


def build_id(assets):
    digest = hashlib.sha256()
    for name in ("generate.py", "workbench.py"):
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes() + b"\0")
    for path, body in assets.items():
        digest.update(path.encode() + b"\0" + body + b"\0")
    return "v4-" + digest.hexdigest()[:12]


def model_error(evidence):
    status = evidence.get("http_status")
    if status in (401, 403):
        return "DeepSeek 拒绝了本机配置的 Key，请检查配置。"
    if status == 402:
        return "DeepSeek 余额不足，请检查账户。"
    if status == 429:
        return "DeepSeek 请求过多，请稍后重试。"
    if evidence.get("error_type") in ("TimeoutError", "timeout"):
        return "生成超时，需求已保留。"
    return GENERATION_ERRORS.get(evidence["status"], "DeepSeek 暂时无法完成请求，请重试。")


def serve(key, port, runs=generate.RUNS):
    generation_lock = threading.Lock()
    assets = {url: (ROOT / "workbench" / name).read_bytes()
              for url, (name, _) in STATIC.items()}
    build = build_id(assets)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Keep untrusted inputs and request headers out of terminal logs.
            pass

        def send_bytes(self, status, body, content_type, policy=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if policy:
                self.send_header("Content-Security-Policy", policy)
                paths = (*STATIC, "/api/config", "/api/generate", "/api/tools/*")
                allowed = " ".join(f'"http://{host}{path}"' for path in paths)
                self.send_header("Connection-Allowlist", f"({allowed}); webrtc=block")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, payload):
            self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode(),
                            "application/json; charset=utf-8")

        def error(self, status, code, message):
            self.send_json(status, {"code": code, "message": message})

        def valid_host(self):
            if self.headers.get("Host") == host:
                return True
            self.error(403, "invalid_host", "请使用启动时显示的本机地址。")
            return False

        def do_GET(self):
            if not self.valid_host():
                return
            if self.path in assets:
                _, mime = STATIC[self.path]
                self.send_bytes(200, assets[self.path], mime,
                                SHELL_POLICY if self.path == "/" else None)
            elif self.path == "/api/config":
                self.send_json(200, {"configured": bool(key), "build": build})
            elif self.path == "/api/egress-check":
                self.send_json(200, {"allowed": True})
            elif match := TOOL_PATH.fullmatch(self.path):
                self.send_tool(match[1])
            else:
                self.error(404, "not_found", "页面不存在。")

        def send_tool(self, tool_id):
            try:
                html = (runs / tool_id / "app.html").read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                self.error(404, "tool_unavailable", "上次的工具文件不可用。")
                return
            self.send_json(200, {"id": tool_id, "html": html})

        def do_POST(self):
            if not self.valid_host():
                return
            if self.path != "/api/generate":
                self.error(404, "not_found", "页面不存在。")
                return
            if self.headers.get("Origin") != "http://" + host:
                self.error(403, "invalid_origin", "请从本机工作台发起生成。")
                return
            if self.headers.get_content_type() != "application/json":
                self.error(415, "invalid_type", "生成请求必须是 JSON。")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16000:
                    raise ValueError
                self.connection.settimeout(10)
                data = json.loads(self.rfile.read(size).decode("utf-8"))
                requirement = data["requirement"].strip()
                if not 0 < len(requirement) <= 2000 or (key and key in requirement):
                    raise ValueError
                requirement.encode("utf-8")
            except (ValueError, AttributeError, KeyError, TypeError, OSError):
                self.error(400, "invalid_requirement", "请输入 1–2000 字的工具需求，避免填写凭据。")
                return
            if not key:
                self.error(503, "key_required", "请在本机服务配置 DeepSeek Key。")
                return
            if not generation_lock.acquire(blocking=False):
                self.error(409, "busy", "另一个生成请求正在处理，请稍后重试。")
                return
            try:
                run, evidence = generate.run_generation(requirement, key, runs)
            except OSError:
                self.error(500, "save_failed", "本机无法保存生成结果，请检查运行目录。")
                return
            finally:
                generation_lock.release()
            if evidence["status"] == "generated":
                self.send_tool(run.name)
            else:
                self.error(502, evidence["status"], model_error(evidence))

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        host = f"127.0.0.1:{server.server_port}"
        print(f"ROOT {build} · http://{host}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--prompt-key", action="store_true", help="在本机终端隐藏输入 Key")
    args = parser.parse_args()
    key = generate.prompt_key() if args.prompt_key else os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if args.prompt_key and not key:
        return 2
    if key and not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        parser.error("DeepSeek Key 格式无效，请检查本机配置。")
    if not key:
        print("尚未配置 DeepSeek Key。可查看工作台及恢复既有工具。", flush=True)
    serve(key, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
