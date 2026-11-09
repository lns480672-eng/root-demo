"""TEST-018-B1 using the actual HTTP entry and controlled model dispatch."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
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
            "markup":'<p id="answer"></p>', "script":'document.querySelector("#answer").textContent="5";'}
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
    return stages(stage, requirement, prior, key, admit, record)

workbench.serve("fixture-key", 0, runs, staged_enabled=True, staged_provider=stages,
                staged_revision_provider=revise, generation_limits=(5, 100), admin_usernames=("staged-operator",))
"""


class StagedHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.runs = Path(directory.name)
        cls.server = subprocess.Popen([sys.executable, "-B", "-c", PROGRAM, str(cls.runs)],
                                      stdout=subprocess.PIPE, text=True)
        cls.addClassCleanup(cls.server.stdout.close)
        cls.addClassCleanup(lambda: (cls.server.terminate(), cls.server.wait(timeout=5)))
        cls.base = re.search(r"http://127\.0\.0\.1:\d+", cls.server.stdout.readline()).group()
        cls.store = Store(cls.runs / "workspace.sqlite3")

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
        code, _, cookie = self.request("/api/login", {"username":"staged-operator", "password":"test-only-password"})
        self.assertEqual(code, 200)
        auth = (cookie.split(";")[0], "")
        code, session, _ = self.request("/api/session", auth=auth)
        self.assertEqual(code, 200)
        code, view, _ = self.request("/api/trial/admin", auth=(auth[0], session["marker"]))
        self.assertEqual(code, 200)
        return view["generation"]

    def gen(self, auth, requirement, request=None):
        return self.request("/api/generate", {"requirement":requirement, "requestId":request or uuid4().hex}, auth)

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
        failed_path = self.runs / candidate["id"] / "app.pending"
        if failed_path.exists():
            failed_path.unlink()
        failed_path.mkdir()
        status, failure, _ = self.request(path + "/publish", data, owner)
        self.assertEqual((status, failure["code"]), (503, "staged_save_failed"))
        self.assertEqual(self.request(path, auth=owner)[1]["task"]["state"], "complete")
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], 0)
        failed_path.rmdir()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.request(path + "/publish", data, owner), range(2)))
        self.assertEqual([response[0] for response in responses], [200,200])
        self.assertEqual([response[1]["id"] for response in responses], [candidate["id"]] * 2)
        self.assertEqual(self.request(path, auth=owner)[1]["task"]["toolId"], candidate["id"])
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
        self.assertEqual(self.request(path + "/publish", initial, owner)[0], 200)
        tool = "/api/tools/" + candidate["id"]
        data = {"requirement":"计算两个数并展示结果的运算历史", "version":1, "requestId":uuid4().hex}
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
        self.assertNotEqual(revised["runId"], revised["id"])
        self.assertEqual(self.request(tool + "/versions", auth=owner)[1]["current"], 1)
        changes = [["history","persisted-marker"],["new","written while generating"]]
        self.assertEqual(self.request(tool + "/data", {"entries":changes,"revision":1,"version":1}, owner)[0], 200)
        payload = {**revised["staged"], "toolId":revised["id"], "runId":revised["runId"],
                   "entries":revised["entries"], "revision":revised["revision"]}
        self.assertEqual(self.request(rev_path + "/publish", payload, owner)[1]["code"], "revision_conflict")
        newest = self.request(rev_path + "/candidate", {}, owner)[1]
        self.assertEqual(newest["staged"], revised["staged"])
        self.assertEqual(newest["entries"], changes)
        payload.update(revision=newest["revision"], entries=newest["entries"])
        published = self.request(rev_path + "/publish", payload, owner)
        self.assertEqual(published[0], 200)
        self.assertEqual((published[1]["id"], published[1]["version"], published[1]["entries"]),
                         (candidate["id"], 2, changes))
        self.assertEqual(self.request(rev_path + "/publish", payload, owner)[1], published[1])
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
        self.assertEqual(self.request(tool + "/commit", {"entries": initial["entries"]}, owner)[0], 200)
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
                    "entries": candidate["entries"], "revision": candidate["revision"]}
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
        status, saved, _ = self.request(path + "/publish",
                                       {**current["staged"], "toolId":current["id"], "entries":[]}, owner)
        self.assertEqual(status, 200)
        self.assertEqual(self.request(path + "/resolve-publish", {"nonce":current["staged"]["nonce"]}, owner)[1],
                         saved)
        self.assertEqual(self.request("/api/trial", auth=owner)[1]["generation"]["used"], 1)

    def test_paused_generation_cannot_create_jobs_or_repositories(self):
        owner = self.account("staged-paused-owner")
        status, _, cookie = self.request("/api/login", {"username":"staged-operator", "password":"test-only-password"})
        self.assertEqual(status, 200)
        cookie = cookie.split(";")[0]
        session = self.request("/api/session", auth=(cookie, ""))[1]
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
