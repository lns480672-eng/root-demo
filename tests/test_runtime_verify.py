import json
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen
from unittest.mock import patch

from accounts import ApiError
from runtime_verify import RuntimeVerificationError, _command, verify
from staged_record_code import verification_entries


def artifact(script):
    return ('<!doctype html><title>工具</title><p>初始页面</p><script>' + script + '</script>').encode()


class RuntimeVerifierTests(unittest.TestCase):
    def setUp(self):
        try:
            _command()
        except ApiError:
            self.skipTest("Node and sandboxed Chromium are required for the real verifier tests")

    def test_runs_candidate_with_the_given_snapshot_in_an_opaque_frame(self):
        source = artifact("""
            if (self.origin !== 'null' || localStorage.getItem('record') !== '草稿') throw new Error('context');
            try { void parent.document; throw new Error('parent access'); }
            catch (error) { if (error.name !== 'SecurityError') throw error; }
            document.querySelector('p').textContent = '实际启动';
        """)
        verify(source, [["record", "草稿"]])
        with self.assertRaises(ApiError) as result:
            verify(source, [["record", "错误快照"]])
        self.assertEqual((result.exception.status, result.exception.code), (422, "staged_runtime_failed"))

    def test_startup_exception_and_forged_ready_cannot_pass(self):
        for script in (
            "throw new Error('startup');",
            "const invalid = ;",
            "document.getElementById('missing').textContent = 'unreachable';",
            """
            const token = document.scripts[0].textContent.match(/"([0-9a-f-]{36})"/)?.[1];
            parent.postMessage({ token, type: 'ready' }, '*');
            setTimeout(() => Promise.reject(new Error('startup rejection')), 20);
            """,
        ):
            with self.subTest(script=script), self.assertRaises(ApiError) as result:
                verify(artifact(script), [])
            self.assertEqual((result.exception.status, result.exception.code), (422, "staged_runtime_failed"))

    def test_no_storage_candidate_cannot_obtain_the_workbench_storage_bridge(self):
        def no_storage(script):
            return artifact(script).replace(b"<!doctype html>", b"<!doctype html><!--root:storage=none-->", 1)

        verify(no_storage("document.querySelector('p').textContent = 120 / 3;"), [])
        for source, entries, diagnostic in (
            (no_storage("window['local' + 'Storage'].clear();"), [], "script_exception"),
            (no_storage("void 0;"), [["record", "old"]], "storage_not_allowed"),
        ):
            with self.subTest(source=source), self.assertRaises(ApiError) as result:
                verify(source, entries)
            self.assertEqual((result.exception.status, result.exception.code), (422, "staged_runtime_failed"))
            self.assertEqual(result.exception.diagnostic, diagnostic)

    def test_record_candidate_missing_initial_read_is_a_runtime_rejection(self):
        source = artifact("void 0;").replace(
            b"<!doctype html>", b"<!doctype html><!--root:implementation=record-code-v1-->", 1,
        )
        with self.assertRaises(ApiError) as result:
            verify(source, [])
        self.assertEqual((result.exception.status, result.exception.code), (422, "staged_runtime_failed"))
        self.assertEqual(result.exception.diagnostic, "record_initialization")
        delayed = source.replace(b"void 0;", b"setTimeout(() => rootRecords.list(), 500);")
        verify(delayed, [])

    def test_record_field_probe_observes_real_submission(self):
        source = b"""<!doctype html><!--root:implementation=record-code-v1-->
        <form><input id="field_1" type="date"><button>Save</button></form>
        <script>
          rootRecords.list();
          document.querySelector('form').onsubmit = event => {
            event.preventDefault();
            rootRecords.create({field_1: document.querySelector('input').value}).catch(() => {});
          };
        </script>"""
        field = {"id": "field_1", "type": "date"}
        verify(source, [], record_field=field, record_fields=[{**field, "required":False}])
        for broken in (
            source.replace(b'type="date"', b'type="text"'),
            source.replace(b"rootRecords.create({field_1:", b"if (document.querySelector('input').value) rootRecords.create({field_1:"),
            source.replace(b"field_1: document.querySelector('input').value", b"other: '2026-01-17'"),
            source.replace(b"event.preventDefault();", b"event.preventDefault(); event.stopImmediatePropagation();")
                  .replace(b"rootRecords.create", b"setTimeout(() => rootRecords.create")
                  .replace(b".catch(() => {});", b".catch(() => {}), 30);")
                  .replace(b".onsubmit = event => {", b".onclick = event => {"),
        ):
            with self.subTest(source=broken), self.assertRaises(ApiError) as rejected:
                verify(broken, [], record_field=field, record_fields=[{**field, "required":False}])
            self.assertEqual(getattr(rejected.exception, "diagnostic", None), "record_field_behavior")

    def test_old_record_probe_catches_a_candidate_that_only_works_when_empty(self):
        baseline = artifact("rootRecords.list();").replace(
            b"<!doctype html>", b"<!doctype html><!--root:implementation=record-code-v1-->", 1,
        )
        candidate = baseline.replace(
            b"rootRecords.list();",
            b"rootRecords.list().then(rows => { if (rows.length) throw new Error('old rows'); });",
        )
        entries = verification_entries([{"id": "name", "type": "text", "required": True, "options": []}])
        verify(candidate, [])
        verify(baseline, entries)
        with self.assertRaises(ApiError) as rejected:
            verify(candidate, entries)
        self.assertEqual(rejected.exception.diagnostic, "script_exception")

    def test_candidate_cannot_hide_old_record_values_visible_in_baseline(self):
        baseline = b"""<!doctype html><!--root:implementation=record-code-v1-->
        <p id="value"></p><script>
        rootRecords.list().then(rows => { document.getElementById('value').textContent = rows[0].book; });
        </script>"""
        entries = verification_entries([{"id":"book", "type":"text", "required":True}])
        visible = verify(baseline, entries, record_visibility=[])
        self.assertEqual(visible, ["验证记录"])
        hidden = baseline.replace(b'<p id="value">', b'<p id="value" hidden>')
        with self.assertRaises(RuntimeVerificationError) as rejected:
            verify(hidden, entries, record_visibility=visible)
        self.assertEqual(rejected.exception.diagnostic, "record_visibility")

    def test_record_field_probe_rejects_missing_required_value(self):
        source = b"""<!doctype html><!--root:implementation=record-code-v1-->
        <form><input id="field_1" type="date"><button>Save</button></form>
        <script>
          rootRecords.list();
          document.querySelector('form').onsubmit = event => {
            event.preventDefault();
            rootRecords.create({field_1: document.querySelector('input').value}).catch(() => {});
          };
        </script>"""
        field = {"id": "field_1", "type": "date"}
        fields = [{**field, "required":False}, {"id":"name", "type":"text", "required":True}]
        with self.assertRaises(ApiError) as rejected:
            verify(source, [], record_field=field, record_fields=fields)
        self.assertEqual(getattr(rejected.exception, "diagnostic", None), "record_field_behavior")

    def test_runtime_diagnostics_distinguish_script_exception_from_inert_website_tabs(self):
        with self.assertRaises(ApiError) as startup:
            verify(artifact("throw new Error('private-model-text');"), [])
        self.assertEqual(getattr(startup.exception, "diagnostic", None), "script_exception")
        self.assertNotIn("private-model-text", str(startup.exception))

        tabs = (b'<!doctype html><div role="tablist">'
                b'<button role="tab" aria-selected="true">First</button>'
                b'<button role="tab" aria-selected="false">Second</button></div>')
        with self.assertRaises(ApiError) as interaction:
            verify(tabs, [], website=True)
        self.assertEqual(getattr(interaction.exception, "diagnostic", None), "website_tabs")

    def test_candidate_cannot_reach_http_or_webrtc_loopback_probe(self):
        arrivals = []

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self):
                arrivals.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Probe) as http, \
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(("127.0.0.1", 0))
            udp.settimeout(0.2)
            server = threading.Thread(target=http.serve_forever, daemon=True)
            server.start()
            try:
                base = f"http://127.0.0.1:{http.server_port}"
                with urlopen(base + "/control", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                udp.sendto(b"positive-control", udp.getsockname())
                self.assertEqual(udp.recv(256), b"positive-control")
                target = json.dumps(base + "/candidate")
                stun = json.dumps(f"stun:127.0.0.1:{udp.getsockname()[1]}")
                verify(artifact(f"""
                    fetch({target}).then(() => {{ throw new Error('unexpected HTTP'); }}, () => {{}});
                    navigator.sendBeacon({target}, 'unexpected');
                    const ws = new WebSocket({target}.replace('http:', 'ws:'));
                    ws.onopen = () => {{ throw new Error('unexpected WebSocket'); }};
                    const pc = new RTCPeerConnection({{iceServers:[{{urls:{stun}}}]}});
                    if (pc.getConfiguration().iceServers.length) throw new Error('unexpected ICE configuration');
                    pc.createDataChannel('probe');
                    pc.createOffer().then(offer => pc.setLocalDescription(offer));
                """), [])
                self.assertEqual(arrivals, ["/control"])
                with self.assertRaises(socket.timeout):
                    udp.recv(256)
            finally:
                http.shutdown()
                server.join(timeout=2)

    def test_website_tabs_must_switch_selection_and_declared_panels(self):
        markup = (
            '<!doctype html><div role="tablist">'
            '<button id="early" role="tab" aria-selected="true" aria-controls="first">第一时段</button>'
            '<button id="late" role="tab" aria-selected="false" aria-controls="second">第二时段</button>'
            '</div><p id="first" role="tabpanel">集合</p><p id="second" role="tabpanel" hidden>观测</p>'
        )
        selection_only = """
            document.querySelector('#late').addEventListener('click', () => {
                document.querySelector('#early').setAttribute('aria-selected', 'false');
                document.querySelector('#late').setAttribute('aria-selected', 'true');
            });
        """
        forward_only = selection_only + """
            document.querySelector('#late').addEventListener('click', () => {
                document.querySelector('#first').hidden = true;
                document.querySelector('#second').hidden = false;
            });
        """
        for script in ("void 0;", selection_only, forward_only):
            with self.subTest(script=script):
                with self.assertRaises(ApiError) as result:
                    verify((markup + "<script>" + script + "</script>").encode(), [], website=True)
                self.assertEqual((result.exception.status, result.exception.code), (422, "staged_runtime_failed"))
        verify((markup + "<script>" + forward_only + """
            document.querySelector('#early').addEventListener('click', () => {
                document.querySelector('#early').setAttribute('aria-selected', 'true');
                document.querySelector('#late').setAttribute('aria-selected', 'false');
                document.querySelector('#first').hidden = false;
                document.querySelector('#second').hidden = true;
            });
        </script>""").encode(), [], website=True)

    def test_website_tabs_can_rerender_and_update_panels_asynchronously(self):
        source = artifact("""
            const host = document.createElement('div');
            document.body.append(host);
            const render = selected => {
                host.innerHTML = '<div role="tablist">' + ['a', 'b'].map(id =>
                    '<button id="' + id + '" role="tab" aria-controls="shared panel-' + id +
                    '" aria-selected="' + (id === selected) + '">' + id + '</button>').join('') +
                    '</div><p id="shared">共同说明</p><p id="panel-a">内容 A</p><p id="panel-b">内容 B</p>';
                for (const tab of host.querySelectorAll('button')) tab.onclick = () => render(tab.id);
                setTimeout(() => {
                    host.querySelector('#panel-a').hidden = selected !== 'a';
                    host.querySelector('#panel-b').hidden = selected !== 'b';
                }, 60);
            };
            render('a');
        """)
        verify(source, [], website=True)

    def test_website_tabs_without_distinct_panels_must_change_visible_content(self):
        for controls in ("", ' aria-controls="detail"'):
            markup = (
                '<!doctype html><main><section><div role="tablist">'
                f'<button role="tab" aria-selected="true"{controls}>第一档</button>'
                f'<button role="tab" aria-selected="false"{controls}>第二档</button>'
                '</div></section><article id="detail">集合时间</article><p id="status"></p></main>'
            )
            handlers = """
                for (const tab of document.querySelectorAll('button')) {
                    tab.onclick = () => {
                        for (const other of document.querySelectorAll('button')) {
                            other.setAttribute('aria-selected', String(other === tab));
                        }
                        ACTION
                    };
                }
            """
            with self.subTest(controls=controls):
                noop = handlers.replace("ACTION", "tab.textContent += '已选';")
                with self.assertRaises(ApiError) as result:
                    verify((markup + "<script>" + noop + "</script>").encode(), [], website=True)
                self.assertEqual(result.exception.code, "staged_runtime_failed")
                if controls:
                    status_only = handlers.replace("ACTION", "document.querySelector('#status').textContent = tab.textContent;")
                    with self.assertRaises(ApiError):
                        verify((markup + "<script>" + status_only + "</script>").encode(), [], website=True)
                working = handlers.replace("ACTION", """
                    setTimeout(() => {
                        document.querySelector('#detail').textContent =
                            tab.textContent === '第一档' ? '集合时间' : '观测地点';
                    }, 40);
                """)
                verify((markup + "<script>" + working + "</script>").encode(), [], website=True)

    def test_website_without_custom_tabs_keeps_native_controls(self):
        verify(b'<!doctype html><details><summary>FAQ</summary><p>Answer</p></details>', [], website=True)

    def test_missing_browser_cannot_authorize_a_candidate(self):
        with patch.dict(os.environ, {"ROOT_VERIFY_CHROME": "/path/to/nonexistent/chromium"}), \
                self.assertRaises(ApiError) as result:
            verify(artifact("document.body.dataset.ready = 'true';"), [])
        self.assertEqual((result.exception.status, result.exception.code), (503, "staged_verifier_unavailable"))


if __name__ == "__main__":
    unittest.main()
