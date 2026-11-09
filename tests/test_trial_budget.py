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

    def test_analysis_resets_at_beijing_midnight_and_global_total_does_not_reset(self):
        self.budget.reserve(*self.alice, "analysis", "old")
        before = self.budget.balance(*self.alice)["analysis"]
        self.assertEqual((before["remaining"], before["totalRemaining"]), (0, 1))
        self.assertEqual(before["resetAt"], "2026-11-10T00:00:00+08:00")
        self.failure("analysis_duplicate", self.budget.reserve, *self.alice, "analysis", "old")
        self.failure("analysis_account_limit", self.budget.reserve, *self.alice, "analysis", "other")
        self.now += 1
        after = self.budget.balance(*self.alice)["analysis"]
        self.assertEqual((after["remaining"], after["totalRemaining"]), (1, 1))
        self.assertEqual(after["resetAt"], "2026-11-11T00:00:00+08:00")
        self.failure("analysis_duplicate", self.budget.reserve, *self.alice, "analysis", "old")
        self.budget.reserve(*self.alice, "analysis", "new")
        self.failure("analysis_total_limit", self.budget.reserve, *self.bob, "analysis")
        self.assertEqual(self.budget.balance(*self.bob)["generation"]["totalUsed"], 0)

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
