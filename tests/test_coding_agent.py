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
        for environment, expected in (({}, (300, 100, (1000, 10000))), (overrides, (150, 16, (11, 180)))):
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                args = ["workbench.py", "--coding-agent", "--runs-dir", str(Path(directory))]
                with patch("sys.argv", args), patch.dict("os.environ", environment, clear=True), patch.object(workbench, "serve") as serve:
                    self.assertEqual(workbench.main(), 0)
                settings = serve.call_args.kwargs
                self.assertEqual(
                    (settings["coding_agent_timeout"], settings["coding_agent_max_attempts"], settings["generation_limits"]),
                    expected,
                )


class AgentWebsiteTests(unittest.TestCase):
    def test_large_website_and_table_styles_fit_shared_budget_and_can_be_reopened(self):
        import staged_blueprint as blueprint
        from generation_settings import MAXIMUMS
        import staged_program

        style = "".join(f".item-{i} {{ padding: 10px; color: #234; }}\n" for i in range(350))
        self.assertGreater(len(style.encode()), 4096)
        for spec, code in (
            ({"kind":"website", "title":"茶壶官网", "layout":"brand", "goal":"展示茶具"},
             {"format":"website-code-v1", "markup":"<h1>茶具</h1>", "style":style, "script":"void 0;"}),
            ({"kind":"program", "title":"计算器", "goal":"计算", "storage":"none"},
             {"markup":"<p>计算</p>", "style":style, "script":"void 0;"}),
            ({"kind":"table", "title":"导入表格", "filters":[], "style":style}, None),
        ):
            files = {"spec.json":json.dumps(spec, ensure_ascii=False).encode()}
            if code is not None:
                files["implementation.json"] = json.dumps(code, ensure_ascii=False).encode()
                blueprint.validate("implementation", files["implementation.json"], files)
                self.assertEqual(staged_revision.prepare(files)["source"]["implementation"], code)
            else:
                blueprint.validate("spec", files["spec.json"], {})
            self.assertIn(style.encode(), blueprint.assemble(files))

        with self.assertRaisesRegex(ValueError, "style_reference"):
            staged_program.validate_style(style + "body { background: url(https://example.test/a) }")
        with self.assertRaisesRegex(ValueError, "style_size"):
            staged_program.validate_style(" " * (MAXIMUMS.sourceBytes + 1))

    def test_agent_receives_render_diagnostic_before_accepting_website(self):
        import coding_agent
        from runtime_verify import RuntimeVerificationError

        spec = {"kind":"website", "title":"花房", "layout":"brand", "goal":"介绍花束"}
        prior = {"spec.json":json.dumps(spec).encode()}
        markup = "<h1>花房</h1>"
        responses = []
        good_style = "h1 { color: purple; }"

        def browser_check(html, entries, *, website):
            self.assertTrue(website)
            self.assertEqual(entries, [])
            responses.append(html)
            if good_style.encode() not in html:
                raise RuntimeVerificationError("website_style")

        def validate_submissions(_request, _key, _dispatch, validate, _timeout):
            files = {"tool.html":markup, "tool.css":"", "tool.js":"void 0;"}
            with self.assertRaisesRegex(ValueError, "bad_website_style_empty"):
                validate({"files":files, "decision":{}})
            files["tool.css"] = ".missing { color: purple; }"
            with self.assertRaisesRegex(ValueError, "website_style"):
                validate({"files":files, "decision":{}})
            files["tool.css"] = good_style
            return validate({"files":files, "decision":{}})

        with patch.object(coding_agent, "run", validate_submissions), patch.object(
            coding_agent.runtime_verify, "verify", browser_check
        ):
            result = coding_agent.provide("implementation", "做个花店官网", prior, "fixture-key", None,
                                          timeout_seconds=120)
        self.assertEqual(json.loads(result)["style"], good_style)
        self.assertEqual(len(responses), 2)


class AgentProgramTests(unittest.TestCase):
    def test_calculator_agent_can_style_and_replace_stateless_controls(self):
        import coding_agent
        import staged_blueprint as blueprint

        spec = {"kind":"program", "title":"计算器", "goal":"四则运算", "storage":"none"}
        old = {"markup":"<form><select id='op'><option>加</option></select><button>计算</button></form>",
               "script":"void 0;"}
        markup = "<section class='keys'><button id='plus'>+</button><button id='equals'>=</button><output id='result'></output></section>"
        style = ".keys { display: grid; grid-template-columns: repeat(2, 1fr); }"
        script = "document.getElementById('equals').onclick = () => { document.getElementById('result').textContent = '5'; };"
        base = {"kind":"program", "source":{"spec":spec, "implementation":old}}
        prior = {"spec.json":json.dumps(spec).encode()}

        def submit(request, _key, _dispatch, validate, _timeout):
            self.assertIn("tool.css", request["files"])
            files = {"tool.html":markup, "tool.js":script, "tool.css":style}
            with self.assertRaisesRegex(ValueError, "style_reference"):
                validate({"files":{**files, "tool.css":"body { background: url(https://example.test/a) }"}, "decision":{}})
            return validate({"files":files, "decision":{}})

        with patch.object(coding_agent, "run", submit):
            generated = coding_agent.provide("implementation", "做个四则计算器", prior, "fixture-key", None,
                                            timeout_seconds=120)
            revised = coding_agent.provide("revision-plan", "用按钮替换下拉框和计算按钮", {}, "fixture-key", None,
                                          base=base, timeout_seconds=120)
        artifacts = staged_revision.plan_artifacts(revised, base)
        with self.assertRaises(staged_revision.ApiError):
            staged_revision.describe(base, {"revision-plan.json":revised}, "", [["old", "record"]])
        self.assertEqual(json.loads(generated), json.loads(artifacts["implementation"]))
        html = blueprint.assemble({**prior, "implementation.json":artifacts["implementation"]}).decode()
        self.assertIn(style, html)
        self.assertIn("id='equals'", html)
        self.assertNotIn('<select', html)
        with self.assertRaises(staged_revision.RevisionPatchError):
            stored = {**base, "source":{**base["source"], "spec":{**spec, "storage":"tool"}}}
            staged_revision.plan_artifacts(revised, stored)


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
