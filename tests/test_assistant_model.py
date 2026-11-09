"""AC-012 response and admission checks without an external model call."""

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import assistant_model
from accounts import ApiError


KEY = "provider-fixture-secret"
IDEA = {"step": "start", "idea": "团队周报整理起来很慢"}
DIRECTION = {"title": "周报汇总", "description": "按项目整理进展"}
QUESTION = {"prompt": "希望怎么分组？", "options": ["按项目", "按负责人"]}
DRAFT = {"kind": "draft", "text": "做一个可保存的进展记录工具。"}


def upstream(content, finish="stop"):
    return {"status": 200, "body": json.dumps({
        "id": "request-demo",
        "model": "deepseek-flash",
        "usage": {"total_tokens": 82, "unexpected": KEY},
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}],
    })}


class AssistantProviderTest(unittest.TestCase):
    def test_rejects_invalid_input_before_admission(self):
        admit = Mock()
        with patch.object(assistant_model, "_exchange") as transport:
            for data in (
                {"step": "unknown", "idea": "说明"},
                {"step": "start", "idea": "  "},
                {"step": "direction", "idea": "说明", "direction": {"title": KEY, "description": "草稿"}},
                {"step": "answers", "idea": "说明", "direction": DIRECTION,
                 "questions": [QUESTION], "answers": ["未提供的选项"]},
            ):
                with self.assertRaises(ApiError) as failure:
                    assistant_model.analyze(data, KEY, admit)
                self.assertEqual(failure.exception.code, "invalid_analysis")
            transport.assert_not_called()
        admit.assert_not_called()

    def test_provider_json_and_phase_validation(self):
        choices = [DIRECTION, {"title": "提醒列表", "description": "按截止日期提醒"}]
        cases = [
            (IDEA, {"kind": "directions", "understanding": "进展汇总耗时", "directions": choices}),
            ({**IDEA, "step": "direction", "direction": DIRECTION}, {"kind": "questions", "questions": [QUESTION]}),
            ({**IDEA, "step": "answers", "direction": DIRECTION,
              "questions": [QUESTION], "answers": ["按项目"]}, DRAFT),
        ]
        for request, result in cases:
            with self.subTest(step=request["step"]):
                evidence = Mock()

                def exchange(payload, admit):
                    body = json.loads(payload)["body"]
                    self.assertEqual(body["response_format"], {"type": "json_object"})
                    self.assertEqual(body["thinking"], {"type": "disabled"})
                    self.assertEqual(body["max_tokens"], 800)
                    self.assertEqual(json.loads(body["messages"][1]["content"]), request)
                    admit()
                    return upstream(result)

                admit = Mock()
                with patch.object(assistant_model, "_exchange", exchange):
                    self.assertEqual(assistant_model.analyze(request, KEY, admit, record=evidence), result)
                admit.assert_called_once_with()
                self.assertEqual(evidence.call_args.args[0]["usage"], {"total_tokens": 82})
                self.assertEqual(evidence.call_args.args[0]["request_id"], "request-demo")
                self.assertNotIn(KEY, json.dumps(evidence.call_args.args[0]))

    def test_rejects_secret_incomplete_or_wrong_phase_output(self):
        for response in (
            upstream({**DRAFT, "text": KEY}),
            upstream(DRAFT, finish="length"),
            upstream({"kind": "questions", "questions": [QUESTION]}),
            upstream({"kind": "directions", "understanding": "想法", "directions": [DIRECTION, DIRECTION]}),
            upstream({**DRAFT, "extra": "不接受额外字段"}),
        ):
            with patch.object(assistant_model, "_exchange", return_value=response):
                with self.assertRaises(ApiError) as failure:
                    assistant_model.analyze(IDEA, KEY, Mock())
                self.assertEqual(failure.exception.code, "invalid_analysis_result")
                self.assertNotIn(KEY, str(failure.exception))

    def test_rejects_empty_or_duplicate_json_when_provider_reports_stop(self):
        for content in ("   ", '{"kind":"draft","text":"覆盖值","text":"原值"}'):
            response = upstream(DRAFT)
            completion = json.loads(response["body"])
            completion["choices"][0]["message"]["content"] = content
            response["body"] = json.dumps(completion)
            with patch.object(assistant_model, "_exchange", return_value=response):
                with self.assertRaises(ApiError) as failure:
                    assistant_model.analyze(IDEA, KEY, Mock())
            self.assertEqual(failure.exception.code, "invalid_analysis_result")

    def test_answers_requests_only_the_draft_shape(self):
        request = {**IDEA, "step": "answers", "direction": DIRECTION,
                   "questions": [QUESTION], "answers": ["按项目"]}
        with patch.object(assistant_model, "_exchange", return_value=upstream(DRAFT)) as exchange:
            self.assertEqual(assistant_model.analyze(request, KEY, Mock()), DRAFT)
        body = json.loads(exchange.call_args.args[0])["body"]
        self.assertEqual(json.loads(body["messages"][1]["content"]), request)
        system = body["messages"][0]["content"]
        self.assertIn('"kind":"draft"', system)
        self.assertNotIn('"kind":"questions"', system)
        self.assertNotIn('"kind":"directions"', system)

    def test_invalid_response_receipt_distinguishes_failure_without_logging_content(self):
        malformed = upstream(DRAFT)
        completion = json.loads(malformed["body"])
        completion["choices"][0]["message"]["content"] = "invalid-json-" + KEY
        malformed["body"] = json.dumps(completion)
        for response, phase, reason in (
            ({"status": 200, "body": "not-an-envelope"}, "provider_envelope", None),
            (upstream(DRAFT, finish="length"), "finish_reason", "length"),
            (upstream(DRAFT, finish=KEY), "finish_reason", "other"),
            (malformed, "content_json", None),
            (upstream({**DRAFT, "extra": KEY}), "content_schema", "invalid shape"),
            (upstream({**DRAFT, "text": KEY}), "credential_echo", None),
        ):
            with self.subTest(phase=phase):
                record = Mock()
                with patch.object(assistant_model, "_exchange", return_value=response):
                    with self.assertRaises(ApiError) as failure:
                        assistant_model.analyze(IDEA, KEY, Mock(), record=record)
                self.assertEqual(failure.exception.code, "invalid_analysis_result")
                receipt = record.call_args.args[0]
                self.assertEqual(receipt.get("validation_stage"), phase)
                self.assertEqual(receipt.get("validation_reason"), reason)
                self.assertNotIn(KEY, json.dumps(receipt))

    def test_maps_balance_rate_and_timeout_without_retry(self):
        for envelope, code in (
            ({"status": 402}, "analysis_balance"),
            ({"status": 429}, "analysis_rate_limit"),
            ({"error": "timeout"}, "analysis_timeout"),
        ):
            with patch.object(assistant_model, "_exchange", return_value=envelope) as transport:
                with self.assertRaises(ApiError) as failure:
                    assistant_model.analyze(IDEA, KEY, Mock())
                self.assertEqual(failure.exception.code, code)
                transport.assert_called_once()

    def test_admission_denial_does_not_release_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            dispatch = Path(directory) / "started"
            code = (
                "import pathlib,sys\n"
                "sys.stdin.buffer.readline()\n"
                "sys.stdout.buffer.write(b'R');sys.stdout.buffer.flush()\n"
                f"if sys.stdin.buffer.read(1)==b'G': pathlib.Path({str(dispatch)!r}).touch()\n"
            )
            admit = Mock(side_effect=ApiError(503, "model_paused", "暂停"))
            with patch.object(assistant_model, "WORKER_COMMAND", (sys.executable, "-c", code)):
                with self.assertRaises(ApiError) as failure:
                    assistant_model.analyze(IDEA, KEY, admit)
            self.assertEqual(failure.exception.code, "model_paused")
            admit.assert_called_once_with()
            self.assertFalse(dispatch.exists())

    def test_worker_receives_payload_and_permit_without_key_in_environment(self):
        code = (
            "import json,os,sys\n"
            "payload=json.loads(sys.stdin.buffer.readline())\n"
            "assert 'DEEPSEEK_API_KEY' not in os.environ\n"
            f"assert payload['key']=={KEY!r}\n"
            "sys.stdout.buffer.write(b'R');sys.stdout.buffer.flush()\n"
            "assert sys.stdin.buffer.read(1)==b'G'\n"
            f"print({json.dumps(upstream(DRAFT))!r})\n"
        )
        admit = Mock()
        with patch.dict(assistant_model.os.environ, {"DEEPSEEK_API_KEY": KEY}), \
             patch.object(assistant_model, "WORKER_COMMAND", (sys.executable, "-c", code)):
            self.assertEqual(assistant_model.analyze(IDEA, KEY, admit), DRAFT)
        admit.assert_called_once_with()

    def test_worker_is_reaped_after_dispatch_timeout(self):
        code = (
            "import sys,time\n"
            "sys.stdin.buffer.readline()\n"
            "sys.stdout.buffer.write(b'R');sys.stdout.buffer.flush()\n"
            "if sys.stdin.buffer.read(1)==b'G': time.sleep(15)\n"
        )
        admit = Mock()
        started = time.monotonic()
        with patch.object(assistant_model, "WORKER_COMMAND", (sys.executable, "-c", code)), \
             patch.object(assistant_model, "WALL_SECONDS", 0.8):
            with self.assertRaises(ApiError) as failure:
                assistant_model.analyze(IDEA, KEY, admit)
        self.assertEqual(failure.exception.code, "analysis_timeout")
        admit.assert_called_once_with()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_worker_launch_failure_does_not_charge(self):
        admit = Mock()
        with patch.object(assistant_model.subprocess, "Popen", side_effect=OSError):
            with self.assertRaises(ApiError) as failure:
                assistant_model.analyze(IDEA, KEY, admit)
        self.assertEqual(failure.exception.code, "analysis_provider")
        admit.assert_not_called()
