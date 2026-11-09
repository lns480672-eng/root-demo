"""Serve ROOT's local workbench using only Python's standard library."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import generate
from accounts import ApiError, SESSION_SECONDS, Store

ROOT = Path(__file__).resolve().parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
    "/workbench.js": ("workbench.js", "text/javascript; charset=utf-8"),
}
TOOL_PATH = re.compile(r"/api/tools/(run-[a-z0-9_]{8})")
TOOL_ACTION = re.compile(r"/api/tools/(run-[a-z0-9_]{8})/(commit|open|data)")
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
    for name in ("generate.py", "workbench.py", "accounts.py"):
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
    store = Store(runs / "workspace.sqlite3")
    assets = {url: (ROOT / "workbench" / name).read_bytes()
              for url, (name, _) in STATIC.items()}
    build = build_id(assets)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Keep untrusted inputs and request headers out of terminal logs.
            pass

        def send_bytes(self, status, body, content_type, policy=None, cookie=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if policy:
                self.send_header("Content-Security-Policy", policy)
                paths = (*STATIC, "/api/config", "/api/session", "/api/register", "/api/login", "/api/logout",
                         "/api/generate", "/api/tools", "/api/tools/*")
                allowed = " ".join(f'"http://{host}{path}"' for path in paths)
                self.send_header("Connection-Allowlist", f"({allowed}); webrtc=block")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, payload, cookie=None):
            self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode(),
                            "application/json; charset=utf-8", cookie=cookie)

        def error(self, status, code, message):
            self.send_json(status, {"code": code, "message": message})

        def valid_host(self):
            if self.headers.get("Host") == host:
                return True
            self.error(403, "invalid_host", "请使用启动时显示的本机地址。")
            return False

        def cookie_token(self):
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                value = cookies["root_session"].value
                return value if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) else ""
            except (CookieError, KeyError):
                return ""

        def marker(self):
            return self.headers.get("X-Root-Session", "")

        def do_GET(self):
            if not self.valid_host():
                return
            try:
                self.handle_get()
            except (ApiError, sqlite3.Error) as error:
                self.handle_error(error)

        def handle_error(self, error):
            if isinstance(error, ApiError):
                self.error(error.status, error.code, str(error))
            else:
                self.error(503, "storage_unavailable", "本机数据暂时无法读取或保存，请重试。")

        def handle_get(self):
            if self.path in assets:
                _, mime = STATIC[self.path]
                self.send_bytes(200, assets[self.path], mime,
                                SHELL_POLICY if self.path == "/" else None)
            elif self.path == "/api/config":
                self.send_json(200, {"configured": bool(key), "build": build})
            elif self.path == "/api/session":
                try:
                    with store.connection() as db:
                        session = store.session(db, self.cookie_token())
                except ApiError:
                    self.send_json(200, {"user": None})
                else:
                    self.send_json(200, {"user": session["username"], "marker": session["marker"],
                                         "latest": session["latest_tool"]})
            elif self.path == "/api/egress-check":
                self.send_json(200, {"allowed": True})
            elif self.path == "/api/tools":
                self.send_json(200, store.list_tools(self.cookie_token(), self.marker()))
            elif match := TOOL_PATH.fullmatch(self.path):
                row = store.get_tool(self.cookie_token(), self.marker(), match[1])
                self.send_tool(match[1], json.loads(row["entries"]), row["revision"])
            else:
                self.error(404, "not_found", "页面不存在。")

        def send_tool(self, tool_id, entries, revision):
            try:
                html = (runs / tool_id / "app.html").read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                self.error(404, "tool_unavailable", "此工具不可用。")
                return
            self.send_json(200, {"id": tool_id, "html": html, "entries": entries, "revision": revision})

        def do_POST(self):
            if not self.valid_host():
                return
            if self.headers.get("Origin") != "http://" + host:
                self.error(403, "invalid_origin", "请从本机工作台发起生成。")
                return
            if self.headers.get_content_type() != "application/json":
                self.error(415, "invalid_type", "生成请求必须是 JSON。")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                cap = 2 * 1024 * 1024 if TOOL_ACTION.fullmatch(self.path) else 16000
                if not 0 < size <= cap:
                    raise ValueError
                self.connection.settimeout(10)
                data = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError
                json.dumps(data, ensure_ascii=False).encode("utf-8")
            except (ValueError, TypeError, OSError):
                self.error(400, "invalid_body", "请求内容无效。")
                return
            try:
                self.handle_action(data)
            except (ApiError, sqlite3.Error) as error:
                self.handle_error(error)

        def handle_action(self, data):
            token = self.cookie_token()
            if self.path == "/api/register":
                store.register(data)
                self.send_json(201, {"ok": True})
                return
            if self.path == "/api/login":
                new_token = store.login(data, token)
                self.send_json(200, {"ok": True},
                               f"root_session={new_token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}")
                return
            if self.path == "/api/logout":
                store.logout(token, self.marker())
                self.send_json(200, {"ok": True}, "root_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                return
            if match := TOOL_ACTION.fullmatch(self.path):
                self.send_json(200, store.change_tool(token, self.marker(), match[1], match[2], data))
                return
            if self.path != "/api/generate":
                raise ApiError(404, "not_found", "页面不存在。")
            with store.connection() as db:
                store.session(db, token, self.marker())
            requirement = data.get("requirement")
            requirement = requirement.strip() if isinstance(requirement, str) else ""
            if not 0 < len(requirement) <= 2000 or (key and key in requirement):
                raise ApiError(400, "invalid_requirement", "请输入 1–2000 字的工具需求，避免填写凭据。")
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
                store.draft(token, self.marker(), run.name, requirement)
                self.send_tool(run.name, [], 0)
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
    parser.add_argument("--runs-dir", type=Path, default=generate.RUNS)
    parser.add_argument("--create-invite", action="store_true")
    parser.add_argument("--invite-hours", type=int, default=24)
    parser.add_argument("--invite-uses", type=int, default=1)
    args = parser.parse_args()
    if args.create_invite:
        if not 1 <= args.invite_hours <= 720 or not 1 <= args.invite_uses <= 50:
            parser.error("本地邀请码期限 1–720 小时、使用次数 1–50。")
        print(Store(args.runs_dir / "workspace.sqlite3").create_invite(
            uses=args.invite_uses, hours=args.invite_hours))
        return 0
    key = generate.prompt_key() if args.prompt_key else os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if args.prompt_key and not key:
        return 2
    if key and not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        parser.error("DeepSeek Key 格式无效，请检查本机配置。")
    if not key:
        print("尚未配置 DeepSeek Key。可查看工作台及恢复既有工具。", flush=True)
    serve(key, args.port, args.runs_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
