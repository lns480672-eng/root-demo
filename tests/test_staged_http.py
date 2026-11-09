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
from staged_route import choose_route


PROGRAM = """
from pathlib import Path
import json
import sys
import generate
import workbench
from accounts import ApiError

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
    success = stage != "summary" or requirement in failed
    record({"stage":stage,"status":"ok" if success else "fixture_failed","dispatched":True})
    if not success:
        failed.add(requirement)
        raise ApiError(502, "fixture_failed", "受控统计阶段失败")
    return json.dumps(modules[stage]).encode()

generate.request_completion = ordinary
workbench.serve("fixture-key", 0, Path(sys.argv[1]), staged_enabled=True, staged_provider=stages)
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

    def test_classification_uses_capabilities_and_does_not_charge_rejections(self):
        user = self.account("staged-routing")
        self.assertEqual(choose_route("新增看板卡片，分栏、跨栏移动、编辑和删除、备份导出与导入。"), "staged")
        self.assertEqual(choose_route("帮我整理找工作的申请：按申请阶段分组，移动卡片，新建和改写申请，能保存成文件再加载。"), "staged")
        self.assertEqual(choose_route("帮我写一份工作申请文案。"), "single")
        before = self.request("/api/trial", auth=user)[1]["generation"]["totalUsed"]
        for requirement in ("计算两个数字之和", "一个只添加书名的清单。" * 30,
                            "一个记事清单。不需要统计和导出，不需要组合筛选。"):
            status, tool, _ = self.gen(user, requirement)
            self.assertEqual(status, 200)
            self.assertIn("html", tool)
            self.assertNotIn("stagedBackup", tool)
        status, error, _ = self.gen(user, "做一个工单台账，筛选并统计和导出，要求连接外部接口多人实时协同。")
        self.assertEqual((status, error["code"]), (422, "staged_unsupported"))
        after = self.request("/api/trial", auth=user)[1]["generation"]["totalUsed"]
        self.assertEqual(after - before, 3)

    def test_task_resume_and_atomic_replayable_publication(self):
        owner = self.account("staged-owner")
        other = self.account("staged-other")
        requirement = "设备维修台账：新增记录、修改状态，组合筛选、统计和导出。"
        request = uuid4().hex
        before = self.request("/api/trial", auth=owner)[1]["generation"]["totalUsed"]
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
        self.wait_task(owner, task, "complete")
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
