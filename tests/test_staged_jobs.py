from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from accounts import ApiError, Store
from trial_budget import TrialBudget
from staged_jobs import StagedJobs


class Blueprint:
    STAGES = ("spec", "entry", "filters", "assemble")

    @staticmethod
    def read_artifact(_content):
        return {}

    def stage_plan(self, spec):
        return self.STAGES

    @staticmethod
    def validate(stage, content, prior):
        if content == b"invalid":
            raise ValueError("invalid artifact")

    @staticmethod
    def assemble(prior):
        return b"<!doctype html><html>" + b"".join(prior.values()) + b"</html>"


class StagedJobsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / "accounts.sqlite3")
        self.budget = TrialBudget(self.store, self.root)
        self.jobs = StagedJobs(self.store, self.budget, self.root, Blueprint())
        self.owner = self.account("owner")
        self.other = self.account("other")

    def account(self, name):
        token, view = self.store.register({
            "username":name, "password":"staged-test-only", "invite":self.store.create_invite()})
        return token, view["marker"]

    def test_failure_history_cannot_crowd_out_active_jobs(self):
        now = [1000]
        self.jobs.clock = lambda: now[0]
        ready = self.jobs.create(*self.owner, "可预览任务", uuid.uuid4().hex)["id"]
        interrupted = self.jobs.create(*self.owner, "读取中断任务", uuid.uuid4().hex)["id"]
        failed = self.jobs.create(*self.owner, "录入失败的原需求", uuid.uuid4().hex)["id"]
        other = self.jobs.create(*self.other, "其他账号的需求", uuid.uuid4().hex)["id"]
        with self.store.connection(write=True) as db:
            db.execute("UPDATE staged_jobs SET state='complete' WHERE id=?", (ready,))
            db.execute("UPDATE staged_jobs SET state='running', lease_until=999 WHERE id=?", (interrupted,))
            db.execute("UPDATE staged_jobs SET state='failed', failure='fixture_failure', updated_at=2000 WHERE id IN (?,?)",
                       (failed, other))
        expired_ids = []
        for index in range(32):
            now[0] += 1
            expired_ids.append(self.jobs.create(*self.owner, f"过期需求{index}", uuid.uuid4().hex)["id"])
        with self.store.connection(write=True) as db:
            db.executemany("UPDATE staged_jobs SET expires=999 WHERE id=?", [(item,) for item in expired_ids])
        self.assertEqual({row["id"]:row["state"] for row in self.jobs.list(*self.owner)},
                         {ready:"complete", interrupted:"interrupted"})
        history = self.jobs.list(*self.owner, history=True)
        self.assertEqual(len(history), 30)
        self.assertEqual((history[0]["id"], history[0]["requirement"], history[0]["reason"]),
                         (failed, "录入失败的原需求", "fixture_failure"))
        self.assertEqual({row["state"] for row in history}, {"failed", "expired"})
        self.assertEqual([row["id"] for row in self.jobs.list(*self.other, history=True)], [other])
        self.assertEqual(self.jobs.status(*self.owner, failed)["state"], "failed")

    def test_same_account_resumes_only_unchecked_stages(self):
        requirement = "新增台账并按状态筛选、统计、导出"
        request = uuid.uuid4().hex
        created = self.jobs.create(*self.owner, requirement, request)
        job = created["id"]
        self.assertEqual(self.jobs.create(*self.owner, requirement, request)["id"], job)
        with self.assertRaises(ApiError) as forbidden:
            self.jobs.status(*self.other, job)
        self.assertEqual(forbidden.exception.status, 404)
        seen = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            fences = list(pool.map(lambda _: self.jobs.claim(*self.owner, job), range(2)))
        self.assertCountEqual(fences, [1, None])
        first_fence = 1
        def provider(stage, requirement, prior, admit, record):
            seen.append(stage)
            admit()
            if stage == "entry" and seen.count(stage) == 1:
                raise ApiError(502, "fixture_failure", "中间阶段失败")
            return stage.encode()
        self.jobs.run(job, first_fence, provider)
        failed = self.jobs.status(*self.owner, job)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["completed"], ["spec"])
        self.assertTrue(failed["issue"])
        self.assertEqual(self.budget.balance(*self.owner)["generation"]["used"], 0)
        self.assertEqual(self.store.list_tools(*self.owner)["items"], [])
        new_token = self.store.login({"username":"owner","password":"staged-test-only"}, "")
        with self.store.connection() as db:
            new_marker = self.store.session(db, new_token)["marker"]
        again = (new_token, new_marker)
        self.assertEqual(self.jobs.status(*again)["id"], job)
        self.jobs.run(job, self.jobs.claim(*again, job), provider)
        self.assertEqual(seen, ["spec","entry","entry","filters"])
        self.assertEqual(self.jobs.status(*again, job)["state"], "complete")
        self.assertEqual(self.budget.balance(*again)["generation"]["totalUsed"], 4)
        self.assertEqual(self.store.list_tools(*again)["items"], [])
        self.assertEqual(self.jobs.artifact(*again, job)[1], b"<!doctype html><html>specentryfilters</html>")
        with self.assertRaises(ApiError) as forbidden:
            self.jobs.artifact(*self.other, job)
        self.assertEqual(forbidden.exception.status, 404)

    def test_process_exit_after_git_checkpoint_recovers_without_dispatch(self):
        now = [1000.0]
        self.jobs.clock = lambda: now[0]
        job = self.jobs.create(*self.owner, "复杂台账", uuid.uuid4().hex)["id"]
        fence = self.jobs.claim(*self.owner, job)
        program = """
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import runpy
import sys
from accounts import Store
from staged_jobs import StagedJobs
from trial_budget import TrialBudget
root, job, fence = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
blueprint = runpy.run_path('tests/test_staged_jobs.py')['Blueprint']()
store = Store(root / 'accounts.sqlite3')
jobs = StagedJobs(store, TrialBudget(store, root), root, blueprint, clock=lambda:1000)
jobs.advance_row = lambda *args: os._exit(91)
def provider(stage, requirement, prior, admit, record):
    admit()
    return stage.encode()
jobs.run(job, fence, provider)
"""
        child = subprocess.run([sys.executable, "-B", "-c", program, str(self.root), job, str(fence)],
                               timeout=12, capture_output=True)
        self.assertEqual(child.returncode, 91, child.stderr)
        self.assertEqual(self.jobs.status(*self.owner, job)["completed"], [])
        self.assertEqual(self.budget.balance(*self.owner)["generation"]["totalUsed"], 1)
        with self.store.connection() as db:
            old_row = self.jobs.row(db, job)
            old_tip = self.jobs.tip(old_row)
        self.assertNotEqual(old_tip, old_row["head"])

        now[0] += self.jobs.lease_seconds + 1
        self.assertEqual(self.jobs.status(*self.owner, job)["state"], "interrupted")
        new_fence = self.jobs.claim(*self.owner, job)
        self.assertEqual(self.jobs.status(*self.owner, job)["completed"], ["spec"])
        with self.assertRaises(ApiError) as stale:
            self.jobs.checkpoint(job, fence, 1, "entry", b"stale", {"spec.json":b"spec"})
        self.assertEqual(stale.exception.code, "staged_stale")
        self.jobs.git(old_row, "update-ref", "-d", self.jobs.ref(old_row), old_tip)
        calls = []
        def provider(stage, requirement, prior, admit, record):
            calls.append(stage)
            admit()
            return stage.encode()
        self.jobs.run(job, new_fence, provider)
        self.assertEqual(calls, ["entry", "filters"])
        self.assertEqual(self.budget.balance(*self.owner)["generation"]["totalUsed"], 3)
        self.assertEqual(self.jobs.status(*self.owner, job)["state"], "complete")

    def test_hard_limit_cannot_be_reset_by_resuming(self):
        self.jobs.max_attempts = 1
        job = self.jobs.create(*self.owner, "复杂台账", uuid.uuid4().hex)["id"]
        dispatched = []
        def provider(stage, requirement, prior, admit, record):
            admit()
            dispatched.append(stage)
            raise ApiError(502, "fixture_failure", "调用失败")
        self.jobs.run(job, self.jobs.claim(*self.owner, job), provider)
        self.jobs.run(job, self.jobs.claim(*self.owner, job), provider)
        self.assertEqual(dispatched, ["spec"])
        self.assertEqual(self.jobs.status(*self.owner, job)["state"], "exhausted")
        self.assertIsNone(self.jobs.claim(*self.owner, job))
        self.assertEqual(self.budget.balance(*self.owner)["generation"]["totalUsed"], 1)

    def test_task_allows_100_dispatches_and_rejects_the_next(self):
        from workbench import DEFAULT_AGENT_MODEL_CALLS, DEFAULT_GENERATION_LIMITS

        budget = TrialBudget(self.store, self.root, generation_limits=DEFAULT_GENERATION_LIMITS)
        jobs = StagedJobs(self.store, budget, self.root, Blueprint(), max_attempts=DEFAULT_AGENT_MODEL_CALLS)
        job = jobs.create(*self.owner, "复杂官网", uuid.uuid4().hex)["id"]
        fence = jobs.claim(*self.owner, job)
        for _ in range(100):
            jobs.dispatch(job, fence, 0, uuid.uuid4().hex)
        with self.assertRaises(ApiError) as exhausted:
            jobs.dispatch(job, fence, 0, uuid.uuid4().hex)
        self.assertEqual(exhausted.exception.code, "staged_exhausted")
        generation = budget.balance(*self.owner)["generation"]
        self.assertEqual((generation["used"], generation["limit"], generation["totalUsed"],
                          generation["totalLimit"]), (0, 1000, 100, 10000))
        self.assertEqual(budget.balance(*self.other)["generation"]["totalRemaining"], 9900)
        restarted = TrialBudget(self.store, self.root, generation_limits=DEFAULT_GENERATION_LIMITS)
        self.assertEqual(restarted.balance(*self.owner)["generation"], generation)

    def test_request_id_replay_has_a_fixed_requirement(self):
        request = uuid.uuid4().hex
        task = self.jobs.create(*self.owner, "首次需求", request)
        self.assertEqual(self.jobs.create(*self.owner, " 首次需求 ", request), task)
        with self.assertRaises(ApiError) as changed:
            self.jobs.create(*self.owner, "改了需求", request)
        self.assertEqual(changed.exception.code, "staged_request_changed")
        self.assertNotEqual(self.jobs.create(*self.other, "另一个账号的需求", request)["id"], task["id"])
