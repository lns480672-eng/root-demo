"""TEST-001: enforce the same output contract for the CLI and workbench."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generate

HTML = "<!doctype html><html><body>可用工具</body></html>"
KEY = "example-test-secret"


class GenerationContract(unittest.TestCase):
    def test_only_complete_safe_html_is_saved(self):
        run_generation = getattr(generate, "run_generation", None)
        self.assertTrue(callable(run_generation), "工作台应能调用共用生成检查并获得运行记录")
        for content, finish, expected in (
            (f"```html\n{HTML}\n```", "stop", "generated"),
            (HTML, "length", "incomplete_html"),
            ("<!doctype html><html>", "stop", "incomplete_html"),
            ({"not": "text"}, "stop", "incomplete_html"),
            (HTML.replace("可用工具", KEY), "stop", "credential_in_output"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                response = {"id": "unit-test", "model": generate.MODEL,
                            "choices": [{"message": {"content": content}, "finish_reason": finish}]}
                with patch.object(generate, "request_completion", return_value=(200, json.dumps(response).encode())):
                    run, evidence = run_generation("新需求", KEY, Path(folder))
                self.assertEqual(evidence["status"], expected)
                path = run / "app.html"
                self.assertEqual(path.exists(), expected == "generated")
                if path.exists():
                    self.assertEqual(path.read_text(encoding="utf-8").strip(), HTML)
                self.assertNotIn(KEY, (run / "generation.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
