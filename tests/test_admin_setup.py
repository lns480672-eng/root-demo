"""AC-021 through the workbench's actual HTTP routes."""
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from accounts import Store


class AdminSetupHTTP(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runs = Path(directory.name)
        self.store = Store(self.runs / "workspace.sqlite3")
        self.environment = {**os.environ, "ROOT_TRIAL_ADMINS": "", "DEEPSEEK_API_KEY": ""}
        self.server = subprocess.Popen(
            [sys.executable, "-B", "workbench.py", "--port", "0", "--runs-dir", directory.name],
            stdout=subprocess.PIPE, text=True, env=self.environment,
        )
        self.addCleanup(self.server.stdout.close)
        self.addCleanup(lambda: (self.server.terminate(), self.server.wait(timeout=5)))
        startup = self.server.stdout.readline() + self.server.stdout.readline()
        self.base = re.search(r"http://127\.0\.0\.1:\d+", startup).group()
        self.password = "fixture-owner-password"
        self.changed = "fixture-changed-password"

    def request(self, path, payload=None, *, cookie="", marker="", origin=None):
        headers = {"Origin": origin or self.base, "Content-Type": "application/json",
                   "Cookie": cookie, "X-Root-Session": marker}
        req = Request(self.base + path, None if payload is None else json.dumps(payload).encode(), headers)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            body = response.read().decode()
            if response.headers.get_content_type() == "application/json":
                body = json.loads(body)
            return response.status, body, response.headers.get("Set-Cookie")

    def setup_code(self):
        return subprocess.check_output([sys.executable, "-B", "workbench.py", "--runs-dir", str(self.runs),
                                        "--create-admin-setup-code"], text=True, env=self.environment).strip()

    def test_setup_requires_valid_code_and_only_one_request_wins(self):
        self.assertEqual(self.request("/admin")[0], 401)
        self.assertEqual(self.request("/api/admin/invites", {"requestId": str(uuid4())})[0], 401)
        code = self.setup_code()
        status, html, _ = self.request("/admin")
        self.assertEqual(status, 200)
        self.assertIn("设置首个管理员", html)
        self.assertNotIn(code, html)
        payload = {"username": "owner-one", "password": self.password, "code": code}
        self.assertEqual(self.request("/api/admin/setup", {**payload, "code": "X" * 43})[0], 403)
        self.assertEqual(self.request("/api/admin/setup", payload, origin="https://example.test")[0], 403)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda username: self.request("/api/admin/setup", {**payload, "username": username}),
                                    ("owner-one", "owner-two")))
        self.assertEqual(sorted(result[0] for result in results), [201, 409])
        _, identity, issued = next(result for result in results if result[0] == 201)
        cookie = issued.split(";", 1)[0]
        self.assertEqual(self.request("/admin")[0], 401)
        self.assertEqual(self.request("/api/admin/setup", payload)[0], 409)
        status, invite, _ = self.request("/api/admin/invites", {"requestId": str(uuid4())},
                                         cookie=cookie, marker=identity["marker"])
        self.assertEqual(status, 201)
        code = invite["code"]
        self.assertEqual(self.request("/api/register", {"username":"reviewer-one","password":self.password,"invite":code})[0], 201)
        self.assertEqual(self.request("/api/register", {"username":"reviewer-two","password":self.password,"invite":code})[0], 400)

    def test_expiry_rotation_and_existing_username(self):
        expired = self.setup_code()
        with self.store.connection(write=True) as db:
            db.execute("UPDATE admin_setup SET expires=0")
        payload = {"username":"existing-one", "password":self.password, "code":expired}
        self.assertEqual(self.request("/admin")[0], 401)
        self.assertEqual(self.request("/api/admin/setup", payload)[0], 403)
        previous = self.setup_code()
        fresh = self.setup_code()
        self.assertEqual(self.request("/api/admin/setup", {**payload,"code":previous})[0], 403)
        self.assertEqual(self.request("/api/register", {
            "username":"existing-one","password":self.password,"invite":self.store.create_invite()
        })[0], 201)
        self.assertEqual(self.request("/api/admin/setup", {**payload,"code":fresh})[0], 409)
        self.assertEqual(self.request("/api/admin/setup", {**payload,"code":fresh,"username":"owner-one"})[0], 201)
        result = subprocess.run([sys.executable, "-B", "workbench.py", "--runs-dir", str(self.runs),
                                 "--create-admin-setup-code"], text=True, capture_output=True, env=self.environment)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_password_change_requires_current_password_and_revokes_old_sessions(self):
        code = self.setup_code()
        status, _, _ = self.request("/api/admin/setup", {"username":"owner-one","password":self.password,"code":code})
        self.assertEqual(status, 201)
        def login():
            status, _, issued = self.request("/api/login", {"username":"owner-one","password":self.password})
            self.assertEqual(status, 200)
            cookie = issued.split(";", 1)[0]
            _, identity, _ = self.request("/api/session", cookie=cookie)
            return {"cookie":cookie, "marker":identity["marker"]}
        active = login()
        second = login()
        _, identity, ordinary = self.request("/api/register", {"username":"ordinary-one","password":self.password,
                                                                "invite":self.store.create_invite()})
        payload = {"currentPassword":self.password,"newPassword":self.changed}
        path = "/api/admin/password"
        self.assertEqual(self.request(path, payload, cookie=ordinary.split(";", 1)[0], marker=identity["marker"])[0], 403)
        self.assertEqual(self.request(path, {**payload,"currentPassword":"wrong"}, **active)[0], 401)
        self.assertEqual(self.request(path, payload, **active)[0], 200)
        for session in (active, second):
            self.assertEqual(self.request("/api/trial/admin", **session)[0], 401)
        self.assertEqual(self.request("/api/login", {"username":"owner-one","password":self.password})[0], 401)
        status, _, issued = self.request("/api/login", {"username":"owner-one","password":self.changed})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/admin", cookie=issued.split(";", 1)[0])[0], 200)
