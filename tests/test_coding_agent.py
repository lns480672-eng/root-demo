"""Acceptance checks for the local Agent candidate boundary."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import staged_revision
import workbench


class AgentConfigurationTests(unittest.TestCase):
    def test_trial_limits_default_and_can_be_overridden(self):
        overrides = {
            "ROOT_AGENT_TIMEOUT_SECONDS": "150",
            "ROOT_AGENT_MAX_MODEL_CALLS": "16",
            "ROOT_ACCOUNT_GENERATIONS": "11",
            "ROOT_TOTAL_GENERATIONS": "180",
        }
        for environment, expected in (({}, (120, 12, (10, 120))), (overrides, (150, 16, (11, 180)))):
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                args = ["workbench.py", "--coding-agent", "--runs-dir", str(Path(directory))]
                with patch("sys.argv", args), patch.dict("os.environ", environment, clear=True), patch.object(workbench, "serve") as serve:
                    self.assertEqual(workbench.main(), 0)
                settings = serve.call_args.kwargs
                self.assertEqual(
                    (settings["coding_agent_timeout"], settings["coding_agent_max_attempts"], settings["generation_limits"]),
                    expected,
                )


class AgentRevisionTests(unittest.TestCase):
    def test_backfill_request_cannot_be_satisfied_with_page_code_alone(self):
        import coding_agent

        spec = {"kind":"ledger", "title":"巡检", "modules":[], "implementation":"record-code-v1",
                "fields":[{"id":"note", "label":"备注", "type":"text", "required":False, "options":[]}]}
        original = {"format":"record-code-v1", "markup":"<p>巡检</p>", "style":"",
                    "script":"window.rootRecords.list();"}
        base = {"kind":"record_code", "source":{"spec":spec, "implementation":original}}
        request = "把已有记录里空白的「备注」统一填为「待复核」，有值的备注保留。"

        def validate_submissions(_request, _key, _dispatch, validate, _timeout):
            files = {"tool.html":"<p>巡检</p><p>待复核</p>", "tool.css":"", "tool.js":original["script"]}
            with self.assertRaisesRegex(ValueError, "missing_record_data_proposal"):
                validate({"files":files, "decision":{}})
            return validate({"files":files, "decision":{
                "operation":{"kind":"backfill", "field":"note", "value":"待复核"},
            }})

        with patch.object(coding_agent, "run", validate_submissions):
            result = coding_agent.provide("revision-plan", request, {}, "fixture-key", None,
                                          base=base, timeout_seconds=120)
        self.assertEqual(json.loads(result)["operation"]["kind"], "backfill")

    def test_candidate_cannot_use_an_undeclared_new_record_field(self):
        spec = {"kind":"ledger", "title":"书单", "modules":[], "implementation":"record-code-v1",
                "fields":[{"id":"book", "label":"书名", "type":"text", "required":True, "options":[]}]}
        original = {"format":"record-code-v1", "markup":"<form><input id='book'></form>",
                    "style":"", "script":"window.rootRecords.list();"}
        base = {"kind":"record_code", "source":{"spec":spec, "implementation":original}}
        updated = {**original, "markup":"<form><input id='book'><input id='archive' name='field_1' type='date'></form>"}
        plan = {"status":"proposed", "implementation":updated}
        with self.assertRaisesRegex(ValueError, "undeclared_record_field"):
            staged_revision.plan_artifacts(json.dumps(plan).encode(), base)

    def test_data_change_reports_exact_effect_and_preserves_input(self):
        field = {"id":"count", "label":"数量", "type":"text", "required":False, "options":[]}
        base = {"kind":"record_code", "source":{"spec":{"fields":[field]}}}
        entries = [["root-structured-records-v1", json.dumps([{"id":1,"count":"12"},{"id":2,"count":""}])]]
        operation = {"kind":"change_type", "field":"count", "type":"integer"}
        impact, updated = staged_revision.record_data_change(base, operation, entries)
        self.assertEqual(impact["affected"], 1)
        self.assertEqual(impact["examples"], [{"id":1, "before":"12", "after":12}])
        self.assertEqual(json.loads(updated[0][1]), [{"id":1,"count":12},{"id":2,"count":""}])
        self.assertEqual(json.loads(entries[0][1])[0]["count"], "12")
        with self.assertRaises(staged_revision.ApiError):
            staged_revision.record_data_change(base, operation, [["root-structured-records-v1", '[{"id":1,"count":"十二"}]']])
        backfill = {"kind":"backfill", "field":"count", "value":"待清点"}
        impact, updated = staged_revision.record_data_change(base, backfill, entries)
        self.assertEqual(impact, {"affected":1, "examples":[{"id":2, "before":"", "after":"待清点"}]})
        self.assertEqual(json.loads(updated[0][1]), [{"id":1,"count":"12"},{"id":2,"count":"待清点"}])

    def test_candidate_can_add_website_navigation_without_exact_anchors(self):
        spec = {"kind": "website", "title": "路线", "layout": "brand", "goal": "介绍路线"}
        original = {"format": "website-code-v1", "markup": '<nav><a href="#home">首页</a></nav><section id="home">首页</section>',
                    "style": "", "script": "void 0;"}
        updated = {**original, "markup": original["markup"].replace("</nav>", '<a href="#trail">路线</a></nav>')
                   + '<section id="trail">路线详情</section>'}
        base = {"kind": "website", "source": {"spec": spec, "implementation": original}}
        plan = {"status": "proposed", "implementation": updated}
        files = staged_revision.plan_artifacts(json.dumps(plan).encode(), base)
        self.assertEqual(json.loads(files["implementation"]), updated)

    def test_candidate_cannot_drop_record_controls_or_retype_existing_fields(self):
        spec = {"kind": "ledger", "title": "书单", "modules": [], "implementation": "record-code-v1",
                "fields": [{"id": "book", "label": "书名", "type": "text", "required": True, "options": []}]}
        original = {"format": "record-code-v1", "markup": '<form><input id="book"></form>',
                    "style": "", "script": "window.rootRecords.list();"}
        base = {"kind": "record_code", "source": {"spec": spec, "implementation": original}}
        for plan in (
            {"status": "proposed", "implementation": {**original, "markup": "<p>新版</p>"}},
            {"status": "proposed", "implementation": original,
             "operation": {"kind": "change_type", "label": "书名", "type": "date", "required": False}},
        ):
            with self.subTest(plan=plan), self.assertRaises((ValueError, staged_revision.ApiError)):
                staged_revision.plan_artifacts(json.dumps(plan).encode(), base)


class AgentDispatchTests(unittest.TestCase):
    def test_host_counts_each_call_and_rejects_over_budget_before_provider_dispatch(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch

        import coding_agent

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("pi-agent-core", "pi-ai"):
                entry = root / name / "dist" / "index.js"
                entry.parent.mkdir(parents=True)
                entry.touch()
            helper = root / "fixture.mjs"
            helper.write_text("""
import {createInterface} from 'node:readline';
const lines = createInterface({input:process.stdin})[Symbol.asyncIterator]();
await lines.next();
console.log(JSON.stringify({kind:'dispatch'}));
if (JSON.parse((await lines.next()).value).ok) {
  console.log(JSON.stringify({kind:'receipt',status:'ok',usage:{total_tokens:12},finish_reason:'toolUse'}));
}
console.log(JSON.stringify({kind:'dispatch'}));
if (!(await lines.next()).done) throw Error('Unbudgeted dispatch permitted');
""")
            receipts = []
            reservations = []

            def dispatch():
                if reservations:
                    raise staged_revision.ApiError(429, "staged_exhausted", "任务已达到上限")
                reservations.append(True)
                return receipts.append

            with patch.object(coding_agent, "HELPER", helper), patch.dict("os.environ", {"ROOT_PI_MODULES":directory}):
                with self.assertRaises(staged_revision.ApiError) as caught:
                    coding_agent.run({"stage":"implementation"}, "fixture-key", dispatch, self.fail, 5)
            self.assertEqual(caught.exception.code, "staged_exhausted")
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["usage"], {"total_tokens":12})
