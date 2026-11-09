"""TEST-018 contracts for each declarative stage and its final candidate."""

import copy
import json
import unittest
from unittest.mock import Mock, patch

from accounts import ApiError
import assistant_model
import staged_blueprint as blueprint


def artifacts():
    field_specs = [
        ("device", "设备", "text", []),
        ("date", "日期", "date", []),
        ("delay", "停机分钟", "integer", []),
        ("status", "处理状态", "choice", ["待处理", "已处理"]),
    ]
    plans = {
        "spec": {
            "title": "设备故障",
            "fields": [{"id": name, "label": label, "type": kind, "options": options, "required": True}
                       for name, label, kind, options in field_specs],
        },
        "entry": {
            "form": ["device", "date", "delay"],
            "columns": [item[0] for item in field_specs],
            "sort": {"field": "date", "direction": "desc"},
            "edit_choice": {"field": "status", "initial": "待处理"},
        },
        "filters": {"search": ["device"], "exact": ["status"], "date": "date"},
        "summary": {"count": "总数", "sum": {"field": "delay", "label": "总分钟"}, "group": None},
        "export": {"columns": [item[0] for item in field_specs], "label": "导出"},
    }
    return {name + ".json": json.dumps(plan, ensure_ascii=False).encode() for name, plan in plans.items()}


def envelope(content, *, finish="stop", request_id="response-1", usage=None):
    return {
        "status": 200,
        "body": json.dumps({
            "id": request_id,
            "model": blueprint.MODEL,
            "choices": [{"finish_reason": finish, "message": {"content": content}}],
            "usage": usage if usage is not None else {"completion_tokens": 15},
        }, ensure_ascii=False),
    }


class StageArtifactTests(unittest.TestCase):
    def test_accepts_one_blueprint_and_requires_exact_assembly(self):
        files = artifacts()
        html = blueprint.assemble(files)
        self.assertIn("<title>设备故障</title>".encode(), html)
        self.assertEqual(blueprint.assemble(dict(reversed(tuple(files.items())))), html)
        blueprint.validate("assemble", html, files)
        with self.assertRaisesRegex(ValueError, "invalid_assembly"):
            blueprint.validate("assemble", b"<!doctype html><html><script>alert(1)</script></html>", files)
        with self.assertRaisesRegex(ValueError, "bad_payload_size"):
            blueprint.assemble({name: value for name, value in files.items() if name != "summary.json"})

    def test_rendered_labels_are_data_and_cannot_open_markup(self):
        files = artifacts()
        spec = json.loads(files["spec.json"])
        spec["title"] = "</TiTle><script>alert(7)</script>"
        files["spec.json"] = json.dumps(spec).encode()
        html = blueprint.assemble(files)
        self.assertIn(b"&lt;/TiTle&gt;&lt;script&gt;", html)
        self.assertIn(br"\u003c/TiTle>\u003cscript>", html)
        self.assertNotIn(b"<script>alert(7)", html)

    def test_rejects_duplicates_bad_unicode_cross_stage_references_and_lost_fields(self):
        files = artifacts()
        spec = json.loads(files["spec.json"])
        duplicate = copy.deepcopy(spec)
        duplicate["fields"].append(spec["fields"][0])
        cases = [
            ("spec", json.dumps(duplicate).encode(), "bad_field_id"),
            ("spec", b'{"title":"x","title":"y","fields":[]}', "duplicate_json_keys"),
            ("spec", b'{"title":"\\ud800","fields":[]}', "bad_title"),
            ("filters", b'{"search":["date"],"exact":[],"date":null}', "bad_field_reference"),
            ("summary", b'{"count":null,"sum":{"field":"device","label":"sum"},"group":null}',
             "bad_field_reference"),
            ("entry", b'{"form":["device"],"columns":["device"],"sort":null,"edit_choice":null}',
             "missing_input"),
            ("export", b'{"columns":["unknown"],"label":"out"}', "bad_field_reference"),
        ]
        for stage, content, error in cases:
            with self.subTest(stage=stage, error=error), self.assertRaisesRegex(ValueError, error):
                blueprint.validate(stage, content, files)


    def test_board_and_backup_are_opt_in_and_reference_the_movable_choice(self):
        files = artifacts()
        entry = json.loads(files["entry.json"])
        entry.update(board="status", allow_edit=True, allow_delete=True, backup=True)
        files["entry.json"] = json.dumps(entry).encode()
        files["export.json"] = b'{"columns":[],"label":null}'
        html = blueprint.assemble(files)
        blueprint.validate("assemble", html, files)
        self.assertTrue(blueprint.permits_backup(files))
        self.assertFalse(blueprint.permits_backup(artifacts()))
        with self.assertRaisesRegex(ValueError, "bad_export_without_backup"):
            blueprint.validate("export", files["export.json"], artifacts())
        for update, error in (
            ({"board": "delay"}, "bad_field_reference"),
            ({"board": "status", "edit_choice": None}, "bad_board_choice"),
            ({"allow_edit": 1}, "bad_entry_flag"),
            ({"backup": "true"}, "bad_entry_flag"),
        ):
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, error):
                blueprint.validate("entry", json.dumps({**entry, **update}).encode(), files)


    def test_unified_program_plan_accepts_one_implementation_without_backup_privilege(self):
        plan = b'{"kind":"program","title":"Calculator","goal":"Add two numbers"}'
        blueprint.validate("spec", plan, {})
        self.assertEqual(blueprint.stage_plan(plan), ("spec", "implementation", "assemble"))
        ledger = artifacts()["spec.json"]
        self.assertEqual(blueprint.stage_plan(ledger), blueprint.STAGES)
        previous = {"spec.json":plan}
        payload = json.dumps({
            "markup": '<form id="numbers"><label>A<input id="a" type="number"></label>'
                      '<button type="submit">Add</button><output id="result">&lt; 3</output></form>',
            "script": 'document.querySelector("#numbers").addEventListener("submit", (e) => {'
                      'e.preventDefault(); document.querySelector("#result").textContent = 1; });',
        }).encode()
        blueprint.validate("implementation", payload, previous)
        previous["implementation.json"] = payload
        result = blueprint.assemble(previous)
        blueprint.validate("assemble", result, previous)
        self.assertIn(b'<title>Calculator</title>', result)
        self.assertIn(b'<form id="numbers">', result)
        self.assertFalse(blueprint.permits_backup(previous))
        with self.assertRaises(ValueError):
            blueprint.validate("entry", artifacts()["entry.json"], previous)
        with self.assertRaises(ValueError):
            blueprint.validate("implementation", payload, {"spec.json":ledger})

    def test_program_rejects_active_markup_urls_and_script_end_escape(self):
        previous = {"spec.json":b'{"kind":"program","title":"Calculator","goal":"Add numbers"}'}
        bad = [
            ('<script>alert(1)</script>', 'safe()'),
            ('<img src=x onerror=alert(1)', 'safe()'),
            ('<button onclick="alert(1)">Go</button>', 'safe()'),
            ('<a href="&#x6a;avascript:alert(1)">Bad</a>', 'safe()'),
            ('<form action="https://example.test/"><button>Go</button></form>', 'safe()'),
            ('<!doctype html><html><body>Bad</body></html>', 'safe()'),
            ('<div><span>x</div>', 'safe()'),
            ('<p>OK</p>', 'const bad = "</ScRiPt><script>alert(1)</script>"'),
        ]
        for markup, script in bad:
            with self.subTest(markup=markup), self.assertRaises(ValueError):
                blueprint.validate("implementation", json.dumps({"markup":markup, "script":script}).encode(), previous)


class StageProviderTests(unittest.TestCase):
    def test_requests_one_json_module_and_records_only_sanitized_evidence(self):
        files = artifacts()
        admitted = Mock()
        receipts = []
        secret = "secret-test-key"
        requirement = "PRIVATE synthetic requirement"

        def exchange(payload, admit, *, wall_seconds):
            sent = json.loads(payload)
            self.assertEqual(sent["key"], secret)
            self.assertEqual(wall_seconds, 45)
            self.assertEqual(sent["wall_seconds"], 45)
            self.assertEqual(assistant_model.WALL_SECONDS, 30)
            self.assertEqual(sent["body"]["response_format"], {"type": "json_object"})
            data = json.loads(sent["body"]["messages"][1]["content"])
            self.assertEqual(data["requirement"], requirement)
            self.assertEqual(data["accepted_spec"]["title"], "设备故障")
            admitted.assert_not_called()
            admit()
            return envelope(files["filters.json"].decode(), request_id=secret,
                            usage={"completion_tokens": 15, "unexpected": requirement})

        with patch.object(assistant_model, "_exchange", exchange):
            result = blueprint.provide("filters", requirement, files, secret, admitted, receipts.append)
        self.assertEqual(json.loads(result), json.loads(files["filters.json"]))
        admitted.assert_called_once_with()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "ok")
        self.assertEqual(receipts[0]["usage"], {"completion_tokens": 15})
        self.assertTrue(receipts[0]["dispatched"])
        self.assertNotIn(requirement, json.dumps(receipts))
        self.assertNotIn(secret, json.dumps(receipts))

    def test_program_provider_builds_only_the_planned_implementation(self):
        spec = b'{"kind":"program","title":"Calculator","goal":"Add two numbers"}'
        implementation = json.dumps({
            "markup": '<p id="answer"></p>',
            "script": 'document.querySelector("#answer").textContent=3;',
        })
        seen = []

        def exchange(payload, admit, *, wall_seconds):
            body = json.loads(payload)["body"]
            request = json.loads(body["messages"][1]["content"])
            stage = request["current_stage"]
            seen.append(stage)
            admit()
            if stage == "spec":
                self.assertIn('"kind":"program"', body["messages"][0]["content"])
                return envelope(spec.decode())
            self.assertEqual(request["accepted_spec"], json.loads(spec))
            self.assertEqual(body["max_tokens"], 3200)
            return envelope(implementation)

        with patch.object(assistant_model, "_exchange", exchange):
            accepted = blueprint.provide("spec", "加法计算", {}, "secret", lambda: None)
            self.assertEqual(blueprint.stage_plan(accepted), ("spec", "implementation", "assemble"))
            blueprint.provide("implementation", "加法计算", {"spec.json":accepted}, "secret", lambda: None)
            with self.assertRaises(ApiError):
                blueprint.provide("entry", "加法计算", {"spec.json":accepted}, "secret", lambda: None)
        self.assertEqual(seen, ["spec", "implementation"])

    def test_invalid_and_unsupported_results_are_not_reported_as_success(self):
        for result, finish, error in (
            ('{"unsupported":true}', "stop", "staged_unsupported"),
            ('{"title":"x","fields":[]}', "stop", "invalid_staged_result"),
            ('{"unfinished":true}', "length", "invalid_staged_result"),
            ('{"title":"test-\\u006bey","fields":[]}', "stop", "invalid_staged_result"),
        ):
            def exchange(payload, admit, **_):
                admit()
                return envelope(result, finish=finish)

            receipts = []
            with self.subTest(error=error), patch.object(assistant_model, "_exchange", exchange):
                with self.assertRaises(ApiError) as raised:
                    blueprint.provide("spec", "做一个台账", {}, "test-key", Mock(), receipts.append)
            self.assertEqual(raised.exception.code, error)
            self.assertEqual(receipts[0]["status"], error)
            self.assertTrue(receipts[0]["dispatched"])

    def test_rejected_admission_and_transport_timeout_are_distinct(self):
        def exchange(payload, admit, **_):
            admit()
            raise ApiError(504, "analysis_timeout", "timeout")

        paused = ApiError(503, "model_paused", "paused")
        for admit, code, dispatched in (
            (Mock(side_effect=paused), "model_paused", False),
            (Mock(), "staged_timeout", True),
        ):
            receipts = []
            with patch.object(assistant_model, "_exchange", exchange), self.assertRaises(ApiError) as raised:
                blueprint.provide("spec", "做一个台账", {}, "test-key", admit, receipts.append)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(receipts[0]["status"], code)
            self.assertIs(receipts[0]["dispatched"], dispatched)


if __name__ == "__main__":
    unittest.main()
