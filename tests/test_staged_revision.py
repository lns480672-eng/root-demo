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
    def test_legacy_uses_bounded_fragment_and_code_keys(self):
        base = revision.prepare({}, LEGACY)
        self.assertEqual(json.loads(json.dumps(base)), base)
        self.assertEqual(base["kind"], "program")
        self.assertEqual(base["storageKeys"], ["book-records"])
        self.assertIn("<h2>Reading list</h2>", base["source"]["implementation"]["markup"])
        self.assertNotIn("style=", base["source"]["implementation"]["markup"])
        self.assertNotIn("<html>", json.dumps(base))
        self.assertNotIn("entries", base)

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

    def test_model_receives_only_prior_stage_and_rejection_is_recorded_before_success(self):
        base = revision.prepare({}, LEGACY)
        spec = encode(base["source"]["spec"])
        prior = {"spec.json":spec}
        bad = {"markup":"<p>新版</p>","script":"localStorage.getItem('renamed-key');"}
        good = {"markup":"<p>新版</p>","script":"localStorage.getItem('book-records');"}
        responses = iter((bad, good))
        receipts = []
        admit = Mock()

        def exchange(payload, admit, **_):
            body = json.loads(payload)["body"]
            message = json.loads(body["messages"][1]["content"])
            self.assertEqual(set(message["revision_source"]), {"kind", "storageKeys", "prior_stage"})
            self.assertEqual(message["revision_source"]["prior_stage"], base["source"]["implementation"])
            self.assertNotIn("entries", message)
            self.assertEqual(message["requirement"], "增加已读筛选")
            admit()
            return envelope(json.dumps(next(responses)))

        with patch.object(assistant_model, "_exchange", exchange):
            with self.assertRaises(ApiError) as raised:
                revision.provide("implementation", "增加已读筛选", prior, "fixture-key", admit, receipts.append,
                                 base=base)
            self.assertEqual(raised.exception.code, "incompatible_revision")
            result = revision.provide("implementation", "增加已读筛选", prior, "fixture-key", admit, receipts.append,
                                      base=base)
        self.assertEqual(json.loads(result), good)
        self.assertEqual([row["status"] for row in receipts], ["incompatible_revision", "ok"])
        self.assertEqual(admit.call_count, 2)


if __name__ == "__main__":
    unittest.main()
