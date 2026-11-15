"""Serve ROOT's local workbench using only Python's standard library."""

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from html import escape
from html.parser import HTMLParser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import generate
import generation_settings
import agent_production
import assistant_fixture
import assistant_model
import table_import
import staged_blueprint
import staged_revision
import staged_record_code
import runtime_verify
from staged_jobs import MAX_ATTEMPTS, StagedJobs
from accounts import ApiError, DEFAULT_INVITE_HOURS, InviteError, SESSION_SECONDS, Store, TABLE_STORAGE_BYTES, TOOL_STORAGE_BYTES
from trial_budget import TrialBudget

ROOT = Path(__file__).resolve().parent
DEFAULT_GENERATION_LIMITS = (1000, 10000)
DEFAULT_AGENT_MODEL_CALLS = 100
DEFAULT_MODEL_CONCURRENCY = 2
ADMIN = "/admin"
WORKBENCH = "/workbench"
LOCAL_SITE = "http://[::1]:8788/"
SHELL_ROUTES = ("/", WORKBENCH, "/updates", ADMIN)
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    WORKBENCH: ("index.html", "text/html; charset=utf-8"),
    "/updates": ("index.html", "text/html; charset=utf-8"),
    ADMIN: ("admin.html", "text/html; charset=utf-8"),
    "/admin.css": ("admin.css", "text/css; charset=utf-8"),
    "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
    "/admin-login.js": ("admin-login.js", "text/javascript; charset=utf-8"),
    "/admin-setup.js": ("admin-setup.js", "text/javascript; charset=utf-8"),
    "/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
    "/assistant.js": ("assistant.js", "text/javascript; charset=utf-8"),
    "/assistant.css": ("assistant.css", "text/css; charset=utf-8"),
    "/table.js": ("table.js", "text/javascript; charset=utf-8"),
    "/table.css": ("table.css", "text/css; charset=utf-8"),
    "/workbench.js": ("workbench.js", "text/javascript; charset=utf-8"),
}
TOOL_PATH = re.compile(r"/api/tools/(run-[a-z0-9_]{8})")
TOOL_ACTION = re.compile(r"/api/tools/(run-[a-z0-9_]{8})/(commit|open|data|records|publish|revise|restore|resolve-publish|resolve-commit|source)")
DISCOVERY = "/api/discovery"
DISCOVERY_ITEM = re.compile(r"/api/discovery/([0-9a-f]{32})(/thumbnail)?")
TOOL_SHARE = re.compile(r"/api/tools/(run-[a-z0-9_]{8})/share")
TOOL_VERSIONS = re.compile(r"/api/tools/(run-[a-z0-9_]{8})/versions")
TOOL_REVISION_TASKS = re.compile(r"/api/tools/(run-[a-z0-9_]{8})/revision-tasks")
DRAFTS = "/api/requirement-drafts"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
DRAFT_PATH = re.compile(DRAFTS + "/(" + UUID + ")")
ANALYZE = "/api/assistant/analyze"
CLAIM_ANALYSIS_DRAFT = "/api/assistant/claim-draft"
PROFILE = "/api/profile"
TRIAL = "/api/trial"
TRIAL_ADMIN = "/api/trial/admin"
STAGED = "/api/staged"
STAGED_HISTORY = STAGED + "/history"
STAGED_TASK = re.compile(STAGED + r"/([a-f0-9]{32})")
STAGED_ACTION = re.compile(STAGED + r"/([a-f0-9]{32})/(resume|answer-revision|repair-revision|candidate|confirm-revision|cancel-revision|verify-preview|publish|resolve-publish|source)")
ADMIN_INVITES = "/api/admin/invites"
ADMIN_SETUP = "/api/admin/setup"
ADMIN_PASSWORD = "/api/admin/password"
ADMIN_LOGIN = "/api/admin/login"
ADMIN_SESSION = "/api/admin/session"
ADMIN_LOGOUT = "/api/admin/logout"
ADMIN_GENERATION_SETTINGS = "/api/admin/generation-settings"
ADMIN_ACTIONS = (ADMIN_GENERATION_SETTINGS, TRIAL_ADMIN, ADMIN_INVITES, ADMIN_SETUP, ADMIN_PASSWORD, ADMIN_LOGIN, ADMIN_LOGOUT)
TABLE_INSPECT = "/api/table/inspect"
TABLE_GENERATE = "/api/table/generate"
TABLE_CONFIG = "/api/table/config"
RELEASES = (
    ("0.9.5", "2026-11-14", None, "修复新建台账和看板的预览校验，以及新建失败或重试时显示旧工具的问题。"),
    ("0.9.4", "2026-11-14", "v4-84259f118cb3", "新增发现与作品分享，支持源码浏览和导出，可在全部工具中按名称搜索。"),
    ("0.9.3", "2026-11-14", "v4-3cc91591d320", "官网增加品牌官网示例。工作台补充生成等待动效，将最后一步标为准备预览，并拦截显示 HTML 标签的异常官网。"),
    ("0.9.2", "2026-11-14", "v4-d417a4dd9e13", "新建工具使用统一生成状态卡。可查看失败详情、重试或编辑需求，旧工具仍可使用。"),
    ("0.9.1", "2026-11-14", "v4-6b961219ca38", "后台新增生成配置。扩大源码与续改上下文容量，更新工作台导航与修改对话，修正草稿和独立工具入口。"),
    ("0.9", "2026-11-14", "v4-ecf34a605b87", "更新工作台和后台。生成任务支持短标题，草稿工具名称在保存后保留，断连后可恢复原任务。"),
    ("0.8", "2026-11-14", "v4-396a683db32f", "启用编程 Agent 生成与继续修改工具，支持预览后确认适用的记录变更。"),
    ("0.7", "2026-11-13", "v4-49174f46be99", "修复继续修改时的补丁校验和字段类型澄清，新增可空字段后保留原记录。"),
    ("0.6", "2026-11-13", "v4-d83afcadbc2e", "更新登录注册和新建入口。生成成功后可留在原页试用，清单支持确认后新增可空日期字段。"),
    ("0.5", "2026-11-13", "v4-56dee154de48", "生成看板时保留自动填写的状态，支持新增后移列及刷新恢复。"),
    ("0.4", "2026-11-13", "v4-9bfb2f867522", "新建台账和看板时由 AI 生成页面、样式和交互，支持录入、移列及确认后改色。"),
    ("0.3", "2026-11-09", "v4-ee53b3f92d2c", "支持继续修改已有工具、查看工具版本历史，并调整窄屏生成流程。"),
    ("0.2", "2026-11-09", "v4-44e3da0cae1b", "加入邀请账号、个人工具列表和跨浏览器数据恢复。"),
    ("0.1", "2026-11-09", "v4-f58378aa55af", "提供自然语言生成、隔离预览、保存和刷新恢复。"),
)
SHELL_POLICY = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; "
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


def build_id(assets, coding_agent_enabled=False):
    digest = hashlib.sha256()
    for name in ("generate.py", "workbench.py", "accounts.py", "assistant_fixture.py", "assistant_model.py",
                 "table_import.py", "trial_budget.py", "generation_settings.py", "staged_blueprint.py", "staged_jobs.py", "staged_program.py", "staged_revision.py",
                 "staged_website.py", "staged_record_code.py", "runtime_verify.py", "runtime-verify.mjs",
                 "workbench/staged-runtime.js"):
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes() + b"\0")
    if coding_agent_enabled:
        for name in ("coding_agent.py", "coding-agent.mjs", "agent_production.py"):
            digest.update(name.encode() + (ROOT / name).read_bytes())
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


def session_cookie(token, max_age=SESSION_SECONDS, *, admin=False):
    name = "root_admin" if admin else "root_session"
    secure = "; Secure" if admin else ""
    return f"{name}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}"


def serve(key, port, runs=generate.RUNS, *, generation_limits=(5, 30), analysis_limits=(6, 60),
          admin_usernames=(), analysis_provider=assistant_model.analyze, table_limits=None,
          staged_enabled=False, public_site_url=LOCAL_SITE, staged_provider=staged_blueprint.provide,
          staged_revision_provider=staged_revision.provide, staged_runtime_verifier=runtime_verify.verify,
          coding_agent_enabled=False, coding_agent_timeout=None, coding_agent_max_attempts=DEFAULT_AGENT_MODEL_CALLS,
          generation_defaults=None,
          model_concurrency=DEFAULT_MODEL_CONCURRENCY):
    model_slots = threading.BoundedSemaphore(model_concurrency)
    staged_workers = ThreadPoolExecutor(max_workers=model_concurrency, thread_name_prefix="root-staged")
    scheduled_tasks = set()
    schedule_lock = threading.Lock()
    store = Store(runs / "workspace.sqlite3")
    budget = TrialBudget(store, runs, generation_limits=generation_limits, analysis_limits=analysis_limits,
                         admin_usernames=admin_usernames, admin_session_required=True)
    generation_defaults = generation_defaults or generation_settings.GenerationSettings()
    if coding_agent_timeout is not None:
        generation_defaults = generation_settings.validate({**vars(generation_defaults),
                                                            "timeoutSeconds": coding_agent_timeout})
    settings_store = generation_settings.GenerationSettingsStore(store, generation_defaults)
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

    coding_provider = None
    if coding_agent_enabled:
        from coding_agent import provide

        def coding_provider(stage, requirement, prior, dispatch, *, base):
            return provide(stage, requirement, prior, key, dispatch, base=base,
                           timeout_seconds=generation_settings.current().timeoutSeconds)

    history = StagedJobs(store, budget, runs, staged_blueprint, record=stage_receipt, revision=staged_revision,
                         runtime_verifier=staged_runtime_verifier, coding_provider=coding_provider,
                         max_attempts=coding_agent_max_attempts if coding_agent_enabled else MAX_ATTEMPTS)
    staged = history if staged_enabled else None

    def stage_provider(stage, requirement, prior, admit, record, base=None, category="auto"):
        if base is not None:
            return staged_revision_provider(stage, requirement, prior, key, admit, record, base=base)
        if staged_provider is staged_blueprint.provide:
            return staged_provider(stage, requirement, prior, key, admit, record, category=category,
                                   record_code=True)
        if category != "auto":
            return staged_provider(stage, requirement, prior, key, admit, record, category=category)
        return staged_provider(stage, requirement, prior, key, admit, record)

    def run_staged(token, marker, task, resume):
        # Claim after a slot opens so time spent waiting does not consume the lease.
        with model_slots:
            try:
                with store.connection() as db:
                    settings = settings_store.read(db)
            except (sqlite3.Error, ValueError) as error:
                with schedule_lock:
                    scheduled_tasks.discard(task)
                logging.getLogger(__name__).warning("generation settings load failed: %s", type(error).__name__)
                return
            with generation_settings.activate(settings):
                execute_staged(token, marker, task, resume)

    def execute_staged(token, marker, task, resume):
        with schedule_lock:
            try:
                fence = staged.claim(token, marker, task, resume=resume)
            except (ApiError, OSError, ValueError, sqlite3.Error) as error:
                logging.getLogger(__name__).warning("staged claim failed task=%s reason=%s",
                                                    task, getattr(error, "code", type(error).__name__))
                return
            finally:
                scheduled_tasks.discard(task)
        if fence is None:
            return
        base = staged.revision_context(task)
        category = staged.generation_category(task)

        def provider(stage, requirement, prior, admit, record):
            return stage_provider(stage, requirement, prior, admit, record, base, category)

        def retry_revision(stage, requirement, prior, admit, record, feedback):
            return stage_provider(stage, requirement, prior, admit, record,
                                  {**base, "patch_feedback": feedback}, category)

        staged.run(task, fence, provider, retry_revision if base else None)

    def start_staged(token, marker, task, *, resume):
        with schedule_lock:
            if task in scheduled_tasks:
                return staged.status(token, marker, task)
            status = staged.resume_status(token, marker, task) if resume else staged.status(token, marker, task)
            eligible = status["resumable"] if resume else status["state"] == "queued"
            if eligible:
                scheduled_tasks.add(task)
                staged_workers.submit(run_staged, token, marker, task, resume)
            return status

    assets = {url: (ROOT / "workbench" / name).read_bytes()
              for url, (name, _) in STATIC.items()}
    setup_page = (ROOT / "workbench" / "admin-setup.html").read_bytes()
    login_page = (ROOT / "workbench" / "admin-login.html").read_bytes()
    build = build_id({**assets, "admin-setup.html": setup_page, "admin-login.html": login_page}, coding_agent_enabled)

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
                         "/api/generate", "/api/tools", "/api/tools/*", DRAFTS, DRAFTS + "/*", ANALYZE, CLAIM_ANALYSIS_DRAFT,
                         TABLE_INSPECT, TABLE_GENERATE, TABLE_CONFIG, PROFILE, TRIAL, ADMIN_SESSION,
                         DISCOVERY, DISCOVERY + "/*", *ADMIN_ACTIONS)
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

        def send_source(self, source, data):
            if data.get("format") != "zip":
                self.send_json(200, source)
                return
            if not source["files"]:
                raise ApiError(404, "tool_source_unavailable", "此版本没有可导出的代码。")
            buffer = io.BytesIO()
            with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
                for file in source["files"]:
                    path = file["path"]
                    if PurePosixPath(path).is_absolute() or "\\" in path or not path or ".." in path.split("/"):
                        raise ApiError(503, "tool_source_unavailable", "此版本的文件路径无法导出。")
                    archive.writestr(path, file["text"].encode())
            self.send_bytes(200, buffer.getvalue(), "application/zip")

        def error(self, status, code, message):
            self.send_json(status, {"code": code, "message": message})

        def valid_host(self):
            if self.headers.get("Host") == host:
                return True
            self.error(403, "invalid_host", "请使用启动时显示的本机地址。")
            return False

        def cookie_token(self, *, admin=False):
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                value = cookies["root_admin" if admin else "root_session"].value
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

        def send_shell(self, page, mime):
            # The same backend serves the legacy IP and the shared-domain proxy.
            if self.path == WORKBENCH or self.headers.get("X-Root-Workbench-Path") == WORKBENCH:
                page = page.replace(b'href="/"', b'href="/workbench"')
            site_url = "/" if self.headers.get("X-Root-Workbench-Path") == WORKBENCH else public_site_url
            page = page.replace(f'href="{LOCAL_SITE}"'.encode(),
                                f'href="{escape(site_url, quote=True)}"'.encode())
            self.send_bytes(200, page, mime, SHELL_POLICY)

        def handle_get(self):
            if self.path in assets:
                if self.path == ADMIN:
                    with store.connection() as db:
                        if not admin_usernames and store.setup_available(store.setup_state(db)):
                            self.send_shell(setup_page, STATIC[ADMIN][1])
                            return
                        try:
                            budget.require_admin(db, self.cookie_token(admin=True), None)
                        except ApiError as error:
                            if error.code not in ("login_required", "admin_required"):
                                raise
                            self.send_shell(login_page, STATIC[ADMIN][1])
                            return
                _, mime = STATIC[self.path]
                if self.path in SHELL_ROUTES:
                    self.send_shell(assets[self.path], mime)
                else:
                    self.send_bytes(200, assets[self.path], mime)
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
            elif self.path == PROFILE:
                self.send_json(200, store.profile(self.cookie_token(), self.marker()))
            elif self.path == TRIAL:
                balance = budget.balance(self.cookie_token(), self.marker())
                self.send_json(200, {**balance, "analysisMode": (
                    "fixture" if analysis_provider is assistant_fixture.provider else "deepseek")})
            elif staged and self.path == STAGED:
                self.send_json(200, {"tasks":staged.list(self.cookie_token(), self.marker())})
            elif staged and self.path == STAGED_HISTORY:
                self.send_json(200, {"tasks":staged.list(self.cookie_token(), self.marker(), history=True)})
            elif staged and (match := STAGED_TASK.fullmatch(self.path)):
                self.send_json(200, {"task":staged.status(self.cookie_token(), self.marker(), match[1])})
            elif self.path == ADMIN_SESSION:
                with store.connection() as db:
                    token = self.cookie_token(admin=True)
                    budget.require_admin(db, token, None)
                    session = store.session(db, token, admin=True)
                self.send_json(200, {"user": session["username"], "marker": session["marker"]})
            elif self.path == ADMIN_GENERATION_SETTINGS:
                with store.connection() as db:
                    budget.require_admin(db, self.cookie_token(admin=True), self.marker())
                    settings = settings_store.response(db)
                self.send_json(200, settings)
            elif self.path == TRIAL_ADMIN:
                self.send_json(200, budget.admin_state(self.cookie_token(admin=True), self.marker()))
            elif self.path == TABLE_CONFIG:
                with store.connection() as db:
                    store.session(db, self.cookie_token(), self.marker())
                self.send_json(200, {"bytes": table_limits.bytes, "rows": table_limits.rows, "columns": table_limits.columns})
            elif self.path == DISCOVERY:
                self.send_json(200, store.discovery(self.cookie_token(), self.marker()))
            elif match := DISCOVERY_ITEM.fullmatch(self.path):
                thumbnail = match[2] is not None
                result = store.shared_tool(self.cookie_token(), None if thumbnail else self.marker(), match[1], self.read_tool_html,
                                           thumbnail=thumbnail)
                if thumbnail:
                    self.send_bytes(200, result, "image/jpeg")
                else:
                    self.send_json(200, result)
            elif match := TOOL_SHARE.fullmatch(self.path):
                self.send_json(200, store.tool_share(self.cookie_token(), self.marker(), match[1]))
            elif match := TOOL_VERSIONS.fullmatch(self.path):
                versions = store.versions(self.cookie_token(), self.marker(), match[1],
                                          ensure_history=history.ensure_tool_history)
                initial = versions["items"][-1]
                if initial["effectiveAt"] is None:
                    try:
                        initial["effectiveAt"] = int((runs / match[1] / "app.html").stat().st_mtime)
                        initial["approximate"] = True
                    except OSError:
                        pass
                self.send_json(200, versions)
            elif staged and (match := TOOL_REVISION_TASKS.fullmatch(self.path)):
                tasks = staged.list_tool_revisions(self.cookie_token(), self.marker(), match[1])
                self.send_json(200, {"tasks": tasks})
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

        def prepare_share_demo(self, db, tool):
            html = self.read_tool_html(tool["run_id"])
            entries = []
            try:
                if tool["table_tool"]:
                    table = table_import.read_saved_table(tool["entries"])
                    samples = {"number": "1", "date": "2026-01-15", "boolean": "是"}
                    demo = {
                        "filename": "演示表格", "columns": table["columns"],
                        "rows": [[samples.get(col["type"], "示例") for col in table["columns"]]],
                        "done": [False],
                    }
                    entries = [[table_import.TABLE_KEY, json.dumps(demo, ensure_ascii=False)]]
                else:
                    source = history.published_source(db, tool["run_id"])
                    if source is not None:
                        spec = history.blueprint.read_spec(history.read(source, source["head"], "spec.json"))
                        if spec.get("fields"):
                            entries = staged_record_code.verification_entries(spec["fields"])
            except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
                raise ApiError(409, "share_demo_unavailable", "无法准备演示数据，请重试。") from error
            if staged_record_code.MARKER in html[:100] and not entries:
                raise ApiError(409, "share_demo_unavailable", "无法准备演示数据，请重试。")
            return html, entries

        @staticmethod
        def verify_share_demo(html, entries, *, table):
            try:
                return runtime_verify.verify(
                    html.encode(), entries, thumbnail=True,
                    storage_limit=TABLE_STORAGE_BYTES if table else TOOL_STORAGE_BYTES,
                )
            except runtime_verify.RuntimeVerificationError as error:
                raise ApiError(409, "share_demo_unavailable", "此工具无法启动试玩，请检查预览后再分享。") from error

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
                elif (self.path == TABLE_GENERATE or TOOL_ACTION.fullmatch(self.path)
                      or (STAGED_ACTION.fullmatch(self.path) and self.path.endswith(("/publish", "/verify-preview")))):
                    cap = 2 * TABLE_STORAGE_BYTES
                else:
                    cap = 16000
                if not 0 < size <= cap:
                    raise ValueError
                self.connection.settimeout(10)
                data = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError
                json.dumps(data, ensure_ascii=False).encode("utf-8")
            except (ValueError, TypeError, OSError, RecursionError):
                self.error(400, "invalid_body", "请求内容无效。")
                return
            try:
                self.handle_action(data)
            except (ApiError, sqlite3.Error) as error:
                self.handle_error(error)

        def handle_action(self, data):
            token = self.cookie_token(admin=self.path in ADMIN_ACTIONS)
            if self.path == ADMIN_LOGIN:
                new_token = store.login(data, token, authorize=budget.is_admin)
                with store.connection() as db:
                    session = store.session(db, new_token, admin=True)
                self.send_json(200, {"user": session["username"], "marker": session["marker"]},
                               session_cookie(new_token, admin=True))
                return
            if self.path == ADMIN_LOGOUT:
                store.logout(token, self.marker(), admin=True)
                self.send_json(200, {"ok": True}, session_cookie("", 0, admin=True))
                return
            if self.path == "/api/register":
                new_token, identity = store.register(data, token)
                self.send_json(201, identity, session_cookie(new_token))
                return
            if self.path == "/api/login":
                new_token = store.login(data, token)
                self.send_json(200, {"ok": True}, session_cookie(new_token))
                return
            if self.path == PROFILE:
                self.send_json(200, store.profile(token, self.marker(), data))
                return
            if self.path == "/api/logout":
                store.logout(token, self.marker())
                self.send_json(200, {"ok": True}, session_cookie("", 0))
                return
            if match := TOOL_SHARE.fullmatch(self.path):
                self.send_json(200, store.tool_share(token, self.marker(), match[1], data, self.prepare_share_demo, self.verify_share_demo))
                return
            if match := DRAFT_PATH.fullmatch(self.path):
                value = data.get("text")
                if key and any(isinstance(item, str) and key in item for item in (value, data.get("title"))):
                    with store.connection() as db:
                        store.session(db, token, self.marker())
                    raise ApiError(400, "invalid_draft_text", "请移除凭据后再保存。")
                self.send_json(200, store.requirement_draft(token, self.marker(), match[1], data))
                return
            if self.path == ANALYZE:
                self.handle_analysis(data, token)
                return
            if self.path == CLAIM_ANALYSIS_DRAFT:
                offer_id = data.get("offerId")
                if not isinstance(offer_id, str) or not re.fullmatch(UUID, offer_id):
                    raise ApiError(400, "invalid_analysis", "草稿回执编号无效。")
                self.send_json(200, budget.claim_analysis_draft(token, self.marker(), offer_id))
                return
            if self.path == ADMIN_GENERATION_SETTINGS:
                with store.connection(write=True) as db:
                    admin = budget.require_admin(db, token, self.marker())
                    settings = settings_store.update(db, data, admin)
                self.send_json(200, settings)
                return
            if self.path == TRIAL_ADMIN:
                self.send_json(200, budget.admin_action(token, self.marker(), data))
                return
            if self.path == ADMIN_INVITES:
                self.send_json(201, budget.issue_invite(token, self.marker(), data.get("requestId")))
                return
            if self.path == ADMIN_SETUP:
                if admin_usernames:
                    raise ApiError(409, "setup_closed", "管理员已配置，请登录。")
                new_token, identity = store.setup_admin(data, token)
                self.send_json(201, identity, session_cookie(new_token, admin=True))
                return
            if self.path == ADMIN_PASSWORD:
                store.change_admin_password(data, token, self.marker(), budget.require_admin)
                self.send_json(200, {"ok": True}, session_cookie("", 0, admin=True))
                return
            if staged and (match := STAGED_ACTION.fullmatch(self.path)):
                marker = self.marker()
                if match[2] == "resume":
                    if staged.status(token, marker, match[1])["state"] == "cancelled":
                        raise ApiError(409, "staged_not_ready", "此修改方案已取消。")
                    self.send_json(202, {"task":start_staged(token, marker, match[1], resume=True)})
                elif match[2] == "answer-revision":
                    if key and isinstance(data.get("answer"), str) and key in data["answer"]:
                        raise ApiError(400, "revision_answer_invalid", "请移除凭据后再补答。")
                    staged.answer_revision(token, marker, match[1], data)
                    self.send_json(202, {"task":start_staged(token, marker, match[1], resume=False)})
                elif match[2] == "repair-revision":
                    if key and isinstance(data.get("supplement"), str) and key in data["supplement"]:
                        raise ApiError(400, "staged_input", "请移除凭据后再补充。")
                    staged.repair_revision(token, marker, match[1], data)
                    self.send_json(202, {"task":start_staged(token, marker, match[1], resume=False)})
                elif match[2] == "candidate":
                    self.send_json(200, staged.candidate(token, marker, match[1]))
                elif match[2] == "source":
                    self.send_source(staged.candidate_source(token, marker, match[1], data), data)
                elif match[2] == "confirm-revision":
                    staged.confirm_revision(token, marker, match[1], data)
                    self.send_json(200, {"confirmed":True})
                elif match[2] == "cancel-revision":
                    self.send_json(200, {"task":staged.cancel_revision(token, marker, match[1], data.get("nonce"))})
                elif match[2] == "verify-preview":
                    staged.verify_preview(token, marker, match[1], data)
                    self.send_json(200, {"verified":True})
                elif match[2] == "resolve-publish":
                    row = staged.resolve_publication(token, marker, match[1], data.get("nonce"))
                    if row is None:
                        self.send_json(200, {"applied": False})
                    else:
                        self.send_tool(row, applied=True)
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
                if match[2] == "source":
                    self.send_source(history.current_source(token, self.marker(), match[1], data.get("version")), data)
                    return
                if match[2] == "revise":
                    self.handle_generation(data, match[1])
                elif match[2] == "records":
                    if staged is None:
                        raise ApiError(404, "not_found", "页面不存在。")
                    self.send_json(200, staged.change_record(token, self.marker(), match[1], data))
                elif match[2] == "restore":
                    row, run_id = store.prepare_restore(token, self.marker(), match[1], data, self.prepare_table_restore)
                    self.send_tool(row, draft=run_id)
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
                                                         record_model_success=budget.record_generation_success,
                                                         ensure_history=history.ensure_tool_history,
                                                         allow_snapshot=lambda db, row: history.record_code_spec(db, row) is None))
                return
            if self.path != "/api/generate":
                raise ApiError(404, "not_found", "页面不存在。")
            self.handle_generation(data)

        def prepare_table_restore(self, row, historical_run):
            try:
                table = table_import.read_saved_table(row["entries"])
                current = table_import.tool_configuration(self.read_tool_html(row["run_id"]))
                historical_html = self.read_tool_html(historical_run)
                target = table_import.tool_configuration(historical_html)
                if any(index >= len(table["columns"]) for config in (current, target) for index in config["filters"]):
                    raise ValueError("invalid filter column")
            except (ApiError, TypeError, ValueError, RecursionError):
                raise ApiError(409, "incompatible_restore", "无法验证此旧版与当前记录兼容，当前版本仍可用。") from None
            try:
                run = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
                (run / "app.html").write_text(historical_html, encoding="utf-8")
            except OSError:
                raise ApiError(503, "save_failed", "本机无法准备旧版本，请重试。") from None
            return run.name, target["title"]

        def handle_analysis(self, data, token):
            marker = self.marker()
            with store.connection() as db:
                store.session(db, token, marker)
            request_id = data.get("requestId")
            round_id = data.get("roundId")
            if any(not isinstance(value, str) or not re.fullmatch(UUID, value) for value in (request_id, round_id)):
                raise ApiError(400, "invalid_analysis", "分析请求或梳理轮标识无效，请重试。")
            if not model_slots.acquire(blocking=False):
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
                budget.reserve(token, marker, "analysis", request_id=request_id, dispatch_id=dispatch_id,
                               round_id=round_id, idea=data["idea"])
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
                if result["kind"] == "draft":
                    budget.offer_analysis_draft(token, marker, round_id, request_id, result["text"], result.get("title"))
                    result = {"kind": "draft_ready", "offerId": request_id}
                else:
                    with store.connection() as db:
                        store.session(db, token, marker)
            finally:
                model_slots.release()
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
            if not 0 < len(requirement) <= 2000 or (key and (key in requirement or (isinstance(data.get("title"), str) and key in data["title"]))):
                raise ApiError(400, "invalid_requirement", "请输入 1–2000 字的工具需求，避免填写凭据。")
            if not key:
                self.error(503, "key_required", "请在本机服务配置 DeepSeek Key。")
                return
            if staged:
                task = staged.create(token, marker, requirement, data.get("requestId"),
                                     tool_id=tool_id, version=data.get("version"), category=data.get("category", "auto"), title=data.get("title"))
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
            if not model_slots.acquire(blocking=False):
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
                model_slots.release()
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
        finally:
            staged_workers.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--prompt-key", action="store_true", help="在本机终端隐藏输入 Key")
    parser.add_argument("--runs-dir", type=Path, default=generate.RUNS)
    agent_mode = parser.add_mutually_exclusive_group()
    agent_mode.add_argument("--coding-agent", action="store_true", help="使用本机 Pi Agent 试用实现")
    agent_mode.add_argument("--coding-agent-production", action="store_true", help="启用已准备状态副本的生产 Agent")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-coding-agent-production-from", type=Path, metavar="SOURCE",
                      help="将停写的完整来源状态目录复制到新的 --runs-dir")
    mode.add_argument("--create-invite", action="store_true")
    mode.add_argument("--create-admin-setup-code", action="store_true")
    parser.add_argument("--invite-hours", type=int, default=DEFAULT_INVITE_HOURS)
    parser.add_argument("--invite-uses", type=int, default=1)
    args = parser.parse_args()
    if args.prepare_coding_agent_production_from:
        if args.coding_agent or args.coding_agent_production:
            parser.error("准备状态副本和启动 Agent 必须分别执行。")
        if args.runs_dir.resolve() == generate.RUNS.resolve():
            parser.error("请用 --runs-dir 指定新的生产状态副本目录。")
        try:
            agent_production.prepare(args.prepare_coding_agent_production_from, args.runs_dir)
        except (ValueError, OSError, sqlite3.Error) as error:
            parser.error(str(error))
        print("生产 Agent 状态副本已准备。")
        return 0
    if args.coding_agent_production:
        try:
            agent_production.require_prepared(args.runs_dir)
            import coding_agent
            coding_agent.dependencies()
        except (ValueError, OSError, ApiError) as error:
            parser.error(str(error))
    if args.coding_agent:
        if args.runs_dir.resolve() == generate.RUNS.resolve():
            parser.error("Agent 试用请用 --runs-dir 指定独立数据目录。")
        trial_db = args.runs_dir / "workspace.sqlite3"
        marker = args.runs_dir / agent_production.TRIAL_MARKER
        if (args.runs_dir / agent_production.MARKER).exists():
            parser.error("生产状态副本必须使用 --coding-agent-production。")
        if trial_db.exists() and not marker.is_file():
            parser.error("已有数据库未标记为 Agent 试用库，请另选空目录。")
        args.runs_dir.mkdir(parents=True, exist_ok=True)
        marker.touch()
    if args.create_admin_setup_code:
        if any(name.strip() for name in os.environ.get("ROOT_TRIAL_ADMINS", "").split(",")):
            parser.error("管理员已由部署配置指定。")
        try:
            print(Store(args.runs_dir / "workspace.sqlite3").create_admin_setup_code())
        except ApiError as error:
            parser.error(str(error))
        return 0
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
    coding_agent_enabled = args.coding_agent or args.coding_agent_production
    try:
        account_default, total_default = DEFAULT_GENERATION_LIMITS
        generation_limits = (int(os.environ.get("ROOT_ACCOUNT_GENERATIONS", account_default)),
                             int(os.environ.get("ROOT_TOTAL_GENERATIONS", total_default)))
        model_concurrency = int(os.environ.get("ROOT_MODEL_CONCURRENCY", DEFAULT_MODEL_CONCURRENCY))
        if model_concurrency < 1:
            raise ValueError("ROOT_MODEL_CONCURRENCY 必须是正整数")
        generation_defaults = generation_settings.from_environment()
        agent_settings = {}
        if coding_agent_enabled:
            agent_settings = {
                "coding_agent_timeout": generation_defaults.timeoutSeconds,
                "coding_agent_max_attempts": int(os.environ.get("ROOT_AGENT_MAX_MODEL_CALLS", DEFAULT_AGENT_MODEL_CALLS)),
            }
            if min(agent_settings.values()) <= 0:
                raise ValueError("ROOT_AGENT_TIMEOUT_SECONDS 和 ROOT_AGENT_MAX_MODEL_CALLS 必须是正整数")
        analysis_limits = (int(os.environ.get("ROOT_ACCOUNT_DAILY_ANALYSES", "6")),
                           int(os.environ.get("ROOT_TOTAL_ANALYSES", "60")))
        if not 0 < generation_limits[0] <= generation_limits[1] or min(analysis_limits) <= 0:
            raise ValueError("额度须为正整数，个人生成额度不得超过全站生成调用额度")
        table_limits = table_import.limits_from_env()
        public_site_url = os.environ.get("ROOT_PUBLIC_SITE_URL", LOCAL_SITE)
        site = urlsplit(public_site_url)
        if (site.scheme not in ("http", "https") or not site.hostname or site.username is not None
                or site.password is not None or site.port == 0
                or any(char.isspace() or ord(char) < 32 or char == "\\" for char in public_site_url)):
            raise ValueError("ROOT_PUBLIC_SITE_URL 必须是无凭据的 HTTP 或 HTTPS 官网地址。")
        admins = tuple(name.strip().lower() for name in os.environ.get("ROOT_TRIAL_ADMINS", "").split(",") if name.strip())
        if any(not re.fullmatch(r"[a-z0-9_-]{3,32}", name) for name in admins):
            raise ValueError("管理员用户名格式无效")
    except ValueError as error:
        parser.error(str(error))
    serve(key, args.port, args.runs_dir, generation_limits=generation_limits, analysis_limits=analysis_limits,
          admin_usernames=admins, table_limits=table_limits, public_site_url=public_site_url,
          staged_enabled=os.environ.get("ROOT_STAGED_ENABLED", "1") != "0",
          coding_agent_enabled=coding_agent_enabled, model_concurrency=model_concurrency,
          generation_defaults=generation_defaults, **agent_settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
