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
    def test_record_code_preserves_model_markup_style_and_interaction(self):
        spec = json.loads(artifacts()["spec.json"])
        spec.update(kind="ledger", modules=[], implementation="record-code-v1")
        previous = {"spec.json": json.dumps(spec).encode()}
        self.assertEqual(blueprint.stage_plan(previous["spec.json"]), ("spec", "implementation", "assemble"))
        with self.assertRaises(ValueError):
            blueprint.assemble(previous)
        code = {
            "format": "record-code-v1",
            "markup": '<main><button id="move" type="button">归档</button></main>',
            "style": '#move { background: #b6d4ca }',
            "script": "document.querySelector('#move').onclick = () => rootRecords.update(1, {status: '已处理'});",
        }
        for unsafe in ({**code, "style": "body{background:url(https://example.com/a)}"},
                       {**code, "markup": '<iframe src="/"></iframe>'}):
            with self.assertRaises(ValueError):
                blueprint.validate("implementation", json.dumps(unsafe).encode(), previous)
        previous["implementation.json"] = json.dumps(code).encode()
        blueprint.validate("implementation", previous["implementation.json"], previous)
        page = blueprint.assemble(previous).decode()
        for field in ("markup", "style", "script"):
            self.assertIn(code[field], page)
        self.assertNotIn('id="record-board"', page)
        self.assertFalse(blueprint.permits_backup(previous))

    def test_record_code_accepts_native_form_validation_opt_out(self):
        for markup in ("<form novalidate></form>", '<form novalidate=""></form>',
                       '<form novalidate="novalidate"></form>'):
            with self.subTest(markup=markup):
                blueprint.staged_record_code.validate({
                    "format": "record-code-v1", "markup": markup, "style": "", "script": "void 0;",
                })
        for markup in ('<div novalidate></div>', '<form novalidate="false"></form>',
                       '<form novalidate novalidate></form>',
                       '<form novalidate action="/external"></form>'):
            with self.subTest(markup=markup), self.assertRaisesRegex(ValueError, "bad_program_attribute"):
                blueprint.staged_record_code.validate({
                    "format": "record-code-v1", "markup": markup, "style": "", "script": "void 0;",
                })

    def test_record_code_accepts_semantic_table_headers_without_opening_attribute_policy(self):
        import staged_record_code
        code = {
            "format": "record-code-v1",
            "markup": '<table><thead><tr><th scope="col">巡检点位</th></tr></thead><tbody></tbody></table>',
            "style": "table { width: 100% }",
            "script": "rootRecords.list();",
        }
        staged_record_code.validate(code)
        for markup in (
            code["markup"].replace('scope="col"', 'scope="other"'),
            '<div scope="col"></div>',
            code["markup"].replace('scope="col"', 'scope="col" scope="row"'),
            code["markup"].replace('scope="col"', 'scope="col" onclick="alert(1)"'),
            '<form action="https://example.com/"></form>',
        ):
            with self.subTest(markup=markup), self.assertRaisesRegex(ValueError, "bad_program_attribute"):
                staged_record_code.validate({**code, "markup": markup})

    def test_record_mutation_only_changes_requested_record_and_validated_fields(self):
        from staged_record_code import mutate
        fields = json.loads(artifacts()["spec.json"])["fields"]
        first = {"id": 1, "device": "风扇", "date": "2026-11-13", "delay": 2, "status": "待处理"}
        second = {**first, "id": 2, "device": "电机"}
        updated = mutate(fields, [first, second], {"action": "update", "id": 1, "values": {"status": "已处理"}})
        self.assertEqual(updated, [{**first, "status": "已处理"}, second])
        self.assertEqual(first["status"], "待处理")
        for operation in (
            {"action": "replace", "values": []},
            {"action": "update", "id": 1, "values": {"id": 2}},
            {"action": "update", "id": 1, "values": {"date": "2026-02-30"}},
            {"action": "update", "id": 2, "values": {"delay": True}},
            {"action": "update", "id": 1, "values": {"status": "不存在"}},
            {"action": "update", "id": 3, "values": {"status": "已处理"}},
        ):
            with self.subTest(operation=operation), self.assertRaises(ApiError):
                mutate(fields, [first, second], operation)
        self.assertEqual(mutate(fields, [first, second], {"action": "create", "values": {
            key: value for key, value in first.items() if key != "id"
        }}), [first, second, {**first, "id": 3}])

    def test_record_null_values_normalize_only_for_optional_fields(self):
        fields = [{"id":"date", "type":"date", "required":False},
                  {"id":"name", "type":"text", "required":True}]
        create = {"action":"create", "values":{"name":"书", "date":None}}
        self.assertEqual(blueprint.staged_record_code.mutate(fields, [], create), [{"id":1, "name":"书", "date":""}])
        with self.assertRaises(ApiError):
            blueprint.staged_record_code.mutate(fields, [], {**create, "values":{"name":None}})

    def test_ledger_runs_only_the_requested_optional_modules(self):
        files = artifacts()
        spec = json.loads(files["spec.json"])
        for modules, expected in (
            ([], ("spec", "entry", "assemble")),
            (["summary", "filters"], ("spec", "entry", "filters", "summary", "assemble")),
            (["export"], ("spec", "entry", "export", "assemble")),
        ):
            with self.subTest(modules=modules):
                planned = json.dumps({**spec, "kind": "ledger", "modules": modules}).encode()
                self.assertEqual(blueprint.stage_plan(planned), expected)
                parts = {stage + ".json": files[stage + ".json"] for stage in expected[1:-1]}
                parts["spec.json"] = planned
                blueprint.validate("assemble", blueprint.assemble(parts), parts)
        for modules in (["unknown"], ["export", "export"], "export"):
            with self.subTest(invalid=modules), self.assertRaisesRegex(ValueError, "bad_ledger_modules"):
                blueprint.stage_plan(json.dumps({**spec, "modules": modules}).encode())

    def test_requested_csv_must_have_columns_even_with_json_backup(self):
        files = artifacts()
        spec = json.loads(files["spec.json"])
        files["spec.json"] = json.dumps({**spec, "modules": ["export"]}).encode()
        entry = json.loads(files["entry.json"])
        files["entry.json"] = json.dumps({**entry, "backup": True}).encode()
        with self.assertRaisesRegex(ValueError, "bad_empty_ledger_module"):
            blueprint.validate("export", b'{"columns":[],"label":null}', files)

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
        # Old committed plans retain the export checkpoint even when they need neither export nor backup.
        legacy = artifacts()
        legacy["export.json"] = files["export.json"]
        self.assertEqual(blueprint.stage_plan(legacy["spec.json"]), blueprint.STAGES)
        blueprint.validate("assemble", blueprint.assemble(legacy), legacy)
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
    def test_manual_website_layout_must_match_the_selected_category(self):
        website = {"kind": "website", "layout": "landing", "title": "山野咖啡", "goal": "品牌介绍与菜单"}
        with patch.object(assistant_model, "_exchange", return_value=envelope(json.dumps(website))):
            saved = blueprint.provide("spec", "山野咖啡官网", {}, "fixture-key", Mock(), category="landing")
            self.assertEqual(json.loads(saved), website)
            with self.assertRaises(ApiError):
                blueprint.provide("spec", "山野咖啡官网", {}, "fixture-key", Mock(), category="brand")

    def test_new_website_uses_model_code_instead_of_a_content_template(self):
        spec = {"kind": "website", "layout": "brand", "title": "风筝工坊", "goal": "展示工坊和课程"}
        previous = {"spec.json": json.dumps(spec).encode()}
        model_code = {
            "format": "website-code-v1",
            "markup": "<main id='workshop'><h1>独立生成标记</h1></main>",
            "style": "#workshop { padding: 16px; }",
            "script": "document.getElementById('workshop').hidden = false;",
        }
        requirement = "为风筝工坊做官网，偏好活泼的插画风"
        with patch.object(assistant_model, "_exchange", return_value=envelope(json.dumps(model_code))) as dispatch:
            previous["implementation.json"] = blueprint.provide(
                "implementation", requirement, previous, "fixture-key", Mock(), category="brand")
        messages = json.loads(dispatch.call_args.args[0])["body"]["messages"]
        self.assertEqual(json.loads(messages[1]["content"])["requirement"], requirement)
        for criterion in ("受众", "首要任务", "用户视觉偏好", "字阶", "390px", "focus-visible"):
            self.assertIn(criterion, messages[0]["content"])
        app = blueprint.assemble(previous).decode()
        self.assertIn(model_code["markup"], app)
        self.assertIn(model_code["style"], app)
        self.assertIn(model_code["script"], app)

    def test_new_website_rejects_template_content_and_external_css(self):
        spec = {"kind": "website", "layout": "brand", "title": "风筝工坊", "goal": "品牌和课程"}
        previous = {"spec.json": json.dumps(spec).encode()}
        code = {"format": "website-code-v1", "markup": "<h1>风筝工坊</h1>", "script": "void 0;"}
        invalid = [
            {"intro": {"heading": "旧格式"}},
            {**code, "style": "@import 'https://example.invalid/style.css';"},
            {**code, "style": "h1 { background: url(https://example.invalid/image); }"},
            {**code, "style": "h1 { background: u" + chr(92) + "72l(https://example.invalid/image); }"},
            {**code, "style": "</style><script>void 0;</script>"},
            {**code, "style": "", "script": "void 0;</script><script>void 0;"},
            {**code, "style": "", "markup": "<iframe src='https://example.invalid'></iframe>"},
        ]
        for result in invalid:
            with self.subTest(result=result), patch.object(
                assistant_model, "_exchange", return_value=envelope(json.dumps(result))
            ), self.assertRaises(ApiError) as error:
                blueprint.provide("implementation", "品牌官网", previous, "fixture-key", Mock(), category="brand")
            self.assertEqual(error.exception.code, "invalid_staged_result")

    def test_provider_and_model_json_failures_have_distinct_sanitized_diagnostics(self):
        for payload, code, finish in (
            ({"status": 200, "body": '{"choices":['}, "invalid_provider_json", None),
            (envelope('{"markup":"hello","script":"const x = "private-value";"}'), "invalid_model_json", "stop"),
        ):
            with self.subTest(code=code):
                receipts = []
                spec = b'{"kind":"program","title":"Example","goal":"Example tool"}'
                with patch.object(assistant_model, "_exchange", return_value=payload):
                    with self.assertRaises(ApiError) as failure:
                        blueprint.provide("implementation", "interactive page", {"spec.json": spec},
                                          "fixture-key", Mock(), receipts.append)
                self.assertEqual(failure.exception.code, "invalid_staged_result")
                self.assertEqual(receipts[0]["validation"], code)
                self.assertEqual(receipts[0].get("finish_reason"), finish)
                self.assertNotIn("private-value", json.dumps(receipts))

    def test_entry_uses_committed_field_ids_and_reports_invalid_reference_without_its_value(self):
        files = artifacts()
        valid = json.loads(files["entry.json"])
        invalid = {**valid, "board": "private-invalid-alias"}
        results = iter((invalid, valid))
        receipts = []
        admitted = Mock()

        def exchange(payload, admit, **_):
            body = json.loads(payload)["body"]
            context = json.loads(body["messages"][1]["content"])
            self.assertEqual(context["entry_fields"], {
                "ids": ["device", "date", "delay", "status"],
                "sortable_ids": ["date", "delay"],
                "choice_ids": ["status"],
            })
            system = body["messages"][0]["content"]
            self.assertIn("从 accepted_spec.fields 对应字段的 options 逐字复制 initial", system)
            self.assertIn("form 不得包含该状态字段，columns 必须包含该状态字段", system)
            admit()
            return envelope(json.dumps(next(results)))

        with patch.object(assistant_model, "_exchange", exchange):
            with self.assertRaises(ApiError) as failure:
                blueprint.provide("entry", "按状态分栏", files, "fixture-key", admitted, receipts.append)
            result = blueprint.provide("entry", "按状态分栏", files, "fixture-key", admitted, receipts.append)
        self.assertEqual(failure.exception.code, "invalid_staged_result")
        self.assertEqual(receipts[0]["validation"], "bad_field_reference")
        self.assertEqual(receipts[0]["reference_slot"], "board")
        self.assertEqual(receipts[0]["reference_problem"], "unknown_id")
        self.assertNotIn("private-invalid-alias", json.dumps(receipts))
        self.assertEqual(admitted.call_count, 2)
        self.assertEqual(json.loads(result), valid)

    def test_entry_reports_each_state_constraint_and_rejects_the_result(self):
        files = artifacts()
        valid = json.loads(files["entry.json"])
        for updates, code in (
            ({"edit_choice": {"field": "status", "initial": "private-option"}}, "bad_choice_initial"),
            ({"form": [*valid["form"], "status"]}, "bad_choice_in_form"),
            ({"columns": [field for field in valid["columns"] if field != "status"]}, "bad_choice_missing_column"),
        ):
            with self.subTest(code=code):
                bad = json.dumps({**valid, **updates})
                receipts = []
                admit, accept = Mock(), Mock()

                def exchange(payload, before_dispatch, **_):
                    before_dispatch()
                    return envelope(bad)

                with patch.object(assistant_model, "_exchange", exchange), self.assertRaises(ApiError) as failure:
                    blueprint.provide("entry", "新增后可修改状态", files, "fixture-key", admit,
                                      receipts.append, accept=accept)
                self.assertEqual(failure.exception.code, "invalid_staged_result")
                self.assertEqual(receipts[0]["validation"], code)
                self.assertNotIn("private-option", json.dumps(receipts))
                admit.assert_called_once_with()
                accept.assert_not_called()

    def test_new_ledger_spec_cannot_fall_back_to_a_legacy_plan(self):
        legacy = artifacts()["spec.json"]
        with patch.object(assistant_model, "_exchange", return_value=envelope(legacy.decode())):
            with self.assertRaises(ApiError) as failure:
                blueprint.provide("spec", "只录入并保存", {}, "fixture-secret", Mock())
        self.assertEqual(failure.exception.code, "invalid_staged_result")
        self.assertEqual(blueprint.stage_plan(legacy), blueprint.STAGES)

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
            system = body["messages"][0]["content"]
            self.assertIn("、".join(sorted(blueprint.staged_program.ELEMENTS)), system)
            self.assertIn("、".join(sorted(blueprint.staged_program.ATTRIBUTES)), system)
            self.assertIn("style", system)
            return envelope(implementation)

        with patch.object(assistant_model, "_exchange", exchange):
            accepted = blueprint.provide("spec", "加法计算", {}, "secret", lambda: None)
            self.assertEqual(blueprint.stage_plan(accepted), ("spec", "implementation", "assemble"))
            blueprint.provide("implementation", "加法计算", {"spec.json":accepted}, "secret", lambda: None)
            with self.assertRaises(ApiError):
                blueprint.provide("entry", "加法计算", {"spec.json":accepted}, "secret", lambda: None)
        self.assertEqual(seen, ["spec", "implementation"])

    def test_record_spec_distinguishes_form_inputs_from_persisted_workflow_fields(self):
        spec = json.loads(artifacts()["spec.json"])
        spec.update(kind="ledger", modules=[])
        requirement = "雨具借还看板。表单只填雨具名称，新增默认待归还，点击移动到已归还，刷新保留状态。"

        def exchange(payload, admit, **_):
            messages = json.loads(payload)["body"]["messages"]
            self.assertIn("表单只填", messages[0]["content"])
            self.assertIn("持久化的 choice 字段", messages[0]["content"])
            self.assertEqual(json.loads(messages[1]["content"])["requirement"], requirement)
            admit()
            return envelope(json.dumps(spec))

        with patch.object(assistant_model, "_exchange", exchange):
            result = blueprint.provide("spec", requirement, {}, "fixture-key", lambda: None, record_code=True)
        self.assertEqual(json.loads(result)["implementation"], "record-code-v1")

    def test_program_provider_rejects_attributes_outside_the_markup_contract(self):
        previous = {"spec.json": b'{"kind":"program","title":"Split","goal":"Split a total"}'}
        for attribute in ("style='color:red'", "onclick='calculate()'", "formaction='/other'"):
            with self.subTest(attribute=attribute):
                implementation = {
                    "markup": f"<button {attribute}>Calculate</button>",
                    "script": "document.querySelector('button').addEventListener('click', calculate);",
                }
                receipts = []
                accept = Mock()

                def exchange(payload, admit, **_):
                    admit()
                    return envelope(json.dumps(implementation))

                admit = Mock()
                with patch.object(assistant_model, "_exchange", exchange), self.assertRaises(ApiError) as rejected:
                    blueprint.provide("implementation", "费用分摊", previous, "fixture-key", admit,
                                      receipts.append, accept=accept)
                self.assertEqual(rejected.exception.code, "invalid_staged_result")
                self.assertEqual(receipts[0]["validation"], "bad_program_attribute")
                admit.assert_called_once_with()
                accept.assert_not_called()

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
