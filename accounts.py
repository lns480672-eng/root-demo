"""Local accounts and account-scoped workspace state."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager

SESSION_SECONDS = 12 * 60 * 60
USERNAME = re.compile(r"[a-z0-9_-]{3,32}")


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


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


class Store:
    def __init__(self, path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    salt BLOB NOT NULL, password_hash BLOB NOT NULL, latest_tool TEXT
                );
                CREATE TABLE IF NOT EXISTS invites (
                    digest TEXT PRIMARY KEY, remaining INTEGER NOT NULL, expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, account INTEGER NOT NULL REFERENCES accounts(id),
                    marker TEXT NOT NULL, expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tools (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES accounts(id),
                    title TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0,
                    entries TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS tools_owner ON tools(owner, published);
                CREATE TABLE IF NOT EXISTS tool_versions (
                    tool_id TEXT NOT NULL REFERENCES tools(id), version INTEGER NOT NULL,
                    run_id TEXT NOT NULL UNIQUE, note TEXT NOT NULL, effective_at INTEGER,
                    PRIMARY KEY (tool_id, version)
                );
                CREATE TABLE IF NOT EXISTS revision_drafts (
                    run_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL REFERENCES tools(id),
                    base_version INTEGER NOT NULL, note TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def create_invite(self, code=None, uses=1, hours=24):
        code = secrets.token_urlsafe(24) if code is None else code
        with self.connection(write=True) as db:
            db.execute("INSERT INTO invites VALUES (?, ?, ?)",
                       (digest(code), uses, int(time.time() + 3600 * hours)))
        return code

    def register(self, data):
        name, password = credentials(data, registering=True)
        invite = data.get("invite")
        error = ApiError(400, "invalid_invite", "邀请码无效、已过期或已用完，请确认后再试。")
        if not isinstance(invite, str) or not 1 <= len(invite.strip()) <= 128:
            raise error
        code = digest(invite.strip())
        salt = secrets.token_bytes(16)
        hashed = password_hash(password, salt)
        with self.connection(write=True) as db:
            used = db.execute(
                "UPDATE invites SET remaining=remaining-1 WHERE digest=? AND remaining>0 AND expires>?",
                (code, int(time.time())),
            )
            if not used.rowcount:
                raise error
            try:
                db.execute("INSERT INTO accounts(username,salt,password_hash) VALUES (?,?,?)", (name, salt, hashed))
            except sqlite3.IntegrityError:
                raise ApiError(409, "username_taken", "用户名已被使用。") from None

    def login(self, data, previous_token):
        name, password = credentials(data)
        with self.connection() as db:
            account = db.execute("SELECT * FROM accounts WHERE username=?", (name,)).fetchone()
        hashed = password_hash(password, account["salt"] if account else bytes(16))
        if not hmac.compare_digest(hashed, account["password_hash"] if account else bytes(32)):
            raise ApiError(401, "login_failed", "用户名或密码错误。")
        token, marker = secrets.token_urlsafe(32), secrets.token_urlsafe(18)
        now = int(time.time())
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE digest=? OR expires<=?", (digest(previous_token), now))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (digest(token), account["id"], marker, now + SESSION_SECONDS))
        return token

    def session(self, db, token, marker=None):
        row = db.execute(
            "SELECT accounts.id, username, marker, latest_tool FROM sessions "
            "JOIN accounts ON accounts.id=sessions.account WHERE digest=? AND expires>?",
            (digest(token), int(time.time())),
        ).fetchone()
        if row is None:
            raise ApiError(401, "login_required", "请先登录工作台。")
        if marker is not None and row["marker"] != marker:
            raise ApiError(409, "session_changed", "浏览器会话已变更，请重新打开工作台。")
        return row

    def logout(self, token, marker):
        with self.connection(write=True) as db:
            self.session(db, token, marker)
            db.execute("DELETE FROM sessions WHERE digest=?", (digest(token),))

    def list_tools(self, token, marker):
        with self.connection() as db:
            user = self.session(db, token, marker)
            items = db.execute(
                "SELECT id, title FROM tools WHERE owner=? AND published=1 ORDER BY rowid DESC",
                (user["id"],),
            ).fetchall()
            return {"items": [dict(item) for item in items], "latest": user["latest_tool"]}

    @staticmethod
    def owned_tool(db, user, tool_id, published=True):
        tool = db.execute("""
            SELECT tools.*, coalesce(v.version, 1) AS version, coalesce(v.run_id, tools.id) AS run_id
            FROM tools LEFT JOIN tool_versions v ON v.tool_id=tools.id
              AND v.version=(SELECT max(version) FROM tool_versions WHERE tool_id=tools.id)
            WHERE tools.id=? AND owner=? AND published=?
        """, (tool_id, user["id"], int(published))).fetchone()
        if tool is None:
            raise ApiError(404, "tool_unavailable", "此工具不可用。")
        return tool

    def get_tool(self, token, marker, tool_id):
        with self.connection() as db:
            user = self.session(db, token, marker)
            return dict(self.owned_tool(db, user, tool_id))

    def draft(self, token, marker, tool_id, requirement):
        title = " ".join(requirement.split())[:48]
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            db.execute("INSERT INTO tools(id, owner, title) VALUES (?,?,?)", (tool_id, user["id"], title))

    @staticmethod
    def require_version(value, version):
        if type(value) is not int or value != version:
            raise ApiError(409, "version_conflict", "工具已更新，请重新打开后再修改。")

    def versions(self, token, marker, tool_id):
        with self.connection() as db:
            user = self.session(db, token, marker)
            self.owned_tool(db, user, tool_id)
            rows = db.execute("""
                SELECT version, note, effective_at AS effectiveAt FROM tool_versions
                WHERE tool_id=? ORDER BY version DESC
            """, (tool_id,)).fetchall()
            items = [dict(row) for row in rows] or [
                {"version": 1, "note": "首次生成", "effectiveAt": None},
            ]
            return {"current": items[0]["version"], "items": items}

    def revision_draft(self, token, marker, tool_id, version, run_id, note):
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            row = self.owned_tool(db, user, tool_id)
            self.require_version(version, row["version"])
            db.execute("INSERT INTO revision_drafts VALUES (?,?,?,?)", (run_id, tool_id, version, note))
            return dict(row)

    def resolve_publication(self, token, marker, tool_id, run_id):
        if not isinstance(run_id, str):
            raise ApiError(400, "invalid_draft", "修改候选无效。")
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            row = self.owned_tool(db, user, tool_id)
            # Share the publication write lock so a pending request cannot commit after this readback.
            db.execute("DELETE FROM revision_drafts WHERE tool_id=? AND run_id=?", (tool_id, run_id))
            applied = db.execute(
                "SELECT 1 FROM tool_versions WHERE tool_id=? AND run_id=? AND version>1", (tool_id, run_id),
            ).fetchone() is not None
            return row, applied

    def publish_revision(self, db, row, data):
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
        packed = pack_entries(data.get("entries"))
        if not dict(json.loads(row["entries"])).items() <= dict(data["entries"]).items():
            raise ApiError(409, "incompatible_data", "新版无法原样保留已有数据，旧版仍可用。")
        db.execute("INSERT OR IGNORE INTO tool_versions VALUES (?,1,?,'首次生成',NULL)",
                   (row["id"], row["id"]))
        version = row["version"] + 1
        db.execute("INSERT INTO tool_versions VALUES (?,?,?,?,?)",
                   (row["id"], version, run_id, draft["note"], int(time.time())))
        db.execute("DELETE FROM revision_drafts WHERE run_id=?", (run_id,))
        db.execute("UPDATE tools SET entries=?,revision=? WHERE id=?", (packed, revision + 1, row["id"]))
        db.execute("UPDATE accounts SET latest_tool=? WHERE id=?", (row["id"], row["owner"]))
        return {"revision": revision + 1, "version": version}

    def change_tool(self, token, marker, tool_id, action, data):
        with self.connection(write=True) as db:
            user = self.session(db, token, marker)
            row = self.owned_tool(db, user, tool_id, published=action != "commit")
            if action == "publish":
                return self.publish_revision(db, row, data)
            revision = row["revision"]
            if action == "data":
                self.require_version(data.get("version", 1), row["version"])
            if action == "open" and "version" in data:
                self.require_version(data["version"], row["version"])
            if action in ("commit", "data"):
                entries = pack_entries(data.get("entries"))
                if action == "data" and (type(data.get("revision")) is not int or data["revision"] != revision):
                    raise ApiError(409, "revision_conflict", "此工具已在别处更新，请重新打开后编辑。")
                revision += 1
                db.execute("UPDATE tools SET entries=?,revision=?,published=1 WHERE id=? AND owner=?",
                           (entries, revision, tool_id, user["id"]))
            if action == "commit":
                db.execute("INSERT INTO tool_versions VALUES (?,1,?,'首次生成',?)",
                           (tool_id, tool_id, int(time.time())))
            if action != "data":
                db.execute("UPDATE accounts SET latest_tool=? WHERE id=?", (tool_id, user["id"]))
            return {"revision": revision}


def pack_entries(entries):
    if not isinstance(entries, list) or any(
        not isinstance(entry, list) or len(entry) != 2 or not all(isinstance(value, str) for value in entry)
        for entry in entries
    ):
        raise ApiError(400, "invalid_entries", "工具存储格式无效。")
    if len({entry[0] for entry in entries}) != len(entries):
        raise ApiError(400, "invalid_entries", "工具存储含有重复键。")
    packed = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    if len(packed.encode("utf-8")) > 256 * 1024:
        raise ApiError(413, "tool_quota", "工具存储超过 256 KiB。")
    return packed
