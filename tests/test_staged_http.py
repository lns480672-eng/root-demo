"""TEST-018-B1 using the actual HTTP entry and controlled model dispatch."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Barrier
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from accounts import Store


PROGRAM = """
from pathlib import Path
import json
import sys
import time
from unittest.mock import patch
import assistant_model
import generate
import staged_revision
from staged_jobs import StagedJobs
import workbench
from accounts import ApiError, Store

publish = StagedJobs.publish
def delayed_publish(self, token, marker, task, data):
    if data.get("entries") == [["late", "test"]]:
        (self.runs / "publish-waiting").touch()
        deadline = time.monotonic() + 4
        while not (self.runs / "publish-go").exists() and time.monotonic() < deadline:
            time.sleep(.01)
    return publish(self, token, marker, task, data)

StagedJobs.publish = delayed_publish

def ordinary(requirement, key):
    return 200, json.dumps({"choices":[{"finish_reason":"stop",
        "message":{"content":"<!doctype html><html><body>单轮工具</body></html>"}}]}).encode()

spec = {"title":"设备台账","fields":[
    {"id":"name","label":"名称","type":"text","required":True,"options":[]},
    {"id":"count","label":"数量","type":"integer","required":True,"options":[]},
    {"id":"status","label":"状态","type":"choice","required":True,"options":["待处理","已完成"]}]}
modules = {
    "spec":spec,
    "entry":{"form":["name","count"],"columns":["name","count","status"],"sort":None,
             "edit_choice":{"field":"status","initial":"待处理"},"backup":True},
    "filters":{"search":["name"],"exact":["status"],"date":None},
    "summary":{"count":"笔数","sum":{"field":"count","label":"合计"},"group":None},
    "export":{"columns":["name","count","status"],"label":"导出"},
}

failed = set()
def stages(stage, requirement, prior, key, admit, record):
    admit()
    if requirement.startswith("超出"):
        record({"stage":stage,"status":"staged_unsupported","dispatched":True})
        raise ApiError(422, "staged_unsupported", "固定替身表示不支持")
    if requirement.startswith("计算"):
        content = {"kind":"program","title":"加法计算器","goal":"计算两数和"} if stage == "spec" else {
            "markup":'<p id="answer"></p><p id="caption">相加</p>', "script":'document.querySelector("#answer").textContent="5";'}
        if stage == "implementation" and requirement.endswith("启动失败探针"):
            content["script"] = 'throw new Error("synthetic startup failure");'
        record({"stage":stage,"status":"ok","dispatched":True})
        return json.dumps(content).encode()
    if requirement.startswith("登记"):
        legacy = requirement.startswith("登记旧任务")
        csv = requirement.startswith("登记并导出")
        backup = csv or requirement.startswith("登记并备份")
        fixtures = {
            **modules,
            "spec":spec if legacy else {**spec, "kind":"ledger", "modules":["export"] if csv else []},
            "entry":{**modules["entry"], "backup":backup},
            "filters":{"search":[], "exact":[], "date":None},
            "summary":{"count":None, "sum":None, "group":None},
            "export":modules["export"] if csv else {"columns":[], "label":None},
        }
        if legacy and stage == "export" and requirement not in failed:
            failed.add(requirement)
            record({"stage":stage,"status":"invalid_staged_result","dispatched":True})
            raise ApiError(502, "invalid_staged_result", "旧版导出步骤失败")
        record({"stage":stage,"status":"ok","dispatched":True})
        return json.dumps(fixtures[stage]).encode()
    success = stage != "summary" or requirement in failed
    record({"stage":stage,"status":"ok" if success else "fixture_failed","dispatched":True})
    if not success:
        failed.add(requirement)
        raise ApiError(502, "fixture_failed", "受控统计阶段失败")
    return json.dumps(modules[stage]).encode()

generate.request_completion = ordinary
runs = Path(sys.argv[1])
store = Store(runs / "workspace.sqlite3")
if len(sys.argv) == 2 or sys.argv[2] == "bootstrap":
    store.register({"username":"staged-operator","password":"test-only-password","invite":store.create_invite()})
def revise(stage, requirement, prior, key, admit, record, *, base):
    assert "persisted-marker" not in json.dumps(base, ensure_ascii=False)
    if base["kind"] == "table":
        def exchange(payload, dispatch, **kwargs):
            message = json.loads(payload)["body"]["messages"][-1]["content"]
            assert "private-table.csv" not in message and "private-row" not in message
            context = json.loads(message)["revision_source"]
            assert context["kind"] == "table"
            if "负责人" in requirement:
                assert context["columns"] == [{"index": 1, "name": "负责人", "type": "text"}]
            dispatch()
            if "超出范围" in requirement:
                result = {"addFilter": 999}
            elif "误给标题" in requirement:
                result = {"title": "误给标题"}
            elif "标题" in requirement:
                result = {"title": "每周交付台账"}
            else:
                result = {"addFilter": 1}
            return {"status": 200, "body": json.dumps({"choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps(result)}}
            ]})}
        with patch.object(assistant_model, "_exchange", exchange):
            return staged_revision.provide(stage, requirement, prior, key, admit, record, base=base)
    if base["kind"] == "ledger":
        def exchange(payload, dispatch, **kwargs):
            dispatch()
            if stage == "filters":
                old = base["source"]["filters"]
                result = {**old, "exact":[*old["exact"], "count"]}
            elif "错删功能台账" in requirement:
                result = {**base["source"]["spec"], "kind":"ledger", "title":"错删功能台账", "modules":[]}
            elif "标题" in requirement:
                result = {"title":"新版设备台账"}
            elif "CSV" in requirement:
                result = {"removeCsv":True}
            else:
                result = {"addFilter":True}
            return {"status": 200, "body": json.dumps({"choices": [
                {"finish_reason":"stop", "message":{"content":json.dumps(result, ensure_ascii=False)}}
            ]})}
        with patch.object(assistant_model, "_exchange", exchange):
            return staged_revision.provide(stage, requirement, prior, key, admit, record, base=base)
    return staged_revision.provide(stage, requirement, prior, key, admit, record, base=base)

def verify_runtime(html, entries, *, backup, storage_limit):
    if b'throw new Error("synthetic startup failure");' in html:
        raise ApiError(422, "staged_runtime_failed", "固定替身检测到启动失败")
    if entries == [["result", "verify-late"]]:
        (runs / "verify-waiting").touch()
        deadline = time.monotonic() + 4
        while not (runs / "verify-go").exists() and time.monotonic() < deadline:
            time.sleep(.01)

workbench.serve("fixture-key", 0, runs, staged_enabled=True, staged_provider=stages,
                staged_revision_provider=revise, staged_runtime_verifier=verify_runtime,
                generation_limits=(5, 100), admin_usernames=("staged-operator",))
"""


class StagedHTTPFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.runs = Path(directory.name)
        cls.start_server(bootstrap=True)
        cls.store = Store(cls.runs / "workspace.sqlite3")

    @classmethod
    def start_server(cls, *, bootstrap=False):
        mode = "bootstrap" if bootstrap else "restart"
        cls.server = subprocess.Popen([sys.executable, "-B", "-c", PROGRAM, str(cls.runs), mode],
                                      stdout=subprocess.PIPE, text=True)
        cls.addClassCleanup(cls.server.stdout.close)
        cls.addClassCleanup(cls.stop_server, cls.server)
        cls.base = re.search(r"http://127\.0\.0\.1:\d+", cls.server.stdout.readline()).group()

    @staticmethod
    def stop_server(server):
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=5)

    def request(self, path, data=None, auth=None):
        headers = {"Origin":self.base, "Content-Type":"application/json"}
        if auth:
            headers.update({"Cookie":auth[0], "X-Root-Session":auth[1]})
        request = Request(self.base + path, None if data is None else json.dumps(data).encode(), headers)
        try:
            response = urlopen(request, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response), response.headers.get("Set-Cookie")

    def account(self, name):
        status, user, cookie = self.request("/api/register", {"username":name, "password":"test-only-password",
                                                              "invite":self.store.create_invite()})
        self.assertEqual(status, 201)
        return cookie.split(";")[0], user["marker"]

    def admin_generation(self):
        code, _, cookie = self.request("/api/admin/login", {"username":"staged-operator", "password":"test-only-password"})
        self.assertEqual(code, 200)
        auth = (cookie.split(";")[0], "")
        code, session, _ = self.request("/api/admin/session", auth=auth)
        self.assertEqual(code, 200)
        code, view, _ = self.request("/api/trial/admin", auth=(auth[0], session["marker"]))
        self.assertEqual(code, 200)
        return view["generation"]

    def gen(self, auth, requirement, request=None):
        return self.request("/api/generate", {"requirement":requirement, "requestId":request or uuid4().hex}, auth)

    def confirm_revision(self, path, payload, auth):
        data = {name: payload[name] for name in ("nonce", "confirmation")}
        self.assertEqual(self.request(path + "/confirm-revision", data, auth)[:2], (200, {"confirmed":True}))

    def assert_preview_verified(self, path, payload, auth):
        self.assertEqual(self.request(path + "/verify-preview", payload, auth)[:2], (200, {"verified":True}))

    def wait_task(self, auth, task, state):
        deadline = time.monotonic() + 10
        path = "/api/staged/" + task
        while time.monotonic() < deadline:
            status, body, _ = self.request(path, auth=auth)
            self.assertEqual(status, 200)
            if body["task"]["state"] == state:
                return body["task"]
            time.sleep(0.05)
        self.fail(f"{state} not reached: {body['task']}")


class StagedReconnectHTTP(StagedHTTPFixture):
    def test_reconnect_replays_one_generation_and_publication_after_server_restart(self):
        """TEST-020-04: retry only the original operation against the same SQLite and Git."""
        owner = self.account("reconnect-owner")
        other = self.account("reconnect-other")
        requirement = "计算两个数字之和，验证重启后请求重放"
        request_id = uuid4().hex
        before = self.request("/api/trial", auth=owner)[1]["generation"]
        start = Barrier(2, timeout=5)

        def submit(_):
            start.wait()
            return self.gen(owner, requirement, request_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(submit, range(2))
        self.assertEqual((first[0], second[0]), (202, 202))
        task = first[1]["task"]["id"]
        self.assertEqual(second[1]["task"]["id"], task)
        self.assertEqual(self.wait_task(owner, task, "complete")["completed"], ["spec", "implementation", "assemble"])
        self.assertEqual([item["id"] for item in self.request("/api/staged", auth=owner)[1]["tasks"]], [task])
        path = "/api/staged/" + task
        self.assertEqual(self.request(path, auth=other)[0], 404)
        status, candidate, _ = self.request(path + "/candidate", {}, owner)
        self.assertEqual(status, 200)
        payload = {**candidate["staged"], "toolId":candidate["id"], "entries":[["calculation", "5"]]}
        tool_path = "/api/tools/" + candidate["id"]
        self.assert_preview_verified(path, payload, owner)

        # Keep the response unread while another client observes the committed result.
        with closing(HTTPConnection(urlsplit(self.base).netloc, timeout=5)) as connection:
            connection.request("POST", path + "/publish", json.dumps(payload), {
                "Origin":self.base, "Content-Type":"application/json", "Cookie":owner[0], "X-Root-Session":owner[1],
            })
            published = self.wait_task(owner, task, "published")
        self.assertEqual(published["toolId"], candidate["id"])
        status, snapshot, _ = self.request(tool_path, auth=owner)
        self.assertEqual(status, 200)
        self.assertEqual((snapshot["id"], snapshot["version"], snapshot["html"], snapshot["entries"]),
                         (candidate["id"], 1, candidate["html"], payload["entries"]))
        status, history, _ = self.request(tool_path + "/versions", auth=owner)
        self.assertEqual(status, 200)
        self.assertEqual([(item["version"], item["commit"]) for item in history["items"]],
                         [(1, candidate["staged"]["commit"])])

        self.stop_server(self.server)
        self.start_server()
        self.assertEqual(self.gen(owner, requirement, request_id)[:2], (202, {"task":published}))
        self.assertEqual(self.request(path, auth=owner)[:2], (200, {"task":published}))
        self.assertEqual(self.request(tool_path, auth=owner)[:2], (200, snapshot))
        self.assertEqual(self.request(path, auth=other)[0], 404)
        self.assertEqual(self.request(tool_path, auth=other)[0], 404)
        self.assertEqual(self.request(path + "/publish", payload, owner)[:2], (200, {**snapshot, "applied":True}))
        self.assertEqual(self.request(tool_path + "/versions", auth=owner)[:2], (200, history))
        tools = self.request("/api/tools", auth=owner)[1]["items"]
        self.assertEqual([(tool["id"], tool["version"]) for tool in tools], [(candidate["id"], 1)])
        after = self.request("/api/trial", auth=owner)[1]["generation"]
        self.assertEqual((after["used"] - before["used"], after["totalUsed"] - before["totalUsed"]), (1, 2))


class StagedHTTP(StagedHTTPFixture):
    def test_runtime_proof_is_bound_to_the_candidate_snapshot_and_nonce(self):
        owner = self.account("runtime-proof-owner")
        other = self.account("runtime-proof-other")
        before = self.request("/api/trial", auth=owner)[1]["generation"]["used"]
        status, response, _ = self.gen(owner, "计算两数之和，验证运行门禁")
        self.assertEqual(status, 202)
        task = response["task"]["id"]
        self.wait_task(owner, task, "complete")
        path = "/api/staged/" + task
        status, candidate, _ = self.request(path + "/candidate", {}, owner)
        self.assertEqual(status, 200)
        payload = {**candidate["staged"], "toolId":candidate["id"], "entries":[["result", "5"]]}
        self.assertEqual(self.request(path + "/verify-preview", payload, other)[0], 404)
        bad_digest = {**payload, "sha256":"0" * 64}
        self.assertEqual(self.request(path + "/verify-preview", bad_digest, owner)[0], 409)
        status, body, _ = self.request(path + "/publish", payload, owner)
        self.assertEqual((status, body.get("code")), (409, "staged_runtime_required"))
        self.assert_preview_verified(path, payload, owner)

        altered = {**payload, "entries":[["result", "9"]]}
        status, body, _ = self.request(path + "/publish", altered, owner)
        self.assertEqual((status, body.get("code")), (409, "staged_runtime_required"))
        late = {**payload, "entries":[["result", "verify-late"]]}
        with ThreadPoolExecutor(max_workers=1) as pool:
            verification = pool.submit(self.request, path + "/verify-preview", late, owner)
            waiting = self.runs / "verify-waiting"
            deadline = time.monotonic() + 3
            try:
                while not waiting.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(waiting.exists())
                self.assertEqual(self.request(path + "/resolve-publish", {"nonce":payload["nonce"]}, owner)[:2],
                                 (200, {"applied":False}))
            finally:
                (self.runs / "verify-go").touch()
            status, body, _ = verification.result()
            self.assertEqual((status, body.get("code")), (409, "staged_candidate_changed"))
        current = self.request(path + "/candidate", {}, owner)[1]
        self.assertNotEqual(current["staged"]["nonce"], payload["nonce"])
        self.assertEqual(self.request(path + "/publish", payload, owner)[1]["code"], "staged_candidate_changed")
        payload.update(current["staged"])
        status, body, _ = self.request(path + "/publish", payload, owner)
        self.assertEqual((status, body.get("code")), (409, "staged_runtime_required"))
        self.assertEqual(self.request("/api/tools", auth=owner)[1]["items"], [])
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], before)

        self.assert_preview_verified(path, payload, owner)
        status, saved, _ = self.request(path + "/publish", payload, owner)
        self.assertEqual(status, 200)
        self.assertEqual(saved["entries"], payload["entries"])
        self.assertEqual(self.request(path + "/publish", payload, owner)[:2], (200, saved))
        versions = self.request("/api/tools/" + candidate["id"] + "/versions", auth=owner)[1]
        self.assertEqual([item["version"] for item in versions["items"]], [1])
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], before + 1)

    def test_runtime_failure_does_not_publish_or_charge_the_candidate(self):
        owner = self.account("runtime-failure-owner")
        before = self.request("/api/trial", auth=owner)[1]["generation"]["used"]
        status, response, _ = self.gen(owner, "计算两个数字，启动失败探针")
        self.assertEqual(status, 202)
        task = response["task"]["id"]
        self.wait_task(owner, task, "complete")
        path = "/api/staged/" + task
        status, candidate, _ = self.request(path + "/candidate", {}, owner)
        self.assertEqual(status, 200)
        self.assertIn('throw new Error("synthetic startup failure");', candidate["html"])
        payload = {**candidate["staged"], "toolId":candidate["id"], "entries":[]}
        status, body, _ = self.request(path + "/verify-preview", payload, owner)
        self.assertEqual((status, body.get("code")), (422, "staged_runtime_failed"))
        status, body, _ = self.request(path + "/publish", payload, owner)
        self.assertEqual((status, body.get("code")), (409, "staged_runtime_required"))
        self.assertEqual(self.request("/api/tools", auth=owner)[1]["items"], [])
        self.assertEqual(self.request("/api/tools/" + candidate["id"], auth=owner)[0], 404)
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], before)

    def test_ledger_local_revisions_preserve_the_other_modules(self):
        """ROOT-REV-IA-01 / CHG-027 through generation, candidate verification, and publication."""
        owner = self.account("ledger-revision-scope")
        status, response, _ = self.gen(owner, "设备台账，需要筛选、统计与 CSV 导出。")
        self.assertEqual(status, 202)
        task = response["task"]["id"]
        path = "/api/staged/" + task
        self.wait_task(owner, task, "failed")
        self.assertEqual(self.request(path + "/resume", {}, owner)[0], 202)
        self.wait_task(owner, task, "complete")
        status, initial, _ = self.request(path + "/candidate", {}, owner)
        self.assertEqual(status, 200)
        entries = [["root-structured-records-v1", json.dumps([
            {"id":1, "name":"设备甲", "count":3, "status":"待处理"},
        ], ensure_ascii=False)]]
        tool = "/api/tools/" + initial["id"]

        def config(snapshot):
            return json.loads(re.search(r"const config = (.+);", snapshot["html"]).group(1))

        def publish(candidate_path, candidate):
            payload = {**candidate["staged"], "toolId":candidate["id"], "entries":entries}
            if "runId" in candidate:
                payload.update(runId=candidate["runId"], revision=candidate["revision"],
                               confirmation=candidate["revisionPlan"]["confirmation"])
                self.confirm_revision(candidate_path, payload, owner)
            self.assert_preview_verified(candidate_path, payload, owner)
            status, saved, _ = self.request(candidate_path + "/publish", payload, owner)
            self.assertEqual(status, 200)
            self.assertEqual((saved["id"], saved["entries"]), (initial["id"], entries))
            return saved

        def revision_path(requirement, version, state):
            status, response, _ = self.request(tool + "/revise", {
                "requirement":requirement, "version":version, "requestId":uuid4().hex,
            }, owner)
            self.assertEqual(status, 202)
            identifier = response["task"]["id"]
            self.wait_task(owner, identifier, state)
            return "/api/staged/" + identifier

        def successful_revision(requirement, current_version):
            candidate_path = revision_path(requirement, current_version, "complete")
            status, candidate, _ = self.request(candidate_path + "/candidate", {}, owner)
            self.assertEqual(status, 200)
            proposal = candidate["revisionPlan"]
            self.assertEqual((proposal["baseVersion"], proposal["toolId"]), (current_version, initial["id"]))
            payload = {**candidate["staged"], "toolId":candidate["id"], "runId":candidate["runId"],
                       "revision":candidate["revision"], "entries":entries}
            denied = self.request(candidate_path + "/verify-preview", payload, owner)
            self.assertEqual((denied[0], denied[1]["code"]), (409, "revision_confirmation_required"))
            payload["confirmation"] = proposal["confirmation"]
            denied = self.request(candidate_path + "/verify-preview", payload, owner)
            self.assertEqual((denied[0], denied[1]["code"]), (409, "revision_confirmation_required"))
            saved = publish(candidate_path, candidate)
            self.assertEqual(saved["version"], current_version + 1)
            return config(saved)

        original = publish(path, initial)
        original_config = config(original)
        self.assertEqual(original_config["filters"]["search"], ["name"])
        self.assertEqual(original_config["summary"]["sum"]["field"], "count")
        self.assertEqual(original_config["export"]["columns"], ["name", "count", "status"])
        before = self.request("/api/trial", auth=owner)[1]["generation"]["used"]
        failed = revision_path("只把标题改成错删功能台账，保留筛选、统计和 CSV 导出", 1, "unsupported")
        self.assertEqual(self.request(failed + "/candidate", {}, owner)[0], 409)
        self.assertEqual(self.request(tool, auth=owner)[1]["html"], original["html"])
        self.assertEqual(self.request(tool, auth=owner)[1]["version"], 1)
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], before)

        renamed = successful_revision("只把标题改成新版设备台账，保留筛选、统计和 CSV 导出", 1)
        self.assertEqual(renamed["spec"]["title"], "新版设备台账")
        self.assertEqual(self.request("/api/tools", auth=owner)[1]["items"][0]["title"], "新版设备台账")
        for module in ("entry", "filters", "summary", "export"):
            self.assertEqual(renamed[module], original_config[module])

        without_csv = successful_revision("只移除 CSV 导出，其他功能保持不变", 2)
        self.assertEqual(without_csv["export"], {"columns":[], "label":None})
        for module in ("entry", "filters", "summary"):
            self.assertEqual(without_csv[module], renamed[module])

        added_filter = successful_revision("增加数量精确筛选，保留原有功能", 3)
        self.assertEqual(added_filter["filters"], {
            **without_csv["filters"], "exact":[*without_csv["filters"]["exact"], "count"],
        })
        for module in ("entry", "summary", "export"):
            self.assertEqual(added_filter[module], without_csv[module])
        history = self.request(tool + "/versions", auth=owner)[1]
        self.assertEqual([item["version"] for item in history["items"]], [4, 3, 2, 1])
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], before + 3)

        with self.store.connection(write=True) as db:
            account = db.execute("SELECT id FROM accounts WHERE username='ledger-revision-scope'").fetchone()[0]
            db.execute("INSERT INTO generation_grants VALUES (?,2)", (account,))
        cancelled_path = revision_path("名称改为设备名称", 4, "complete")
        cancelled = self.request(cancelled_path + "/candidate", {}, owner)[1]
        self.assertIn("已有记录 1 条", " ".join(cancelled["revisionPlan"]["changes"]))
        cancel = {"nonce": cancelled["staged"]["nonce"]}
        cancelled_payload = {**cancelled["staged"], "toolId":cancelled["id"], "runId":cancelled["runId"],
                             "revision":cancelled["revision"], "entries":entries,
                             "confirmation":cancelled["revisionPlan"]["confirmation"]}
        self.confirm_revision(cancelled_path, cancelled_payload, owner)
        self.assert_preview_verified(cancelled_path, cancelled_payload, owner)
        self.assertEqual(self.request(cancelled_path + "/cancel-revision", cancel, owner)[1]["task"]["state"],
                         "cancelled")
        for action in ("candidate", "confirm-revision", "verify-preview", "publish", "resume"):
            self.assertEqual(self.request(cancelled_path + "/" + action, cancelled_payload, owner)[0], 409)
        self.assertEqual(self.request(tool, auth=owner)[1]["version"], 4)
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], before + 3)

        renamed_field = successful_revision("名称改为设备名称", 4)
        self.assertEqual(renamed_field["spec"]["fields"][0]["label"], "设备名称")
        self.assertEqual(renamed_field["spec"]["fields"][0]["id"], "name")
        with_project = successful_revision("新增一个可空文本字段「项目」", 5)
        field = with_project["spec"]["fields"][-1]
        self.assertEqual((field["label"], field["required"]), ("项目", False))
        self.assertIn(field["id"], with_project["entry"]["form"])
        self.assertIn(field["id"], with_project["entry"]["columns"])
        self.assertEqual(self.request(tool, auth=owner)[1]["entries"], entries)
        risky = revision_path("删除项目字段和已有记录", 6, "unsupported")
        self.assertEqual(self.request(risky, auth=owner)[1]["task"]["reason"], "revision_risky")
        self.assertEqual(self.request(tool, auth=owner)[1]["version"], 6)
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], before + 5)

    def test_generation_category_is_part_of_the_request_identity(self):
        auth = self.account("generation-category")
        path = "/api/generate"
        data = {"requirement": "计算两个数字", "requestId": uuid4().hex}
        status, initial, _ = self.request(path, data, auth)
        self.assertEqual(status, 202)
        status, replay, _ = self.request(path, {**data, "category": "auto"}, auth)
        self.assertEqual((status, replay["task"]["id"]), (202, initial["task"]["id"]))
        status, rejected, _ = self.request(path, {**data, "category": "brand"}, auth)
        self.assertEqual((status, rejected["code"]), (409, "staged_request_changed"))
        status, rejected, _ = self.request(path, {**data, "category": "other"}, auth)
        self.assertEqual((status, rejected["code"]), (400, "staged_category"))

    def test_one_endpoint_uses_the_plan_for_a_simple_tool(self):
        user = self.account("staged-routing")
        before = self.request("/api/trial", auth=user)[1]["generation"]["totalUsed"]
        status, response, _ = self.gen(user, "计算两个数字之和")
        self.assertEqual(status, 202)
        self.assertNotIn("html", response)
        task = response["task"]["id"]
        complete = self.wait_task(user, task, "complete")
        self.assertEqual(complete["steps"], ["spec", "implementation", "assemble"])
        path = "/api/staged/" + task
        code, candidate, _ = self.request(path + "/candidate", {}, user)
        self.assertEqual(code, 200)
        self.assertIn('id="answer"', candidate["html"])
        self.assertNotIn("stagedBackup", candidate)
        status, response, _ = self.gen(user, "超出边界的复杂需求")
        self.assertEqual(status, 202)
        self.wait_task(user, response["task"]["id"], "unsupported")
        after = self.request("/api/trial", auth=user)[1]["generation"]["totalUsed"]
        self.assertEqual(after - before, 3)

    def test_ledger_dispatches_only_requested_modules_and_preserves_backup_permission(self):
        owner = self.account("staged-ledger-modules")
        for requirement, expected, backup in (
            ("登记记录，仅录入、列表和保存，不要筛选、统计或导出",
             ["spec", "entry", "assemble"], False),
            ("登记并备份 JSON，支持导入恢复，无需 CSV",
             ["spec", "entry", "assemble"], True),
            ("登记并导出 CSV，也支持 JSON 备份恢复",
             ["spec", "entry", "export", "assemble"], True),
        ):
            with self.subTest(requirement=requirement):
                before = self.request("/api/trial", auth=owner)[1]["generation"]
                status, response, _ = self.gen(owner, requirement)
                self.assertEqual(status, 202)
                task = response["task"]["id"]
                self.assertEqual(self.wait_task(owner, task, "complete")["steps"], expected)
                path = "/api/staged/" + task
                status, candidate, _ = self.request(path + "/candidate", {}, owner)
                self.assertEqual(status, 200)
                self.assertEqual(candidate.get("stagedBackup", False), backup)
                payload = {**candidate["staged"], "toolId":candidate["id"], "entries":[]}
                self.assert_preview_verified(path, payload, owner)
                self.assertEqual(self.request(path + "/publish", payload, owner)[0], 200)
                after = self.request("/api/trial", auth=owner)[1]["generation"]
                self.assertEqual(after["used"] - before["used"], 1)
                self.assertEqual(after["totalUsed"] - before["totalUsed"], len(expected) - 1)

    def test_legacy_ledger_resumes_its_existing_export_checkpoint_without_adding_export(self):
        owner = self.account("staged-legacy-modules")
        status, response, _ = self.gen(owner, "登记旧任务，只录入、列表和保存")
        self.assertEqual(status, 202)
        task = response["task"]["id"]
        path = "/api/staged/" + task
        failed = self.wait_task(owner, task, "failed")
        self.assertEqual(failed["completed"], ["spec", "entry", "filters", "summary"])
        self.assertEqual(failed["stage"], "export")
        before = self.request("/api/trial", auth=owner)[1]["generation"]
        self.assertEqual(self.request(path + "/resume", {}, owner)[0], 202)
        completed = self.wait_task(owner, task, "complete")
        self.assertEqual(completed["steps"], failed["steps"])
        status, candidate, _ = self.request(path + "/candidate", {}, owner)
        self.assertEqual(status, 200)
        self.assertNotIn("stagedBackup", candidate)
        self.assertIn('"export":{"columns":[],"label":null}', candidate["html"])
        after = self.request("/api/trial", auth=owner)[1]["generation"]
        self.assertEqual(after["used"], before["used"])
        self.assertEqual(after["totalUsed"] - before["totalUsed"], 1)

    def test_task_resume_and_atomic_replayable_publication(self):
        owner = self.account("staged-owner")
        other = self.account("staged-other")
        requirement = "设备维修台账：新增记录、修改状态，组合筛选、统计和导出。"
        request = uuid4().hex
        before = self.request("/api/trial", auth=owner)[1]["generation"]["totalUsed"]
        initial_admin = self.admin_generation()
        status, response, _ = self.gen(owner, requirement, request)
        self.assertEqual(status, 202)
        task = response["task"]["id"]
        self.assertEqual(self.gen(owner, requirement, request)[1]["task"]["id"], task)
        path = "/api/staged/" + task
        self.assertEqual(self.request(path, auth=other)[0], 404)
        self.assertEqual(self.request(path + "/resume", {}, other)[0], 404)
        failed = self.wait_task(owner, task, "failed")
        self.assertEqual(failed["completed"], ["spec","entry","filters"])
        self.assertEqual(failed["stage"], "summary")
        self.assertTrue(failed["resumable"])
        self.assertTrue(failed["issue"])
        self.assertEqual(self.request(path + "/candidate", {}, owner)[0], 409)
        self.assertEqual(self.request(path + "/resume", {}, owner)[0], 202)
        complete = self.wait_task(owner, task, "complete")
        self.assertEqual(complete["steps"], ["spec","entry","filters","summary","export","assemble"])
        self.assertEqual(self.request(path + "/candidate", {}, other)[0], 404)
        code, candidate, _ = self.request(path + "/candidate", {}, owner)
        self.assertEqual(code, 200)
        self.assertTrue(candidate["stagedBackup"])
        self.assertEqual(self.request(path + "/candidate", {}, owner)[1], candidate)
        self.assertFalse(self.request("/api/tools", auth=owner)[1]["items"])
        self.assertEqual(self.request("/api/tools/" + candidate["id"], auth=owner)[0], 404)
        self.assertEqual(self.request("/api/tools/" + candidate["id"] + "/commit", {"entries":[]}, owner)[0], 404)
        data = {**candidate["staged"], "toolId":candidate["id"], "entries":[["fixture","draft"]]}
        self.assertEqual(self.request(path + "/publish", {**data,"sha256":"wrong"}, owner)[0], 409)
        self.assertEqual(self.request(path + "/publish", {**data,"entries":["invalid"]}, owner)[0], 400)
        large = {**data, "entries": [["ordinary", "x" * (600 * 1024)]]}
        self.assertEqual(self.request(path + "/publish", large, owner)[1]["code"], "tool_quota")
        self.assertFalse(self.request("/api/tools", auth=owner)[1]["items"])
        self.assertEqual(self.request(path + "/publish", data, other)[0], 404)
        self.assert_preview_verified(path, data, owner)
        failed_path = self.runs / candidate["id"] / "app.pending"
        if failed_path.exists():
            failed_path.unlink()
        failed_path.mkdir()
        status, failure, _ = self.request(path + "/publish", data, owner)
        self.assertEqual((status, failure["code"]), (503, "staged_save_failed"))
        self.assertEqual(self.request(path, auth=owner)[1]["task"]["state"], "complete")
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], 0)
        failed_path.rmdir()
        git_refs = self.runs / "staged-repositories" / (task + ".git") / "refs" / "workbench"
        git_refs.touch()
        status, failure, _ = self.request(path + "/publish", data, owner)
        self.assertEqual((status, failure["code"]), (503, "tool_git_unavailable"))
        self.assertEqual(self.request(path, auth=owner)[1]["task"]["state"], "complete")
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], 0)
        self.assertEqual(self.request("/api/tools", auth=owner)[1]["items"], [])
        git_refs.unlink()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.request(path + "/publish", data, owner), range(2)))
        self.assertEqual([response[0] for response in responses], [200,200])
        self.assertEqual([response[1]["id"] for response in responses], [candidate["id"]] * 2)
        self.assertEqual(self.request(path, auth=owner)[1]["task"]["toolId"], candidate["id"])
        version = self.request("/api/tools/" + candidate["id"] + "/versions", auth=owner)[1]["items"][0]
        self.assertEqual(version["commit"], candidate["staged"]["commit"])
        self.assertTrue(self.request("/api/tools/" + candidate["id"], auth=owner)[1]["stagedBackup"])
        balance = self.request("/api/trial", auth=owner)[1]["generation"]
        self.assertEqual((balance["used"], balance["totalUsed"] - before), (1, 6))
        final_admin = self.admin_generation()
        self.assertEqual((final_admin["completed"] - initial_admin["completed"],
                          final_admin["failed"] - initial_admin["failed"]), (6, 1))

    def test_revision_uses_latest_data_and_replays_published_version(self):
        owner = self.account("staged-revision")
        status, response, _ = self.gen(owner, "计算两个数字")
        self.assertEqual(status, 202)
        first = response["task"]["id"]
        self.wait_task(owner, first, "complete")
        path = "/api/staged/" + first
        candidate = self.request(path + "/candidate", {}, owner)[1]
        initial = {**candidate["staged"], "toolId":candidate["id"], "entries":[["history","persisted-marker"]]}
        self.assert_preview_verified(path, initial, owner)
        self.assertEqual(self.request(path + "/publish", initial, owner)[0], 200)
        tool = "/api/tools/" + candidate["id"]
        data = {"requirement":"把页面文案「相加」改为「两数之和」", "version":1, "requestId":uuid4().hex}
        code, result, _ = self.request(tool + "/revise", data, owner)
        self.assertEqual(code, 202)
        revision = result["task"]["id"]
        self.assertEqual(result["task"]["baseToolId"], candidate["id"])
        self.assertEqual(result["task"]["baseVersion"], 1)
        second_data = {**data, "requestId":uuid4().hex}
        competing = self.request(tool + "/revise", second_data, owner)[1]["task"]["id"]
        self.wait_task(owner, revision, "complete")
        self.wait_task(owner, competing, "complete")
        rev_path = "/api/staged/" + revision
        revised = self.request(rev_path + "/candidate", {}, owner)[1]
        self.assertEqual((revised["id"], revised["version"], revised["revision"]), (candidate["id"], 2, 1))
        self.assertEqual(revised["entries"], [["history","persisted-marker"]])
        self.assertIn('<p id="caption">两数之和</p>', revised["html"])
        self.assertIn('document.querySelector("#answer").textContent="5";', revised["html"])
        self.assertNotEqual(revised["runId"], revised["id"])
        self.assertEqual(self.request(tool + "/versions", auth=owner)[1]["current"], 1)
        payload = {**revised["staged"], "toolId":revised["id"], "runId":revised["runId"],
                   "entries":revised["entries"], "revision":revised["revision"],
                   "confirmation":revised["revisionPlan"]["confirmation"]}
        self.confirm_revision(rev_path, payload, owner)
        self.assert_preview_verified(rev_path, payload, owner)
        changes = [["history","persisted-marker"],["new","written while generating"]]
        self.assertEqual(self.request(tool + "/data", {"entries":changes,"revision":1,"version":1}, owner)[0], 200)
        self.assertEqual(self.request(rev_path + "/publish", payload, owner)[1]["code"], "revision_conflict")
        newest = self.request(rev_path + "/candidate", {}, owner)[1]
        self.assertEqual(newest["staged"], revised["staged"])
        self.assertEqual(newest["entries"], changes)
        payload.update(revision=newest["revision"], entries=newest["entries"])
        self.assertEqual(self.request(rev_path + "/publish", payload, owner)[1]["code"],
                         "revision_confirmation_required")
        stale = {"nonce":payload["nonce"], "confirmation":payload["confirmation"]}
        self.assertEqual(self.request(rev_path + "/confirm-revision", stale, owner)[0], 409)
        payload["confirmation"] = newest["revisionPlan"]["confirmation"]
        self.confirm_revision(rev_path, payload, owner)
        altered = {**payload, "entries":[*payload["entries"], ["unapproved", "new data"]]}
        self.assertEqual(self.request(rev_path + "/verify-preview", altered, owner)[1]["code"], "incompatible_data")
        status, body, _ = self.request(rev_path + "/publish", payload, owner)
        self.assertEqual((status, body.get("code")), (409, "staged_runtime_required"))
        self.assert_preview_verified(rev_path, payload, owner)
        published = self.request(rev_path + "/publish", payload, owner)
        self.assertEqual(published[0], 200)
        self.assertEqual((published[1]["id"], published[1]["version"], published[1]["entries"]),
                         (candidate["id"], 2, changes))
        self.assertEqual(self.request(rev_path + "/publish", payload, owner)[1], published[1])
        history = self.request(tool + "/versions", auth=owner)[1]
        self.assertEqual([item["commit"] for item in history["items"]],
                         [revised["staged"]["commit"], candidate["staged"]["commit"]])
        self.assertEqual(self.request(tool + "/revise", data, owner)[1]["task"]["id"], revision)
        runs_before = set(self.runs.glob("run-*"))
        for _ in range(3):
            self.assertEqual(self.request("/api/staged/" + competing + "/candidate", {}, owner)[1]["code"],
                             "version_conflict")
        self.assertEqual(set(self.runs.glob("run-*")), runs_before)
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], 2)

    def test_imported_table_revisions_add_filter_and_preserve_rows_across_versions(self):
        # CHG-029 / TEST-010 / TEST-007 / TEST-018-C through the real account and staged routes.
        owner = self.account("staged-table-revision")
        other = self.account("staged-table-other")
        table = {
            "source": "upload", "filename": "private-table.csv",
            "columns": [{"name": "事项", "type": "text"}, {"name": "负责人", "type": "text"}],
            "rows": [["private-row", "小周"]],
        }
        code, initial, _ = self.request("/api/table/generate", {"table": table}, owner)
        self.assertEqual(code, 200)
        tool = "/api/tools/" + initial["id"]
        repositories = self.runs / "staged-repositories"
        existing = set(repositories.glob("*.git"))
        self.assertEqual(self.request(tool + "/commit", {"entries": initial["entries"]}, owner)[0], 200)
        created = set(repositories.glob("*.git")) - existing
        self.assertEqual(len(created), 1, "首次保存导入表格应自动创建独立 Git 仓库")
        repo = created.pop()
        first_commit = self.request(tool + "/versions", auth=owner)[1]["items"][0]["commit"]
        self.assertEqual(subprocess.check_output(
            ["git", "--git-dir", str(repo), "show", first_commit + ":app.html"]).decode(), initial["html"])
        entries = json.loads(initial["entries"][0][1])
        extra = [["追加记录" + "x" * 3072, "小林"] for _ in range(180)]
        entries["rows"].extend(extra)
        entries["done"].extend([False] * len(extra))
        updated = [[initial["entries"][0][0], json.dumps(entries, ensure_ascii=False)]]
        code, _, _ = self.request(tool + "/data", {"entries": updated, "revision": 1, "version": 1}, owner)
        self.assertEqual(code, 200)
        before = self.request("/api/trial", auth=owner)[1]["generation"]

        def revise(version, requirement):
            request = {"requirement": requirement, "version": version, "requestId": uuid4().hex}
            self.assertEqual(self.request(tool + "/revise", request, other)[0], 404)
            code, response, _ = self.request(tool + "/revise", request, owner)
            self.assertEqual(code, 202)
            return response["task"]["id"]

        def publish(task, version, title):
            complete = self.wait_task(owner, task, "complete")
            self.assertEqual(complete["steps"], ["spec", "assemble"])
            path = "/api/staged/" + task
            code, candidate, _ = self.request(path + "/candidate", {}, owner)
            self.assertEqual(code, 200)
            self.assertEqual((candidate["id"], candidate["version"], candidate["entries"]),
                             (initial["id"], version, updated))
            self.assertEqual(candidate["storageLimit"], 8 * 1024 * 1024)
            self.assertIn("const columnFilters = [1];", candidate["html"])
            self.assertIn("<title>" + title + "</title>", candidate["html"])
            self.assertNotIn("private-row", candidate["html"])
            data = {**candidate["staged"], "toolId": candidate["id"], "runId": candidate["runId"],
                    "entries": candidate["entries"], "revision": candidate["revision"],
                    "confirmation": candidate["revisionPlan"]["confirmation"]}
            self.confirm_revision(path, data, owner)
            self.assert_preview_verified(path, data, owner)
            status, saved, _ = self.request(path + "/publish", data, owner)
            self.assertEqual(status, 200)
            self.assertEqual(self.request(path + "/publish", data, owner)[1], saved)
            self.assertEqual((saved["id"], saved["version"], saved["entries"]),
                             (initial["id"], version, updated))
            self.assertTrue(saved.pop("applied"))
            self.assertEqual(self.request(tool, auth=owner)[1], saved)

        declined = revise(1, "不要增加负责人筛选")
        self.wait_task(owner, declined, "unsupported")
        self.assertEqual(self.request("/api/staged/" + declined + "/candidate", {}, owner)[0], 409)
        publish(revise(1, "增加负责人精确筛选"), 2, "交付跟进板")
        publish(revise(2, "只把标题改成每周交付台账"), 3, "每周交付台账")
        failed = revise(3, "超出范围的负责人筛选")
        self.wait_task(owner, failed, "failed")
        self.assertEqual(self.request("/api/staged/" + failed + "/candidate", {}, owner)[0], 409)
        unrequested = revise(3, "新增负责人筛选，不能误给标题")
        self.wait_task(owner, unrequested, "unsupported")
        self.assertEqual(self.request("/api/staged/" + unrequested + "/candidate", {}, owner)[0], 409)
        self.assertEqual(self.request(tool, auth=owner)[1]["entries"], updated)
        versions = self.request(tool + "/versions", auth=owner)[1]
        self.assertEqual(versions["current"], 3)
        self.assertEqual(versions["items"][0]["note"], "只把标题改成每周交付台账")
        self.assertEqual(self.request("/api/tools", auth=owner)[1]["items"][0]["title"], "每周交付台账")
        after = self.request("/api/trial", auth=owner)[1]["generation"]
        self.assertEqual((after["used"] - before["used"], after["totalUsed"] - before["totalUsed"]), (2, 5))

        current = self.request(tool, auth=owner)[1]
        status, restored, _ = self.request(tool + "/restore", {
            "version": 3, "revision": current["revision"], "restoreVersion": 2,
        }, owner)
        self.assertEqual((status, restored["version"], restored["entries"]), (200, 4, updated))
        self.assertIn("const columnFilters = [1];", restored["html"])
        self.assertEqual(self.request(tool + "/publish", {
            "runId": restored["runId"], "revision": current["revision"], "entries": updated,
        }, owner)[0], 200)
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"], after)
        publish(revise(4, "只把标题改成每周交付台账"), 5, "每周交付台账")
        self.assertEqual(set(repositories.glob("*.git")) - existing, {repo})
        versions = self.request(tool + "/versions", auth=owner)[1]["items"][::-1]
        self.assertEqual([item["version"] for item in versions], [1, 2, 3, 4, 5])
        commits = [item["commit"] for item in versions]
        self.assertEqual(len(set(commits)), 5)
        for older, newer in zip(commits, commits[1:]):
            subprocess.run(["git", "--git-dir", str(repo), "merge-base", "--is-ancestor", older, newer], check=True)
        def historical(commit):
            return subprocess.check_output(["git", "--git-dir", str(repo), "show", commit + ":app.html"])

        self.assertEqual(historical(commits[1]), historical(commits[3]))
        self.assertNotIn(b"private-row", historical(commits[-1]))
        self.assertEqual(self.request(tool + "/versions", auth=other)[0], 404)

    def test_git_failure_leaves_initial_save_retryable_and_other_tool_has_its_own_repository(self):
        owner = self.account("tool-git-failure")
        request = {"table": {
            "source": "upload", "filename": "trial.csv",
            "columns": [{"name": "事项", "type": "text"}], "rows": [["只在数据库"]],
        }}
        candidates = [self.request("/api/table/generate", request, owner)[1] for _ in range(2)]
        root = self.runs / "staged-repositories"
        root.mkdir(exist_ok=True)
        paths = [root / (tool["id"] + ".git") for tool in candidates]
        paths[0].touch()
        action = "/api/tools/" + candidates[0]["id"] + "/commit"
        code, error, _ = self.request(action, {"entries": candidates[0]["entries"]}, owner)
        self.assertEqual((code, error["code"]), (503, "tool_git_unavailable"))
        self.assertEqual(self.request("/api/tools", auth=owner)[1]["items"], [])
        paths[0].unlink()
        for tool, repo in zip(candidates, paths):
            path = "/api/tools/" + tool["id"]
            self.assertEqual(self.request(path + "/commit", {"entries": tool["entries"]}, owner)[0], 200)
            history = self.request(path + "/versions", auth=owner)[1]
            self.assertEqual([item["version"] for item in history["items"]], [1])
            self.assertTrue((repo / "HEAD").is_file())
        with self.store.connection() as db:
            self.assertEqual({tuple(row) for row in db.execute(
                "SELECT tool_id, repo_id FROM tool_git_repositories WHERE tool_id IN (?,?)",
                [tool["id"] for tool in candidates],
            )}, {(tool["id"], tool["id"]) for tool in candidates})

    def test_legacy_split_repositories_keep_original_commits_in_one_published_history(self):
        owner = self.account("tool-git-migration")
        tool_id, second_run, third_run = "run-oldgit01", "run-oldgit02", "run-oldgit03"
        old_repos = [uuid4().hex, uuid4().hex]
        root = self.runs / "staged-repositories"
        html = [b"<!doctype html><p>v1</p>", b"<!doctype html><p>v2</p>", b"<!doctype html><p>v3</p>"]
        for run_id, content in zip((tool_id, second_run, third_run), html):
            (self.runs / run_id).mkdir()
            (self.runs / run_id / "app.html").write_bytes(content)

        def git(repo, *args, input=None):
            return subprocess.check_output(
                ["git", "--git-dir", str(repo), "-c", "user.name=Fixture", "-c", "user.email=test@example.test", *args],
                input=input, stderr=subprocess.DEVNULL,
            ).decode().strip()

        original = []
        for repo_id, content in zip(old_repos, html[1:]):
            repo = root / (repo_id + ".git")
            repo.mkdir(parents=True)
            git(repo, "init", "--bare")
            blob = git(repo, "hash-object", "-w", "--stdin", input=content)
            tree = git(repo, "mktree", input=f"100644 blob {blob}\tapp.html\n".encode())
            commit = git(repo, "commit-tree", tree, "-m", "old staged version")
            git(repo, "update-ref", "refs/tasks/legacy", commit)
            original.append(commit)

        with self.store.connection(write=True) as db:
            account_id = db.execute("SELECT id FROM accounts WHERE username='tool-git-migration'").fetchone()[0]
            db.execute("INSERT INTO tools(id,owner,title,published) VALUES (?,?,?,1)", (tool_id, account_id, "旧工具"))
            for version, run_id in enumerate((tool_id, second_run, third_run), 1):
                db.execute("INSERT INTO tool_versions VALUES (?,?,?,?,?)", (tool_id, version, run_id, f"旧版 {version}", 1))
            for task, commit, run_id, content in zip(old_repos, original, (second_run, third_run), html[1:]):
                db.execute("""INSERT INTO staged_jobs(id,owner,request_id,requirement,head,expires,state)
                              VALUES (?,?,?,?,?,?,'published')""",
                           (task, account_id, task, "历史需求", commit, 1))
                db.execute("INSERT INTO staged_candidates(task,tool_id,commit_id,digest,nonce) VALUES (?,?,?,?,?)",
                           (task, run_id, commit, hashlib.sha256(content).hexdigest(), uuid4().hex))

        path = "/api/tools/" + tool_id + "/versions"
        code, result, _ = self.request(path, auth=owner)
        self.assertEqual(code, 200)
        self.assertEqual(self.request(path, auth=owner)[1], result)
        commits = [item["commit"] for item in result["items"][::-1]]
        canonical = root / (old_repos[0] + ".git")
        self.assertEqual([git(canonical, "show", commit + ":app.html") for commit in commits],
                         [content.decode() for content in html])
        for parent, child in zip(commits, commits[1:]):
            git(canonical, "merge-base", "--is-ancestor", parent, child)
        for commit, publication in zip(original, commits[1:]):
            git(canonical, "merge-base", "--is-ancestor", commit, publication)
        self.assertFalse((root / (tool_id + ".git")).exists())

    def test_resolve_fences_a_late_publish_without_revoking_the_new_preview(self):
        owner = self.account("staged-resolve")
        other = self.account("staged-resolve-other")
        status, response, _ = self.gen(owner, "计算两个数字，验证保存超时")
        self.assertEqual(status, 202)
        task = response["task"]["id"]
        self.wait_task(owner, task, "complete")
        path = "/api/staged/" + task
        first = self.request(path + "/candidate", {}, owner)[1]
        nonce = first["staged"]["nonce"]
        self.assertEqual(self.request(path + "/resolve-publish", {"nonce":nonce}, other)[0], 404)
        payload = {**first["staged"], "toolId":first["id"], "entries":[["late", "test"]]}
        self.assert_preview_verified(path, payload, owner)
        with ThreadPoolExecutor(max_workers=1) as pool:
            late = pool.submit(self.request, path + "/publish", payload, owner)
            try:
                deadline = time.monotonic() + 3
                while not (self.runs / "publish-waiting").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((self.runs / "publish-waiting").exists())
                self.assertEqual(self.request(path + "/resolve-publish", {"nonce":nonce}, owner)[:2],
                                 (200, {"applied":False}))
                self.assertEqual(self.request("/api/tools", auth=owner)[1]["items"], [])
            finally:
                (self.runs / "publish-go").touch()
            self.assertEqual(late.result()[1]["code"], "staged_candidate_changed")
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], 0)
        current = self.request(path + "/candidate", {}, owner)[1]
        self.assertNotEqual(current["staged"]["nonce"], nonce)
        self.assertEqual(self.request(path + "/resolve-publish", {"nonce":nonce}, owner)[1], {"applied":False})
        self.assertEqual(self.request(path + "/candidate", {}, owner)[1], current)
        payload = {**current["staged"], "toolId":current["id"], "entries":[]}
        self.assert_preview_verified(path, payload, owner)
        status, saved, _ = self.request(path + "/publish", payload, owner)
        self.assertEqual(status, 200)
        self.assertEqual(self.request(path + "/resolve-publish", {"nonce":current["staged"]["nonce"]}, owner)[1],
                         saved)
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], 1)

    def test_paused_generation_cannot_create_jobs_or_repositories(self):
        owner = self.account("staged-paused-owner")
        status, _, cookie = self.request("/api/admin/login", {"username":"staged-operator", "password":"test-only-password"})
        self.assertEqual(status, 200)
        cookie = cookie.split(";")[0]
        session = self.request("/api/admin/session", auth=(cookie, ""))[1]
        admin = (cookie, session["marker"])
        self.assertEqual(self.request("/api/trial/admin", {"action":"pause"}, admin)[0], 200)
        try:
            before = set(self.runs.glob("**/*.git"))
            balance = self.request("/api/trial", auth=owner)[1]["generation"]
            for index in range(32):
                status, body, _ = self.gen(owner, f"计算暂停时的提交 {index}")
                self.assertEqual((status, body.get("code")), (503, "model_paused"))
            self.assertEqual(self.request("/api/staged", auth=owner)[1]["tasks"], [])
            self.assertEqual(set(self.runs.glob("**/*.git")), before)
            self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"], balance)
        finally:
            self.assertEqual(self.request("/api/trial/admin", {"action":"resume"}, admin)[0], 200)
