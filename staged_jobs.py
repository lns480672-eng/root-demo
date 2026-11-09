"""Account-owned staged generation with fenced Git checkpoints."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid

from accounts import ApiError

MAX_ATTEMPTS = 8
LEASE_SECONDS = 90
RETAIN_SECONDS = 48 * 60 * 60
ZERO = "0" * 40
IDENTIFIER = re.compile(r"[a-f0-9]{32}")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def filename(stage):
    return "app.html" if stage == "assemble" else stage + ".json"


class StagedJobs:
    def __init__(self, store, budget, runs, blueprint, *, clock=time.time, record=None,
                 max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS, retain_seconds=RETAIN_SECONDS):
        self.store = store
        self.budget = budget
        self.root = Path(runs) / "staged-repositories"
        self.blueprint = blueprint
        self.clock = clock
        self.record = record
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.retain_seconds = retain_seconds
        self.stages = blueprint.STAGES
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS staged_jobs (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES accounts(id),
                    request_id TEXT NOT NULL, requirement TEXT NOT NULL, next_stage INTEGER NOT NULL DEFAULT 0,
                    head TEXT NOT NULL, fence INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                    expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued', issue TEXT,
                    UNIQUE(owner, request_id)
                );
                CREATE INDEX IF NOT EXISTS staged_jobs_owner ON staged_jobs(owner, expires);
                CREATE TABLE IF NOT EXISTS staged_dispatches (
                    id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES staged_jobs(id),
                    stage INTEGER NOT NULL, fence INTEGER NOT NULL
                );
            """)

    @staticmethod
    def unavailable():
        return ApiError(404, "staged_unavailable", "此生成任务不可用。")

    def row(self, db, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise self.unavailable()
        row = db.execute("SELECT * FROM staged_jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise self.unavailable()
        return row

    def owned(self, db, token, marker, identifier):
        owner = self.store.session(db, token, marker)["id"]
        row = self.row(db, identifier)
        if row["owner"] != owner:
            raise self.unavailable()
        return row

    def latest(self, db, owner):
        return db.execute("""
            SELECT * FROM staged_jobs WHERE owner=? AND expires>?
            AND state NOT IN ('published','exhausted')
            ORDER BY expires DESC LIMIT 1
        """, (owner, self.clock())).fetchone()

    def view(self, row):
        state = row["state"]
        if row["expires"] <= self.clock() and state != "published":
            state = "expired"
        elif state == "running" and row["lease_until"] <= self.clock():
            state = "interrupted"
        done = row["next_stage"]
        return {"id":row["id"], "requirement":row["requirement"], "state":state,
                "completed":list(self.stages[:done]), "stage":self.stages[done] if done < len(self.stages) else None,
                "issue":row["issue"], "resumable":state in ("failed", "interrupted")}

    def status(self, token, marker, identifier=None):
        with self.store.connection() as db:
            if identifier is not None:
                return self.view(self.owned(db, token, marker, identifier))
            owner = self.store.session(db, token, marker)["id"]
            row = self.latest(db, owner)
            return self.view(row) if row else None

    def create(self, token, marker, requirement, request_id):
        if (not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id)
                or not isinstance(requirement, str) or not 0 < len(requirement.strip()) <= 2000):
            raise ApiError(400, "staged_input", "生成请求编号或需求无效。")
        requirement = requirement.strip()
        with self.store.connection(write=True) as db:
            owner = self.store.session(db, token, marker)["id"]
            existing = db.execute("SELECT * FROM staged_jobs WHERE owner=? AND request_id=?",
                                  (owner, request_id)).fetchone()
            if existing is not None:
                if existing["requirement"] != requirement:
                    raise ApiError(409, "staged_request_changed", "同一请求编号的需求不能变更。")
                return self.view(existing)
            if self.latest(db, owner) is not None:
                raise ApiError(409, "staged_pending", "已有一项分阶段任务，请先继续该任务。")
            identifier = uuid.uuid4().hex
            db.execute("""
                INSERT INTO staged_jobs(id,owner,request_id,requirement,head,expires) VALUES (?,?,?,?,?,?)
            """, (identifier, owner, request_id, requirement, ZERO, self.clock() + self.retain_seconds))
            return self.view(self.row(db, identifier))

    def repo(self, row):
        return self.root / (row["id"] + ".git")

    def git(self, row, *arguments, content=None, check=True):
        env = {"PATH":os.environ.get("PATH", os.defpath), "GIT_CONFIG_GLOBAL":os.devnull,
               "GIT_CONFIG_NOSYSTEM":"1", "GIT_AUTHOR_NAME":"ROOT", "GIT_AUTHOR_EMAIL":"root@localhost",
               "GIT_COMMITTER_NAME":"ROOT", "GIT_COMMITTER_EMAIL":"root@localhost"}
        result = subprocess.run(["git", "--git-dir", str(self.repo(row)), "-c", "core.hooksPath=/dev/null",
                                 *arguments], env=env, input=content, capture_output=True, timeout=10, check=check)
        return result.stdout if result.returncode == 0 else b""

    @staticmethod
    def ref(row):
        return f'refs/tasks/{row["id"]}/f{row["fence"]}'

    def tip(self, row):
        return self.git(row, "rev-parse", "--verify", self.ref(row), check=False).decode().strip() or ZERO

    def read(self, row, oid, name):
        return self.git(row, "show", oid + ":" + name)

    def files(self, row):
        return {filename(stage): self.read(row, row["head"], filename(stage))
                for stage in self.stages[:row["next_stage"]]}

    def manifest(self, row, stage, files, content):
        return {"task":row["id"], "owner":row["owner"], "fence":row["fence"],
                "stage":stage, "parent":row["head"], "sha256":digest(content),
                "depends":{name:digest(data) for name,data in files.items()}}

    def advance_row(self, db, row, oid):
        next_stage = row["next_stage"] + 1
        complete = next_stage == len(self.stages)
        db.execute("UPDATE staged_jobs SET next_stage=?,head=?,state=?,lease_until=? WHERE id=?",
                   (next_stage, oid, "complete" if complete else "running",
                    0 if complete else self.clock() + self.lease_seconds, row["id"]))

    def recover(self, db, row):
        tip = self.tip(row)
        if tip in (ZERO, row["head"]):
            return
        stage = self.stages[row["next_stage"]]
        prior = self.files(row)
        name = filename(stage)
        content = self.read(row, tip, name)
        self.blueprint.validate(stage, content, prior)
        parent = [] if row["head"] == ZERO else [row["head"]]
        parents = self.git(row, "rev-list", "--parents", "-n", "1", tip).decode().split()[1:]
        actual = json.loads(self.read(row, tip, "manifest.json"))
        if (parents != parent or actual != self.manifest(row, stage, prior, content)
                or any(self.read(row, tip, key) != value for key, value in prior.items())
                or set(self.git(row, "ls-tree", "--name-only", tip).decode().splitlines())
                != set(prior) | {name, "manifest.json"}):
            raise ValueError("unexpected staged checkpoint")
        self.advance_row(db, row, tip)

    def claim(self, token, marker, identifier, *, resume=True):
        with self.store.connection(write=True) as db:
            row = self.owned(db, token, marker, identifier)
            state = self.view(row)["state"]
            if state not in (("queued", "failed", "interrupted") if resume else ("queued",)):
                return None
            if row["fence"]:
                self.recover(db, row)
                row = self.row(db, identifier)
                if row["state"] == "complete":
                    return None
            else:
                self.repo(row).mkdir(parents=True, mode=0o700, exist_ok=True)
                self.git(row, "init", "--bare", "--object-format=sha1", "--template=")
            fence = row["fence"] + 1
            db.execute("UPDATE staged_jobs SET state='running',fence=?,lease_until=?,issue=NULL WHERE id=?",
                       (fence, self.clock() + self.lease_seconds, identifier))
            return fence

    def worker(self, db, identifier, fence, expected):
        row = self.row(db, identifier)
        now = self.clock()
        if (row["state"] != "running" or row["fence"] != fence or row["next_stage"] != expected
                or row["lease_until"] <= now or row["expires"] <= now):
            raise ApiError(409, "staged_stale", "任务执行权已失效。")
        return row

    def dispatch(self, identifier, fence, index, dispatch_id):
        with self.store.connection(write=True) as db:
            row = self.worker(db, identifier, fence, index)
            attempts = db.execute("SELECT count(*) FROM staged_dispatches WHERE task=?", (identifier,)).fetchone()[0]
            if attempts >= self.max_attempts:
                raise ApiError(429, "staged_exhausted", "此任务已达到模型调用上限。")
            self.budget.reserve_generation(db, row["owner"])
            db.execute("INSERT INTO staged_dispatches VALUES (?,?,?,?)", (dispatch_id, identifier, index, fence))

    def checkpoint(self, identifier, fence, index, stage, content, prior):
        self.blueprint.validate(stage, content, prior)
        with self.store.connection(write=True) as db:
            row = self.worker(db, identifier, fence, index)
            files = {**prior, filename(stage):content,
                     "manifest.json":encoded(self.manifest(row, stage, prior, content))}
            entries = []
            for name, value in sorted(files.items()):
                blob = self.git(row, "hash-object", "-w", "--stdin", content=value).decode().strip()
                entries.append(f"100644 blob {blob}\t{name}\n")
            tree = self.git(row, "mktree", content="".join(entries).encode()).decode().strip()
            parent = [] if row["head"] == ZERO else ["-p", row["head"]]
            oid = self.git(row, "commit-tree", tree, *parent, "-m", stage).decode().strip()
            previous = self.tip(row)
            if previous not in (ZERO, row["head"]):
                raise ValueError("staged reference changed")
            self.git(row, "update-ref", self.ref(row), oid, previous)
            self.advance_row(db, row, oid)

    def run(self, identifier, fence, provider):
        index = 0
        try:
            with self.store.connection() as db:
                index = self.row(db, identifier)["next_stage"]
            for index in range(index, len(self.stages)):
                with self.store.connection() as db:
                    row = self.worker(db, identifier, fence, index)
                stage = self.stages[index]
                prior = self.files(row)
                if stage == "assemble":
                    content = self.blueprint.assemble(prior)
                else:
                    dispatch_id = uuid.uuid4().hex

                    def record(receipt):
                        if self.record is not None:
                            self.record(identifier, dispatch_id, receipt)

                    content = provider(stage, row["requirement"], prior,
                                       lambda: self.dispatch(identifier, fence, index, dispatch_id), record)
                self.checkpoint(identifier, fence, index, stage, content, prior)
        except (ApiError, OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as error:
            issue = uuid.uuid4().hex[:12]
            exhausted = isinstance(error, ApiError) and error.code == "staged_exhausted"
            with self.store.connection(write=True) as db:
                db.execute("""UPDATE staged_jobs SET state=?,lease_until=0,issue=?
                              WHERE id=? AND fence=? AND state='running' AND lease_until>?""",
                           ("exhausted" if exhausted else "failed", issue, identifier, fence, self.clock()))

    def artifact(self, token, marker, identifier):
        with self.store.connection() as db:
            row = self.owned(db, token, marker, identifier)
            if self.view(row)["state"] not in ("complete", "published"):
                raise ApiError(409, "staged_not_ready", "此任务尚未生成可预览的候选。")
        commit = row["head"]
        content = self.read(row, commit, "app.html")
        manifest = json.loads(self.read(row, commit, "manifest.json"))
        identity = {"taskId":identifier, "commit":commit, "sha256":digest(content)}
        if (manifest.get("task") != identifier or manifest.get("owner") != row["owner"]
                or manifest.get("stage") != "assemble" or manifest.get("sha256") != identity["sha256"]):
            raise ApiError(503, "staged_artifact_invalid", "生成候选无法校验。")
        return identity, content
