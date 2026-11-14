"""Account-owned staged generation with fenced Git checkpoints."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid

from accounts import ApiError, TABLE_STORAGE_BYTES, TOOL_STORAGE_BYTES, pack_entries
from runtime_verify import RuntimeVerificationError, check_script
from staged_revision import REVISION_GUIDANCE
import staged_record_code

MAX_ATTEMPTS = 8
MAX_CLARIFICATION_ROUNDS = 2
RUNTIME_PROOF_VERSION = 8
REPAIRABLE_REVISION_FAILURES = {"invalid_staged_result", "revision_patch_invalid", "staged_runtime_failed"}
REPAIRABLE_RUNTIME_ERRORS = frozenset({
    "script_exception", "document_not_ready", "record_initialization", "record_field_behavior", "record_visibility", "website_tabs", "website_style",
})
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
                 max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS, retain_seconds=RETAIN_SECONDS,
                 revision=None, runtime_verifier=None, coding_provider=None):
        self.store = store
        self.budget = budget
        self.runs = Path(runs)
        self.root = self.runs / "staged-repositories"
        self.blueprint = blueprint
        self.revision = revision
        self.runtime_verifier = runtime_verifier
        self.clock = clock
        self.record = record
        self.coding_provider = coding_provider
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
                CREATE TABLE IF NOT EXISTS staged_revisions (
                    task TEXT PRIMARY KEY REFERENCES staged_jobs(id), tool_id TEXT NOT NULL REFERENCES tools(id),
                    base_version INTEGER NOT NULL, source_digest TEXT NOT NULL, context TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS staged_revision_answers (
                    task TEXT NOT NULL REFERENCES staged_jobs(id), request_id TEXT NOT NULL,
                    question_id TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, selection TEXT,
                    PRIMARY KEY (task, request_id), UNIQUE (task, question_id)
                );
                CREATE TABLE IF NOT EXISTS staged_revision_repairs (
                    task TEXT NOT NULL REFERENCES staged_jobs(id), issue TEXT NOT NULL,
                    request_id TEXT NOT NULL, reason TEXT NOT NULL, supplement TEXT NOT NULL,
                    PRIMARY KEY (task, issue), UNIQUE (task, request_id)
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
            if "repo_id" not in columns:
                db.execute("ALTER TABLE staged_jobs ADD COLUMN repo_id TEXT")
            if "category" not in columns:
                db.execute("ALTER TABLE staged_jobs ADD COLUMN category TEXT NOT NULL DEFAULT 'auto'")
            if "title" not in columns:
                db.execute("ALTER TABLE staged_jobs ADD COLUMN title TEXT")
            if "generated_title" not in columns:
                db.execute("ALTER TABLE staged_jobs ADD COLUMN generated_title TEXT")
            candidate_columns = {row["name"] for row in db.execute("PRAGMA table_info(staged_candidates)")}
            if "runtime_proof" not in candidate_columns:
                db.execute("ALTER TABLE staged_candidates ADD COLUMN runtime_proof TEXT")
            if "confirmation" not in candidate_columns:
                db.execute("ALTER TABLE staged_candidates ADD COLUMN confirmation TEXT")

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
            AND state NOT IN ('published','exhausted','unsupported','cancelled')
            ORDER BY updated_at DESC, expires DESC LIMIT 1
        """, (owner, self.clock())).fetchone()

    def steps(self, row):
        if row["plan"]:
            return tuple(json.loads(row["plan"]))
        return self.stages if row["next_stage"] else ("spec",)

    def view(self, row, db=None):
        state = row["state"]
        if row["expires"] <= self.clock() and state not in ("published", "cancelled"):
            state = "expired"
        elif state == "running" and row["lease_until"] <= self.clock():
            state = "interrupted"
        done = row["next_stage"]
        steps = self.steps(row)
        result = {"id":row["id"], "requirement":row["requirement"], "state":state,
                  "steps":list(steps), "completed":list(steps[:done]), "stage":steps[done] if done < len(steps) else None,
                  "issue":row["issue"], "reason":row["failure"],
                  "resumable":state in ("queued", "failed", "interrupted")
                  and row["failure"] != "staged_runtime_failed"}
        if row["title"]:
            result.update(title=row["title"], titleSource="draft")
        elif row["generated_title"]:
            result.update(title=row["generated_title"], titleSource="spec")
        guidance = REVISION_GUIDANCE.get(row["failure"])
        if state == "unsupported" and guidance:
            result["guidance"] = guidance
        if state == "unsupported" and steps == ("revision-plan",) and done:
            decision = json.loads(self.read(row, row["head"], "revision-plan.json"))
            if decision["status"] == "clarify":
                result["guidance"] = decision["question"]
            else:
                result["guidance"] = decision["reason"] + " 旧版仍可用，未扣除个人生成次数。"
        if db is not None:
            answers = self.revision_answers(db, row["id"])
            if state == "clarifying":
                decision = json.loads(self.read(row, row["head"], "revision-plan.json"))
                rounds = sum(answer["selection"] is None for answer in answers)
                repeated = self.repeated_question(decision["question"], answers)
                limited = rounds >= MAX_CLARIFICATION_ROUNDS or repeated
                result["clarification"] = {
                    "id":row["head"], "round":min(rounds + 1, MAX_CLARIFICATION_ROUNDS), "limited":limited,
                    "question":None if limited else decision["question"],
                    "field":decision.get("field"), "choices":decision.get("choices", []),
                }
            if answers or "clarification" in result:
                result.setdefault("clarification", {})["answers"] = [
                    {"question":item["question"], "answer":item["answer"]} for item in answers
                ]
        if row["category"] != "auto":
            result["category"] = row["category"]
        if db is not None:
            revision = self.revision_record(db, row["id"])
            if revision:
                result.update(baseToolId=revision["tool_id"], baseVersion=revision["base_version"])
                repairs = self.revision_repairs(db, row["id"])
                if repairs:
                    result["repairs"] = [{"issue": item["issue"], "reason": item["reason"],
                                          "supplement": item["supplement"]} for item in repairs]
                if state == "failed" and row["failure"] in REPAIRABLE_REVISION_FAILURES:
                    attempts = db.execute("SELECT count(*) FROM staged_dispatches WHERE task=?", (row["id"],)).fetchone()[0]
                    result["canSupplement"] = attempts < self.max_attempts
                    result["canRepair"] = result["canSupplement"] and not any(not item["supplement"] for item in repairs)
            if state == "published":
                result["toolId"] = revision["tool_id"] if revision else self.candidate_record(db, row["id"])["tool_id"]
        return result

    def status(self, token, marker, identifier=None):
        with self.store.connection() as db:
            if identifier is not None:
                return self.view(self.owned(db, token, marker, identifier), db)
            owner = self.store.session(db, token, marker)["id"]
            row = self.latest(db, owner)
            return self.view(row, db) if row else None

    def list(self, token, marker, *, history=False):
        with self.store.connection() as db:
            owner = self.store.session(db, token, marker)["id"]
            rows = db.execute("""
                SELECT * FROM staged_jobs WHERE owner=? AND state!='published'
                AND (expires>? AND state IN ('queued','running','complete','clarifying'))=?
                AND (?=0 OR NOT EXISTS (SELECT 1 FROM staged_revisions WHERE task=staged_jobs.id))
                ORDER BY updated_at DESC, expires DESC LIMIT 30
            """, (owner, self.clock(), not history, history)).fetchall()
            return [self.view(row, db) for row in rows]

    def list_tool_revisions(self, token, marker, identifier):
        with self.store.connection() as db:
            owner = self.store.session(db, token, marker)
            self.store.owned_tool(db, owner, identifier)
            rows = db.execute("""
                SELECT job.* FROM staged_jobs job JOIN staged_revisions revision ON revision.task=job.id
                WHERE job.owner=? AND revision.tool_id=?
                ORDER BY job.rowid DESC LIMIT 30
            """, (owner["id"], identifier)).fetchall()
            return [self.view(row, db) for row in reversed(rows)]

    def create(self, token, marker, requirement, request_id, *, tool_id=None, version=None, category="auto", title=None):
        if (not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id)
                or not isinstance(requirement, str) or not 0 < len(requirement.strip()) <= 2000):
            raise ApiError(400, "staged_input", "生成请求编号或需求无效。")
        if category != "auto" and (tool_id or category not in self.blueprint.CATEGORIES):
            raise ApiError(400, "staged_category", "请选择有效的新建类别。已有工具的修改沿用原类别。")
        if title is not None:
            if tool_id or not isinstance(title, str) or not 0 < len(title.strip()) <= 40 or any(c in title.strip() for c in "\r\n"):
                raise ApiError(400, "invalid_draft_title", "工具名称须为 1–40 字的单行名称。")
            title = title.strip()
        requirement = requirement.strip()
        with self.store.connection(write=True) as db:
            owner = self.store.session(db, token, marker)["id"]
            existing = db.execute("SELECT * FROM staged_jobs WHERE owner=? AND request_id=?",
                                  (owner, request_id)).fetchone()
            if existing is not None:
                revision = self.revision_record(db, existing["id"])
                if (existing["requirement"] != requirement or existing["category"] != category or existing["title"] != title or bool(revision) != bool(tool_id)
                        or (revision and (revision["tool_id"], revision["base_version"]) != (tool_id, version))):
                    raise ApiError(409, "staged_request_changed", "同一请求编号的需求或版本不能变更。")
                return self.view(existing, db)
            if tool_id and db.execute("""
                SELECT 1 FROM staged_jobs
                WHERE owner=? AND expires>? AND (state='queued' OR (state='running' AND lease_until>?))
                LIMIT 1
            """, (owner, self.clock(), self.clock())).fetchone():
                raise ApiError(409, "staged_busy", "已有生成或修改正在处理，请结束后再提交。")
            self.budget.require_dispatch_enabled(db)
            identifier = uuid.uuid4().hex
            repo_id, head = None, ZERO
            if tool_id:
                row = self.store.owned_tool(db, {"id":owner}, tool_id)
                self.store.require_version(version, row["version"])
                repo_id, head, source_digest, context = self.revision_source(db, row)
            steps = json.dumps(["revision-plan"]) if tool_id else None
            db.execute("""
                INSERT INTO staged_jobs(id,owner,request_id,requirement,head,expires,updated_at,repo_id,category,plan,title)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (identifier, owner, request_id, requirement, head,
                  self.clock() + self.retain_seconds, self.clock(), repo_id, category, steps, title))
            if tool_id:
                db.execute("INSERT INTO staged_revisions VALUES (?,?,?,?,?)",
                           (identifier, tool_id, version, source_digest, encoded(context).decode()))
            return self.view(self.row(db, identifier), db)

    @staticmethod
    def revision_record(db, identifier):
        return db.execute("SELECT * FROM staged_revisions WHERE task=?", (identifier,)).fetchone()

    def revision_context(self, identifier):
        with self.store.connection() as db:
            row = self.revision_record(db, identifier)
            if not row:
                return None
            task = self.row(db, identifier)
            context = self.revision_plan_context(task, row, self.revision_answers(db, identifier))
            repairs = self.revision_repairs(db, identifier)
            if repairs:
                context["repair_feedback"] = [{"reason": item["reason"], "supplement": item["supplement"]}
                                              for item in repairs]
            return context

    def revision_plan_context(self, task, revision, answers):
        context = json.loads(revision["context"])
        if context["kind"] == "record_code" and answers and answers[-1]["selection"] == "field":
            answer = answers[-1]
            question = json.loads(self.read(task, answer["question_id"], "revision-plan.json"))
            context["selected_field"] = {"label": question["field"], "type": answer["answer"]}
        if answers:
            context["clarification"] = [{"question":item["question"], "answer":item["answer"],
                                         "selection":item["selection"]} for item in answers]
        return context

    @staticmethod
    def revision_repairs(db, identifier):
        return db.execute("SELECT * FROM staged_revision_repairs WHERE task=? ORDER BY rowid",
                          (identifier,)).fetchall()

    def repair_revision(self, token, marker, identifier, data):
        request_id, issue, supplement = data.get("requestId"), data.get("issue"), data.get("supplement", "")
        if (not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id)
                or not isinstance(issue, str) or not re.fullmatch(r"[a-f0-9]{12}", issue)
                or not isinstance(supplement, str) or len(supplement.strip()) > 400):
            raise ApiError(400, "staged_input", "修复请求无效。")
        supplement = supplement.strip()
        with self.store.connection(write=True) as db:
            row = self.owned(db, token, marker, identifier)
            revision = self.revision_record(db, identifier)
            repairs = self.revision_repairs(db, identifier)
            previous = next((item for item in repairs if item["request_id"] == request_id), None)
            if previous:
                if (previous["issue"], previous["supplement"]) != (issue, supplement):
                    raise ApiError(409, "staged_request_changed", "同一修复请求的内容不能变更。")
                return
            if (not revision or self.view(row)["state"] != "failed" or row["issue"] != issue
                    or row["failure"] not in REPAIRABLE_REVISION_FAILURES):
                raise ApiError(409, "revision_repair_unavailable", "这次修改已变化，请重新查看对话。")
            if not supplement and any(not item["supplement"] for item in repairs):
                raise ApiError(409, "revision_repair_unavailable", "本次 AI 修复机会已用完，可以补充要求。")
            current = self.store.owned_tool(db, {"id": row["owner"]}, revision["tool_id"])
            self.store.require_version(revision["base_version"], current["version"])
            self.check_table_schema(revision, current)
            attempts = db.execute("SELECT count(*) FROM staged_dispatches WHERE task=?", (identifier,)).fetchone()[0]
            if attempts >= self.max_attempts:
                raise ApiError(429, "staged_exhausted", "此任务已达到模型调用上限。")
            self.budget.require_dispatch_enabled(db)
            db.execute("INSERT INTO staged_revision_repairs VALUES (?,?,?,?,?)",
                       (identifier, issue, request_id, row["failure"], supplement))
            db.execute("DELETE FROM staged_candidates WHERE task=?", (identifier,))
            db.execute("""UPDATE staged_jobs SET state='queued',next_stage=0,plan=?,fence=fence+1,
                          lease_until=0,issue=NULL,failure=NULL,updated_at=? WHERE id=?""",
                       (json.dumps(["revision-plan"]), self.clock(), identifier))

    @staticmethod
    def revision_answers(db, identifier):
        return db.execute("SELECT * FROM staged_revision_answers WHERE task=? ORDER BY rowid", (identifier,)).fetchall()

    @staticmethod
    def repeated_question(question, answers):
        normalized = lambda value: re.sub(r"[\W_]+", "", value).casefold()
        return any(normalized(item["question"]) == normalized(question) for item in answers)

    def answer_revision(self, token, marker, identifier, data):
        request_id, question_id = data.get("requestId"), data.get("questionId")
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id) or not isinstance(question_id, str):
            raise ApiError(400, "revision_answer_invalid", "补答请求编号无效。")
        answer, selection = data.get("answer"), data.get("selection")
        if (not isinstance(answer, str) or not 0 < len(answer.strip()) <= 400
                or selection not in (None, "field", "operation")):
            raise ApiError(400, "revision_answer_invalid", "请填写补答或选择有效选项。")
        answer = answer.strip()
        with self.store.connection(write=True) as db:
            row = self.owned(db, token, marker, identifier)
            revision = self.revision_record(db, identifier)
            if not revision:
                raise ApiError(409, "revision_question_changed", "这个任务没有待补答的修改。")
            previous = db.execute("SELECT * FROM staged_revision_answers WHERE task=? AND request_id=?",
                                  (identifier, request_id)).fetchone()
            if previous:
                if (previous["question_id"], previous["answer"], previous["selection"]) != (question_id, answer, selection):
                    raise ApiError(409, "staged_request_changed", "同一补答编号的内容不能变更。")
                return False
            state = self.view(row)["state"]
            if state not in ("clarifying", "complete") or row["head"] != question_id:
                raise ApiError(409, "revision_question_changed", "这道问题已更新，请重新打开修改任务。")
            current = self.store.owned_tool(db, {"id":row["owner"]}, revision["tool_id"])
            self.store.require_version(revision["base_version"], current["version"])
            self.check_table_schema(revision, current)
            decision = json.loads(self.read(row, row["head"], "revision-plan.json"))
            operation = decision.get("operation", {})
            field = (operation.get("label") if state == "complete" and operation.get("kind") == "add_field" and not {"edits", "implementation"} & decision.keys()
                     else decision.get("field"))
            if state == "complete" and (selection != "field" or not field or answer == operation.get("type")):
                raise ApiError(409, "revision_question_changed", "此修改方案没有可调整的字段类型。")
            prompt = decision.get("question", "调整字段类型")
            answers = self.revision_answers(db, identifier)
            if selection == "field":
                if not field or answer not in ("date", "integer", "text"):
                    raise ApiError(400, "revision_answer_invalid", "请选择当前方案支持的字段类型。")
            elif selection == "operation":
                if answer not in decision.get("choices", []) or any(item["selection"] == "operation" for item in answers):
                    raise ApiError(400, "revision_answer_invalid", "请选择当前列出的改动。")
            elif (decision.get("choices") or
                  sum(item["selection"] is None for item in answers) >= MAX_CLARIFICATION_ROUNDS
                  or self.repeated_question(prompt, answers)):
                raise ApiError(409, "revision_clarification_limit", "请点选可用项、取消或改写需求。")
            db.execute("INSERT INTO staged_revision_answers VALUES (?,?,?,?,?,?)",
                       (identifier, request_id, question_id, prompt, answer, selection))
            if state == "complete":
                candidate = self.candidate_record(db, identifier)
                if candidate is None:
                    raise ApiError(409, "revision_question_changed", "请重新打开当前修改方案。")
                db.execute("DELETE FROM staged_candidates WHERE task=?", (identifier,))
            db.execute("UPDATE staged_jobs SET state='queued',next_stage=0,plan=?,failure=NULL,updated_at=? WHERE id=?",
                       (json.dumps(["revision-plan"]), self.clock(), identifier))
            return True

    def generation_category(self, identifier):
        with self.store.connection() as db:
            return self.row(db, identifier)["category"]

    @staticmethod
    def published_source(db, run_id):
        return db.execute("""
            SELECT job.*, c.digest FROM staged_jobs job JOIN staged_candidates c ON c.task=job.id
            WHERE c.tool_id=? AND c.commit_id=job.head AND job.state='published'
        """, (run_id,)).fetchone()

    def ensure_tool_history(self, db, tool):
        """Record each published version in its tool's permanent repository."""
        tool_id = tool["id"]
        db.execute("INSERT OR IGNORE INTO tool_versions VALUES (?,1,?,'首次生成',NULL)", (tool_id, tool_id))
        binding = db.execute("SELECT repo_id FROM tool_git_repositories WHERE tool_id=?", (tool_id,)).fetchone()
        versions = db.execute("""
            SELECT v.*, g.commit_id FROM tool_versions v LEFT JOIN tool_git_versions g USING (tool_id, version)
            WHERE v.tool_id=? ORDER BY v.version
        """, (tool_id,)).fetchall()
        try:
            if binding:
                repo_id = binding["repo_id"]
            else:
                source = next((job for version in versions
                               if (job := self.published_source(db, version["run_id"])) is not None), None)
                repo_id = (source["repo_id"] or source["id"]) if source is not None else tool_id
                db.execute("INSERT INTO tool_git_repositories VALUES (?, ?)", (tool_id, repo_id))
            repo = self.root / (repo_id + ".git")
            parent = None
            for version in versions:
                commit = version["commit_id"]
                if commit is None:
                    commit = self.record_version(db, repo, parent, version)
                    db.execute("INSERT INTO tool_git_versions VALUES (?, ?, ?)",
                               (tool_id, version["version"], commit))
                parent = commit
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise ApiError(503, "tool_git_unavailable", "工具版本仓库暂时无法保存或读取，请重试。") from error
        return repo_id, parent

    def record_version(self, db, repo, parent, version):
        run_id = version["run_id"]
        html = (self.runs / run_id / "app.html").read_bytes()
        repo.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not (repo / "HEAD").is_file():
            self.git_at(repo, "init", "--bare", "--object-format=sha1", "--template=")
        source = self.published_source(db, run_id)
        parents = ["-p", parent] if parent else []
        if source is not None:
            commit = source["head"]
            if digest(html) != source["digest"] or self.read(source, commit, "app.html") != html:
                raise ValueError("published source mismatch")
            source_repo = self.repo(source)
            if source_repo != repo:
                self.git_at(repo, "fetch", "--no-tags", "--no-write-fetch-head", str(source_repo.resolve()), commit)
            if not parent or self.ancestor(repo, parent, commit):
                self.keep_commit(repo, commit)
                return commit
            # Older builds could publish successive versions from different Git roots.
            # Preserve the original staged tree and make both histories reachable.
            parents += ["-p", commit]
            tree = self.git_at(repo, "rev-parse", commit + "^{tree}").decode().strip()
        else:
            blob = self.git_at(repo, "hash-object", "-w", "--stdin", content=html).decode().strip()
            tree = self.git_at(repo, "mktree", content=f"100644 blob {blob}\tapp.html\n".encode()).decode().strip()
        commit = self.git_at(repo, "commit-tree", tree, *parents,
                             "-m", f'Publish v{version["version"]}',
                             date=version["effective_at"] or 1).decode().strip()
        self.keep_commit(repo, commit)
        return commit

    def keep_commit(self, repo, commit):
        # These refs retain objects. SQLite alone marks a version as published.
        self.git_at(repo, "update-ref", "refs/workbench/objects/" + commit, commit)

    def ancestor(self, repo, parent, commit):
        try:
            self.git_at(repo, "merge-base", "--is-ancestor", parent, commit)
            return True
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
            return False

    def record_code_spec(self, db, tool):
        source = self.published_source(db, tool["run_id"])
        if source is not None:
            spec = self.blueprint.read_artifact(self.read(source, source["head"], "spec.json"))
            if staged_record_code.matches(spec):
                return spec
        return None

    def change_record(self, token, marker, identifier, data):
        with self.store.connection(write=True) as db:
            user = self.store.session(db, token, marker)
            tool = self.store.owned_tool(db, user, identifier)
            self.store.require_version(data.get("version"), tool["version"])
            spec = self.record_code_spec(db, tool)
            if spec is None:
                raise ApiError(409, "record_code_unavailable", "当前工具不支持逐条记录接口。")
            entries = dict(json.loads(tool["entries"]))
            records = json.loads(entries.get(staged_record_code.STORAGE_KEY, "[]"))
            if data.get("operation") == {"action": "list"}:
                return {"records": records, "revision": tool["revision"]}
            if type(data.get("revision")) is not int or data["revision"] != tool["revision"]:
                raise ApiError(409, "revision_conflict", "已有更新的记录，请重新打开后编辑。")
            records = staged_record_code.mutate(spec["fields"], records, data.get("operation"))
            entries[staged_record_code.STORAGE_KEY] = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
            packed = pack_entries([list(item) for item in entries.items()])
            revision = tool["revision"] + 1
            db.execute("UPDATE tools SET entries=?,revision=? WHERE id=? AND owner=?",
                       (packed, revision, identifier, user["id"]))
            return {"records": records, "revision": revision, "entries": json.loads(packed)}

    def revision_source(self, db, tool):
        repo_id, head = self.ensure_tool_history(db, tool)
        source = self.published_source(db, tool["run_id"])
        try:
            if source:
                files = self.files(source)
                files.pop("revision-plan.json", None)
                require_digest = digest(files["app.html"])
                if require_digest != source["digest"]:
                    raise ValueError("revision source mismatch")
                context = (self.revision.prepare_table(tool["entries"], prior_artifacts=files)
                           if tool["table_tool"] else self.revision.prepare(files))
                return repo_id, head, require_digest, context
            html = (self.runs / tool["run_id"] / "app.html").read_bytes()
            context = (self.revision.prepare_table(tool["entries"], html=html)
                       if tool["table_tool"] else self.revision.prepare({}, html))
            return repo_id, head, digest(html), context
        except (ApiError, ValueError, OSError, subprocess.SubprocessError):
            raise ApiError(422, "staged_revision_source", "这个旧版本暂时无法安全地分阶段修改，原工具与记录仍可用。") from None

    def repo(self, row):
        return self.root / ((row["repo_id"] or row["id"]) + ".git")

    def git(self, row, *arguments, content=None, check=True):
        return self.git_at(self.repo(row), *arguments, content=content, check=check)

    @staticmethod
    def git_at(repo, *arguments, content=None, check=True, date=None):
        env = {"PATH":os.environ.get("PATH", os.defpath), "GIT_CONFIG_GLOBAL":os.devnull,
               "GIT_CONFIG_NOSYSTEM":"1", "GIT_AUTHOR_NAME":"ROOT", "GIT_AUTHOR_EMAIL":"root@localhost",
               "GIT_COMMITTER_NAME":"ROOT", "GIT_COMMITTER_EMAIL":"root@localhost"}
        if date is not None:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{date} +0000"
        result = subprocess.run(["git", "--git-dir", str(repo), "-c", "core.hooksPath=/dev/null",
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
        steps = self.steps(row)
        state = "complete" if stage == "assemble" else "running"
        failure = None
        generated_title = row["generated_title"]
        if stage == "revision-plan":
            decision = json.loads(content)["status"]
            if decision == "proposed":
                steps = ("revision-plan", "spec")
            else:
                state = "clarifying" if decision == "clarify" else "unsupported"
                failure = "revision_clarification_required" if decision == "clarify" else "staged_unsupported"
        elif stage == "spec":
            stages = self.blueprint.stage_plan(content)
            is_revision = steps[0] == "revision-plan"
            steps = ("revision-plan", *stages) if is_revision else stages
            if not is_revision and not row["title"]:
                spec = self.blueprint.read_artifact(content)
                title = spec.get("title")
                if isinstance(title, str):
                    generated_title = title.strip()[:40].rstrip() or None
        db.execute("""
            UPDATE staged_jobs SET next_stage=?,head=?,state=?,lease_until=?,updated_at=?,plan=?,failure=?,
                                   generated_title=? WHERE id=?
        """, (next_stage, oid, state, self.clock() + self.lease_seconds if state == "running" else 0,
              self.clock(), json.dumps(steps), failure, generated_title, row["id"]))

    def check_revision(self, db, row, stage, content, prior):
        revision = self.revision_record(db, row["id"])
        if revision:
            context = json.loads(revision["context"])
            if stage == "revision-plan":
                answers = self.revision_answers(db, row["id"])
                selected = next((answer["answer"] for answer in answers if answer["selection"] == "operation"), None)
                context = self.revision_plan_context(row, revision, answers)
                self.revision.check_plan(content, context, selected or row["requirement"],
                                         operation_selected=selected is not None)
            elif "revision-plan.json" in prior:
                self.revision.check_planned_stage(stage, content, context, prior)
            else:
                self.revision.check(stage, content, context, row["requirement"], previous=prior)
        elif stage == "spec" and self.revision is not None:
            if self.blueprint.stage_plan(content) == self.blueprint.TABLE_PLAN:
                raise ValueError("unexpected table specification")
            if row["category"] != "auto":
                self.blueprint.validate_category(self.blueprint.read_artifact(content), row["category"])

    def recover(self, db, row):
        tip = self.tip(row)
        if tip in (ZERO, row["head"]):
            return
        stage = self.steps(row)[row["next_stage"]]
        prior = self.files(row)
        name = filename(stage)
        content = self.read(row, tip, name)
        if stage != "revision-plan":
            self.blueprint.validate(stage, content, prior)
        self.check_revision(db, row, stage, content, prior)
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
            task = self.view(row)
            if not (task["resumable"] if resume else task["state"] == "queued"):
                return None
            if row["fence"]:
                self.recover(db, row)
                row = self.row(db, identifier)
                if row["next_stage"] >= len(self.steps(row)):
                    return None
            elif row["head"] == ZERO:
                self.repo(row).mkdir(parents=True, mode=0o700, exist_ok=True)
                self.git(row, "init", "--bare", "--object-format=sha1", "--template=")
            else:
                self.git(row, "cat-file", "-e", row["head"] + "^{commit}")
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
            db.execute("UPDATE staged_jobs SET lease_until=? WHERE id=?",
                       (self.clock() + self.lease_seconds, identifier))

    def checkpoint(self, identifier, fence, index, stage, content, prior):
        if stage != "revision-plan":
            self.blueprint.validate(stage, content, prior)
        if stage == "implementation":
            check_script(json.loads(content)["script"])
        with self.store.connection(write=True) as db:
            row = self.worker(db, identifier, fence, index)
            self.check_revision(db, row, stage, content, prior)
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

    def selected_field_plan(self, row):
        with self.store.connection() as db:
            answers = self.revision_answers(db, row["id"])
            if not answers or answers[-1]["selection"] != "field":
                return None
            revision = self.revision_record(db, row["id"])
            if json.loads(revision["context"])["kind"] == "record_code" or self.revision_repairs(db, row["id"]):
                return None
            answer = answers[-1]
            question = json.loads(self.read(row, answer["question_id"], "revision-plan.json"))
            field = question.get("field") or question["operation"]["label"]
            return encoded({"status":"proposed", "operation":{
                "kind":"add_field", "label":field, "type":answer["answer"],
            }})

    def generate_code(self, identifier, fence, index, stage, requirement, prior, base):
        def dispatch():
            dispatch_id = uuid.uuid4().hex
            self.dispatch(identifier, fence, index, dispatch_id)

            def record(receipt):
                if self.record is not None:
                    self.record(identifier, dispatch_id, receipt)

            return record

        return self.coding_provider(stage, requirement, prior, dispatch, base=base)

    def generate_stage(self, identifier, fence, index, stage, row, prior, provider, revision_provider):
        retryable = (
            stage == "implementation"
            and self.steps(row)[0] == "spec"
            and staged_record_code.matches(self.blueprint.read_artifact(prior["spec.json"]))
        )
        patch_retry = stage == "revision-plan" and revision_provider is not None
        patch_feedback = None
        for attempt in range(2 if retryable or patch_retry else 1):
            dispatch_id = uuid.uuid4().hex
            recoverable = False
            validation = None

            def record(receipt):
                nonlocal recoverable, validation
                validation = receipt.get("validation")
                recoverable = receipt.get("dispatched") is True and (
                    (retryable and receipt.get("validation") == "invalid_model_json"
                     and receipt.get("status") == "invalid_staged_result")
                    or (patch_retry and (receipt.get("status") == "revision_patch_invalid"
                        or (receipt.get("status") == "invalid_staged_result"
                            and receipt.get("validation") in ("bad_revision_operation", "bad_revision_plan", "invalid_model_json",
                                                        "bad_program_attribute"))))
                )
                if self.record is not None:
                    self.record(identifier, dispatch_id, receipt)

            try:
                admit = lambda: self.dispatch(identifier, fence, index, dispatch_id)
                if patch_feedback:
                    return revision_provider(stage, row["requirement"], prior, admit, record, patch_feedback)
                return provider(stage, row["requirement"], prior, admit, record)
            except ApiError as error:
                if attempt != 0 or not recoverable:
                    raise
                if patch_retry:
                    patch_feedback = getattr(error, "patch_feedback", {"reason": validation or error.code})

    def run(self, identifier, fence, provider, revision_provider=None):
        index = 0
        try:
            with self.store.connection() as db:
                index = self.row(db, identifier)["next_stage"]
            while True:
                with self.store.connection() as db:
                    row = self.worker(db, identifier, fence, index)
                stage = self.steps(row)[index]
                prior = self.files(row)
                base = self.revision_context(identifier) if self.coding_provider and stage == "revision-plan" else None
                if self.coding_provider and (
                    (stage == "implementation" and "revision-plan.json" not in prior)
                    or (base and base["kind"] in ("program", "website", "record_code"))
                ):
                    content = self.generate_code(identifier, fence, index, stage, row["requirement"], prior, base)
                elif stage == "assemble":
                    content = self.blueprint.assemble(prior)
                elif stage == "revision-plan" and (selected := self.selected_field_plan(row)):
                    content = selected
                else:
                    content = self.generate_stage(identifier, fence, index, stage, row, prior, provider, revision_provider)
                self.checkpoint(identifier, fence, index, stage, content, prior)
                if stage == "assemble" or (stage == "revision-plan" and json.loads(content)["status"] != "proposed"):
                    return
                index += 1
        except (ApiError, OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as error:
            issue = uuid.uuid4().hex[:12]
            code = error.code if isinstance(error, ApiError) else ""
            state = {"staged_exhausted":"exhausted", "staged_unsupported":"unsupported",
                     "revision_no_change":"unsupported", "revision_risky":"unsupported",
                     "revision_clarification_required":"unsupported"}.get(code, "failed")
            if code in REVISION_GUIDANCE:
                state = "unsupported"
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

    def source_files(self, row, expected_digest):
        """Return generated artifacts from the exact tree used for the preview."""
        try:
            commit = row["head"]
            manifest = json.loads(self.read(row, commit, "manifest.json"))
            files = self.files(row)
            html = files.pop("app.html")
            if (manifest.get("task") != row["id"] or manifest.get("owner") != row["owner"]
                    or manifest.get("stage") != "assemble" or manifest.get("sha256") != expected_digest
                    or digest(html) != expected_digest
                    or manifest.get("depends") != {path:digest(data) for path, data in files.items()}):
                raise ValueError("source artifact mismatch")
            files.pop("revision-plan.json", None)
            return [{"path":path, "text":data.decode()} for path, data in sorted({**files, "app.html":html}.items())]
        except (KeyError, ValueError, OSError, subprocess.SubprocessError):
            raise ApiError(503, "tool_source_unavailable", "暂时无法读取此版本的代码，请重试。") from None

    def current_source(self, token, marker, tool_id, version):
        with self.store.connection() as db:
            user = self.store.session(db, token, marker)
            tool = self.store.owned_tool(db, user, tool_id)
            self.store.require_version(version, tool["version"])
            job = self.published_source(db, tool["run_id"])
            if job is not None and job["owner"] != user["id"]:
                raise ApiError(404, "tool_unavailable", "此工具不可用。")
        files = []
        if job is not None:
            try:
                saved_digest = digest((self.runs / tool["run_id"] / "app.html").read_bytes())
            except OSError:
                raise ApiError(404, "tool_unavailable", "此工具不可用。") from None
            if saved_digest != job["digest"]:
                raise ApiError(503, "tool_source_unavailable", "暂时无法读取此版本的代码，请重试。")
            files = self.source_files(job, saved_digest)
        return {"toolId":tool_id, "version":version, "applied":True, "files":files}

    def candidate_source(self, token, marker, identifier, data):
        identity = {"commit":data.get("commit"), "sha256":data.get("sha256")}
        with self.store.connection() as db:
            row, candidate, revision, tool_id = self.publication_binding(db, token, marker, identifier, identity, data)
            if revision is None or self.view(row)["state"] != "complete":
                raise ApiError(409, "staged_not_ready", "此修改候选已变化，请重新打开预览。")
            tool = self.store.owned_tool(db, {"id":row["owner"]}, tool_id)
            self.store.require_version(revision["base_version"], tool["version"])
            version = tool["version"] + 1
            self.store.require_version(data.get("version"), version)
        files = self.source_files(row, candidate["digest"])
        return {"toolId":tool_id, "version":version, "applied":False, "files":files}

    @staticmethod
    def candidate_record(db, identifier):
        return db.execute("SELECT * FROM staged_candidates WHERE task=?", (identifier,)).fetchone()

    def candidate(self, token, marker, identifier):
        identity, html = self.artifact(token, marker, identifier)
        with self.store.connection(write=True) as db:
            row = self.owned(db, token, marker, identifier)
            if self.view(row)["state"] != "complete" or identity["commit"] != row["head"]:
                raise ApiError(409, "staged_not_ready", "此任务尚未生成可预览的候选，或已发布。")
            revision = self.revision_record(db, identifier)
            if revision:
                self.check_revision_candidate(row, revision, html)
                current = self.store.owned_tool(db, {"id":row["owner"]}, revision["tool_id"])
                self.store.require_version(revision["base_version"], current["version"])
                self.check_table_schema(revision, current)
            candidate = self.candidate_record(db, identifier)
            if candidate is None:
                tool_id = Path(tempfile.mkdtemp(prefix="run-", dir=self.runs)).name
                db.execute("INSERT INTO staged_candidates(task,tool_id,commit_id,digest,nonce) VALUES (?,?,?,?,?)",
                           (identifier, tool_id, identity["commit"], identity["sha256"], uuid.uuid4().hex))
                candidate = self.candidate_record(db, identifier)
            if candidate["commit_id"] != identity["commit"] or candidate["digest"] != identity["sha256"]:
                raise ApiError(409, "staged_candidate_changed", "候选已变化，请重新准备预览。")
            result = {"id":candidate["tool_id"], "html":html.decode(), "entries":[], "revision":0, "version":1,
                      "staged":{**identity, "nonce":candidate["nonce"]}}
            if revision:
                result.update(id=current["id"], runId=candidate["tool_id"], entries=json.loads(current["entries"]),
                              revision=current["revision"], version=current["version"] + 1)
                result["revisionPlan"] = self.revision_plan(row, revision, current, candidate)
                migrated = self.migrated_entries(row, revision, current)
                if migrated is not None:
                    pack_entries(migrated, limit=TOOL_STORAGE_BYTES)
                    result["previewEntries"] = migrated
                if current["table_tool"]:
                    result["storageLimit"] = TABLE_STORAGE_BYTES
            if self.permits_backup(row):
                result["stagedBackup"] = True
            return result

    def revision_plan(self, row, revision, current, candidate):
        plan = {
            "toolId":current["id"], "title":current["title"], "baseVersion":current["version"],
            "dataRevision":current["revision"],
            "changes":self.revision.describe(json.loads(revision["context"]), self.files(row),
                                             row["requirement"], json.loads(current["entries"])),
        }
        files = self.files(row)
        decision = json.loads(files.get("revision-plan.json", b"{}"))
        operation = decision.get("operation", {})
        if "implementation" in decision:
            plan["trialPreview"] = True
        if operation.get("kind") == "add_field" and not {"edits", "implementation"} & decision.keys():
            plan["fieldType"] = {"id":row["head"], "label":operation["label"], "type":operation["type"]}
        plan["confirmation"] = digest(encoded({**plan, "nonce":candidate["nonce"], "sha256":candidate["digest"]}))
        return plan

    def migrated_entries(self, row, revision, current):
        decision = json.loads(self.files(row).get("revision-plan.json", b"{}"))
        operation = decision.get("operation", {})
        if operation.get("kind") in ("change_type", "backfill"):
            _, entries = self.revision.record_data_change(json.loads(revision["context"]), operation,
                                                           json.loads(current["entries"]))
            return entries
        return None

    def confirm_revision(self, token, marker, identifier, data):
        with self.store.connection(write=True) as db:
            row = self.owned(db, token, marker, identifier)
            candidate = self.candidate_record(db, identifier)
            revision = self.revision_record(db, identifier)
            if (not revision or not candidate or self.view(row)["state"] != "complete"
                    or data.get("nonce") != candidate["nonce"]):
                raise ApiError(409, "staged_candidate_changed", "修改方案已变化，请重新查看。")
            current = self.store.owned_tool(db, {"id":row["owner"]}, revision["tool_id"])
            self.store.require_version(revision["base_version"], current["version"])
            self.check_table_schema(revision, current)
            if data.get("confirmation") != self.revision_plan(row, revision, current, candidate)["confirmation"]:
                raise ApiError(409, "revision_conflict", "已有更新的数据或方案，请重新查看并确认。")
            db.execute("UPDATE staged_candidates SET confirmation=? WHERE task=?", (data["confirmation"], identifier))

    def cancel_revision(self, token, marker, identifier, nonce):
        with self.store.connection(write=True) as db:
            row = self.owned(db, token, marker, identifier)
            candidate = self.candidate_record(db, identifier)
            revision = self.revision_record(db, identifier)
            state = self.view(row)["state"]
            if revision and row["issue"] and nonce == row["issue"] and state in ("failed", "cancelled"):
                if state == "failed":
                    db.execute("DELETE FROM staged_candidates WHERE task=?", (identifier,))
                    db.execute("UPDATE staged_jobs SET state='cancelled',updated_at=? WHERE id=?",
                               (self.clock(), identifier))
                return self.view(self.row(db, identifier), db)
            if revision and state == "clarifying" and nonce == row["head"]:
                db.execute("UPDATE staged_jobs SET state='cancelled',updated_at=? WHERE id=?",
                           (self.clock(), identifier))
                return self.view(self.row(db, identifier), db)
            if not revision or candidate is None or candidate["nonce"] != nonce or state != "complete":
                raise ApiError(409, "revision_cannot_cancel", "此修改方案已变化或无法取消。")
            db.execute("UPDATE staged_candidates SET nonce=?,runtime_proof=NULL,confirmation=NULL WHERE task=?",
                       (uuid.uuid4().hex, identifier))
            db.execute("UPDATE staged_jobs SET state='cancelled',updated_at=? WHERE id=?",
                       (self.clock(), identifier))
            return self.view(self.row(db, identifier), db)

    def check_revision_candidate(self, row, revision, html):
        context = json.loads(revision["context"])
        files = self.files(row)
        if "revision-plan.json" in files:
            self.revision.check_planned_stage("assemble", html, context, files)
            return
        local = self.revision.planned_local_revision(context, row["requirement"])
        if local or context["kind"] == "ledger":
            self.revision.check("assemble", html, context, row["requirement"], previous=files)
        elif context["kind"] == "table":
            self.revision.check("spec", self.files(row)["spec.json"], context, row["requirement"])

    def check_table_schema(self, revision, tool):
        if tool["table_tool"]:
            context = json.loads(revision["context"])
            if context["kind"] != "table" or context["columns"] != self.revision.table_columns(tool["entries"]):
                raise ApiError(409, "incompatible_revision", "已保存的表格字段发生变化，原工具仍可用。")

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

    def resolve_publication(self, token, marker, identifier, nonce):
        if nonce is not None and (not isinstance(nonce, str) or not IDENTIFIER.fullmatch(nonce)):
            raise ApiError(400, "staged_input", "保存核对标识无效。")
        with self.store.connection(write=True) as db:
            row = self.owned(db, token, marker, identifier)
            state = self.view(row, db)
            if state["state"] == "published":
                return self.store.owned_tool(db, {"id":row["owner"]}, state["toolId"])
            # The same write lock as publish makes this decision final for the old preview.
            if state["state"] == "complete":
                db.execute("""
                    UPDATE staged_candidates SET nonce=?,runtime_proof=NULL,confirmation=NULL WHERE task=? AND (? IS NULL OR nonce=?)
                """, (uuid.uuid4().hex, identifier, nonce, nonce))
            return None

    def publication_binding(self, db, token, marker, identifier, identity, data):
        row = self.owned(db, token, marker, identifier)
        candidate = self.candidate_record(db, identifier)
        revision = self.revision_record(db, identifier)
        result_id = revision["tool_id"] if revision else candidate["tool_id"] if candidate else None
        if (candidate is None or candidate["commit_id"] != row["head"] or candidate["commit_id"] != identity["commit"]
                or result_id != data.get("toolId") or (revision and candidate["tool_id"] != data.get("runId"))
                or candidate["nonce"] != data.get("nonce")
                or candidate["digest"] != identity["sha256"] or identity["sha256"] != data.get("sha256")
                or identity["commit"] != data.get("commit")):
            raise ApiError(409, "staged_candidate_changed", "候选和预览快照不匹配，请重新准备预览。")
        if self.view(row)["state"] not in ("complete", "published"):
            raise ApiError(409, "staged_not_ready", "此任务尚未生成可发布的候选。")
        return row, candidate, revision, result_id

    def publication_snapshot(self, db, row, revision, result_id, data, identity, *, verifying=False):
        current = None
        if revision:
            current = self.store.owned_tool(db, {"id":row["owner"]}, result_id)
            self.store.require_version(revision["base_version"], current["version"])
            self.check_table_schema(revision, current)
            if type(data.get("revision")) is not int or data["revision"] != current["revision"]:
                raise ApiError(409, "revision_conflict", "已有更新的数据，请重新查看并确认修改方案。")
            candidate = self.candidate_record(db, row["id"])
            confirmation = data.get("confirmation")
            plan = self.revision_plan(row, revision, current, candidate)
            trial = verifying and (plan.get("trialPreview") or bool(self.revision_repairs(db, row["id"])))
            if (not confirmation or (not trial and candidate["confirmation"] != confirmation)
                    or confirmation != plan["confirmation"]):
                raise ApiError(409, "revision_confirmation_required", "请先确认当前工具的修改方案。")
        limit = TABLE_STORAGE_BYTES if current and current["table_tool"] else TOOL_STORAGE_BYTES
        packed = pack_entries(data.get("entries"), limit=limit)
        if current is None:
            spec = self.blueprint.read_artifact(self.read(row, identity["commit"], "spec.json"))
            if staged_record_code.matches(spec) and packed != "[]":
                raise ApiError(409, "incompatible_data", "新台账或看板须从空记录开始。")
        if current and dict(json.loads(packed)) != dict(json.loads(current["entries"])):
            raise ApiError(409, "incompatible_data", "此修改无法原样保留已有记录。")
        proof = digest(encoded({**identity, "policy":RUNTIME_PROOF_VERSION, "owner":row["owner"], "nonce":data["nonce"],
                               "revision":current["revision"] if current else None, "entries":packed}))
        return current, limit, proof

    def verify_preview(self, token, marker, identifier, data):
        identity, html = self.artifact(token, marker, identifier)
        with self.store.connection() as db:
            row, candidate, revision, result_id = self.publication_binding(db, token, marker, identifier, identity, data)
            if row["state"] == "published":
                return
            current, limit, proof = self.publication_snapshot(db, row, revision, result_id, data, identity, verifying=True)
            if candidate["runtime_proof"] == proof:
                return
        if self.runtime_verifier is None:
            raise ApiError(503, "staged_verifier_unavailable", "运行检查暂不可用，请稍后重新检查预览。")
        try:
            spec = self.blueprint.read_artifact(self.read(row, identity["commit"], "spec.json"))
            if not isinstance(spec, dict):
                raise ValueError("invalid candidate specification")
        except (ValueError, OSError, RecursionError, subprocess.SubprocessError):
            raise ApiError(503, "staged_artifact_invalid", "生成候选的规格无法校验。") from None
        try:
            verifier_entries = [] if staged_record_code.matches(spec) else data["entries"]
            verifier_options = {"website": spec.get("kind") == "website"}
            if revision and staged_record_code.matches(spec):
                original = json.loads(revision["context"])["source"]["spec"]["fields"]
                verifier_entries = staged_record_code.verification_entries(original)
                try:
                    baseline = (self.runs / current["run_id"] / "app.html").read_bytes()
                    if digest(baseline) != revision["source_digest"]:
                        raise OSError("revision baseline mismatch")
                    visible = self.runtime_verifier(baseline, verifier_entries, backup=False, storage_limit=limit,
                                                    website=False, record_visibility=[])
                    verifier_options["record_visibility"] = visible
                except OSError:
                    raise ApiError(503, "staged_artifact_invalid", "当前工具的基准版本暂时无法校验。") from None
                except RuntimeVerificationError:
                    raise ApiError(422, "revision_baseline_failed", "当前版本未通过旧记录检查，无法比较此次修改。") from None
                decision = json.loads(self.files(row).get("revision-plan.json", b"{}"))
                operation = decision.get("operation", {})
                if operation.get("kind") in ("change_type", "backfill"):
                    verifier_entries = self.migrated_entries(row, revision, current)
                    verifier_options["record_visibility"] = []
                    if operation["kind"] == "change_type":
                        field = next(field for field in spec["fields"] if field["id"] == operation["field"])
                        verifier_options["record_field"] = {name: field[name] for name in ("id", "type", "required")}
                        verifier_options["record_fields"] = spec["fields"]
                if len(spec["fields"]) == len(original) + 1:
                    field = spec["fields"][-1]
                    verifier_options["record_field"] = {name: field[name] for name in ("id", "type")}
                    verifier_options["record_fields"] = spec["fields"]
            self.runtime_verifier(html, verifier_entries, backup=self.permits_backup(row), storage_limit=limit,
                                  **verifier_options)
        except RuntimeVerificationError as error:
            logging.getLogger(__name__).warning("staged runtime rejected task=%s candidate=%s reason=%s",
                                                identifier, identity["sha256"], error.diagnostic)
            permanent_failure = (
                (spec.get("kind") == "website" and error.diagnostic in ("website_tabs", "website_style"))
                or (staged_record_code.matches(spec) and error.diagnostic == "record_initialization")
            )
            if ((revision is None and permanent_failure and not data["entries"])
                    or (revision and error.diagnostic in REPAIRABLE_RUNTIME_ERRORS)):
                with self.store.connection(write=True) as db:
                    db.execute("""
                        UPDATE staged_jobs SET state='failed',failure=?,issue=?,updated_at=?
                        WHERE id=? AND state='complete' AND head=? AND expires>?
                        AND EXISTS (
                            SELECT 1 FROM staged_candidates
                            WHERE task=staged_jobs.id AND nonce=? AND runtime_proof IS NULL
                        )
                    """, (error.code, uuid.uuid4().hex[:12], self.clock(), identifier, identity["commit"],
                          self.clock(), candidate["nonce"]))
            raise
        with self.store.connection(write=True) as db:
            row, _, revision, result_id = self.publication_binding(db, token, marker, identifier, identity, data)
            if row["state"] == "published":
                return
            _, _, proof = self.publication_snapshot(db, row, revision, result_id, data, identity, verifying=True)
            db.execute("UPDATE staged_candidates SET runtime_proof=? WHERE task=?", (proof, identifier))

    def publish(self, token, marker, identifier, data):
        identity, html = self.artifact(token, marker, identifier)
        with self.store.connection(write=True) as db:
            row, candidate, revision, result_id = self.publication_binding(db, token, marker, identifier, identity, data)
            owner = {"id":row["owner"]}
            tool_id = candidate["tool_id"]
            if row["state"] == "complete":
                current, _, proof = self.publication_snapshot(db, row, revision, result_id, data, identity)
                if revision:
                    self.check_revision_candidate(row, revision, html)
                if candidate["runtime_proof"] != proof:
                    raise ApiError(409, "staged_runtime_required", "此候选尚未通过运行检查，请重新检查预览。")
                run = self.runs / tool_id
                path = run / "app.pending"
                try:
                    path.write_bytes(html)
                    path.replace(run / "app.html")
                except OSError:
                    raise ApiError(503, "staged_save_failed", "候选尚未保存，可重新检查预览后继续。") from None
                if revision:
                    self.store.insert_revision_draft(db, current, tool_id, row["requirement"], model=True)
                    self.store.publish_revision(db, current, data, self.budget.record_generation_success,
                                                migrated_entries=self.migrated_entries(row, revision, current))
                    if json.loads(revision["context"])["kind"] in ("ledger", "table", "website"):
                        spec = json.loads(self.read(row, row["head"], "spec.json"))
                        title = " ".join(spec["title"].split())[:48]
                        db.execute("UPDATE tools SET title=? WHERE id=?", (title, result_id))
                else:
                    spec = json.loads(self.read(row, row["head"], "spec.json"))
                    title = row["title"] or " ".join(spec["title"].split())[:48]
                    self.store.insert_draft(db, row["owner"], tool_id, title, pack_entries([]), model=True)
                    tool = self.store.owned_tool(db, owner, tool_id, published=False)
                    self.store.commit_initial(db, tool, data.get("entries"), self.budget.record_generation_success)
                db.execute("UPDATE staged_jobs SET state='published' WHERE id=?", (identifier,))
                self.ensure_tool_history(db, {"id": result_id})
            return self.store.owned_tool(db, owner, result_id)
