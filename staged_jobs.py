"""Account-owned staged generation with fenced Git checkpoints."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid

from accounts import ApiError, pack_entries

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
        self.runs = Path(runs)
        self.root = self.runs / "staged-repositories"
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
                CREATE TABLE IF NOT EXISTS staged_candidates (
                    task TEXT PRIMARY KEY REFERENCES staged_jobs(id), tool_id TEXT NOT NULL UNIQUE,
                    commit_id TEXT NOT NULL, digest TEXT NOT NULL, nonce TEXT NOT NULL
                );
            """)
        with store.connection(write=True) as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(staged_jobs)")}
            if "updated_at" not in columns:
                db.execute("ALTER TABLE staged_jobs ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
            if "failure" not in columns:
                db.execute("ALTER TABLE staged_jobs ADD COLUMN failure TEXT")
            if "plan" not in columns:
                db.execute("ALTER TABLE staged_jobs ADD COLUMN plan TEXT")

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
            AND state NOT IN ('published','exhausted','unsupported')
            ORDER BY updated_at DESC, expires DESC LIMIT 1
        """, (owner, self.clock())).fetchone()

    def steps(self, row):
        if row["plan"]:
            return tuple(json.loads(row["plan"]))
        return self.stages if row["next_stage"] else ("spec",)

    def view(self, row, db=None):
        state = row["state"]
        if row["expires"] <= self.clock() and state != "published":
            state = "expired"
        elif state == "running" and row["lease_until"] <= self.clock():
            state = "interrupted"
        done = row["next_stage"]
        steps = self.steps(row)
        result = {"id":row["id"], "requirement":row["requirement"], "state":state,
                  "steps":list(steps), "completed":list(steps[:done]), "stage":steps[done] if done < len(steps) else None,
                  "issue":row["issue"], "reason":row["failure"],
                  "resumable":state in ("queued", "failed", "interrupted")}
        if state == "published" and db is not None:
            result["toolId"] = self.candidate_record(db, row["id"])["tool_id"]
        return result

    def status(self, token, marker, identifier=None):
        with self.store.connection() as db:
            if identifier is not None:
                return self.view(self.owned(db, token, marker, identifier), db)
            owner = self.store.session(db, token, marker)["id"]
            row = self.latest(db, owner)
            return self.view(row) if row else None

    def list(self, token, marker):
        with self.store.connection() as db:
            owner = self.store.session(db, token, marker)["id"]
            rows = db.execute("""
                SELECT * FROM staged_jobs WHERE owner=? AND state!='published'
                ORDER BY updated_at DESC, expires DESC LIMIT 30
            """, (owner,)).fetchall()
            return [self.view(row) for row in rows]

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
                return self.view(existing, db)
            identifier = uuid.uuid4().hex
            db.execute("""
                INSERT INTO staged_jobs(id,owner,request_id,requirement,head,expires,updated_at) VALUES (?,?,?,?,?,?,?)
            """, (identifier, owner, request_id, requirement, ZERO, self.clock() + self.retain_seconds, self.clock()))
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
                for stage in self.steps(row)[:row["next_stage"]]}

    def manifest(self, row, stage, files, content):
        return {"task":row["id"], "owner":row["owner"], "fence":row["fence"],
                "stage":stage, "parent":row["head"], "sha256":digest(content),
                "depends":{name:digest(data) for name,data in files.items()}}

    def advance_row(self, db, row, oid, stage, content):
        next_stage = row["next_stage"] + 1
        steps = self.blueprint.stage_plan(content) if stage == "spec" else self.steps(row)
        complete = stage == "assemble"
        db.execute("UPDATE staged_jobs SET next_stage=?,head=?,state=?,lease_until=?,updated_at=?,plan=? WHERE id=?",
                   (next_stage, oid, "complete" if complete else "running",
                    0 if complete else self.clock() + self.lease_seconds, self.clock(), json.dumps(steps), row["id"]))

    def recover(self, db, row):
        tip = self.tip(row)
        if tip in (ZERO, row["head"]):
            return
        stage = self.steps(row)[row["next_stage"]]
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
        self.advance_row(db, row, tip, stage, content)

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
            db.execute("UPDATE staged_jobs SET state='running',fence=?,lease_until=?,issue=NULL,failure=NULL,updated_at=? WHERE id=?",
                       (fence, self.clock() + self.lease_seconds, self.clock(), identifier))
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
            self.budget.reserve_generation(db, row["owner"], dispatch_id=dispatch_id)
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
            self.advance_row(db, row, oid, stage, content)

    def run(self, identifier, fence, provider):
        index = 0
        try:
            with self.store.connection() as db:
                index = self.row(db, identifier)["next_stage"]
            while True:
                with self.store.connection() as db:
                    row = self.worker(db, identifier, fence, index)
                stage = self.steps(row)[index]
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
                if stage == "assemble":
                    return
                index += 1
        except (ApiError, OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as error:
            issue = uuid.uuid4().hex[:12]
            code = error.code if isinstance(error, ApiError) else ""
            state = {"staged_exhausted":"exhausted", "staged_unsupported":"unsupported"}.get(code, "failed")
            with self.store.connection(write=True) as db:
                db.execute("""UPDATE staged_jobs SET state=?,lease_until=0,issue=?,failure=?,updated_at=?
                              WHERE id=? AND fence=? AND state='running' AND lease_until>?""",
                           (state, issue, code or "staged_storage", self.clock(), identifier, fence, self.clock()))

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

    @staticmethod
    def candidate_record(db, identifier):
        return db.execute("SELECT * FROM staged_candidates WHERE task=?", (identifier,)).fetchone()

    def candidate(self, token, marker, identifier):
        identity, html = self.artifact(token, marker, identifier)
        with self.store.connection(write=True) as db:
            row = self.owned(db, token, marker, identifier)
            if self.view(row)["state"] != "complete" or identity["commit"] != row["head"]:
                raise ApiError(409, "staged_not_ready", "此任务尚未生成可预览的候选，或已发布。")
            candidate = self.candidate_record(db, identifier)
            if candidate is None:
                tool_id = Path(tempfile.mkdtemp(prefix="run-", dir=self.runs)).name
                db.execute("INSERT INTO staged_candidates VALUES (?,?,?,?,?)",
                           (identifier, tool_id, identity["commit"], identity["sha256"], uuid.uuid4().hex))
                candidate = self.candidate_record(db, identifier)
            if candidate["commit_id"] != identity["commit"] or candidate["digest"] != identity["sha256"]:
                raise ApiError(409, "staged_candidate_changed", "候选已变化，请重新准备预览。")
            result = {"id":candidate["tool_id"], "html":html.decode(), "entries":[], "revision":0, "version":1,
                      "staged":{**identity, "nonce":candidate["nonce"]}}
            if self.permits_backup(row):
                result["stagedBackup"] = True
            return result

    def permits_backup(self, row):
        return self.blueprint.permits_backup(self.files(row))

    def published_backup(self, tool_id, html):
        with self.store.connection() as db:
            row = db.execute("""
                SELECT job.* FROM staged_jobs job JOIN staged_candidates c ON c.task=job.id
                WHERE c.tool_id=? AND c.commit_id=job.head AND c.digest=? AND job.state='published'
            """, (tool_id, digest(html.encode()))).fetchone()
        if row is None:
            return False
        try:
            return self.permits_backup(row)
        except (ValueError, OSError, subprocess.SubprocessError):
            return False

    def publish(self, token, marker, identifier, data):
        identity, html = self.artifact(token, marker, identifier)
        with self.store.connection(write=True) as db:
            row = self.owned(db, token, marker, identifier)
            candidate = self.candidate_record(db, identifier)
            if (candidate is None or candidate["commit_id"] != row["head"]
                    or candidate["tool_id"] != data.get("toolId") or candidate["nonce"] != data.get("nonce")
                    or candidate["digest"] != identity["sha256"] or identity["sha256"] != data.get("sha256")
                    or identity["commit"] != data.get("commit")):
                raise ApiError(409, "staged_candidate_changed", "候选和预览快照不匹配，请重新准备预览。")
            state = self.view(row)["state"]
            if state not in ("complete", "published"):
                raise ApiError(409, "staged_not_ready", "此任务尚未生成可发布的候选。")
            owner = {"id":row["owner"]}
            tool_id = candidate["tool_id"]
            if state == "complete":
                spec = json.loads(self.read(row, row["head"], "spec.json"))
                title = " ".join(spec["title"].split())[:48]
                run = self.runs / tool_id
                path = run / "app.pending"
                try:
                    path.write_bytes(html)
                    path.replace(run / "app.html")
                except OSError:
                    raise ApiError(503, "staged_save_failed", "候选尚未保存，可重新检查预览后继续。") from None
                self.store.insert_draft(db, row["owner"], tool_id, title, pack_entries([]), model=True)
                tool = self.store.owned_tool(db, owner, tool_id, published=False)
                self.store.commit_initial(db, tool, data.get("entries"), self.budget.record_generation_success)
                db.execute("UPDATE staged_jobs SET state='published' WHERE id=?", (identifier,))
            return self.store.owned_tool(db, owner, tool_id)
