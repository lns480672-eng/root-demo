"""Acceptance checks for the local Agent candidate boundary."""

import json
import unittest

import staged_revision


class AgentRevisionTests(unittest.TestCase):
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
