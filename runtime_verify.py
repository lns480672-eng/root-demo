"""Run an immutable tool candidate in an isolated Chromium verifier."""
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading

from accounts import ApiError, TOOL_STORAGE_BYTES, pack_entries
from staged_record_code import mutate


HELPER = Path(__file__).with_name("runtime-verify.mjs")
SLOTS = threading.BoundedSemaphore(1)
DEADLINE_SECONDS = 12
CHROME_NAMES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
)


def _unavailable():
    return ApiError(503, "staged_verifier_unavailable", "服务端预览检查暂不可用，候选尚未保存。")


class RuntimeVerificationError(ApiError):
    def __init__(self, diagnostic):
        super().__init__(422, "staged_runtime_failed", "候选未通过服务端运行检查，原工具仍可用。")
        self.diagnostic = diagnostic


def check_script(source):
    """Parse a classic browser script without executing the generated code."""
    node = shutil.which(os.environ.get("ROOT_VERIFY_NODE", "node"))
    if node is None:
        raise _unavailable()
    parser = """
const {readFileSync} = require('node:fs');
const {Script} = require('node:vm');
const source = readFileSync(0, 'utf8');
try {
    new Script(source);
} catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    process.exitCode = 2;
}
"""
    try:
        result = subprocess.run([node, "-e", parser], input=source.encode("utf-8"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env={"PATH": os.defpath}, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        raise _unavailable() from None
    if result.returncode == 2:
        raise ApiError(502, "invalid_staged_result", "生成的脚本语法有误，请重新生成。")
    if result.returncode != 0:
        raise _unavailable()


def _command():
    node = shutil.which(os.environ.get("ROOT_VERIFY_NODE", "node"))
    configured = os.environ.get("ROOT_VERIFY_CHROME")
    candidates = (configured,) if configured else CHROME_NAMES
    chrome = next((path for name in candidates if (path := shutil.which(name))), None)
    if not node or not chrome:
        raise _unavailable()
    return [node, str(HELPER), chrome]


def verify(html, entries, *, backup=False, storage_limit=TOOL_STORAGE_BYTES, website=False, record_field=None, record_fields=None, record_visibility=None):
    """Require isolated startup and the applicable website checks for the fixed snapshot."""
    packed = pack_entries(entries, limit=storage_limit)
    if not isinstance(html, bytes) or not html.lower().startswith(b"<!doctype html>"):
        raise RuntimeVerificationError("invalid_document")
    try:
        source = html.decode("utf-8")
    except UnicodeError:
        raise RuntimeVerificationError("invalid_document") from None

    command = _command()
    if not SLOTS.acquire(timeout=1):
        raise _unavailable()
    try:
        with tempfile.TemporaryDirectory(prefix="root-runtime-") as directory:
            # The fresh browser does not inherit app secrets or proxy credentials.
            env = {"PATH": os.defpath, "TMPDIR": directory}
            for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
                env[name] = directory
            payload = json.dumps({"html": source, "entries": json.loads(packed), "backup": backup,
                                  "limit": storage_limit, "website": website, "recordField": record_field,
                                  "recordVisibility": record_visibility}).encode()
            with subprocess.Popen([*command, directory], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=env, start_new_session=True) as process:
                try:
                    output, _ = process.communicate(payload, timeout=DEADLINE_SECONDS)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                    raise _unavailable() from None
    except OSError:
        raise _unavailable() from None
    finally:
        SLOTS.release()

    if process.returncode == 0 and output.startswith(b"record:"):
        try:
            result = json.loads(output[len(b"record:"):])
            if record_field:
                submissions = result["submissions"]
                if len(submissions) != (1 if record_field.get("required") else 2):
                    raise ValueError("bad_record_submissions")
                filled = submissions[0]
                mutate(record_fields, [], {"action": "create", "values": filled})
                if not record_field.get("required"):
                    mutate(record_fields, [], {"action": "create", "values": submissions[1]})
            if record_visibility is not None:
                visible = result["visible"]
                if any(value not in visible for value in record_visibility):
                    raise RuntimeVerificationError("record_visibility")
                return visible
            return
        except (ApiError, ValueError, TypeError, KeyError, RecursionError) as error:
            if isinstance(error, RuntimeVerificationError):
                raise
            raise RuntimeVerificationError("record_field_behavior") from None
    if process.returncode == 0 and output == b"ok\n":
        if record_field or record_visibility is not None:
            raise RuntimeVerificationError("record_field_behavior")
        return
    if process.returncode == 0:
        for reason in ("script_exception", "frame_changed", "isolation_event", "document_not_ready",
                       "website_tabs", "record_initialization", "record_field_behavior", "runtime_timeout", "storage_not_allowed"):
            if output == f"failed:{reason}\n".encode("ascii"):
                raise RuntimeVerificationError(reason)
    raise _unavailable()
