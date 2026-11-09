"""Run one DeepSeek generation with a key read directly from the local terminal."""

import getpass
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timezone
from pathlib import Path


MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"
RUNS = Path(__file__).resolve().parent / "runs"
SYSTEM_PROMPT = """根据用户需求生成一个可在隔离预览中使用的简体中文小工具。
只返回从 <!doctype html> 开始、到 </html> 结束的完整 HTML，内联 CSS 与普通 JavaScript。
禁止外部资源、网络、外链、页面跳转、弹窗和模块导入。用响应式布局适配窄容器。
如需保存数据，只使用 localStorage.getItem、setItem、removeItem、clear、key 或 length。
表单提交用 preventDefault() 留在本页，用户输入用 textContent 等文本 API 渲染。
用清晰的标题、可操作的控件和适量留白完成核心需求。"""
REQUIREMENT = """为虚构的「月球温室」做一个简体中文的单页巡检记录应用。首页标题为「月球温室巡检」。
页面首次打开时记录列表为空。表单有一个标为「巡检事项」的必填文本输入框，以及一个写着「新增记录」的提交按钮。
用户提交一条巡检事项后，页面立即在可见的记录列表中显示该事项。使用当前浏览器来源的 localStorage 持久保存记录，
刷新页面后重新渲染已保存记录。提交后留在本页。安全地用 textContent 等文本 API 显示用户输入。
页面必须是一个独立 HTML 文件，CSS 和 JavaScript 均内联，无外部资源、框架或网络请求。
返回完整 HTML 源码，从 <!doctype html> 开始。不要输出 Markdown 代码围栏或其他文字。"""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def prompt_key():
    if not sys.stdin.isatty():
        print("请在本机终端直接运行本脚本。", file=sys.stderr)
        return None

    warnings.filterwarnings("error", category=getpass.GetPassWarning)
    try:
        key = getpass.getpass("DeepSeek API Key（输入不回显）: ").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            print("Key 为空或包含无效字符，未发送请求。", file=sys.stderr)
            return None
    except (getpass.GetPassWarning, OSError, EOFError, KeyboardInterrupt):
        print("\n未取得隐藏输入，未发送请求。", file=sys.stderr)
        return None
    return key


def request_completion(requirement, key):
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": requirement},
            ],
            "thinking": {"type": "disabled"},
            "max_tokens": 4096,
            "stream": False,
        }).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.build_opener(NoRedirects()).open(request, timeout=120) as response:
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("Oversize response")
        return response.status, body


def run_generation(requirement, key, runs=RUNS, *, model_input=None):
    runs.mkdir(mode=0o700, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
    (run / "requirement.txt").write_text(requirement + "\n", encoding="utf-8")
    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    evidence = {
        "started_at_utc": started_at,
        "requested_model": MODEL,
        "thinking": "disabled",
        "endpoint": ENDPOINT,
    }
    try:
        evidence["http_status"], body = request_completion(
            requirement if model_input is None else model_input, key)
        evidence["elapsed_seconds"] = round(time.perf_counter() - start, 3)
        result = json.loads(body)
        choice = result["choices"][0]
        evidence.update({
            "request_id": result.get("id"),
            "returned_model": result.get("model"),
            "usage": result.get("usage"),
            "finish_reason": choice.get("finish_reason"),
        })
        content = choice["message"]["content"]
        html = content.strip() if isinstance(content, str) else ""
        fenced = re.fullmatch(r"```(?:html)?\s*\n(.*?)\n```", html, re.DOTALL | re.IGNORECASE)
        if fenced:
            html = fenced.group(1).strip()
        evidence["removed_markdown_fence"] = bool(fenced)
        if choice.get("finish_reason") != "stop" or not (
            html.lower().startswith("<!doctype html>") and html.lower().endswith("</html>")
        ):
            evidence["status"] = "incomplete_html"
        elif key in html:
            evidence["status"] = "credential_in_output"
        else:
            (run / "app.html").write_text(html + "\n", encoding="utf-8")
            evidence["status"] = "generated"
    except urllib.error.HTTPError as exc:
        evidence.update({"http_status": exc.code, "status": "http_error"})
    except urllib.error.URLError as exc:
        evidence.update({"status": "request_failed", "error_type": type(exc.reason).__name__})
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        evidence.update({"status": "request_failed", "error_type": type(exc).__name__})
    finally:
        evidence.setdefault("elapsed_seconds", round(time.perf_counter() - start, 3))
        evidence_path = run / "generation.json"
        pending = evidence_path.with_suffix(".tmp")
        pending.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(pending, evidence_path)
    return run, evidence


def main():
    key = prompt_key()
    if not key:
        return 2
    run, evidence = run_generation(REQUIREMENT, key)
    print(f"生成状态 {evidence['status']}。详见 {run / 'generation.json'}", flush=True)
    return 0 if evidence["status"] == "generated" else 1


if __name__ == "__main__":
    sys.exit(main())
