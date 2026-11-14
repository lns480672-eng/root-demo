"""Local accounts and account-scoped workspace state."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager

SESSION_SECONDS = 12 * 60 * 60
DEFAULT_INVITE_HOURS = 72
ADMIN_SETUP_SECONDS = 60 * 60
LOGIN_ATTEMPT_WINDOW = 60
LOGIN_ATTEMPT_LIMIT = 12
LOGIN_ATTEMPT_BUCKETS = 1024
# Match the browser String.trim() boundary, including the Unicode BOM.
NICKNAME_TRIM = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
USERNAME = re.compile(r"[a-z0-9_-]{3,32}")
TOOL_STORAGE_BYTES = 256 * 1024
TABLE_STORAGE_BYTES = 8 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


class InviteError(ApiError):
    def __init__(self, reason):
        messages = {
            "missing": "请输入邀请码。",
            "invalid": "邀请码无效，请核对后再试。",
            "expired": "邀请码已过期，请向邀请人索取新码。",
            "used": "邀请码已使用，请向邀请人索取新码。",
        }
        super().__init__(400, "invalid_invite", messages[reason])
        self.reason = reason


def digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def credentials(data, registering=False):
    name = data.get("username")
    password = data.get("password")
    name = name.strip().lower() if isinstance(name, str) else ""
    if not USERNAME.fullmatch(name):
        raise ApiError(400, "invalid_username", "请输入 3–32 位英文字母、数字、连字符或下划线。")
    minimum = 12 if registering else 1
    if not isinstance(password, str) or not minimum <= len(password) <= 128:
        message = "请输入 12–128 个字符。" if registering else "请输入你的密码。"
        raise ApiError(400, "invalid_password", message)
    return name, password


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)


def admin_password(value):
    if not isinstance(value, str) or not 15 <= len(value) <= 128:
        raise ApiError(400, "invalid_password", "管理员密码须为 15–128 个字符。")
    return value


class Store:
    def __init__(self, path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        self.path = path
        self.login_attempts = {}
        self.login_attempt_lock = threading.Lock()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    salt BLOB NOT NULL, password_hash BLOB NOT NULL, latest_tool TEXT
                );
                CREATE TABLE IF NOT EXISTS account_profiles (
                    account INTEGER PRIMARY KEY REFERENCES accounts(id), nickname TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invites (
                    digest TEXT PRIMARY KEY, remaining INTEGER NOT NULL, expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_setup (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    digest TEXT, expires INTEGER,
                    account INTEGER REFERENCES accounts(id),
                    CHECK ((account IS NULL AND digest IS NOT NULL AND expires IS NOT NULL)
                        OR (account IS NOT NULL AND digest IS NULL AND expires IS NULL))
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, account INTEGER NOT NULL REFERENCES accounts(id),
                    marker TEXT NOT NULL, expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    digest TEXT PRIMARY KEY, account INTEGER NOT NULL REFERENCES accounts(id),
                    marker TEXT NOT NULL, expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tools (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES accounts(id),
                    title TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0,
                    entries TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS tools_owner ON tools(owner, published);
                CREATE TABLE IF NOT EXISTS table_tools (
                    id TEXT PRIMARY KEY REFERENCES tools(id)
                );
                CREATE TABLE IF NOT EXISTS model_candidates (
                    run_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL REFERENCES tools(id)
                );
                CREATE TABLE IF NOT EXISTS tool_versions (
                    tool_id TEXT NOT NULL REFERENCES tools(id), version INTEGER NOT NULL,
                    run_id TEXT NOT NULL UNIQUE, note TEXT NOT NULL, effective_at INTEGER,
                    PRIMARY KEY (tool_id, version)
                );
                CREATE TABLE IF NOT EXISTS tool_git_repositories (
                    tool_id TEXT PRIMARY KEY REFERENCES tools(id),
                    repo_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS tool_git_versions (
                    tool_id TEXT NOT NULL, version INTEGER NOT NULL, commit_id TEXT NOT NULL,
                    PRIMARY KEY (tool_id, version),
                    FOREIGN KEY (tool_id, version) REFERENCES tool_versions(tool_id, version)
                );
                CREATE TABLE IF NOT EXISTS revision_drafts (
                    run_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL REFERENCES tools(id),
                    base_version INTEGER NOT NULL, note TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS table_restore_drafts (
                    run_id TEXT PRIMARY KEY REFERENCES revision_drafts(run_id) ON DELETE CASCADE,
                    title TEXT NOT NULL, revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requirement_drafts (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES accounts(id),
                    text TEXT NOT NULL, revision INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requirement_drafts_owner
                    ON requirement_drafts(owner, updated_at DESC, id);
            """)

    @contextmanager
    def connection(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def create_invite(self, code=None, uses=1, hours=DEFAULT_INVITE_HOURS):
        code = secrets.token_urlsafe(24) if code is None else code
        with self.connection(write=True) as db:
            self.insert_invite(db, code, uses, int(time.time() + 3600 * hours))
        return code

    @staticmethod
    def insert_invite(db, code, uses, expires):
        invite_digest = digest(code)
        db.execute("INSERT INTO invites(digest, remaining, expires) VALUES (?, ?, ?)",
                   (invite_digest, uses, expires))
        return invite_digest

    @staticmethod
    def setup_state(db):
        return db.execute("SELECT digest, expires, account FROM admin_setup WHERE singleton=1").fetchone()

    def create_admin_setup_code(self):
        code = secrets.token_urlsafe(32)
        with self.connection(write=True) as db:
            row = self.setup_state(db)
            if row is not None and row["account"] is not None:
                raise ApiError(409, "setup_closed", "管理员已设置。")
            db.execute(
                "INSERT INTO admin_setup(singleton,digest,expires) VALUES (1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET digest=excluded.digest,expires=excluded.expires",
                (digest(code), int(time.time()) + ADMIN_SETUP_SECONDS),
            )
        return code

    @staticmethod
    def setup_available(row):
        return row is not None and row["account"] is None and row["expires"] > int(time.time())

    def require_setup_code(self, db, submitted):
        row = self.setup_state(db)
        if row is not None and row["account"] is not None:
            raise ApiError(409, "setup_closed", "管理员已设置，请登录。")
        if not self.setup_available(row) or not hmac.compare_digest(row["digest"], submitted):
            raise ApiError(403, "invalid_setup_code", "初始化码无效或已过期。")

    def setup_admin(self, data, previous_token):
        code = data.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
            raise ApiError(403, "invalid_setup_code", "初始化码无效或已过期。")
        name, _ = credentials(data, registering=True)
        password = admin_password(data.get("password"))
        submitted = digest(code)
        with self.connection() as db:
            self.require_setup_code(db, submitted)
        salt = secrets.token_bytes(16)
        hashed = password_hash(password, salt)
        with self.connection(write=True) as db:
            self.require_setup_code(db, submitted)
            if db.execute("SELECT 1 FROM accounts WHERE username=?", (name,)).fetchone():
                raise ApiError(409, "username_taken", "用户名已被使用。")
            account = db.execute("INSERT INTO accounts(username,salt,password_hash) VALUES (?,?,?)",
                                 (name, salt, hashed)).lastrowid
            db.execute("UPDATE admin_setup SET account=?,digest=NULL,expires=NULL WHERE singleton=1", (account,))
            token, marker = self.create_session(db, account, previous_token, admin=True)
        return token, {"user": name, "marker": marker, "latest": None}

    def register(self, data, previous_token=""):
        name, password = credentials(data, registering=True)
        invite = data.get("invite")
        if invite is None:
            raise InviteError("missing")
        if not isinstance(invite, str):
            raise InviteError("invalid")
        invite = invite.strip()
        if not invite:
            raise InviteError("missing")
        if len(invite) > 128:
            raise InviteError("invalid")
        code = digest(invite)
        salt = secrets.token_bytes(16)
        hashed = password_hash(password, salt)
        with self.connection(write=True) as db:
            used = db.execute(
                "UPDATE invites SET remaining=remaining-1 WHERE digest=? AND remaining>0 AND expires>?",
                (code, int(time.time())),
            )
            if not used.rowcount:
                invite_row = db.execute("SELECT remaining FROM invites WHERE digest=?", (code,)).fetchone()
                if invite_row is None:
                    raise InviteError("invalid")
                if invite_row["remaining"] <= 0:
                    raise InviteError("used")
                raise InviteError("expired")
            try:
                account = db.execute("INSERT INTO accounts(username,salt,password_hash) VALUES (?,?,?)",
                                     (name, salt, hashed))
            except sqlite3.IntegrityError:
                raise ApiError(409, "username_taken", "用户名已被使用。") from None
            token, marker = self.create_session(db, account.lastrowid, previous_token)
        return token, {"user": name, "marker": marker, "latest": None}

    def login(self, data, previous_token, *, authorize=None):
        name, password = credentials(data)
        # The same password can be verified from either entry point. Bound memory
        # and prevent switching entry points from resetting the attempt budget.
        bucket = hash(name) % LOGIN_ATTEMPT_BUCKETS
        now = time.monotonic_ns()
        with self.login_attempt_lock:
            recent = [t for t in self.login_attempts.get(bucket, ())
                      if now - t < LOGIN_ATTEMPT_WINDOW * 1_000_000_000]
            if len(recent) >= LOGIN_ATTEMPT_LIMIT:
                raise ApiError(429, "login_rate_limit", "登录尝试过多，请稍后再试。")
            self.login_attempts[bucket] = [*recent, now]
        failure = ApiError(401, "login_failed", "账号或密码不正确，或该账号没有管理员权限。"
                           if authorize else "用户名或密码错误。")
        with self.connection() as db:
            account = db.execute("SELECT * FROM accounts WHERE username=?", (name,)).fetchone()
        hashed = password_hash(password, account["salt"] if account else bytes(16))
        if not hmac.compare_digest(hashed, account["password_hash"] if account else bytes(32)):
            raise failure
        with self.connection(write=True) as db:
            current = db.execute("SELECT password_hash FROM accounts WHERE id=?", (account["id"],)).fetchone()
            if current is None or not hmac.compare_digest(hashed, current["password_hash"]):
                raise failure
            if authorize and not authorize(db, account):
                raise failure
            token, _ = self.create_session(db, account["id"], previous_token, admin=authorize is not None)
        with self.login_attempt_lock:
            self.login_attempts[bucket] = [t for t in self.login_attempts[bucket] if t != now]
        return token

    def change_admin_password(self, data, token, marker, require_admin):
        current = data.get("currentPassword")
        new = admin_password(data.get("newPassword"))
        if not isinstance(current, str) or not 1 <= len(current) <= 128:
            raise ApiError(400, "invalid_password", "请输入当前密码。")
        with self.connection() as db:
            name = require_admin(db, token, marker)
            account = db.execute("SELECT id,salt,password_hash FROM accounts WHERE username=?", (name,)).fetchone()
        hashed = password_hash(current, account["salt"])
        if not hmac.compare_digest(hashed, account["password_hash"]):
            raise ApiError(401, "incorrect_password", "当前密码不正确。")
        if current == new:
            raise ApiError(400, "password_unchanged", "请设置与当前密码不同的新密码。")
        salt = secrets.token_bytes(16)
        replacement = password_hash(new, salt)
        with self.connection(write=True) as db:
            require_admin(db, token, marker)
            updated = db.execute("UPDATE accounts SET salt=?,password_hash=? WHERE id=? AND password_hash=?",
                                 (salt, replacement, account["id"], hashed))
            if not updated.rowcount:
                raise ApiError(409, "session_changed", "密码已变更，请重新登录。")
            db.execute("DELETE FROM sessions WHERE account=?", (account["id"],))
            db.execute("DELETE FROM admin_sessions WHERE account=?", (account["id"],))

    @staticmethod
    def create_session(db, account_id, previous_token, *, admin=False):
        table = "admin_sessions" if admin else "sessions"
        token, marker = secrets.token_urlsafe(32), secrets.token_urlsafe(18)
        now = int(time.time())
        db.execute(f"DELETE FROM {table} WHERE digest=? OR expires<=?", (digest(previous_token), now))
        db.execute(f"INSERT INTO {table} VALUES (?,?,?,?)", (digest(token), account_id, marker, now + SESSION_SECONDS))
        return token, marker

    def session(self, db, token, marker=None, *, admin=False):
        table = "admin_sessions" if admin else "sessions"
        row = db.execute(
            f"SELECT accounts.id, username, marker, latest_tool FROM {table} "
            f"JOIN accounts ON accounts.id={table}.account WHERE digest=? AND expires>?",
            (digest(token), int(time.time())),
        ).fetchone()
        if row is None:
            raise ApiError(401, "login_required", "请先登录工作台。")
        if marker is not None and row["marker"] != marker:
            raise ApiError(409, "session_changed", "浏览器会话已变更，请重新打开工作台。")
        return row

    def profile(self, token, marker, change=None):
        with self.connection(write=change is not None) as db:
            user = self.session(db, token, marker)
            if change is not None:
                nickname = change.get("nickname")
                if set(change) != {"nickname"} or not isinstance(nickname, str):
                    raise ApiError(400, "invalid_nickname", "请输入有效昵称。")
                nickname = nickname.strip(NICKNAME_TRIM)
                if len(nickname.encode("utf-16-le")) > 48:
                    raise ApiError(400, "invalid_nickname", "昵称最多 24 个字符（部分表情计 2 个）")
                if nickname:
                    db.execute("""INSERT INTO account_profiles VALUES (?, ?)
                                  ON CONFLICT(account) DO UPDATE SET nickname=excluded.nickname""",
                               (user["id"], nickname))
                else:
                    db.execute("DELETE FROM account_profiles WHERE account=?", (user["id"],))
            row = db.execute("SELECT nickname FROM account_profiles WHERE account=?", (user["id"],)).fetchone()
            return {"username": user["username"], "nickname": row["nickname"] if row else user["username"]}

    def logout(self, token, marker, *, admin=False):
        table = "admin_sessions" if admin else "sessions"
        with self.connection(write=True) as db:
            self.session(db, token, marker, admin=admin)
            db.execute(f"DELETE FROM {table} WHERE digest=?", (digest(token),))

    def list_requirement_drafts(self, token, marker):
        with self.connection() as db:
            user = self.session(db, token, marker)
            rows = db.execute("""
                SELECT id, text, revision, updated_at AS updatedAt FROM requirement_drafts
                WHERE owner=? ORDER BY updated_at DESC, id
            """, (user["id"],))
            return {"items": [
                {"id": row["id"], "revision": row["revision"], "updatedAt": row["updatedAt"],
                 "title": row["text"].strip().splitlines()[0][:70]}
                for row in rows
            ]}

    def requirement_draft(self, token, marker, identifier, change=None):
        unavailable = ApiError(404, "draft_unavailable", "此需求草稿不可用。")
        with self.connection(write=change is not None) as db:
            user = self.session(db, token, marker)
            row = db.execute("""
                SELECT id, owner, text, revision, updated_at AS updatedAt
                FROM requirement_drafts WHERE id=?
            """, (identifier,)).fetchone()
            if row is not None and row["owner"] != user["id"]:
                raise unavailable
            if change is None:
                if row is None:
                    raise unavailable
                return {field: row[field] for field in ("id", "text", "revision", "updatedAt")}
            value, revision = change.get("text"), change.get("revision")
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise ApiError(400, "invalid_draft_text", "请输入 1–2000 字的需求草稿。")
            if type(revision) is not int or revision < 0:
                raise ApiError(400, "invalid_draft_revision", "草稿修订号无效。")
            updated = int(time.time() * 1000)
            if row is None:
                if revision != 0:
                    raise unavailable
                count = db.execute("SELECT count(*) FROM requirement_drafts WHERE owner=?", (user["id"],)).fetchone()[0]
                if count >= 100:
                    raise ApiError(409, "draft_limit", "每个账号最多保存 100 份需求草稿。已有草稿仍可编辑。")
                db.execute("INSERT INTO requirement_drafts VALUES (?,?,?,?,?)",
                           (identifier, user["id"], value, 1, updated))
            elif revision + 1 == row["revision"] and value == row["text"]:
                updated = row["updatedAt"]
            else:
                if revision != row["revision"]:
                    raise ApiError(409, "draft_conflict", "草稿已在别处更新。本次编辑仍保留，请核对后再保存。")
                db.execute("UPDATE requirement_drafts SET text=?,revision=?,updated_at=? WHERE id=? AND owner=?",
                           (value, revision + 1, updated, identifier, user["id"]))
            return {"id": identifier, "text": value, "revision": revision + 1, "updatedAt": updated}

    def list_tools(self, token, marker):
        with self.connection() as db:
            user = self.session(db, token, marker)
            items = db.execute(
                """SELECT id, title,
                    COALESCE((SELECT max(version) FROM tool_versions WHERE tool_id=tools.id), 1) AS version
                    FROM tools WHERE owner=? AND published=1 ORDER BY rowid DESC""",
                (user["id"],),
            ).fetchall()
            return {"items": [dict(item) for item in items], "latest": user["latest_tool"]}

    @staticmethod
    def owned_tool(db, user, tool_id, published=True):
        published = None if published is None else int(published)
        tool = db.execute("""
            SELECT tools.*, coalesce(v.version, 1) AS version, coalesce(v.run_id, tools.id) AS run_id,
              CASE WHEN EXISTS (SELECT 1 FROM table_tools WHERE id=tools.id) THEN 1 ELSE 0 END AS table_tool
            FROM tools LEFT JOIN tool_versions v ON v.tool_id=tools.id
              AND v.version=(SELECT max(version) FROM tool_versions WHERE tool_id=tools.id)
            WHERE tools.id=? AND owner=? AND (? IS NULL OR published=?)
        """, (tool_id, user["id"], published, published)).fetchone()
        if tool is None:
            raise ApiError(404, "tool_unavailable", "此工具不可用。")
        return tool

    def get_tool(self, token, marker, tool_id):
        with self.connection() as db:
            user = self.session(db, token, marker)
            return dict(self.owned_tool(db, user, tool_id))

    def draft(self, token, marker, tool_id, requirement, entries=None, *, table=False, model=False):
        title = " ".join(requirement.split())[:48]
        initial = pack_entries([] if entries is None else entries,
                               limit=TABLE_STORAGE_BYTES if table else TOOL_STORAGE_BYTES)
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            self.insert_draft(db, user["id"], tool_id, title, initial, table=table, model=model)

    @staticmethod
    def insert_draft(db, owner, tool_id, title, entries, *, table=False, model=False):
        db.execute("INSERT INTO tools(id, owner, title, entries) VALUES (?,?,?,?)",
                   (tool_id, owner, title, entries))
        if table:
            db.execute("INSERT INTO table_tools VALUES (?)", (tool_id,))
        if model:
            db.execute("INSERT INTO model_candidates VALUES (?,?)", (tool_id, tool_id))

    @staticmethod
    def require_version(value, version):
        if type(value) is not int or value != version:
            raise ApiError(409, "version_conflict", "工具已更新，请重新打开后再修改。")

    def versions(self, token, marker, tool_id, *, ensure_history=None):
        with self.connection(write=ensure_history is not None) as db:
            user = self.session(db, token, marker)
            tool = self.owned_tool(db, user, tool_id)
            if ensure_history:
                ensure_history(db, tool)
            rows = db.execute("""
                SELECT v.version, v.note, v.effective_at AS effectiveAt, g.commit_id AS "commit"
                FROM tool_versions v LEFT JOIN tool_git_versions g USING (tool_id, version)
                WHERE v.tool_id=? ORDER BY v.version DESC
            """, (tool_id,)).fetchall()
            items = [dict(row) for row in rows] or [
                {"version": 1, "note": "首次生成", "effectiveAt": None},
            ]
            for item in items:
                if item.get("commit") is None:
                    item.pop("commit", None)
            return {"current": items[0]["version"], "items": items}

    def revision_draft(self, token, marker, tool_id, version, run_id, note, *, model=False):
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            row = self.owned_tool(db, user, tool_id)
            self.require_version(version, row["version"])
            self.insert_revision_draft(db, row, run_id, note, model=model)
            return dict(row)

    def prepare_restore(self, token, marker, tool_id, data, prepare):
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            row = self.owned_tool(db, user, tool_id)
            self.require_version(data.get("version"), row["version"])
            if type(data.get("revision")) is not int or data["revision"] != row["revision"]:
                raise ApiError(409, "revision_conflict", "此工具已在别处更新，请重新打开后再恢复。")
            target = data.get("restoreVersion")
            if type(target) is not int or not 1 <= target < row["version"]:
                raise ApiError(400, "invalid_restore", "请选择此工具的较早版本。")
            history = db.execute(
                "SELECT run_id FROM tool_versions WHERE tool_id=? AND version=?", (tool_id, target),
            ).fetchone()
            if not row["table_tool"] or history is None:
                raise ApiError(409, "incompatible_restore", "无法验证此旧版与当前记录兼容，当前版本仍可用。")
            run_id, title = prepare(row, history["run_id"])
            self.insert_revision_draft(db, row, run_id, f"恢复自 v{target}")
            db.execute("INSERT INTO table_restore_drafts VALUES (?,?,?)", (run_id, title, row["revision"]))
            return row, run_id

    @staticmethod
    def insert_revision_draft(db, row, run_id, note, *, model=False):
        db.execute("INSERT INTO revision_drafts VALUES (?,?,?,?)", (run_id, row["id"], row["version"], note))
        if model:
            db.execute("INSERT INTO model_candidates VALUES (?,?)", (run_id, row["id"]))

    def resolve_publication(self, token, marker, tool_id, run_id):
        if not isinstance(run_id, str):
            raise ApiError(400, "invalid_draft", "修改候选无效。")
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            row = self.owned_tool(db, user, tool_id)
            # Share the publication write lock so a pending request cannot commit after this readback.
            db.execute("DELETE FROM revision_drafts WHERE tool_id=? AND run_id=?", (tool_id, run_id))
            db.execute("DELETE FROM model_candidates WHERE tool_id=? AND run_id=? AND run_id<>tool_id",
                       (tool_id, run_id))
            applied = db.execute(
                "SELECT 1 FROM tool_versions WHERE tool_id=? AND run_id=? AND version>1", (tool_id, run_id),
            ).fetchone() is not None
            return row, applied

    def resolve_initial_publication(self, token, marker, tool_id):
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            row = self.owned_tool(db, user, tool_id, published=None)
            if row["published"] == 1:
                return row
            if row["published"] == 0:
                # Fence the original commit even if the browser gave up before it reached SQLite.
                db.execute("UPDATE tools SET published=-1 WHERE id=?", (tool_id,))
                db.execute("DELETE FROM model_candidates WHERE run_id=? AND tool_id=?", (tool_id, tool_id))
            return None

    @staticmethod
    def record_candidate_success(db, row, run_id, record):
        if record and db.execute("SELECT 1 FROM model_candidates WHERE run_id=? AND tool_id=?",
                                 (run_id, row["id"])).fetchone():
            record(db, row["owner"], run_id)
            db.execute("DELETE FROM model_candidates WHERE run_id=?", (run_id,))

    def publish_revision(self, db, row, data, record, *, migrated_entries=None):
        run_id = data.get("runId")
        if not isinstance(run_id, str):
            raise ApiError(400, "invalid_draft", "修改候选无效。")
        revision = data.get("revision")
        if row["version"] > 1 and run_id == row["run_id"] and type(revision) is int and revision + 1 == row["revision"]:
            return {"revision": row["revision"], "version": row["version"]}
        draft = db.execute("SELECT * FROM revision_drafts WHERE run_id=? AND tool_id=?",
                           (run_id, row["id"])).fetchone()
        if draft is None:
            raise ApiError(404, "tool_unavailable", "此修改候选不可用。")
        self.require_version(draft["base_version"], row["version"])
        if type(revision) is not int or revision != row["revision"]:
            raise ApiError(409, "revision_conflict", "已有更新的数据，请用最新记录重新准备预览。")
        restore = db.execute("SELECT title, revision FROM table_restore_drafts WHERE run_id=?", (run_id,)).fetchone()
        if restore is not None and restore["revision"] != revision:
            raise ApiError(409, "revision_conflict", "已有更新的数据，请用最新记录重新准备恢复。")
        limit = TABLE_STORAGE_BYTES if row["table_tool"] else TOOL_STORAGE_BYTES
        packed = pack_entries(data.get("entries"), limit=limit)
        current_entries = dict(json.loads(row["entries"]))
        candidate_entries = dict(data["entries"])
        if (not current_entries.items() <= candidate_entries.items()
                or (restore is not None and current_entries != candidate_entries)):
            raise ApiError(409, "incompatible_data", "新版无法原样保留已有数据，旧版仍可用。")
        if migrated_entries is not None:
            packed = pack_entries(migrated_entries, limit=limit)
        db.execute("INSERT OR IGNORE INTO tool_versions VALUES (?,1,?,'首次生成',NULL)",
                   (row["id"], row["id"]))
        version = row["version"] + 1
        db.execute("INSERT INTO tool_versions VALUES (?,?,?,?,?)",
                   (row["id"], version, run_id, draft["note"], int(time.time())))
        self.record_candidate_success(db, row, run_id, record)
        db.execute("DELETE FROM revision_drafts WHERE run_id=?", (run_id,))
        title = restore["title"] if restore is not None else row["title"]
        db.execute("UPDATE tools SET entries=?,revision=?,title=? WHERE id=?", (packed, revision + 1, title, row["id"]))
        db.execute("UPDATE accounts SET latest_tool=? WHERE id=?", (row["id"], row["owner"]))
        return {"revision": revision + 1, "version": version}

    def commit_initial(self, db, row, entries, record):
        limit = TABLE_STORAGE_BYTES if row["table_tool"] else TOOL_STORAGE_BYTES
        packed = pack_entries(entries, limit=limit)
        if not dict(json.loads(row["entries"])).items() <= dict(entries).items():
            raise ApiError(409, "incompatible_data", "预览未能原样保留预填记录，请重试。")
        revision = row["revision"] + 1
        db.execute("UPDATE tools SET entries=?,revision=?,published=1 WHERE id=? AND owner=?",
                   (packed, revision, row["id"], row["owner"]))
        self.record_candidate_success(db, row, row["id"], record)
        db.execute("INSERT INTO tool_versions VALUES (?,1,?,'首次生成',?)",
                   (row["id"], row["id"], int(time.time())))
        db.execute("UPDATE accounts SET latest_tool=? WHERE id=?", (row["id"], row["owner"]))
        return {"revision": revision}

    def change_tool(self, token, marker, tool_id, action, data, *, record_model_success=None, ensure_history=None, allow_snapshot=None):
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            row = self.owned_tool(db, user, tool_id, published=action != "commit")
            if action in ("data", "publish", "commit") and allow_snapshot is not None and not allow_snapshot(db, row):
                raise ApiError(409, "record_operation_required", "此工具须逐条保存记录。")
            if action in ("publish", "commit"):
                result = (self.publish_revision(db, row, data, record_model_success) if action == "publish"
                          else self.commit_initial(db, row, data.get("entries"), record_model_success))
                if ensure_history:
                    ensure_history(db, row)
                return result
            revision = row["revision"]
            if action == "data":
                self.require_version(data.get("version", 1), row["version"])
            if action == "open" and "version" in data:
                self.require_version(data["version"], row["version"])
            if action == "data":
                limit = TABLE_STORAGE_BYTES if row["table_tool"] else TOOL_STORAGE_BYTES
                entries = pack_entries(data.get("entries"), limit=limit)
                if type(data.get("revision")) is not int or data["revision"] != revision:
                    raise ApiError(409, "revision_conflict", "此工具已在别处更新，请重新打开后编辑。")
                revision += 1
                db.execute("UPDATE tools SET entries=?,revision=?,published=1 WHERE id=? AND owner=?",
                           (entries, revision, tool_id, user["id"]))
            if action != "data":
                db.execute("UPDATE accounts SET latest_tool=? WHERE id=?", (tool_id, user["id"]))
            return {"revision": revision}


def pack_entries(entries, *, limit=TOOL_STORAGE_BYTES):
    if not isinstance(entries, list) or any(
        not isinstance(entry, list) or len(entry) != 2 or not all(isinstance(value, str) for value in entry)
        for entry in entries
    ):
        raise ApiError(400, "invalid_entries", "工具存储格式无效。")
    if len({entry[0] for entry in entries}) != len(entries):
        raise ApiError(400, "invalid_entries", "工具存储含有重复键。")
    packed = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    if len(packed.encode("utf-8")) > limit:
        raise ApiError(413, "tool_quota", f"工具存储超过 {limit // 1024} KiB。")
    return packed
