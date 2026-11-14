"""Server defaults, persisted administrator overrides, and per-run settings."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import json
import os

from accounts import ApiError


@dataclass(frozen=True)
class GenerationSettings:
    sourceBytes: int = 24 * 1024 * 100
    revisionContextBytes: int = 40 * 1024 * 100
    outputTokens: int = 384 * 1024
    timeoutSeconds: int = 300


MAXIMUMS = GenerationSettings(8 * 1024 * 1024, 16 * 1024 * 1024, 384 * 1024, 1800)
# JSON can escape a single source byte as six ASCII bytes.
MAX_JSON_BYTES = 6 * MAXIMUMS.sourceBytes + 64 * 1024
MAX_CONTEXT_BYTES = MAXIMUMS.revisionContextBytes
_current = ContextVar("generation_settings", default=None)


def validate(values):
    limits = asdict(MAXIMUMS)
    if (not isinstance(values, dict) or values.keys() != limits.keys()
            or any(type(values[key]) is not int or not 1 <= values[key] <= limit
                   for key, limit in limits.items())
            or values["revisionContextBytes"] < values["sourceBytes"]):
        raise ValueError("生成配置必须在允许范围内，且续改上下文容量不能小于源码容量。")
    return GenerationSettings(**values)


def from_environment():
    fields = {
        "sourceBytes": "ROOT_AGENT_MAX_SOURCE_BYTES",
        "revisionContextBytes": "ROOT_AGENT_MAX_REVISION_CONTEXT_BYTES",
        "outputTokens": "ROOT_AGENT_MAX_OUTPUT_TOKENS",
        "timeoutSeconds": "ROOT_AGENT_TIMEOUT_SECONDS",
    }
    defaults = asdict(GenerationSettings())
    try:
        return validate({field: int(os.environ.get(env, defaults[field])) for field, env in fields.items()})
    except ValueError as error:
        raise ValueError("ROOT_AGENT 环境配置无效：" + str(error)) from None


def current():
    return _current.get() or GenerationSettings()


def extend_lease(seconds):
    settings = _current.get()
    return seconds + settings.timeoutSeconds if settings else seconds


@contextmanager
def activate(settings):
    token = _current.set(settings)
    try:
        yield
    finally:
        _current.reset(token)


class GenerationSettingsStore:
    def __init__(self, store, defaults):
        self.store = store
        self.defaults = defaults
        with store.connection(write=True) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS generation_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    settings TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)

    def read(self, db):
        row = db.execute("SELECT settings FROM generation_settings WHERE singleton=1").fetchone()
        return validate(json.loads(row["settings"])) if row else self.defaults

    def response(self, db):
        return {"settings": asdict(self.read(db)), "defaults": asdict(self.defaults),
                "maximums": asdict(MAXIMUMS)}

    def update(self, db, data, admin):
        if data == {"reset": True} and data["reset"] is True:
            db.execute("DELETE FROM generation_settings WHERE singleton=1")
        elif data.keys() == {"settings"}:
            try:
                settings = validate(data["settings"])
            except ValueError as error:
                raise ApiError(400, "invalid_generation_settings", str(error)) from None
            db.execute("""
                INSERT INTO generation_settings VALUES (1, ?, ?, datetime('now'))
                ON CONFLICT(singleton) DO UPDATE SET settings=excluded.settings,
                    updated_by=excluded.updated_by, updated_at=excluded.updated_at
            """, (json.dumps(asdict(settings)), admin))
        else:
            raise ApiError(400, "invalid_generation_settings", "请求格式无效。")
        return self.response(db)
