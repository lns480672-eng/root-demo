"""SQLite trial quotas and operator actions for the workbench."""

from datetime import datetime, time as midnight, timedelta, timezone
from pathlib import Path
import secrets
import time
from uuid import UUID

from accounts import ApiError, DEFAULT_INVITE_HOURS, USERNAME, digest


BEIJING = timezone(timedelta(hours=8))
MAX_COUNTER = 2**53 - 1
INVITE_SECONDS = DEFAULT_INVITE_HOURS * 60 * 60
DRAFT_OFFER_SECONDS = 15 * 60


def usage(used, limit):
    return {"used": used, "limit": limit, "remaining": max(0, limit - used)}


def budget_view(own, limit, total, total_limit):
    return {
        **usage(own, limit),
        "totalUsed": total, "totalLimit": total_limit, "totalRemaining": max(0, total_limit - total),
    }


class TrialBudget:
    def __init__(self, store, runs, generation_limits=(5, 30), analysis_limits=(6, 60),
                 admin_usernames=(), clock=time.time, admin_session_required=False):
        self.store = store
        self.pause_file = Path(runs) / "generation.paused"
        self.clock = clock
        self.admin_session_required = admin_session_required
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
                CREATE TABLE IF NOT EXISTS analysis_rounds (
                    account INTEGER NOT NULL REFERENCES accounts(id),
                    id TEXT NOT NULL, idea_digest TEXT NOT NULL, completed_day TEXT,
                    PRIMARY KEY(account, id)
                );
                CREATE INDEX IF NOT EXISTS analysis_rounds_day ON analysis_rounds(account, completed_day);
                CREATE TABLE IF NOT EXISTS analysis_draft_offers (
                    account INTEGER NOT NULL, id TEXT NOT NULL,
                    round_id TEXT NOT NULL, session_digest TEXT NOT NULL, text TEXT NOT NULL,
                    expires REAL NOT NULL,
                    PRIMARY KEY(account, id),
                    FOREIGN KEY(account, round_id) REFERENCES analysis_rounds(account, id)
                );
                CREATE TABLE IF NOT EXISTS model_dispatches (
                    kind TEXT NOT NULL CHECK(kind IN ('generation', 'analysis')),
                    id TEXT NOT NULL,
                    outcome INTEGER CHECK(outcome IN (0, 1)),
                    PRIMARY KEY(kind, id)
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
        with store.connection(write=True) as db:
            if "title" not in {row["name"] for row in db.execute("PRAGMA table_info(analysis_draft_offers)")}:
                db.execute("ALTER TABLE analysis_draft_offers ADD COLUMN title TEXT")
            db.execute("DELETE FROM analysis_draft_offers WHERE expires<=?", (self.clock(),))
            if "invite_digest" not in {row["name"] for row in db.execute("PRAGMA table_info(trial_audit)")}:
                db.execute("ALTER TABLE trial_audit ADD COLUMN invite_digest TEXT REFERENCES invites(digest) ON DELETE SET NULL")

    def day_window(self, now=None):
        today = datetime.fromtimestamp(self.clock() if now is None else now, BEIJING).date()
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
                   (SELECT count(*) FROM analysis_rounds WHERE account=accounts.id AND completed_day=?) AS analysis_used
            FROM accounts
            LEFT JOIN generation_grants ON generation_grants.account=accounts.id
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
                "paused": self.paused(db), "admin": self.is_admin(db, user),
            }

    def require_generation_slot(self, row):
        if row["generation_used"] >= self.generation_limit + row["generation_grant"]:
            raise ApiError(429, "generation_account_limit", "你的生成额度已用完。已有工具可以继续使用。")

    def require_dispatch_enabled(self, db):
        if self.paused(db):
            raise ApiError(503, "model_paused", "模型调用暂时暂停。已有工具和草稿可以继续使用。")

    @staticmethod
    def track_dispatch(db, kind, dispatch_id):
        if dispatch_id is not None and not db.execute(
            "INSERT INTO model_dispatches(kind, id) VALUES (?, ?) ON CONFLICT(kind, id) DO NOTHING",
            (kind, dispatch_id),
        ).rowcount:
            raise ApiError(409, "dispatch_duplicate", "此模型调用已登记，请重新发起。")

    def finish_dispatch(self, kind, dispatch_id, *, success):
        """Record the first known provider outcome without creating a new dispatch."""
        with self.store.connection(write=True) as db:
            db.execute("UPDATE model_dispatches SET outcome=? WHERE kind=? AND id=? AND outcome IS NULL",
                       (int(success), kind, dispatch_id))

    def reserve_generation(self, db, account_id, *, dispatch_id=None):
        """Admit one generation dispatch within the caller's authenticated write transaction."""
        self.require_dispatch_enabled(db)
        generation_total, _ = self.totals(db)
        if generation_total >= self.generation_total_limit:
            raise ApiError(429, "generation_total_limit", "试用的总生成额度已用完。已有工具可以继续使用。")
        day, _ = self.day_window()
        self.require_generation_slot(self.account_rows(db, day, account_id).fetchone())
        self.track_dispatch(db, "generation", dispatch_id)
        db.execute("""
            INSERT INTO generation_usage(account, attempts) VALUES (?, 1)
            ON CONFLICT(account) DO UPDATE SET attempts=attempts+1
        """, (account_id,))

    def reserve(self, token, marker, kind, request_id=None, *, dispatch_id=None, round_id=None, idea=None):
        """Count one actual dispatch. Call only after preparing a provider request."""
        if kind not in ("generation", "analysis"):
            raise ValueError("未知的模型调用类型。")
        with self.store.connection(write=True) as db:
            user = self.store.session(db, token, marker)
            account_id = user["id"]
            if kind == "generation":
                self.reserve_generation(db, account_id, dispatch_id=dispatch_id)
                return
            if request_id is not None and db.execute(
                "SELECT 1 FROM analysis_requests WHERE account=? AND request_id=?", (account_id, request_id),
            ).fetchone() is not None:
                raise ApiError(409, "analysis_duplicate", "此分析请求已提交，请等待结果或重新发起。")
            self.require_dispatch_enabled(db)
            day, _ = self.day_window()
            _, analysis_total = self.totals(db)
            if analysis_total >= self.analysis_total_limit:
                raise ApiError(429, "analysis_total_limit", "试用的总分析额度已用完。输入和草稿仍可编辑。")
            idea_digest = digest(idea.strip())
            db.execute("""
                INSERT INTO analysis_rounds(account, id, idea_digest) VALUES (?, ?, ?)
                ON CONFLICT(account, id) DO NOTHING
            """, (account_id, round_id, idea_digest))
            round_row = self.analysis_round(db, account_id, round_id)
            if round_row["idea_digest"] != idea_digest:
                raise ApiError(409, "analysis_round_changed", "原始想法已变化，请开始新一轮需求梳理。")
            self.require_analysis_slot(db, account_id, day, round_row)
            self.track_dispatch(db, "analysis", dispatch_id)
            db.execute("""
                INSERT INTO analysis_usage(account, day, attempts) VALUES (?, ?, 1)
                ON CONFLICT(account, day) DO UPDATE SET attempts=attempts+1
            """, (account_id, day))
            if request_id is not None:
                db.execute("INSERT INTO analysis_requests VALUES (?, ?)", (account_id, request_id))

    @staticmethod
    def analysis_round(db, account_id, round_id):
        return db.execute("SELECT * FROM analysis_rounds WHERE account=? AND id=?", (account_id, round_id)).fetchone()

    def require_analysis_slot(self, db, account_id, day, round_row):
        if round_row["completed_day"] is None and self.account_rows(db, day, account_id).fetchone()["analysis_used"] >= self.analysis_limit:
            raise ApiError(429, "analysis_account_limit", "今日可梳理的需求条数已用完。输入和草稿仍可编辑。")

    def offer_analysis_draft(self, token, marker, round_id, request_id, text, title=None):
        with self.store.connection(write=True) as db:
            user = self.store.session(db, token, marker)
            now = self.clock()
            db.execute("DELETE FROM analysis_draft_offers WHERE expires<=?", (now,))
            db.execute("INSERT INTO analysis_draft_offers (account,id,round_id,session_digest,text,expires,title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (user["id"], request_id, round_id, digest(token), text, now + DRAFT_OFFER_SECONDS, title))

    def claim_analysis_draft(self, token, marker, offer_id):
        """Return an offered draft and charge its round at most once, in one write transaction."""
        with self.store.connection(write=True) as db:
            account_id = self.store.session(db, token, marker)["id"]
            now = self.clock()
            offer = db.execute("""
                SELECT round_id, text, title FROM analysis_draft_offers
                WHERE account=? AND id=? AND session_digest=? AND expires>?
            """, (account_id, offer_id, digest(token), now)).fetchone()
            if offer is None:
                raise ApiError(404, "analysis_draft_unavailable", "这份草稿已无法找回。原想法仍可重新梳理。")
            day, _ = self.day_window(now)
            round_id = offer["round_id"]
            round_row = self.analysis_round(db, account_id, round_id)
            self.require_analysis_slot(db, account_id, day, round_row)
            db.execute("""
                UPDATE analysis_rounds SET completed_day=? WHERE account=? AND id=? AND completed_day IS NULL
            """, (day, account_id, round_id))
            return {"kind": "draft", "text": offer["text"], **({"title": offer["title"]} if offer["title"] else {})}

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

    def is_admin(self, db, user):
        if (user["id"], user["username"]) in self.admins:
            return True
        row = self.store.setup_state(db)
        return row is not None and row["account"] == user["id"]

    def require_admin(self, db, token, marker):
        user = self.store.session(db, token, marker, admin=self.admin_session_required)
        if not self.is_admin(db, user):
            raise ApiError(403, "admin_required", "需要管理员权限。")
        return user["username"]

    @staticmethod
    def dispatch_summary(db):
        return {kind: (completed, failed) for kind, completed, failed in db.execute("""
            SELECT kind, count(*), SUM(outcome=0) FROM model_dispatches
            WHERE outcome IS NOT NULL GROUP BY kind
        """)}

    def admin_state(self, token, marker):
        with self.store.connection() as db:
            db.execute("BEGIN")
            self.require_admin(db, token, marker)
            day, reset = self.day_window()
            generation_total, analysis_total = self.totals(db)
            summaries = self.dispatch_summary(db)

            def global_usage(kind, used, limit):
                completed, failed = summaries.get(kind, (0, 0))
                return {**usage(used, limit), "completed": completed, "failed": failed,
                        "failureRate": failed / completed if completed else None}

            audit = db.execute("""
                SELECT id, actor, action, username, amount, created_at AS createdAt
                FROM trial_audit ORDER BY id DESC LIMIT 100
            """)
            return {
                "generation": global_usage("generation", generation_total, self.generation_total_limit),
                "analysis": global_usage("analysis", analysis_total, self.analysis_total_limit),
                "paused": self.paused(db),
                "accounts": [
                    self.account_view(row, generation_total, analysis_total, reset)
                    for row in self.account_rows(db, day)
                ],
                "audit": [dict(row) for row in audit],
            }

    def issue_invite(self, token, marker, request_id):
        with self.store.connection(write=True) as db:
            actor = self.require_admin(db, token, marker)
            try:
                if not isinstance(request_id, str) or str(UUID(request_id, version=4)) != request_id:
                    raise ValueError
            except ValueError:
                raise ApiError(400, "invalid_admin_request", "发码请求编号无效。") from None
            previous = db.execute("SELECT action FROM trial_audit WHERE actor=? AND request_id=?",
                                  (actor, request_id)).fetchone()
            if previous is not None:
                if previous["action"] == "issue_invite":
                    raise ApiError(409, "invite_already_issued", "本次邀请码已生成，原码无法恢复。")
                raise ApiError(409, "admin_request_conflict", "此管理请求编号已用于其他操作。")

            code = secrets.token_urlsafe(24)
            created = int(self.clock())
            expires = created + INVITE_SECONDS
            invite_digest = self.store.insert_invite(db, code, 1, expires)
            db.execute("""
                INSERT INTO trial_audit(actor, action, created_at, request_id, invite_digest)
                VALUES (?, 'issue_invite', ?, ?, ?)
            """, (actor, created, request_id, invite_digest))
        return {"code": code, "expiresAt": expires, "uses": 1}

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
