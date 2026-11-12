"""TEST-018-C compatibility and source-only model inputs."""
import json
import unittest
from unittest.mock import Mock, patch

from accounts import ApiError
import assistant_model
import staged_blueprint as blueprint
import staged_revision as revision
from test_staged_blueprint import artifacts, envelope


LEGACY = b"""<!doctype html><html><head><title>Reading list</title>
<style>p{color:green}</style></head><body><h1>Reading list</h1>
<form id="add"><label>Book <input id="book"></label><button>Add</button></form>
<ul id="list" style="list-style:none"></ul><script>
const storageKey = "book-records";
const books = JSON.parse(localStorage.getItem(storageKey) || "[]");
document.querySelector("#add").addEventListener("submit", (event) => {
    event.preventDefault();
    books.push(document.querySelector("#book").value);
    localStorage.setItem(storageKey, JSON.stringify(books));
});
</script></body></html>"""


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode()


class RevisionContextTests(unittest.TestCase):
    def test_generated_revision_patch_applies_to_fixed_code_without_replacing_other_parts(self):
        source = {"markup": "<label>原价</label><button>计算</button>", "script": "void 0;"}
        edits = [{"part": "markup", "before": ">原价<", "after": ">原始金额<"}]
        patched = revision.apply_code_edits(source, edits)
        self.assertEqual(patched, {**source, "markup": "<label>原始金额</label><button>计算</button>"})
        self.assertEqual(source["markup"], "<label>原价</label><button>计算</button>")

        for rejected in (
            [{"part": "markup", "before": ">不存在<", "after": ">原始金额<"}],
            [{"part": "markup", "before": ">", "after": " invalid>"}],
            edits + [{"part": "markup", "before": "原价</label>", "after": "冲突</label>"}],
            [{"part": "markup", "before": source["markup"], "after": "<p>整体重写</p>"}],
            [{"part": "storage", "before": "one", "after": "two"}],
        ):
            with self.subTest(rejected=rejected), self.assertRaises(ApiError):
                revision.apply_code_edits(source, rejected)

    def test_revision_plan_sends_original_requirement_and_fixed_source_to_model(self):
        spec = {"kind": "program", "title": "分账计算", "goal": "平均分摊"}
        code = {"markup": "<label>原价</label><button>计算</button>", "script": "void 0;"}
        base = revision.prepare({"spec.json": encode(spec), "implementation.json": encode(code)})
        requirement = "帮使用者看到输入框就知道填的是起始金额"
        plan = {"status": "proposed", "edits": [
            {"part": "markup", "before": ">原价<", "after": ">起始金额<"},
        ]}
        with patch.object(assistant_model, "_exchange", return_value=envelope(json.dumps(plan))) as exchange:
            artifact = revision.provide("revision-plan", requirement, {}, "fixture-key", Mock(), None, base=base)
        self.assertEqual(json.loads(artifact), plan)
        request = json.loads(json.loads(exchange.call_args.args[0])["body"]["messages"][1]["content"])
        self.assertEqual(request["requirement"], requirement)
        self.assertEqual(request["revision_source"]["source"]["implementation"], code)

    def test_model_adds_nullable_field_using_host_identity_and_impact(self):
        base = revision.prepare(artifacts())
        plan = {"status": "proposed", "operation": {
            "kind": "add_field", "label": "下次检查日", "type": "date",
        }}
        output = revision.plan_artifacts(encode(plan), base)
        spec = json.loads(output["spec"])
        new = spec["fields"][-1]
        self.assertEqual(new, {"id": "field_1", "label": "下次检查日", "type": "date",
                               "required": False, "options": []})
        self.assertEqual(spec["fields"][:-1], base["source"]["spec"]["fields"])
        self.assertIn(new["id"], json.loads(output["entry"])["form"])
        changes = revision.describe(base, {"revision-plan.json": encode(plan)}, "",
                                    [["root-structured-records-v1", '[{"id":1},{"id":2}]']])
        self.assertIn("已有记录 2 条", " ".join(changes))
        self.assertIn("显示为空", " ".join(changes))
        plan["operation"]["required"] = True
        with self.assertRaises(ValueError):
            revision.plan_artifacts(encode(plan), base)

    def test_title_and_csv_patches_reuse_unmodified_checkpoint_artifacts(self):
        original = artifacts()
        base = revision.prepare(original)
        cases = (
            ("只把标题改成「设备，巡检」", {"title": "设备，巡检"}, "设备，巡检", ["filters", "summary", "export"]),
            ("请移除 CSV 导出，其他功能保留", {"removeCsv": True}, "设备故障", ["filters", "summary"]),
        )
        for request, output, expected_title, expected_modules in cases:
            with self.subTest(request=request), patch.object(assistant_model, "_exchange", return_value=envelope(json.dumps(output))) as exchange:
                planned = revision.provide("spec", request, {}, "fixture-key", Mock(), None, base=base)
                spec = json.loads(planned)
                self.assertEqual((spec["title"], spec["modules"]), (expected_title, expected_modules))
                prior = {"spec.json": planned}
                for stage in blueprint.stage_plan(planned)[1:-1]:
                    payload = revision.provide(stage, request, prior, "fixture-key", Mock(), None, base=base)
                    self.assertEqual(json.loads(payload), json.loads(original[stage + ".json"]))
                    prior[stage + ".json"] = payload
                revision.check("assemble", blueprint.assemble(prior), base, request, previous=prior)
                exchange.assert_called_once()

    def test_title_only_refuses_lost_or_changed_modules(self):
        original = artifacts()
        base = revision.prepare(original)
        request = "只把标题改成新版设备台账，保留筛选、统计和 CSV 导出"
        title = {**base["source"]["spec"], "kind": "ledger", "title": "新版设备台账"}
        with self.assertRaises(ApiError) as omitted:
            revision.check("spec", encode({**title, "modules": []}), base, request)
        self.assertEqual(omitted.exception.code, "staged_unsupported")
        revised = {**original, "spec.json": encode({**title, "modules": ["filters", "summary", "export"]})}
        revision.check("assemble", blueprint.assemble(revised), base, request, previous=revised)
        changed = {**revised, "summary.json": encode({
            **base["source"]["summary"], "count": "越界改名",
        })}
        with self.assertRaises(ApiError):
            revision.check("assemble", blueprint.assemble(changed), base, request, previous=changed)

    def test_csv_removal_requires_explicit_request_and_keeps_other_capabilities(self):
        original = artifacts()
        base = revision.prepare(original)
        changed = {**original, "spec.json": encode({
            **base["source"]["spec"], "kind": "ledger", "modules": ["filters", "summary"],
        })}
        del changed["export.json"]
        rendered = blueprint.assemble(changed)
        revision.check("assemble", rendered, base, "请移除 CSV 导出，其他功能保留", previous=changed)
        for request in ("不要移除 CSV 导出", "修改标题为「删除 CSV 导出」",
                        "改标题为示例，删除 CSV 导出，保留筛选", "移除 CSV 导出，但不要移除 CSV 导出"):
            with self.subTest(request=request), self.assertRaises(ApiError):
                revision.check("assemble", rendered, base, request, previous=changed)
        damaged = {**changed, "filters.json": encode({"search": [], "exact": ["status"], "date": "date"})}
        with self.assertRaises(ApiError):
            revision.check("assemble", blueprint.assemble(damaged), base,
                           "删除 CSV 导出", previous=damaged)

    def test_add_filter_keeps_previous_filters_and_requires_named_field(self):
        original = artifacts()
        base = revision.prepare(original)
        changed = {**original, "filters.json": encode({
            **base["source"]["filters"], "exact": ["status", "device"],
        })}
        rendered = blueprint.assemble(changed)
        revision.check("assemble", rendered, base, "增加设备精确筛选", previous=changed)
        with self.assertRaises(ApiError):
            revision.check("assemble", rendered, base, "增加停机分钟精确筛选", previous=changed)
        changed["filters.json"] = encode({"search": [], "exact": ["status", "device"], "date": "date"})
        with self.assertRaises(ApiError):
            revision.check("assemble", blueprint.assemble(changed), base, "增加设备精确筛选", previous=changed)

    def test_website_revision_preserves_the_layout(self):
        spec = {"kind": "website", "layout": "brand", "title": "山野咖啡", "goal": "品牌和菜单"}
        content = {"format": "website-code-v1", "markup": "<h1>山野咖啡</h1><p>品牌介绍</p>",
                   "style": "", "script": "void 0;"}
        source = {"spec.json": encode(spec), "implementation.json": encode(content)}
        base = revision.prepare(source)
        self.assertEqual(base["kind"], "website")
        self.assertEqual(base["storageKeys"], [])
        revision.check("spec", encode(spec), base)
        with self.assertRaises(ApiError) as changed:
            revision.check("spec", encode({**spec, "layout": "landing"}), base)
        self.assertEqual(changed.exception.code, "incompatible_revision")
        with self.assertRaises(ApiError) as unchanged:
            revision.check("assemble", blueprint.assemble(source), base, previous=source)
        self.assertEqual(unchanged.exception.code, "revision_no_change")
        revised = {**source, "implementation.json": encode({
            **content, "markup": "<h1>山野咖啡</h1><p>新的品牌介绍</p>",
        })}
        revision.check("assemble", blueprint.assemble(revised), base, previous=revised)

    def test_website_heading_revision_preserves_other_code(self):
        spec = {"kind": "website", "layout": "brand", "title": "山野咖啡", "goal": "品牌和菜单"}
        content = {"format": "website-code-v1", "markup": "<main><h1>咖啡 &amp; 茶</h1><p>菜单</p></main>",
                   "style": "main { padding: 1rem; }", "script": "void 0;"}
        source = {"spec.json": encode(spec), "implementation.json": encode(content)}
        base = revision.prepare(source)
        requirement = "把首页主标题改为「咖啡 < 茶」"
        changed, _ = revision.local_revision(base, requirement)
        files = {**source, "implementation.json": encode(changed["implementation"])}
        revised_html = blueprint.assemble(files)
        revision.check("assemble", revised_html, base, requirement, previous=files)
        self.assertIn("咖啡 &lt; 茶", revised_html.decode())
        self.assertEqual(changed["implementation"]["script"], content["script"])
        self.assertEqual(changed["implementation"]["style"], content["style"])

    def test_ledger_color_rejects_model_substitution_for_unsupported_request(self):
        original = artifacts()
        original["spec.json"] = encode({**json.loads(original["spec.json"]), "theme": "yellow"})
        base = revision.prepare(original)
        with patch.object(assistant_model, "_exchange", return_value=envelope('{"theme":"gray"}')):
            with self.assertRaises(ApiError) as raised:
                revision.provide("spec", "把颜色改成黑色", {}, "fixture-key", Mock(), None, base=base)
        self.assertEqual(raised.exception.code, "staged_unsupported")

    def test_ledger_color_patch_changes_css_and_preserves_the_original_configuration(self):
        original = artifacts()
        base = revision.prepare(original)
        with patch.object(assistant_model, "_exchange", return_value=envelope('{"theme":"yellow"}')):
            spec = revision.provide("spec", "把颜色改成黄色", {}, "fixture-key", Mock(), None, base=base)
        prior = {"spec.json": spec}
        for stage in blueprint.stage_plan(spec)[1:-1]:
            with patch.object(assistant_model, "_exchange") as exchange:
                content = revision.provide(stage, "把颜色改成黄色", prior, "fixture-key", Mock(), None, base=base)
                exchange.assert_not_called()
            self.assertEqual(json.loads(content), json.loads(original[stage + ".json"]))
            prior[stage + ".json"] = content
        rendered = blueprint.assemble(prior)
        self.assertIn(b"#record-board>section{flex:1 0 12rem;background:#fff1b8}", rendered)
        self.assertNotIn(b"background:#fff1b8", blueprint.assemble(original))
        revision.check("assemble", rendered, base, "把颜色改成黄色", previous=prior)

    def test_ledger_semantically_unchanged_candidate_cannot_publish(self):
        original = artifacts()
        base = revision.prepare(original)
        candidate = {**original, "spec.json": encode({
            **base["source"]["spec"], "kind": "ledger", "modules": ["export", "summary", "filters"],
        }), "entry.json": encode({**base["source"]["entry"], "backup": False})}
        with self.assertRaises(ApiError) as raised:
            revision.check("assemble", blueprint.assemble(candidate), base, previous=candidate)
        self.assertEqual(raised.exception.code, "revision_no_change")

    def test_legacy_uses_bounded_fragment_and_code_keys(self):
        base = revision.prepare({}, LEGACY)
        self.assertEqual(json.loads(json.dumps(base)), base)
        self.assertEqual(base["kind"], "program")
        self.assertEqual(base["storageKeys"], ["book-records"])
        self.assertIn("<h2>Reading list</h2>", base["source"]["implementation"]["markup"])
        self.assertNotIn("style=", base["source"]["implementation"]["markup"])
        self.assertNotIn("<html>", json.dumps(base))
        self.assertNotIn("entries", base)

    def test_targeted_program_copy_preserves_working_markup_script_and_style(self):
        base = revision.prepare({}, LEGACY)
        request = "把页面文案「Add」改为「Add book」"
        spec = revision.provide("spec", request, {}, "", Mock(), None, base=base)
        prior = {"spec.json": spec}
        candidate = revision.provide("implementation", request, prior, "", Mock(), None, base=base)
        revision.check("implementation", candidate, base, request)
        original = base["source"]["implementation"]
        changed = json.loads(candidate)
        self.assertEqual(changed, {**original, "markup": original["markup"].replace(">Add<", ">Add book<")})
        rendered = blueprint.assemble({**prior, "implementation.json": candidate})
        self.assertIn(b"<button>Add book</button>", rendered)
        self.assertIn(b'localStorage.setItem(storageKey, JSON.stringify(books))', rendered)

    def test_field_renames_can_use_a_color_word_explicitly_or_a_single_character(self):
        base = revision.prepare(artifacts())
        for requirement, expected in (("把字段「设备」显示名改为「白色」", "白色"),
                                      ("设备改为白", "白")):
            with self.subTest(requirement=requirement):
                spec = revision.provide("spec", requirement, {}, "", Mock(), None, base=base)
                fields = {field["id"]: field["label"] for field in json.loads(spec)["fields"]}
                self.assertEqual(fields["device"], expected)

    def test_explicit_optional_time_fields_reuse_supported_types(self):
        base = revision.prepare(artifacts())
        for label, field_type in (("文本", "text"), ("日期", "date"), ("整数", "integer")):
            with self.subTest(field_type=field_type):
                request = f"新增可空{label}型字段「阅读时间」"
                spec = revision.provide("spec", request, {}, "", Mock(), None, base=base)
                field = json.loads(spec)["fields"][-1]
                self.assertEqual((field["label"], field["type"], field["required"]), ("阅读时间", field_type, False))

    def test_risky_actions_require_more_than_a_field_label(self):
        base = revision.prepare(artifacts())
        for request in ("删掉「设备」列", "把「设备」改成数字", "新增字段项目并把所有记录清空",
                        "「删除」备注字段", "把所有记录「清空」", "清除所有记录", "去掉备注列",
                        "重置数据", "删记录", "delete all records", "clear data", "把状态改成单选"):
            with self.subTest(request=request), self.assertRaises(ApiError) as raised:
                revision.provide("spec", request, {}, "", Mock(), None, base=base)
            self.assertEqual(raised.exception.code, "revision_risky")

    def test_legacy_rejects_external_or_unbounded_sources(self):
        variants = [
            LEGACY.replace(b"<script>", b'<script src="https://example.test/lib.js">'),
            LEGACY.replace(b"<script>", b'<script type="application/json">'),
            LEGACY.replace(b"<ul", b'<iframe src="https://example.test/"></iframe><ul'),
            LEGACY.replace(b"<form", b'<form onclick="unsafe()"'),
            LEGACY + b" " * revision.MAX_LEGACY_HTML,
        ]
        for source in variants:
            with self.subTest(size=len(source)), self.assertRaises(ApiError) as raised:
                revision.prepare({}, source)
            self.assertEqual(raised.exception.code, "revision_source_unsupported")
        with self.assertRaises(ApiError):
            revision.prepare({**artifacts(), "entries":b'["should never enter model context"]'})

    def test_ledger_preserves_field_types_options_and_required_direction(self):
        base = revision.prepare(artifacts())
        old = base["source"]["spec"]
        optional = {"id":"notes", "label":"备注", "type":"text", "required":False, "options":[]}
        status = old["fields"][-1]
        revised = {**old, "fields":[*old["fields"][:-1], {**status,"options":[*status["options"],"归档"]}, optional]}
        blueprint.validate("spec", encode(revised), {})
        revision.check("spec", encode(revised), base)
        for fields in (
            [*old["fields"][:-1], {**status,"id":"phase"}],
            [*old["fields"][:-1], {**status,"options":["已处理","归档"]}],
            [*old["fields"][:-1], {**status,"options":list(reversed(status["options"]))}],
            [{**old["fields"][0],"type":"date"}, *old["fields"][1:]],
            [*old["fields"], {**optional,"required":True}],
        ):
            with self.subTest(fields=fields), self.assertRaises(ApiError) as raised:
                revision.check("spec", encode({**old,"fields":fields}), base)
            self.assertEqual(raised.exception.code, "incompatible_revision")
        with self.assertRaises(ApiError):
            revision.check("spec", b'{"kind":"program","title":"Different","goal":"Migrate"}', base)

    def test_program_revision_outside_supported_scope_never_calls_model(self):
        base = revision.prepare({}, LEGACY)
        with patch.object(assistant_model, "_exchange") as exchange:
            for requirement, code in (("增加已读筛选", "staged_unsupported"),
                                      ("把所有记录「清空」", "revision_risky")):
                with self.subTest(requirement=requirement), self.assertRaises(ApiError) as raised:
                    revision.provide("spec", requirement, {}, "fixture-key", Mock(), None, base=base)
                self.assertEqual(raised.exception.code, code)
        exchange.assert_not_called()


if __name__ == "__main__":
    unittest.main()
