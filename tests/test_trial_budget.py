from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from accounts import ApiError, Store
from trial_budget import TrialBudget


class TrialBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runs = Path(directory.name)
        self.store = Store(self.runs / "trial.sqlite3")
        self.alice = self.account("alice")
        self.bob = self.account("bob")
        self.admin = self.account("administrator")
        self.now = datetime(2026, 11, 9, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        self.budget = self.make_budget()

    def account(self, name):
        token, identity = self.store.register({
            "username": name, "password": "fixture-password", "invite": self.store.create_invite(),
        })
        return token, identity["marker"]

    def make_budget(self, generation=(1, 3), analysis=(1, 2)):
        return TrialBudget(self.store, self.runs, generation_limits=generation, analysis_limits=analysis,
                           admin_usernames=(" Administrator ",), clock=lambda: self.now)

    def failure(self, code, action, *args):
        with self.assertRaises(ApiError) as error:
            action(*args)
        self.assertEqual(error.exception.code, code)

    def complete(self, credentials, run_id, *, rollback=False):
        with self.store.connection(write=True) as db:
            account_id = self.store.session(db, *credentials)["id"]
            self.budget.record_generation_success(db, account_id, run_id)
            if rollback:
                raise RuntimeError("publication failed")

    def test_generation_charges_global_dispatch_and_personal_success_separately(self):
        self.budget.reserve(*self.alice, "generation")
        current = self.budget.balance(*self.alice)["generation"]
        self.assertEqual((current["remaining"], current["totalRemaining"]), (1, 2))
        self.budget.reserve(*self.alice, "generation")
        with self.assertRaisesRegex(RuntimeError, "publication failed"):
            self.complete(self.alice, "run-rollback", rollback=True)
        self.assertEqual(self.budget.balance(*self.alice)["generation"]["used"], 0)
        self.complete(self.alice, "run-success")
        self.complete(self.alice, "run-success")
        self.failure("generation_account_limit", self.budget.reserve, *self.alice, "generation")
        self.budget.reserve(*self.bob, "generation")
        self.failure("generation_total_limit", self.budget.reserve, *self.bob, "generation")
        self.assertEqual(self.budget.balance(*self.alice)["generation"]["used"], 1)

    def test_generation_admission_shares_the_task_dispatch_transaction(self):
        with self.store.connection(write=True) as db:
            db.execute("CREATE TABLE task_dispatches (task TEXT PRIMARY KEY)")

        with self.assertRaisesRegex(RuntimeError, "dispatch interrupted"):
            with self.store.connection(write=True) as db:
                owner = self.store.session(db, *self.alice)["id"]
                db.execute("INSERT INTO task_dispatches VALUES ('task-a')")
                self.budget.reserve_generation(db, owner)
                raise RuntimeError("dispatch interrupted")

        self.assertEqual(self.budget.balance(*self.alice)["generation"]["totalUsed"], 0)
        with self.store.connection(write=True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM task_dispatches").fetchone()[0], 0)
            owner = self.store.session(db, *self.alice)["id"]
            db.execute("INSERT INTO task_dispatches VALUES ('task-a')")
            self.budget.reserve_generation(db, owner)

        current = self.budget.balance(*self.alice)["generation"]
        self.assertEqual((current["used"], current["totalUsed"]), (0, 1))

    def test_admin_outcomes_ignore_unknown_legacy_and_rejected_dispatches_after_restart(self):
        # TEST-017-STAT. Results are per dispatch and survive a trial-budget restart.
        self.budget = self.make_budget(generation=(5, 100), analysis=(6, 100))
        initial = self.budget.admin_state(*self.admin)
        for kind in ("generation", "analysis"):
            self.assertEqual(initial[kind], {
                "used": 0, "limit": 100, "remaining": 100,
                "completed": 0, "failed": 0, "failureRate": None,
            })
        self.budget.reserve(*self.alice, "generation")  # pre-migration usage has no known outcome
        self.budget.reserve(*self.alice, "generation", dispatch_id="in-flight")
        self.budget.reserve(*self.alice, "generation", dispatch_id="valid")
        self.budget.finish_dispatch("generation", "valid", success=True)
        self.budget.finish_dispatch("generation", "valid", success=False)
        self.analysis_dispatch(self.alice, "first-analysis", "round-1", dispatch_id="failed")
        self.budget.finish_dispatch("analysis", "failed", success=False)
        self.now += 2
        self.budget.admin_action(*self.admin, {"action": "pause"})
        with self.assertRaises(ApiError) as error:
            self.budget.reserve(*self.alice, "generation", dispatch_id="paused")
        self.assertEqual(error.exception.code, "model_paused")
        self.budget.finish_dispatch("generation", "paused", success=False)
        restarted = self.make_budget(generation=(5, 100), analysis=(6, 100))
        state = restarted.admin_state(*self.admin)
        self.assertTrue(state["paused"])
        self.assertEqual(state["generation"], {
            "used": 3, "limit": 100, "remaining": 97,
            "completed": 1, "failed": 0, "failureRate": 0.0,
        })
        self.assertEqual(state["analysis"], {
            "used": 1, "limit": 100, "remaining": 99,
            "completed": 1, "failed": 1, "failureRate": 1.0,
        })
        self.assertEqual(restarted.balance(*self.alice)["analysis"]["used"], 0)

    def analysis_dispatch(self, user, request, round_id, **kwargs):
        self.budget.reserve(*user, "analysis", request, round_id=round_id, idea="需求", **kwargs)

    def offer(self, user, request, round_id):
        self.analysis_dispatch(user, request, round_id)
        self.budget.offer_analysis_draft(*user, round_id, request, "可编辑草稿")

    def test_analysis_charges_on_the_beijing_day_the_draft_is_claimed(self):
        self.offer(self.alice, "old", "same-round")
        before = self.budget.balance(*self.alice)["analysis"]
        self.assertEqual((before["remaining"], before["totalRemaining"]), (1, 1))
        self.assertEqual(before["resetAt"], "2026-11-10T00:00:00+08:00")
        self.failure("analysis_duplicate", self.analysis_dispatch, self.alice, "old", "same-round")

        self.now += 1
        after = self.budget.balance(*self.alice)["analysis"]
        self.assertEqual(after["resetAt"], "2026-11-11T00:00:00+08:00")
        draft = self.budget.claim_analysis_draft(*self.alice, "old")
        self.assertEqual(self.budget.claim_analysis_draft(*self.alice, "old"), draft)
        self.assertEqual(self.budget.balance(*self.alice)["analysis"]["remaining"], 0)
        self.failure("analysis_account_limit", self.analysis_dispatch, self.alice, "other", "another-round")
        self.analysis_dispatch(self.alice, "followup", "same-round")
        self.failure("analysis_total_limit", self.analysis_dispatch, self.bob, "new", "bobs-round")
        self.assertEqual(self.budget.balance(*self.bob)["generation"]["totalUsed"], 0)

    def test_analysis_last_slot_and_replay_are_atomic(self):
        self.budget = self.make_budget(analysis=(1, 10))
        self.offer(self.alice, "offer-a", "round-a")
        self.offer(self.alice, "offer-b", "round-b")

        def claim(offer):
            try:
                return self.budget.claim_analysis_draft(*self.alice, offer)
            except ApiError as error:
                return error.code

        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(claim, ("offer-a", "offer-b")))
        winning = "offer-a" if isinstance(outcomes[0], dict) else "offer-b"
        self.assertEqual(outcomes.count("analysis_account_limit"), 1)
        self.assertEqual(self.budget.balance(*self.alice)["analysis"]["used"], 1)
        self.budget = self.make_budget(analysis=(1, 10))
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(claim, (winning, winning))), [{"kind": "draft", "text": "可编辑草稿"}] * 2)
        self.assertEqual(self.budget.balance(*self.alice)["analysis"]["totalUsed"], 2)
        self.now += 15 * 60
        self.failure("analysis_draft_unavailable", self.budget.claim_analysis_draft, *self.alice, winning)

    def test_legacy_analysis_dispatch_history_does_not_become_completed_requirements(self):
        day, _ = self.budget.day_window()
        with self.store.connection(write=True) as db:
            alice_id = self.store.session(db, *self.alice)["id"]
            db.execute("INSERT INTO analysis_usage VALUES (?, ?, 1)", (alice_id, day))
        self.budget = self.make_budget()
        current = self.budget.balance(*self.alice)["analysis"]
        self.assertEqual((current["used"], current["totalUsed"]), (0, 1))
        self.offer(self.alice, "draft", "new-round")
        self.budget.claim_analysis_draft(*self.alice, "draft")
        self.failure("analysis_total_limit", self.analysis_dispatch, self.bob, "new", "bobs-round")

    def test_reservation_and_publication_check_caps_atomically(self):
        budget = self.make_budget(generation=(1, 1), analysis=(3, 3))
        self.budget = budget

        def race_reserve(credentials):
            try:
                budget.reserve(*credentials, "generation")
                return "reserved"
            except ApiError as error:
                return error.code

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(race_reserve, (self.alice, self.bob)))
        self.assertCountEqual(results, ["reserved", "generation_total_limit"])

        def race_publish(run_id):
            try:
                self.complete(self.alice, run_id)
                return "saved"
            except ApiError as error:
                return error.code

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(race_publish, ("run-a", "run-b")))
        self.assertCountEqual(results, ["saved", "generation_account_limit"])

    def test_legacy_generation_dispatches_are_preserved(self):
        with self.store.connection(write=True) as db:
            alice_id = self.store.session(db, *self.alice)["id"]
            db.execute("INSERT INTO generation_usage VALUES (?, 2)", (alice_id,))
        budget = self.make_budget()
        current = budget.balance(*self.alice)["generation"]
        self.assertEqual((current["totalUsed"], current["used"]), (2, 0))
        budget.reserve(*self.alice, "generation")
        self.failure("generation_total_limit", budget.reserve, *self.bob, "generation")

    def test_configuring_an_unprovisioned_name_does_not_authorize_later_registration(self):
        budget = TrialBudget(self.store, self.runs, admin_usernames=("future-admin",))
        registrant = self.account("future-admin")
        self.assertFalse(budget.balance(*registrant)["admin"])
        self.failure("admin_required", budget.admin_state, *registrant)

    def test_admin_pause_grants_audit_and_existing_data_routes(self):
        self.failure("login_required", self.budget.balance, "", "")
        self.failure("session_changed", self.budget.balance, self.alice[0], self.bob[1])
        self.failure("admin_required", self.budget.admin_state, *self.alice)
        self.failure("admin_required", self.budget.admin_action, *self.alice, {"action": "pause"})
        self.failure("invalid_grant", self.budget.admin_action, *self.admin,
                     {"action": "grant", "username": "bob", "amount": True})
        grant = {"action": "grant", "username": "bob", "amount": 3, "requestId": "grant-1"}
        self.assertEqual(self.budget.admin_action(*self.admin, grant)["amount"], 3)
        self.assertEqual(self.budget.admin_action(*self.admin, grant)["amount"], 3)
        self.failure("admin_request_conflict", self.budget.admin_action, *self.admin, {**grant, "amount": 1})
        self.assertEqual(self.budget.balance(*self.bob)["generation"]["limit"], 4)
        self.assertTrue(self.budget.admin_action(*self.admin, {"action": "pause"})["paused"])
        for kind in ("analysis", "generation"):
            self.failure("model_paused", self.budget.reserve, *self.bob, kind)
        self.assertEqual(self.store.list_tools(*self.bob)["items"], [])
        self.assertEqual(self.store.requirement_draft(*self.bob, "draft", {"text": "still works", "revision": 0})["revision"], 1)
        legacy_pause = self.runs / "generation.paused"
        legacy_pause.touch()
        self.failure("external_pause", self.budget.admin_action, *self.admin, {"action": "resume"})
        self.assertTrue(self.budget.admin_state(*self.admin)["paused"])
        legacy_pause.unlink()
        self.assertFalse(self.budget.admin_action(*self.admin, {"action": "resume"})["paused"])
        for _ in range(3):
            self.budget.reserve(*self.bob, "generation")
        self.failure("generation_total_limit", self.budget.reserve, *self.bob, "generation")
        state = self.budget.admin_state(*self.admin)
        self.assertEqual([entry["action"] for entry in state["audit"]], ["resume", "pause", "grant"])
        self.assertEqual(state["audit"][-1]["actor"], "administrator")
        self.assertEqual(state["audit"][-1]["username"], "bob")
