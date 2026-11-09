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


    def test_model_outcome_stays_successful_when_saving_the_valid_html_fails(self):
        # TEST-017-STAT. Filesystem failures after model validation are excluded from model failures.
        response = {"id": "unit-test", "model": generate.MODEL,
                    "choices": [{"message": {"content": HTML}, "finish_reason": "stop"}]}
        write = Path.write_text

        def fail_html(path, data, **kwargs):
            if path.name == "app.html":
                raise OSError("fixture disk error")
            return write(path, data, **kwargs)

        outcomes = []
        with tempfile.TemporaryDirectory() as folder, patch.object(
            generate, "request_completion", return_value=(200, json.dumps(response).encode())
        ), patch.object(Path, "write_text", fail_html):
            run, evidence = generate.run_generation("新需求", KEY, Path(folder), record_outcome=outcomes.append)
            self.assertEqual(evidence["status"], "request_failed")
            self.assertFalse((run / "app.html").exists())
        self.assertEqual(outcomes, [True])


if __name__ == "__main__":
    unittest.main()
