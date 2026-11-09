"""Populate full-release trial ledgers on a private, offline legacy-state copy."""
from pathlib import Path
import json
import sys

from accounts import Store
from trial_budget import TrialBudget


def migrate(runs):
    store = Store(runs / "workspace.sqlite3")
    TrialBudget(store, runs)
    with store.connection(write=True) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("legacy database failed integrity check")
        actual_calls, = db.execute("SELECT COALESCE(SUM(attempts),0) FROM generation_usage").fetchone()
        if db.execute("SELECT COUNT(*) FROM model_dispatches").fetchone()[0]:
            raise ValueError("only run migration on a legacy copy")
        db.execute("""
            INSERT INTO generation_success(run_id, account)
            SELECT v.run_id, t.owner FROM tool_versions AS v JOIN tools AS t ON t.id=v.tool_id
            WHERE t.published=1
            UNION ALL
            SELECT t.id, t.owner FROM tools AS t WHERE t.published=1
            AND NOT EXISTS (SELECT 1 FROM tool_versions AS v WHERE v.tool_id=t.id)
        """)
        saved, = db.execute("SELECT COUNT(*) FROM generation_success").fetchone()
        if saved > actual_calls:
            raise ValueError("legacy published versions exceed actual model calls")
        records = []
        for path in sorted(runs.glob("run-*/generation.json")):
            evidence = json.loads(path.read_text(encoding="utf-8"))
            outcome = evidence.get("status")
            if outcome not in {"generated", "incomplete_html", "http_error", "request_failed", "credential_in_output"}:
                raise ValueError("unrecognized legacy model outcome")
            records.append(("generation", path.parent.name, int(outcome == "generated")))
        if len(records) > actual_calls:
            raise ValueError("legacy outcomes exceed actual model calls")
        records.extend(("generation", f"legacy-unrecorded-{index}", None)
                       for index in range(actual_calls - len(records)))
        db.executemany("INSERT INTO model_dispatches VALUES (?,?,?)", records)
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("legacy foreign key check failed")
    print(json.dumps({"legacy_calls": actual_calls, "personal_successes": saved, "known_outcomes": sum(
        outcome is not None for _, _, outcome in records
    )}))


if __name__ == "__main__":
    migrate(Path(sys.argv[1]))
