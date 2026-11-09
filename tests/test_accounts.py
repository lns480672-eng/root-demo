"""TEST-004: account boundaries through the real local HTTP entry."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from accounts import Store


class AccountHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.runs = directory.name
        cls.server = subprocess.Popen(
            [sys.executable, "-B", "-c",
             "import sys, workbench, generate; from pathlib import Path; "
             "import json; generate.request_completion = lambda *args: (200, json.dumps({"
             "'id':'fixture-account','model':generate.MODEL,'choices':[{'finish_reason':'stop',"
             "'message':{'content':'<!doctype html><html><body>工具</body></html>'}}]}).encode()); "
             "workbench.serve('fixture-key', 0, Path(sys.argv[1]))",
             cls.runs], stdout=subprocess.PIPE, text=True,
        )
        cls.addClassCleanup(cls.server.stdout.close)
        cls.addClassCleanup(lambda: (cls.server.terminate(), cls.server.wait(timeout=5)))
        cls.base = re.search(r"http://127\.0\.0\.1:\d+", cls.server.stdout.readline()).group()
        cls.store = Store(Path(cls.runs) / "workspace.sqlite3")

    def request(self, path, data=None, cookie=None, marker=None):
        headers = {"Origin": self.base, "Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        if marker:
            headers["X-Root-Session"] = marker
        request = Request(self.base + path, None if data is None else json.dumps(data).encode(), headers)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response), response.headers.get("Set-Cookie")

    def test_unauthenticated_generation_is_rejected(self):
        status, body, _ = self.request("/api/generate", {"requirement": "待授权"})
        self.assertEqual((status, body.get("code")), (401, "login_required"))

    def register(self, name, invite):
        return self.request("/api/register", {
            "username": name, "password": "fixture-test-password!", "invite": invite,
        })[:2]

    def login(self, name):
        status, _, cookie = self.request("/api/login", {
            "username": name, "password": "fixture-test-password!",
        })
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        cookie = cookie.split(";", 1)[0]
        _, state, _ = self.request("/api/session", cookie=cookie)
        self.assertEqual(state["user"], name)
        return cookie, state["marker"]

    def test_invitation_and_revocation(self):
        expired = self.store.create_invite(hours=-1)
        for invite in (None, "wrong-invite", expired):
            self.assertEqual(self.register("invite-check", invite)[1]["code"], "invalid_invite")
        self.assertEqual(self.request("/api/login", {
            "username": "invite-check", "password": "fixture-test-password!",
        })[0], 401)
        invite = self.store.create_invite()
        self.assertEqual(self.register("invite-check", invite)[0], 201)
        self.assertEqual(self.register("second-invite", invite)[1]["code"], "invalid_invite")
        self.assertEqual(self.request("/api/login", {
            "username": "invite-check", "password": "incorrect",
        })[0], 401)
        cookie, marker = self.login("invite-check")
        self.assertEqual(self.request("/api/logout", {}, cookie, marker)[0], 200)
        for path in ("/api/tools", "/api/tools/run-aaaaaaaa"):
            self.assertEqual(self.request(path, cookie=cookie, marker=marker)[0], 401)
        self.assertEqual(self.request("/api/generate", {"requirement": "已退出"}, cookie, marker)[0], 401)

    def test_invite_consumption_is_atomic(self):
        invite = self.store.create_invite()
        with ThreadPoolExecutor(2) as pool:
            statuses = list(pool.map(lambda name: self.register(name, invite)[0], ["invite-race-a", "invite-race-b"]))
        self.assertEqual(sorted(statuses), [201, 400])

    def test_owner_scope_and_revision(self):
        for name in ("owner-one", "owner-two"):
            self.assertEqual(self.register(name, self.store.create_invite())[0], 201)
        owner, marker = self.login("owner-one")
        other, other_marker = self.login("owner-two")
        status, tool, _ = self.request("/api/generate", {"requirement": "测试工具"}, owner, marker)
        self.assertEqual(status, 200)
        path = "/api/tools/" + tool["id"]
        self.assertEqual(self.request(path, cookie=owner, marker=marker)[0], 404)
        self.assertEqual(self.request(path + "/commit", {"entries": []}, other, other_marker)[0], 404)
        self.assertEqual(self.request(path + "/commit", {"entries": [["item", "first"]]}, owner, marker)[0], 200)
        for action in ("data", "open"):
            self.assertEqual(self.request(path + "/" + action, {"entries": [], "revision": 1}, other, other_marker)[0], 404)
        self.assertEqual(self.request(path, cookie=other, marker=other_marker)[0], 404)
        status, current, _ = self.request(path, cookie=owner, marker=marker)
        self.assertEqual((status, current["entries"]), (200, [["item", "first"]]))
        change = {"entries": [["item", "second"]], "revision": current["revision"]}
        self.assertEqual(self.request(path + "/data", change, owner, marker)[0], 200)
        self.assertEqual(self.request(path + "/data", change, owner, marker)[1]["code"], "revision_conflict")
        self.assertEqual(self.request(path + "/data", change, owner, other_marker)[1]["code"], "session_changed")
        status, current, _ = self.request(path, cookie=owner, marker=marker)
        self.assertEqual((status, current["entries"]), (200, [["item", "second"]]))


if __name__ == "__main__":
    unittest.main()
