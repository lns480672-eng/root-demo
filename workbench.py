"""Serve ROOT's local workbench using only Python's standard library."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from html.parser import HTMLParser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import generate
import assistant_fixture
import assistant_model
import table_import
import staged_blueprint
import staged_revision
from staged_jobs import StagedJobs
from accounts import ApiError, InviteError, SESSION_SECONDS, Store, TABLE_STORAGE_BYTES, TOOL_STORAGE_BYTES
from trial_budget import TrialBudget

ROOT = Path(__file__).resolve().parent
ADMIN = "/admin"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/updates": ("index.html", "text/html; charset=utf-8"),
    ADMIN: ("admin.html", "text/html; charset=utf-8"),
    "/admin.css": ("admin.css", "text/css; charset=utf-8"),
    "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
    "/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
    "/entry.js": ("entry.js", "text/javascript; charset=utf-8"),
    "/assistant.js": ("assistant.js", "text/javascript; charset=utf-8"),
    "/assistant.css": ("assistant.css", "text/css; charset=utf-8"),
    "/table.js": ("table.js", "text/javascript; charset=utf-8"),
    "/table.css": ("table.css", "text/css; charset=utf-8"),
    "/workbench.js": ("workbench.js", "text/javascript; charset=utf-8"),
}
TOOL_PATH = re.compile(r"/api/tools/(run-[a-z0-9_]{8})")
TOOL_ACTION = re.compile(r"/api/tools/(run-[a-z0-9_]{8})/(commit|open|data|publish|revise|resolve-publish|resolve-commit)")
TOOL_VERSIONS = re.compile(r"/api/tools/(run-[a-z0-9_]{8})/versions")
DRAFTS = "/api/requirement-drafts"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
DRAFT_PATH = re.compile(DRAFTS + "/(" + UUID + ")")
ANALYZE = "/api/assistant/analyze"
TRIAL = "/api/trial"
TRIAL_ADMIN = "/api/trial/admin"
STAGED = "/api/staged"
STAGED_TASK = re.compile(STAGED + r"/([a-f0-9]{32})")
STAGED_ACTION = re.compile(STAGED + r"/([a-f0-9]{32})/(resume|candidate|publish)")
TABLE_INSPECT = "/api/table/inspect"
TABLE_GENERATE = "/api/table/generate"
TABLE_CONFIG = "/api/table/config"
RELEASES = (
    ("0.3", "2026-11-09", None, "支持继续修改已有工具、查看工具版本历史，并调整窄屏生成流程。"),
    ("0.2", "2026-11-09", "v4-44e3da0cae1b", "加入邀请账号、个人工具列表和跨浏览器数据恢复。"),
    ("0.1", "2026-11-09", "v4-f58378aa55af", "提供自然语言生成、隔离预览、保存和刷新恢复。"),
)
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


class ToolTitle(HTMLParser):
    """Read a display label from generated HTML without running it."""

    def __init__(self):
        super().__init__()
        self.field = None
        self.parts = {}

    def handle_starttag(self, tag, attrs):
        if tag in ("title", "h1") and tag not in self.parts:
            self.parts[tag] = []
            self.field = tag

    def handle_endtag(self, tag):
        if tag == self.field:
            self.field = None

    def handle_data(self, data):
        if self.field:
            self.parts[self.field].append(data)

    def label(self, fallback):
        for tag in ("title", "h1"):
            label = " ".join("".join(self.parts.get(tag, ())).split())
            if label:
                return label
        return fallback


def build_id(assets):
    digest = hashlib.sha256()
    for name in ("generate.py", "workbench.py", "accounts.py", "assistant_fixture.py", "assistant_model.py",
                 "table_import.py", "trial_budget.py", "staged_blueprint.py", "staged_jobs.py", "staged_program.py", "staged_revision.py",
                 "workbench/staged-runtime.js"):
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


def session_cookie(token, max_age=SESSION_SECONDS):
    return f"root_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"


def serve(key, port, runs=generate.RUNS, *, generation_limits=(5, 30), analysis_limits=(6, 60),
          admin_usernames=(), analysis_provider=assistant_model.analyze, table_limits=None,
          staged_enabled=False, staged_provider=staged_blueprint.provide,
          staged_revision_provider=staged_revision.provide):
    model_lock = threading.Lock()
    store = Store(runs / "workspace.sqlite3")
    budget = TrialBudget(store, runs, generation_limits=generation_limits, analysis_limits=analysis_limits,
                         admin_usernames=admin_usernames)
    table_limits = table_limits or table_import.TableLimits()

    def stage_receipt(task, dispatch, receipt):
        if receipt.get("dispatched") is True:
            try:
                budget.finish_dispatch("generation", dispatch, success=receipt.get("status") == "ok")
            except sqlite3.Error:
                pass
        try:
            with (runs / "staged-usage.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"task":task, "dispatch":dispatch, **receipt}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    staged = StagedJobs(store, budget, runs, staged_blueprint, record=stage_receipt,
                        revision=staged_revision) if staged_enabled else None

    def stage_provider(stage, requirement, prior, admit, record, base=None):
        if not model_lock.acquire(timeout=20):
            raise ApiError(409, "staged_busy", "模型服务正在处理其他请求，请稍后继续。")
        try:
            if base is not None:
                return staged_revision_provider(stage, requirement, prior, key, admit, record, base=base)
            return staged_provider(stage, requirement, prior, key, admit, record)
        finally:
            model_lock.release()

    def start_staged(token, marker, task, *, resume):
        fence = staged.claim(token, marker, task, resume=resume)
        if fence is not None:
            base = staged.revision_context(task)

            def provider(stage, requirement, prior, admit, record):
                return stage_provider(stage, requirement, prior, admit, record, base)

            threading.Thread(target=staged.run, args=(task, fence, provider), daemon=True).start()
        return staged.status(token, marker, task)
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
                paths = (*STATIC, "/api/config", "/api/releases", "/api/session", "/api/register", "/api/login", "/api/logout",
                         "/api/generate", "/api/tools", "/api/tools/*", DRAFTS, DRAFTS + "/*", ANALYZE,
                         TABLE_INSPECT, TABLE_GENERATE, TABLE_CONFIG, TRIAL, TRIAL_ADMIN)
                if staged:
                    paths += (STAGED, STAGED + "/*")
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
            if isinstance(error, InviteError):
                self.send_json(error.status, {"code": error.code, "message": str(error), "inviteReason": error.reason})
            elif isinstance(error, ApiError):
                self.error(error.status, error.code, str(error))
            else:
                self.error(503, "storage_unavailable", "本机数据暂时无法读取或保存，请重试。")

        def handle_get(self):
            if self.path in assets:
                if self.path == ADMIN:
                    with store.connection() as db:
                        budget.require_admin(db, self.cookie_token(), None)
                _, mime = STATIC[self.path]
                self.send_bytes(200, assets[self.path], mime,
                                SHELL_POLICY if self.path in ("/", "/updates", ADMIN) else None)
            elif self.path == "/api/config":
                config = {"configured": bool(key), "build": build}
                if staged:
                    config["staged"] = True
                self.send_json(200, config)
            elif self.path == "/api/releases":
                items = [{"version": version, "date": date, "build": source or build, "note": note}
                         for version, date, source, note in RELEASES]
                self.send_json(200, {"current": items[0]["version"], "items": items})
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
            elif self.path == DRAFTS:
                self.send_json(200, store.list_requirement_drafts(self.cookie_token(), self.marker()))
            elif match := DRAFT_PATH.fullmatch(self.path):
                self.send_json(200, store.requirement_draft(self.cookie_token(), self.marker(), match[1]))
            elif self.path == TRIAL:
                balance = budget.balance(self.cookie_token(), self.marker())
                self.send_json(200, {**balance, "analysisMode": (
                    "fixture" if analysis_provider is assistant_fixture.provider else "deepseek")})
            elif staged and self.path == STAGED:
                self.send_json(200, {"tasks":staged.list(self.cookie_token(), self.marker())})
            elif staged and (match := STAGED_TASK.fullmatch(self.path)):
                self.send_json(200, {"task":staged.status(self.cookie_token(), self.marker(), match[1])})
            elif self.path == TRIAL_ADMIN:
                self.send_json(200, budget.admin_state(self.cookie_token(), self.marker()))
            elif self.path == TABLE_CONFIG:
                with store.connection() as db:
                    store.session(db, self.cookie_token(), self.marker())
                self.send_json(200, {"bytes": table_limits.bytes, "rows": table_limits.rows, "columns": table_limits.columns})
            elif match := TOOL_VERSIONS.fullmatch(self.path):
                history = store.versions(self.cookie_token(), self.marker(), match[1])
                initial = history["items"][-1]
                if initial["effectiveAt"] is None:
                    try:
                        initial["effectiveAt"] = int((runs / match[1] / "app.html").stat().st_mtime)
                        initial["approximate"] = True
                    except OSError:
                        pass
                self.send_json(200, history)
            elif match := TOOL_PATH.fullmatch(self.path):
                row = store.get_tool(self.cookie_token(), self.marker(), match[1])
                self.send_tool(row)
            else:
                self.error(404, "not_found", "页面不存在。")

        def send_tool(self, row, draft=None, *, applied=None):
            html = self.read_tool_html(draft or row["run_id"])
            backup = staged and not draft and staged.published_backup(row["run_id"], html)
            self.send_json(200, {
                "id": row["id"], "html": html,
                "entries": json.loads(row["entries"]), "revision": row["revision"],
                "version": row["version"] + bool(draft), **({"runId": draft} if draft else {}),
                **({"storageLimit": TABLE_STORAGE_BYTES} if row["table_tool"] else {}),
                **({"applied": applied} if applied is not None else {}),
                **({"stagedBackup": True} if backup else {}),
            })

        def read_tool_html(self, tool_id):
            try:
                return (runs / tool_id / "app.html").read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise ApiError(404, "tool_unavailable", "此工具不可用。") from None

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
                if self.path == TABLE_INSPECT:
                    cap = 6 * table_limits.bytes + 65536
                elif self.path == TABLE_GENERATE or TOOL_ACTION.fullmatch(self.path):
                    cap = 2 * TABLE_STORAGE_BYTES
                elif STAGED_ACTION.fullmatch(self.path) and self.path.endswith("/publish"):
                    cap = 2 * TOOL_STORAGE_BYTES
                else:
                    cap = 16000
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
                new_token, identity = store.register(data, token)
                self.send_json(201, identity, session_cookie(new_token))
                return
            if self.path == "/api/login":
                new_token = store.login(data, token)
                self.send_json(200, {"ok": True}, session_cookie(new_token))
                return
            if self.path == "/api/logout":
                store.logout(token, self.marker())
                self.send_json(200, {"ok": True}, session_cookie("", 0))
                return
            if match := DRAFT_PATH.fullmatch(self.path):
                value = data.get("text")
                if key and isinstance(value, str) and key in value:
                    with store.connection() as db:
                        store.session(db, token, self.marker())
                    raise ApiError(400, "invalid_draft_text", "请移除凭据后再保存。")
                self.send_json(200, store.requirement_draft(token, self.marker(), match[1], data))
                return
            if self.path == ANALYZE:
                self.handle_analysis(data, token)
                return
            if self.path == TRIAL_ADMIN:
                self.send_json(200, budget.admin_action(token, self.marker(), data))
                return
            if staged and (match := STAGED_ACTION.fullmatch(self.path)):
                marker = self.marker()
                if match[2] == "resume":
                    self.send_json(202, {"task":start_staged(token, marker, match[1], resume=True)})
                elif match[2] == "candidate":
                    self.send_json(200, staged.candidate(token, marker, match[1]))
                else:
                    self.send_tool(staged.publish(token, marker, match[1], data), applied=True)
                return
            if self.path in (TABLE_INSPECT, TABLE_GENERATE):
                with store.connection() as db:
                    store.session(db, token, self.marker())
                if self.path == TABLE_INSPECT:
                    self.send_json(200, table_import.inspect_table(data, table_limits))
                else:
                    title, html, entries = table_import.build_table_tool(data.get("table"), table_limits)
                    try:
                        run = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
                        (run / "app.html").write_text(html, encoding="utf-8")
                    except OSError:
                        raise ApiError(503, "save_failed", "本机无法保存表格工具，请重试。") from None
                    store.draft(token, self.marker(), run.name, title, entries, table=True)
                    self.send_json(200, {"id": run.name, "html": html, "entries": entries, "revision": 0, "version": 1,
                                         "storageLimit": TABLE_STORAGE_BYTES})
                return
            if match := TOOL_ACTION.fullmatch(self.path):
                if match[2] == "revise":
                    self.handle_generation(data, match[1])
                elif match[2] == "resolve-publish":
                    row, applied = store.resolve_publication(token, self.marker(), match[1], data.get("runId"))
                    self.send_tool(row, applied=applied)
                elif match[2] == "resolve-commit":
                    row = store.resolve_initial_publication(token, self.marker(), match[1])
                    if row is None:
                        self.send_json(200, {"applied": False})
                    else:
                        self.send_tool(row, applied=True)
                else:
                    self.send_json(200, store.change_tool(token, self.marker(), match[1], match[2], data,
                                                         record_model_success=budget.record_generation_success))
                return
            if self.path != "/api/generate":
                raise ApiError(404, "not_found", "页面不存在。")
            self.handle_generation(data)

        def handle_analysis(self, data, token):
            marker = self.marker()
            with store.connection() as db:
                store.session(db, token, marker)
            request_id = data.get("requestId")
            if not isinstance(request_id, str) or not re.fullmatch(UUID, request_id):
                raise ApiError(400, "invalid_analysis", "分析请求标识无效，请重试。")
            if not model_lock.acquire(blocking=False):
                raise ApiError(409, "busy", "另一个模型请求正在处理，请稍后重试。")

            def receipt(evidence):
                try:
                    with (runs / "analysis-usage.jsonl").open("a", encoding="utf-8") as output:
                        output.write(json.dumps({"request": request_id, **evidence}, ensure_ascii=False) + "\n")
                except OSError:
                    pass

            dispatch_id = uuid4().hex
            admitted = False

            def admit():
                nonlocal admitted
                budget.reserve(token, marker, "analysis", request_id=request_id, dispatch_id=dispatch_id)
                admitted = True

            try:
                try:
                    result = analysis_provider(data, key, admit, record=receipt)
                except ApiError:
                    if admitted:
                        budget.finish_dispatch("analysis", dispatch_id, success=False)
                    raise
                if admitted:
                    budget.finish_dispatch("analysis", dispatch_id, success=True)
                with store.connection() as db:
                    store.session(db, token, marker)
            finally:
                model_lock.release()
            self.send_json(200, result)

        def handle_generation(self, data, tool_id=None):
            token, marker = self.cookie_token(), self.marker()
            if tool_id and staged is None:
                row = store.get_tool(token, marker, tool_id)
                store.require_version(data.get("version"), row["version"])
            else:
                with store.connection() as db:
                    store.session(db, token, marker)
            requirement = data.get("requirement")
            requirement = requirement.strip() if isinstance(requirement, str) else ""
            if not 0 < len(requirement) <= 2000 or (key and key in requirement):
                raise ApiError(400, "invalid_requirement", "请输入 1–2000 字的工具需求，避免填写凭据。")
            if not key:
                self.error(503, "key_required", "请在本机服务配置 DeepSeek Key。")
                return
            if staged:
                task = staged.create(token, marker, requirement, data.get("requestId"),
                                     tool_id=tool_id, version=data.get("version"))
                self.send_json(202, {"task":start_staged(token, marker, task["id"], resume=False)})
                return
            model_input = None
            if tool_id:
                previous = self.read_tool_html(row["run_id"])
                keys = [name for name, _ in json.loads(row["entries"])]
                model_input = (
                    "修改现有工具，返回完整 HTML。保留既有功能和每个 localStorage 键及值格式。"
                    "启动时读取已有记录并渲染，勿在启动时重写或删除已有记录。\n"
                    f"修改要求：{requirement}\n存储键名：{json.dumps(keys, ensure_ascii=False)}\n"
                    f"现有 HTML：\n{previous}"
                )
            if not model_lock.acquire(blocking=False):
                self.error(409, "busy", "另一个生成请求正在处理，请稍后重试。")
                return
            dispatch_id = uuid4().hex
            try:
                run, evidence = generate.run_generation(
                    requirement, key, runs, model_input=model_input,
                    admit=lambda: budget.reserve(token, marker, "generation", dispatch_id=dispatch_id),
                    record_outcome=lambda success: budget.finish_dispatch("generation", dispatch_id, success=success),
                )
            except OSError:
                self.error(500, "save_failed", "本机无法保存生成结果，请检查运行目录。")
                return
            finally:
                model_lock.release()
            if evidence["status"] == "generated":
                if tool_id:
                    latest = store.revision_draft(token, marker, tool_id, row["version"], run.name, requirement, model=True)
                    self.send_tool(latest, run.name)
                    return
                html = self.read_tool_html(run.name)
                title = ToolTitle()
                try:
                    title.feed(html)
                    label = title.label(requirement)
                except AssertionError:
                    # HTMLParser may reject malformed declarations that the browser tolerates.
                    label = requirement
                store.draft(token, self.marker(), run.name, label, model=True)
                self.send_json(200, {"id": run.name, "html": html, "entries": [], "revision": 0, "version": 1})
            else:
                self.send_json(502, {"code": evidence["status"], "message": model_error(evidence), "issue": run.name})

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
    try:
        generation_limits = (int(os.environ.get("ROOT_ACCOUNT_GENERATIONS", "5")),
                             int(os.environ.get("ROOT_TOTAL_GENERATIONS", "30")))
        analysis_limits = (int(os.environ.get("ROOT_ACCOUNT_DAILY_ANALYSES", "6")),
                           int(os.environ.get("ROOT_TOTAL_ANALYSES", "60")))
        if any(not 0 < account <= total for account, total in (generation_limits, analysis_limits)):
            raise ValueError("个人额度与全站额度须为正整数，且个人额度不得超过全站额度")
        table_limits = table_import.limits_from_env()
        admins = tuple(name.strip().lower() for name in os.environ.get("ROOT_TRIAL_ADMINS", "").split(",") if name.strip())
        if any(not re.fullmatch(r"[a-z0-9_-]{3,32}", name) for name in admins):
            raise ValueError("管理员用户名格式无效")
    except ValueError as error:
        parser.error(str(error))
    serve(key, args.port, args.runs_dir, generation_limits=generation_limits, analysis_limits=analysis_limits,
          admin_usernames=admins, table_limits=table_limits,
          staged_enabled=os.environ.get("ROOT_STAGED_ENABLED", "1") != "0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
