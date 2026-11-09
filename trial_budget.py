"""SQLite trial quotas and operator actions for the workbench."""

from datetime import datetime, time as midnight, timedelta, timezone
from pathlib import Path
import time

from accounts import ApiError, USERNAME


BEIJING = timezone(timedelta(hours=8))
MAX_COUNTER = 2**53 - 1


def usage(used, limit):
    return {"used": used, "limit": limit, "remaining": max(0, limit - used)}


def budget_view(own, limit, total, total_limit):
    return {
        **usage(own, limit),
        "totalUsed": total, "totalLimit": total_limit, "totalRemaining": max(0, total_limit - total),
    }


class TrialBudget:
    def __init__(self, store, runs, generation_limits=(5, 30), analysis_limits=(6, 60),
                 admin_usernames=(), clock=time.time):
        self.store = store
        self.pause_file = Path(runs) / "generation.paused"
        self.clock = clock
        names = frozenset(name.strip().lower() for name in admin_usernames)
        if any(not USERNAME.fullmatch(name) for name in names):
            raise ValueError("管理员用户名配置无效。")
        self.generation_limit, self.generation_total_limit = generation_limits
        self.analysis_limit, self.analysis_total_limit = analysis_limits
        if any(type(value) is not int or not 0 < value <= MAX_COUNTER for value in (
            self.generation_limit, self.generation_total_limit, self.analysis_limit, self.analysis_total_limit,
        )):
            raise ValueError("额度必须是正整数。")
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS generation_usage (
                    account INTEGER PRIMARY KEY REFERENCES accounts(id),
                    attempts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_success (
                    run_id TEXT PRIMARY KEY,
                    account INTEGER NOT NULL REFERENCES accounts(id)
                );
                CREATE INDEX IF NOT EXISTS generation_success_account ON generation_success(account);
                CREATE TABLE IF NOT EXISTS generation_grants (
                    account INTEGER PRIMARY KEY REFERENCES accounts(id),
                    extra INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_usage (
                    account INTEGER NOT NULL REFERENCES accounts(id),
                    day TEXT NOT NULL, attempts INTEGER NOT NULL,
                    PRIMARY KEY(account, day)
                );
                CREATE TABLE IF NOT EXISTS analysis_requests (
                    account INTEGER NOT NULL REFERENCES accounts(id),
                    request_id TEXT NOT NULL,
                    PRIMARY KEY(account, request_id)
                );
                CREATE TABLE IF NOT EXISTS trial_control (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    paused INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO trial_control VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS trial_audit (
                    id INTEGER PRIMARY KEY,
                    actor TEXT NOT NULL, action TEXT NOT NULL, username TEXT, amount INTEGER,
                    created_at INTEGER NOT NULL, request_id TEXT,
                    UNIQUE(actor, request_id)
                );
            """)
            # An invite recipient must not acquire operator privileges by claiming an unused name.
            self.admins = {(row["id"], row["username"]) for row in db.execute("SELECT id, username FROM accounts")
                           if row["username"] in names}

    def day_window(self):
        today = datetime.fromtimestamp(self.clock(), BEIJING).date()
        reset = datetime.combine(today + timedelta(days=1), midnight(), BEIJING)
        return today.isoformat(), reset.isoformat()

    def paused(self, db):
        return bool(db.execute("SELECT paused FROM trial_control WHERE id=1").fetchone()[0]) or self.pause_file.exists()

    @staticmethod
    def totals(db):
        return db.execute("""
            SELECT COALESCE((SELECT SUM(attempts) FROM generation_usage), 0),
                   COALESCE((SELECT SUM(attempts) FROM analysis_usage), 0)
        """).fetchone()

    @staticmethod
    def account_rows(db, day, account_id=None):
        return db.execute("""
            SELECT accounts.username,
                   (SELECT count(*) FROM generation_success WHERE account=accounts.id) AS generation_used,
                   COALESCE(generation_grants.extra, 0) AS generation_grant,
                   COALESCE(analysis_usage.attempts, 0) AS analysis_used
            FROM accounts
            LEFT JOIN generation_grants ON generation_grants.account=accounts.id
            LEFT JOIN analysis_usage ON analysis_usage.account=accounts.id AND analysis_usage.day=?
            WHERE (? IS NULL OR accounts.id=?)
            ORDER BY accounts.username
        """, (day, account_id, account_id))

    def account_view(self, row, generation_total, analysis_total, reset):
        return {
            "username": row["username"],
            "generation": budget_view(row["generation_used"], self.generation_limit + row["generation_grant"],
                                      generation_total, self.generation_total_limit),
            "analysis": {
                **budget_view(row["analysis_used"], self.analysis_limit, analysis_total, self.analysis_total_limit),
                "resetAt": reset,
            },
        }

    def balance(self, token, marker):
        with self.store.connection() as db:
            db.execute("BEGIN")
            user = self.store.session(db, token, marker)
            day, reset = self.day_window()
            account = self.account_rows(db, day, user["id"]).fetchone()
            return {
                **self.account_view(account, *self.totals(db), reset),
                "paused": self.paused(db), "admin": (user["id"], user["username"]) in self.admins,
            }

    def require_generation_slot(self, row):
        if row["generation_used"] >= self.generation_limit + row["generation_grant"]:
            raise ApiError(429, "generation_account_limit", "你的生成额度已用完。已有工具可以继续使用。")

    def reserve(self, token, marker, kind, request_id=None):
        """Count one actual dispatch. Call only after preparing a provider request."""
        if kind not in ("generation", "analysis"):
            raise ValueError("未知的模型调用类型。")
        with self.store.connection(write=True) as db:
            user = self.store.session(db, token, marker)
            account_id = user["id"]
            if kind == "analysis" and request_id is not None and db.execute(
                "SELECT 1 FROM analysis_requests WHERE account=? AND request_id=?", (account_id, request_id),
            ).fetchone() is not None:
                raise ApiError(409, "analysis_duplicate", "此分析请求已提交，请等待结果或重新发起。")
            if self.paused(db):
                raise ApiError(503, "model_paused", "模型调用暂时暂停。已有工具和草稿可以继续使用。")
            day, _ = self.day_window()
            own = self.account_rows(db, day, account_id).fetchone()
            generation_total, analysis_total = self.totals(db)
            if kind == "generation":
                if generation_total >= self.generation_total_limit:
                    raise ApiError(429, "generation_total_limit", "试用的总生成额度已用完。已有工具可以继续使用。")
                self.require_generation_slot(own)
                db.execute("""
                    INSERT INTO generation_usage(account, attempts) VALUES (?, 1)
                    ON CONFLICT(account) DO UPDATE SET attempts=attempts+1
                """, (account_id,))
                return
            if analysis_total >= self.analysis_total_limit:
                raise ApiError(429, "analysis_total_limit", "试用的总分析额度已用完。输入和草稿仍可编辑。")
            if own["analysis_used"] >= self.analysis_limit:
                raise ApiError(429, "analysis_account_limit", "你的今日分析额度已用完。输入和草稿仍可编辑。")
            db.execute("""
                INSERT INTO analysis_usage(account, day, attempts) VALUES (?, ?, 1)
                ON CONFLICT(account, day) DO UPDATE SET attempts=attempts+1
            """, (account_id, day))
            if request_id is not None:
                db.execute("INSERT INTO analysis_requests VALUES (?, ?)", (account_id, request_id))

    def record_generation_success(self, db, account_id, run_id):
        """Charge personal quota in the same write transaction as a real-model publication."""
        previous = db.execute("SELECT account FROM generation_success WHERE run_id=?", (run_id,)).fetchone()
        if previous is not None:
            if previous["account"] != account_id:
                raise ApiError(404, "tool_unavailable", "此工具不可用。")
            return
        day, _ = self.day_window()
        self.require_generation_slot(self.account_rows(db, day, account_id).fetchone())
        db.execute("INSERT INTO generation_success VALUES (?, ?)", (run_id, account_id))

    def require_admin(self, db, token, marker):
        user = self.store.session(db, token, marker)
        if (user["id"], user["username"]) not in self.admins:
            raise ApiError(403, "admin_required", "需要管理员权限。")
        return user["username"]

    def admin_state(self, token, marker):
        with self.store.connection() as db:
            db.execute("BEGIN")
            self.require_admin(db, token, marker)
            day, reset = self.day_window()
            generation_total, analysis_total = self.totals(db)
            audit = db.execute("""
                SELECT id, actor, action, username, amount, created_at AS createdAt
                FROM trial_audit ORDER BY id DESC LIMIT 100
            """)
            return {
                "generation": usage(generation_total, self.generation_total_limit),
                "analysis": usage(analysis_total, self.analysis_total_limit),
                "paused": self.paused(db),
                "accounts": [
                    self.account_view(row, generation_total, analysis_total, reset)
                    for row in self.account_rows(db, day)
                ],
                "audit": [dict(row) for row in audit],
            }

    def admin_action(self, token, marker, data):
        with self.store.connection(write=True) as db:
            actor = self.require_admin(db, token, marker)
            action = data.get("action")
            request_id = data.get("requestId")
            if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 80):
                raise ApiError(400, "invalid_admin_request", "管理请求编号无效。")
            username = amount = None
            if action == "grant":
                value = data.get("username")
                username = value.strip().lower() if isinstance(value, str) else ""
                amount = data.get("amount")
                if not USERNAME.fullmatch(username) or type(amount) is not int or not 0 < amount <= MAX_COUNTER:
                    raise ApiError(400, "invalid_grant", "请输入有效账号及正整数补额。")
            elif action not in ("pause", "resume"):
                raise ApiError(400, "invalid_admin_action", "不支持此管理操作。")
            if request_id is not None:
                previous = db.execute("""
                    SELECT action, username, amount FROM trial_audit WHERE actor=? AND request_id=?
                """, (actor, request_id)).fetchone()
                if previous is not None:
                    if tuple(previous) != (action, username, amount):
                        raise ApiError(409, "admin_request_conflict", "此管理请求编号已用于其他操作。")
                    return self.action_result(db, action, username, amount)
            if action == "grant":
                row = db.execute("""
                    SELECT accounts.id, COALESCE(generation_grants.extra, 0) AS extra
                    FROM accounts LEFT JOIN generation_grants ON generation_grants.account=accounts.id
                    WHERE username=?
                """, (username,)).fetchone()
                if row is None:
                    raise ApiError(404, "account_unavailable", "此账号不存在。")
                if self.generation_limit + row["extra"] + amount > MAX_COUNTER:
                    raise ApiError(400, "invalid_grant", "补额后的上限过大，请减少数量。")
                db.execute("""
                    INSERT INTO generation_grants VALUES (?, ?)
                    ON CONFLICT(account) DO UPDATE SET extra=extra+excluded.extra
                """, (row["id"], amount))
            else:
                if action == "resume" and self.pause_file.exists():
                    raise ApiError(409, "external_pause", "模型调用仍被部署操作员暂停，请先移除部署暂停。")
                db.execute("UPDATE trial_control SET paused=? WHERE id=1", (int(action == "pause"),))
            db.execute("""
                INSERT INTO trial_audit(actor, action, username, amount, created_at, request_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (actor, action, username, amount, int(self.clock()), request_id))
            return self.action_result(db, action, username, amount)

    def action_result(self, db, action, username, amount):
        result = {"ok": True, "paused": self.paused(db), "action": action}
        if action == "grant":
            result.update(username=username, amount=amount)
        return result
