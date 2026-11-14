"""Check a change record and the linked review routes for the revision journey."""

import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
CHECKS = {
    "my-tools": ("AC-006", "我的工具", "design/root-tool-experience-review.html#tool", "tool-review"),
    "confirmation": ("AC-007 / AC-018-C", "修改前确认", "design/root-revision-clarification.html#confirmation", "confirm"),
    "recovery": ("AC-018-B2", "失败恢复", "design/root-revision-clarification.html#recovery", "repair"),
}
REFERENCES = {
    "实施计划.md": ("继续修改前先确认", "继续修改失败后的自助恢复"),
    "DESIGN.md": ("工具修改对话",),
    "docs/开发与验收流程.md": ("继续修改已保存工具",),
}


class RenderedPage(HTMLParser):
    VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self):
        super().__init__()
        self.stack = []
        self.visible_ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = "hidden" in attrs or bool(self.stack and self.stack[-1][1])
        if not hidden and "id" in attrs:
            self.visible_ids.add(attrs["id"])
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, hidden))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def check_record(record):
    if not isinstance(record, dict) or not isinstance(record.get("change"), str) or not record["change"].strip():
        raise ValueError("变更记录需要填写 change。")
    entries = record.get("checks")
    if not isinstance(entries, dict):
        raise ValueError("变更记录需要填写 checks。")
    missing = [f"{ac}「{label}」" for key, (ac, label, _, _) in CHECKS.items() if key not in entries]
    if missing:
        raise ValueError("缺少关联项 " + "、".join(missing))
    for key, item in entries.items():
        if key not in CHECKS:
            raise ValueError(f"未知关联项 {key}")
        if not isinstance(item, dict) or item.get("status") not in ("已同步", "已核对，无需改动"):
            raise ValueError(f"{key} 的 status 应为「已同步」或「已核对，无需改动」。")
        evidence = item.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"{key} 缺少核对依据 evidence。")


def check_references():
    for path, markers in REFERENCES.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                raise ValueError(f"{path} 缺少「{marker}」。")
    for _, label, route, _ in CHECKS.values():
        if not (ROOT / route.split("#", 1)[0]).is_file():
            raise ValueError(f"「{label}」的原型文件不存在：{route}")


def check_browser():
    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    chrome = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome and mac_chrome.is_file():
        chrome = str(mac_chrome)
    if not chrome:
        raise ValueError("找不到 Chrome。请设置 CHROME_BIN。")

    with TemporaryDirectory(prefix="root-related-ci-") as profile:
        for ac, label, route, marker in CHECKS.values():
            filename, fragment = route.split("#", 1)
            url = (ROOT / filename).as_uri() + "#" + fragment
            result = subprocess.run(
                [chrome, "--headless", "--no-first-run", "--no-default-browser-check",
                 f"--user-data-dir={profile}", "--dump-dom", url],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
            if result.returncode:
                raise ValueError(f"Chrome 无法打开 {route}（退出码 {result.returncode}）。")
            page = RenderedPage()
            page.feed(result.stdout)
            if marker not in page.visible_ids:
                raise ValueError(f"{ac}「{label}」未显示预期状态：{route}")
            print(f"通过 {ac}「{label}」")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="本次变更的 JSON 核对记录")
    args = parser.parse_args()
    try:
        check_record(json.loads(args.record.read_text(encoding="utf-8")))
        check_references()
        check_browser()
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"关联需求检查失败：{error}", file=sys.stderr)
        return 1
    print("关联需求记录通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
