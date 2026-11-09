"""TEST-004: account boundaries through the real local HTTP entry."""
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
from accounts import Store, digest


class AccountHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.runs = directory.name
        cls.server = subprocess.Popen(
            [sys.executable, "-B", "-c",
             "import sys, workbench, generate, assistant_fixture, json\n"
             "from pathlib import Path\n"
             "from accounts import ApiError, Store\n"
             "def fixture(requirement, key):\n"
             "    if requirement == '额度测试超时': raise TimeoutError('fixture timeout')\n"
             "    if requirement == '额度统计不完整': return 200, b'{}'\n"
             "    return 200, json.dumps({'id':'fixture-account','model':generate.MODEL,"
             "'choices':[{'finish_reason':'stop','message':{'content':'<!doctype html><html><body>工具</body></html>'}}]}).encode()\n"
             "generate.request_completion = fixture\n"
             "def analysis(data, key, admit, *, record=None):\n"
             "    if data.get('idea') == '额度统计不完整':\n"
             "        admit()\n"
             "        raise ApiError(502, 'invalid_analysis_result', 'fixture validation error')\n"
             "    return assistant_fixture.provider(data, key, admit, record=record)\n"
             "runs = Path(sys.argv[1])\n"
             "store = Store(runs / 'workspace.sqlite3')\n"
             "store.register({'username':'trial-admin','password':'fixture-test-password!',"
             "'invite':store.create_invite()})\n"
             "workbench.serve('fixture-key', 0, runs, generation_limits=(5, 100), "
             "analysis_limits=(6, 100), admin_usernames=('trial-admin',), analysis_provider=analysis)",
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

    def test_trial_quotas_pause_and_admin_authorization(self):
        self.assertEqual(self.register("trial-user", self.store.create_invite())[0], 201)
        admin, admin_marker = self.login("trial-admin")
        user, marker = self.login("trial-user")
        balance = "/api/trial"
        manage = "/api/trial/admin"
        status, initial, _ = self.request(balance, cookie=user, marker=marker)
        self.assertEqual(status, 200)
        self.assertEqual(initial["generation"]["remaining"], 5)
        self.assertEqual(initial["analysis"]["remaining"], 6)
        self.assertEqual(self.request(manage, cookie=user, marker=marker)[0], 403)
        self.assertEqual(self.request(manage, {"action": "pause"}, user, marker)[0], 403)
        self.assertEqual(self.request(manage, {"action": "pause"}, admin, admin_marker)[0], 200)
        self.assertEqual(self.request("/api/generate", {"requirement": "空耗测试"}, user, marker)[1]["code"], "model_paused")
        analysis = {"step": "start", "idea": "记录项目跟进", "requestId": str(uuid4())}
        self.assertEqual(self.request("/api/assistant/analyze", analysis, user, marker)[1]["code"], "model_paused")
        draft = "/api/requirement-drafts/" + str(uuid4())
        self.assertEqual(self.request(draft, {"text": "暂停时可保存", "revision": 0}, user, marker)[0], 200)
        self.assertEqual(self.request(manage, {"action": "resume"}, admin, admin_marker)[0], 200)
        self.assertEqual(self.request("/api/generate", {"requirement": " "}, user, marker)[0], 400)
        self.assertEqual(self.request("/api/generate", {"requirement": "额度测试超时"}, user, marker)[0], 502)
        after_failure = self.request(balance, cookie=user, marker=marker)[1]
        self.assertEqual(after_failure["generation"]["remaining"], 5)
        self.assertEqual(after_failure["generation"]["totalUsed"], initial["generation"]["totalUsed"] + 1)
        self.assertEqual(self.request("/api/assistant/analyze", analysis, user, marker)[0], 200)
        self.assertEqual(self.request("/api/assistant/analyze", analysis, user, marker)[1]["code"], "analysis_duplicate")
        for _ in range(5):
            status, tool, _ = self.request("/api/generate", {"requirement": "读书清单"}, user, marker)
            self.assertEqual(status, 200)
            self.assertEqual(self.request("/api/tools/" + tool["id"] + "/commit", {"entries": []}, user, marker)[0], 200)
        self.assertEqual(self.request("/api/generate", {"requirement": "清单"}, user, marker)[1]["code"], "generation_account_limit")
        self.assertEqual(self.request(manage, {"action": "grant", "username": "trial-user", "amount": 2}, admin, admin_marker)[0], 200)
        status, current, _ = self.request(balance, cookie=user, marker=marker)
        self.assertEqual(status, 200)
        self.assertEqual((current["generation"]["remaining"], current["analysis"]["remaining"]), (2, 5))

    def test_admin_page_requires_operator_session(self):
        # TEST-017-ADMIN. Direct navigation has its own gate, in addition to the data API.
        def page(cookie=None):
            headers = {"Cookie": cookie} if cookie else {}
            try:
                response = urlopen(Request(self.base + "/admin", headers=headers), timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status, response.read().decode()

        self.assertEqual(page()[0], 401)
        self.assertEqual(self.register("admin-route-member", self.store.create_invite())[0], 201)
        member, _ = self.login("admin-route-member")
        admin, _ = self.login("trial-admin")
        self.assertEqual(page(member)[0], 403)
        status, html = page(admin)
        self.assertEqual(status, 200)
        self.assertIn("用量与额度", html)
        self.assertIn('src="/admin.js"', html)

    def test_trial_admin_model_outcomes_follow_actual_dispatches(self):
        # TEST-017-STAT. Exercise provider-backed handlers rather than seeding outcome totals.
        self.assertEqual(self.register("stats-user", self.store.create_invite())[0], 201)
        admin, admin_marker = self.login("trial-admin")
        user, marker = self.login("stats-user")
        manage = "/api/trial/admin"
        generate = "/api/generate"
        analyze = "/api/assistant/analyze"

        def snapshot():
            status, body, _ = self.request(manage, cookie=admin, marker=admin_marker)
            self.assertEqual(status, 200)
            return body

        def analyze_input(idea):
            return {"step": "start", "idea": idea, "requestId": str(uuid4())}

        def counts(body, kind):
            item = body[kind]
            self.assertTrue({"used", "completed", "failed", "failureRate"} <= set(item),
                            f"{kind} must expose model outcomes")
            return item["used"], item["completed"], item["failed"]

        initial = snapshot()
        self.assertEqual(self.request(manage, cookie=user, marker=marker)[0], 403)
        self.assertEqual(self.request(manage, {"action": "pause"}, admin, admin_marker)[0], 200)
        self.addCleanup(self.request, manage, {"action": "resume"}, admin, admin_marker)
        self.assertEqual(self.request(generate, {"requirement": "不应派发"}, user, marker)[0], 503)
        self.assertEqual(self.request(analyze, analyze_input("不应派发"), user, marker)[0], 503)
        self.assertEqual(snapshot()["generation"], initial["generation"])
        self.assertEqual(snapshot()["analysis"], initial["analysis"])
        self.assertEqual(self.request(manage, {"action": "resume"}, admin, admin_marker)[0], 200)
        self.assertEqual(self.request(analyze, analyze_input(" "), user, marker)[0], 400)

        for requirement, status in (("读书清单", 200), ("额度测试超时", 502), ("额度统计不完整", 502)):
            self.assertEqual(self.request(generate, {"requirement": requirement}, user, marker)[0], status)
        valid_analysis = analyze_input("记录项目跟进")
        self.assertEqual(self.request(analyze, valid_analysis, user, marker)[0], 200)
        self.assertEqual(self.request(analyze, valid_analysis, user, marker)[0], 409)
        self.assertEqual(self.request(analyze, analyze_input("额度统计不完整"), user, marker)[0], 502)
        current = snapshot()
        for kind, expected in (("generation", (3, 3, 2)), ("analysis", (2, 2, 1))):
            before = counts(initial, kind)
            after = counts(current, kind)
            self.assertEqual(tuple(now - old for now, old in zip(after, before)), expected)
            self.assertAlmostEqual(current[kind]["failureRate"], after[2] / after[1])
        own = self.request("/api/trial", cookie=user, marker=marker)[1]
        self.assertEqual(own["generation"]["used"], 0)

    def test_initial_publication_readback_fences_late_commit_and_keeps_single_charge(self):
        self.assertEqual(self.register("initial-recover", self.store.create_invite())[0], 201)
        cookie, marker = self.login("initial-recover")
        self.assertEqual(self.register("initial-other", self.store.create_invite())[0], 201)
        other, other_marker = self.login("initial-other")
        _, first, _ = self.request("/api/generate", {"requirement": "保存回执未到达"}, cookie, marker)
        path = "/api/tools/" + first["id"]
        self.assertEqual(self.request(path + "/resolve-commit", {}, other, other_marker)[0], 404)
        for _ in range(2):
            self.assertEqual(self.request(path + "/resolve-commit", {}, cookie, marker)[:2],
                             (200, {"applied": False}))
        self.assertEqual(self.request(path + "/commit", {"entries": []}, cookie, marker)[0], 404)
        self.assertEqual(self.request("/api/trial", cookie=cookie, marker=marker)[1]["generation"]["used"], 0)
        _, second, _ = self.request("/api/generate", {"requirement": "保存已提交"}, cookie, marker)
        path = "/api/tools/" + second["id"]
        self.assertEqual(self.request(path + "/commit", {"entries": []}, cookie, marker)[0], 200)
        for _ in range(2):
            status, resolved, _ = self.request(path + "/resolve-commit", {}, cookie, marker)
            self.assertEqual((status, resolved["applied"], resolved["id"]), (200, True, second["id"]))
        self.assertEqual(self.request("/api/trial", cookie=cookie, marker=marker)[1]["generation"]["used"], 1)
        self.assertEqual([item["id"] for item in self.request("/api/tools", cookie=cookie, marker=marker)[1]["items"]],
                         [second["id"]])

    def test_requirement_drafts_owner_revision_and_replay(self):
        for username in ("draft-one", "draft-two"):
            self.assertEqual(self.register(username, self.store.create_invite())[0], 201)
        owner, marker = self.login("draft-one")
        other, other_marker = self.login("draft-two")
        collection = "/api/requirement-drafts"
        identifier = str(uuid4())
        path = collection + "/" + identifier
        original = {"revision": 0, "text": "  可编辑需求\n第二行  "}
        self.assertEqual(self.request(collection)[0], 401)
        self.assertEqual(self.request(path, original, owner, marker)[0], 200)
        first = self.request(path, cookie=owner, marker=marker)[1]
        self.assertEqual((first["text"], first["revision"]), (original["text"], 1))
        self.assertEqual(self.request(path, original, owner, marker)[1], first)
        self.assertEqual(self.request(path, cookie=other, marker=other_marker)[0], 404)
        self.assertEqual(self.request(path, original, other, other_marker)[0], 404)
        self.assertEqual(self.request(collection, cookie=other, marker=other_marker)[1]["items"], [])
        second, second_marker = self.login("draft-one")
        self.assertEqual(self.request(collection, cookie=second, marker=second_marker)[1]["items"][0]["id"], identifier)
        with ThreadPoolExecutor(2) as pool:
            attempts = [
                pool.submit(self.request, path, {"revision": 1, "text": text}, cookie, token)
                for text, cookie, token in [("窗口甲", owner, marker), ("窗口乙", second, second_marker)]
            ]
            statuses = sorted(attempt.result()[0] for attempt in attempts)
        self.assertEqual(statuses, [200, 409])
        current = self.request(path, cookie=owner, marker=marker)[1]
        self.assertEqual(current["revision"], 2)
        self.assertEqual(self.request(path, {"revision": 1, "text": current["text"]}, second, second_marker)[1], current)
        self.assertEqual(self.request("/api/tools", cookie=owner, marker=marker)[1]["items"], [])

    def test_assistant_fixture_has_structured_paths_without_generation(self):
        path = "/api/assistant/analyze"
        start = {"step": "start", "idea": "每周都要在群里问项目进度，不知道谁卡住了"}
        self.assertEqual(self.request(path, {**start, "requestId": str(uuid4())})[0], 401)
        self.assertEqual(self.register("assistant-path", self.store.create_invite())[0], 201)
        cookie, marker = self.login("assistant-path")
        def analyze(data):
            return self.request(path, {**data, "requestId": str(uuid4())}, cookie, marker)
        status, first, _ = analyze(start)
        self.assertEqual(status, 200)
        self.assertEqual(first["kind"], "directions")
        self.assertGreaterEqual(len(first["directions"]), 2)
        results = []
        for selected in first["directions"][:2]:
            status, questions, _ = analyze({
                **start, "step": "direction", "direction": selected,
            })
            self.assertEqual(status, 200)
            self.assertEqual(questions["kind"], "questions")
            prompts = questions["questions"]
            status, draft, _ = analyze({
                **start, "step": "answers", "direction": selected, "questions": prompts,
                "answers": [item["options"][0] for item in prompts],
            })
            self.assertEqual((status, draft["kind"]), (200, "draft"))
            self.assertIn(selected["title"], draft["text"])
            results.append(prompts)
        self.assertNotEqual(*results)
        status, clear, _ = analyze({
            "step": "start", "idea": "做一个读书清单，可以添加书名并标记已读，刷新后保留记录。",
        })
        self.assertEqual((status, clear["kind"]), (200, "draft"))
        self.assertEqual(analyze({**start, "step": "answers"})[0], 400)
        self.assertEqual(self.request("/api/tools", cookie=cookie, marker=marker)[1]["items"], [])

    def test_csv_confirmation_publishes_with_original_rows(self):
        inspect = "/api/table/inspect"
        generate = "/api/table/generate"
        payload = {"source": "upload", "filename": "交付.csv", "csv": "编号,事项,状态\n001,原始事项,待处理\n"}
        self.assertEqual(self.request(inspect, payload)[0], 401)
        for username in ("table-owner", "table-other"):
            self.assertEqual(self.register(username, self.store.create_invite())[0], 201)
        owner, marker = self.login("table-owner")
        other, other_marker = self.login("table-other")
        status, table, _ = self.request(inspect, payload, owner, marker)
        self.assertEqual(status, 200)
        self.assertEqual(table["rows"], [["001", "原始事项", "待处理"]])
        self.assertEqual(self.request(inspect, {**payload, "csv": "a,b\n1\n"}, owner, marker)[0], 400)
        self.assertEqual(self.request("/api/tools", cookie=owner, marker=marker)[1]["items"], [])
        status, tool, _ = self.request(generate, {"table": table}, owner, marker)
        self.assertEqual(status, 200)
        self.assertNotIn("原始事项", tool["html"])
        self.assertEqual(self.request("/api/tools", cookie=owner, marker=marker)[1]["items"], [])
        path = "/api/tools/" + tool["id"]
        self.assertEqual(self.request(path + "/commit", {"entries": tool["entries"]}, other, other_marker)[0], 404)
        self.assertEqual(self.request(path + "/commit", {"entries": []}, owner, marker)[1]["code"], "incompatible_data")
        self.assertEqual(self.request(path + "/commit", {"entries": tool["entries"]}, owner, marker)[0], 200)
        self.assertEqual(self.request(path, cookie=owner, marker=marker)[1]["entries"], tool["entries"])
        self.assertEqual(self.request(path, cookie=other, marker=other_marker)[0], 404)

    def test_large_table_uses_only_its_trusted_storage_limit(self):
        self.assertEqual(self.register("large-table", self.store.create_invite())[0], 201)
        cookie, marker = self.login("large-table")
        before = self.request("/api/trial", cookie=cookie, marker=marker)[1]["generation"]
        csv = "项目,备注\n" + "".join(f"{index},{'x' * 4000}\n" for index in range(70))
        status, table, _ = self.request("/api/table/inspect", {
            "source": "upload", "filename": "large.csv", "csv": csv,
        }, cookie, marker)
        self.assertEqual(status, 200)
        status, tool, _ = self.request("/api/table/generate", {"table": table}, cookie, marker)
        self.assertEqual(status, 200)
        self.assertGreater(len(tool["entries"][0][1]), 256 * 1024)
        self.assertGreater(tool["storageLimit"], 256 * 1024)
        path = "/api/tools/" + tool["id"]
        self.assertEqual(self.request(path + "/commit", {"entries": tool["entries"]}, cookie, marker)[0], 200)
        self.assertEqual(self.request(path, cookie=cookie, marker=marker)[1]["entries"], tool["entries"])
        self.assertEqual(self.request("/api/trial", cookie=cookie, marker=marker)[1]["generation"], before)

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
        expired = self.store.create_invite(hours=0)
        used_and_expired = self.store.create_invite(uses=0, hours=-1)
        for invite, reason, text in (
            (None, "missing", "请输入邀请码"),
            ("", "missing", "请输入邀请码"),
            ("  ", "missing", "请输入邀请码"),
            (False, "invalid", "邀请码无效"),
            ("x" * 129, "invalid", "邀请码无效"),
            ("wrong-invite", "invalid", "邀请码无效"),
            (expired, "expired", "邀请码已过期"),
            (used_and_expired, "used", "邀请码已使用"),
        ):
            with self.subTest(reason=reason, text=text):
                status, body = self.register("invite-check", invite)
                self.assertEqual((status, body.get("code"), body.get("inviteReason")), (400, "invalid_invite", reason))
                self.assertIn(text, body["message"])
        self.assertEqual(self.request("/api/login", {
            "username": "invite-check", "password": "fixture-test-password!",
        })[0], 401)
        invite = self.store.create_invite()
        self.assertEqual(self.register("invite-check", invite)[0], 201)
        status, body = self.register("second-invite", invite)
        self.assertEqual((status, body.get("code"), body.get("inviteReason")), (400, "invalid_invite", "used"))
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
            results = list(pool.map(lambda name: self.register(name, invite), ["invite-race-a", "invite-race-b"]))
        self.assertEqual(sorted(status for status, _ in results), [201, 400])
        failure = next(body for status, body in results if status == 400)
        self.assertEqual((failure["code"], failure.get("inviteReason")), ("invalid_invite", "used"))

    def test_unsuccessful_registration_keeps_a_valid_invite_usable(self):
        self.assertEqual(self.register("invite-conflict", self.store.create_invite())[0], 201)
        invite = self.store.create_invite()
        status, body, _ = self.request("/api/register", {"username":"invite-new", "password":"short", "invite":invite})
        self.assertEqual((status, body.get("code")), (400, "invalid_password"))
        status, body = self.register("invite-conflict", invite)
        self.assertEqual((status, body.get("code")), (409, "username_taken"))
        self.assertEqual(self.register("invite-new", " " + invite + " ")[0], 201)
        status, body = self.register("invite-extra", invite)
        self.assertEqual((status, body.get("code"), body.get("inviteReason")), (400, "invalid_invite", "used"))

    def test_admin_invite_issuance_permissions_audit_and_idempotency(self):
        path = "/api/admin/invites"
        data = {"requestId": str(uuid4())}
        self.assertEqual(self.request(path, data)[0], 401)
        self.assertEqual(self.register("invite-non-admin", self.store.create_invite())[0], 201)
        user, user_marker = self.login("invite-non-admin")
        self.assertEqual(self.request(path, data, user, user_marker)[0], 403)
        admin, marker = self.login("trial-admin")
        self.assertEqual(self.request(path, data, admin, "stale-marker")[0], 409)
        self.assertEqual(self.request(path, {"requestId":"invalid"}, admin, marker)[0], 400)
        wrong_origin = Request(self.base + path, json.dumps(data).encode(), {
            "Origin": "http://127.0.0.1:1", "Content-Type": "application/json",
            "Cookie": admin, "X-Root-Session": marker,
        })
        with self.assertRaises(HTTPError) as rejected:
            urlopen(wrong_origin, timeout=5)
        self.assertEqual(rejected.exception.code, 403)
        rejected.exception.close()

        def issue(_):
            request = Request(self.base + path, json.dumps(data).encode(), {
                "Origin": self.base, "Content-Type": "application/json", "Cookie": admin,
                "X-Root-Session": marker,
            })
            try:
                response = urlopen(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response), response.headers["Cache-Control"]

        now = int(time.time())
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(issue, range(2)))
        self.assertEqual(sorted(status for status, _, _ in responses), [201, 409])
        self.assertTrue(all(cache == "no-store" for _, _, cache in responses))
        issued = next(body for status, body, _ in responses if status == 201)
        replay = next(body for status, body, _ in responses if status == 409)
        self.assertEqual(replay["code"], "invite_already_issued")
        self.assertTrue(issued["code"] not in json.dumps(replay))
        self.assertTrue(now + 86400 <= issued["expiresAt"] <= int(time.time()) + 86400)
        self.assertEqual(issued["uses"], 1)
        code = issued["code"]
        with self.store.connection() as db:
            records = list(db.execute("""
                SELECT actor, created_at, invite_digest FROM trial_audit WHERE request_id=?
            """, (data["requestId"],)))
            invite_row = db.execute("SELECT remaining, expires FROM invites WHERE digest=?", (digest(code),)).fetchone()
        self.assertEqual(len(records), 1)
        self.assertEqual(tuple(records[0]), ("trial-admin", issued["expiresAt"] - 86400, digest(code)))
        self.assertEqual(tuple(invite_row), (1, issued["expiresAt"]))
        self.assertFalse(any(code.encode() in path.read_bytes() for path in Path(self.runs).glob("workspace.sqlite3*")))
        status, state, _ = self.request("/api/trial/admin", cookie=admin, marker=marker)
        self.assertEqual(status, 200)
        self.assertEqual(state["audit"][0]["action"], "issue_invite")
        self.assertFalse(digest(code) in json.dumps(state))
        self.assertFalse(code in json.dumps(state))

        # A new server process should read the original audit from the shared isolated database.
        args = [sys.executable, "-B", "-c",
                "import sys, workbench; from pathlib import Path; "
                "workbench.serve('fixture-key', 0, Path(sys.argv[1]), admin_usernames=('trial-admin',))", self.runs]
        with subprocess.Popen(args, stdout=subprocess.PIPE, text=True) as restarted:
            try:
                base = re.search(r"http://127\.0\.0\.1:\d+", restarted.stdout.readline()).group()
                request = Request(base + path, json.dumps(data).encode(), {
                    "Origin": base, "Content-Type": "application/json", "Cookie": admin, "X-Root-Session": marker,
                })
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(request, timeout=5)
                with rejected.exception as response:
                    self.assertEqual((response.status, json.load(response)["code"]), (409, "invite_already_issued"))
            finally:
                restarted.terminate()
                restarted.wait(timeout=5)

        self.assertEqual(self.register("issued-redeemer", code)[0], 201)
        status, body = self.register("issued-repeat", code)
        self.assertEqual((status, body["code"]), (400, "invalid_invite"))

    def test_admin_invite_issuance_rolls_back_if_audit_cannot_be_written(self):
        path = "/api/admin/invites"
        data = {"requestId": str(uuid4())}
        admin, marker = self.login("trial-admin")
        with self.store.connection(write=True) as db:
            before = db.execute("SELECT count(*) FROM invites").fetchone()[0]
            db.execute("""
                CREATE TRIGGER fail_invite_audit BEFORE INSERT ON trial_audit
                WHEN NEW.action='issue_invite' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END
            """)
        try:
            self.assertEqual(self.request(path, data, admin, marker)[0], 503)
            with self.store.connection() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM invites").fetchone()[0], before)
                self.assertIsNone(db.execute("SELECT 1 FROM trial_audit WHERE request_id=?", (data["requestId"],)).fetchone())
        finally:
            with self.store.connection(write=True) as db:
                db.execute("DROP TRIGGER fail_invite_audit")
        self.assertEqual(self.request(path, data, admin, marker)[0], 201)

    def test_registration_immediately_authenticates_new_account(self):
        status, registration, cookie = self.request("/api/register", {
            "username": "register-session",
            "password": "fixture-test-password!",
            "invite": self.store.create_invite(),
        })
        self.assertEqual(status, 201)
        self.assertIsNotNone(cookie, "注册成功后应立即返回可用的会话 Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        cookie = cookie.split(";", 1)[0]
        status, session, _ = self.request("/api/session", cookie=cookie)
        self.assertEqual((status, session["user"]), (200, "register-session"))
        self.assertEqual(registration, session)
        self.assertEqual(self.request("/api/tools", cookie=cookie, marker=session["marker"])[:2],
                         (200, {"items": [], "latest": None}))

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

    def test_tool_versions_preserve_current_data(self):
        self.assertEqual(self.register("version-owner", self.store.create_invite())[0], 201)
        self.assertEqual(self.register("version-other", self.store.create_invite())[0], 201)
        owner, marker = self.login("version-owner")
        other, other_marker = self.login("version-other")
        status, tool, _ = self.request("/api/generate", {"requirement": "版本清单"}, owner, marker)
        self.assertEqual(status, 200)
        path = "/api/tools/" + tool["id"]
        original = [["books", "first"]]
        self.assertEqual(self.request(path + "/commit", {"entries": original}, owner, marker)[0], 200)
        versions = path + "/versions"
        status, history, _ = self.request(versions, cookie=owner, marker=marker)
        self.assertEqual(status, 200)
        self.assertEqual(history["current"], 1)
        self.assertEqual([item["version"] for item in history["items"]], [1])
        self.assertGreater(history["items"][0]["effectiveAt"], 0)
        self.assertEqual(self.request(versions, cookie=other, marker=other_marker)[0], 404)
        instruction = {"requirement": "增加已读筛选", "version": 1}
        self.assertEqual(self.request(path + "/revise", instruction, other, other_marker)[0], 404)
        status, draft, _ = self.request(path + "/revise", instruction, owner, marker)
        self.assertEqual((status, draft["id"], draft["version"]), (200, tool["id"], 2))
        _, abandoned, _ = self.request(path + "/revise", {"requirement": "待取消", "version": 1}, owner, marker)
        resolution = {"runId": abandoned["runId"]}
        self.assertEqual(self.request(path + "/resolve-publish", resolution, other, other_marker)[0], 404)
        status, resolved, _ = self.request(path + "/resolve-publish", resolution, owner, marker)
        self.assertEqual((status, resolved["version"], resolved["applied"]), (200, 1, False))
        self.assertEqual(self.request(path + "/publish", {
            "revision": abandoned["revision"], "runId": abandoned["runId"], "entries": original,
        }, owner, marker)[0], 404)
        status, competing, _ = self.request(path + "/revise", {"requirement": "改标题", "version": 1}, owner, marker)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/tools/" + draft["runId"], cookie=owner, marker=marker)[0], 404)
        self.assertEqual(self.request(versions, cookie=owner, marker=marker)[1], history)
        updated = [["books", "first and second"]]
        self.assertEqual(self.request(path + "/data", {
            "revision": draft["revision"], "version": 1, "entries": updated,
        }, owner, marker)[0], 200)
        publish = {"revision": draft["revision"], "runId": draft["runId"], "entries": original}
        self.assertEqual(self.request(path + "/publish", publish, owner, marker)[1]["code"], "revision_conflict")
        publish["revision"] += 1
        self.assertEqual(self.request(path + "/publish", publish, owner, marker)[1]["code"], "incompatible_data")
        self.assertEqual(self.request(versions, cookie=owner, marker=marker)[1], history)
        publish["entries"] = updated
        self.assertEqual(self.request(path + "/publish", publish, other, other_marker)[0], 404)
        self.assertEqual(self.request(path + "/publish", publish, owner, marker)[0], 200)
        self.assertEqual(self.request(path + "/publish", publish, owner, marker)[0], 200)
        for _ in range(2):
            status, resolved, _ = self.request(path + "/resolve-publish", {"runId": draft["runId"]}, owner, marker)
            self.assertEqual((status, resolved["version"], resolved["applied"], resolved["entries"]),
                             (200, 2, True, updated))
        status, current, _ = self.request(path, cookie=owner, marker=marker)
        self.assertEqual((status, current["version"], current["entries"]), (200, 2, updated))
        self.assertEqual(self.request(path + "/data", {
            "revision": current["revision"], "version": 1, "entries": [],
        }, owner, marker)[1]["code"], "version_conflict")
        self.assertEqual(self.request(path + "/publish", {
            "revision": current["revision"], "runId": competing["runId"], "entries": updated,
        }, owner, marker)[1]["code"], "version_conflict")
        self.assertEqual(len(self.request("/api/tools", cookie=owner, marker=marker)[1]["items"]), 1)
        history = self.request(versions, cookie=owner, marker=marker)[1]
        self.assertEqual(history["current"], 2)
        self.assertEqual([item["version"] for item in history["items"]], [2, 1])
        self.assertEqual(history["items"][0]["note"], instruction["requirement"])

    def test_release_history_matches_running_build(self):
        _, config, _ = self.request("/api/config")
        status, releases, _ = self.request("/api/releases")
        self.assertEqual(status, 200)
        self.assertEqual(releases["items"][0]["build"], config["build"])
        self.assertEqual(releases["items"][0]["version"], releases["current"])
        self.assertIn("v4-44e3da0cae1b", [item["build"] for item in releases["items"][1:]])
        with urlopen(self.base + "/updates", timeout=5) as response:
            self.assertIn("webrtc=block", response.headers["Connection-Allowlist"])

    def test_existing_tool_without_version_metadata(self):
        self.assertEqual(self.register("legacy-version", self.store.create_invite())[0], 201)
        cookie, marker = self.login("legacy-version")
        tool_id = "run-legacy01"
        with self.store.connection(write=True) as db:
            owner = db.execute("SELECT id FROM accounts WHERE username='legacy-version'").fetchone()["id"]
            db.execute("INSERT INTO tools VALUES (?,?,?,?,?,?)",
                       (tool_id, owner, "保留的标题", 1, '[["books","legacy data"]]', 12))
        directory = Path(self.runs) / tool_id
        directory.mkdir()
        (directory / "app.html").write_text("<!doctype html><html><body>原工具</body></html>", encoding="utf-8")
        path = "/api/tools/" + tool_id
        status, current, _ = self.request(path, cookie=cookie, marker=marker)
        self.assertEqual((status, current["version"], current["revision"]), (200, 1, 12))
        original = self.request(path + "/versions", cookie=cookie, marker=marker)[1]["items"][0]
        self.assertTrue(original["approximate"])
        status, draft, _ = self.request(path + "/revise", {"requirement": "保留旧工具", "version": 1}, cookie, marker)
        self.assertEqual(status, 200)
        self.assertEqual(self.request(path + "/publish", {
            "revision": draft["revision"], "runId": draft["runId"], "entries": draft["entries"],
        }, cookie, marker)[0], 200)
        history = self.request(path + "/versions", cookie=cookie, marker=marker)[1]["items"]
        self.assertEqual(history[1], original)
        self.assertEqual(self.request("/api/tools", cookie=cookie, marker=marker)[1]["items"],
                         [{"id": tool_id, "title": "保留的标题"}])


if __name__ == "__main__":
    unittest.main()
