"""Read-only ROOT development board and optional local Wiki entry point."""

import argparse
from contextlib import ExitStack
from datetime import datetime
from html import escape
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
from urllib.parse import parse_qs, urlencode, urlsplit


ROOT = Path(__file__).resolve().parents[1]
ITEMS = ROOT / "docs/dev-board-items.json"
BUGS = ROOT / "缺陷明细.md"
CI = ROOT / "tests/local-ci.py"
WIKI = Path.home() / ".local/bin/dev-wiki"
TEMPLATE = ROOT / "docs/dev-board.html"
STYLES = ROOT / "docs/dev-board.css"
STAGES = {
    "ready": ("需求就绪通知", "需求方", ".ready.json"),
    "kickoff": ("开工交接", "需求方／总控", ".kickoff.json"),
    "qa-design-ack": ("QA 测试设计认领", "QA", ".design-ack.json"),
    "handoff": ("开发提测", "研发", ".sent.json"),
    "qa-accept": ("QA 接测", "QA", ".accepted.json"),
    "qa-result": ("QA 结论", "QA", ".result.json"),
    "product-start": ("产品验收交接", "研发", ".product-start.json"),
    "product-result": ("产品验收结论", "需求方", ".product-result.json"),
    "deploy": ("部署校验", "运维", ""),
}
ORDER = (
    "product-result", "ready", "qa-accept", "qa-result", "qa-design-ack",
    "kickoff", "handoff", "product-start", "deploy", None,
)
TABS = {"all": "全部", "requirements": "需求", "bugs": "Bug", "ledger": "历史缺陷"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def relative(value):
    return str(Path(value).resolve().relative_to(ROOT))


def check_status(index):
    try:
        result = subprocess.run(
            [sys.executable, "-B", str(CI), "status", str(index)],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=8,
        )
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        reason = "本次校验超时，请刷新重查。"
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        reason = getattr(error, "stderr", None) or str(error)
    return {"state": "状态无法核验", "reason": reason.strip(), "unverified": True}


def load_items():
    items = read_json(ITEMS)
    if BUGS.is_file():
        for line in BUGS.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"\| (ROOT-[A-Z0-9-]+) \| (.*?) \| (.*?) \| (.*?) \| (.*?) \|", line)
            if match:
                issue, title, scope, state, note = match.groups()
                items.append({
                    "id": issue, "type": "Bug", "title": title, "ledger": True,
                    "scope": scope, "state": state.replace("`", ""),
                    "note": re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", note),
                })
    return items


def read_details(index, report):
    evidence = []
    try:
        records = read_json(index)
        for stage in report.get("verified", []):
            label, _, suffix = STAGES[stage]
            record = ROOT / records[stage]
            receipt = record.with_name(record.name + suffix)
            when = ""
            if suffix:
                stamp = read_json(receipt)["queued_at"]
                when = datetime.fromisoformat(stamp).astimezone().strftime("%m-%d %H:%M:%S")
            details = read_json(record)
            evidence.append((stage, label, relative(receipt), when, details.get("result")))
    except (ValueError, OSError, KeyError, TypeError):
        return []
    return evidence


def render_details(item, report, index):
    if index is None:
        source = "缺陷明细" if item.get("ledger") else "业务负责人反馈"
        return f'<p>{escape(item["note"])}</p><p>来源《{source}》</p>'
    rows = []
    for stage, label, source, when, result in read_details(index, report):
        description = " · ".join(part for part in (label, result, when) if part)
        url = "/dev-board/evidence?" + urlencode({"id": item["id"], "stage": stage})
        rows.append(f'<li><a href="{escape(url)}">{escape(description)}</a>'
                    f'<br><code>{escape(source)}</code></li>')
    evidence = f"<ul>{''.join(rows)}</ul>" if rows else "<p>暂无可核验的阶段回执</p>"
    history = []
    parent = index.parent if "history" not in item else (ROOT / item["history"]).parent
    for old in sorted({parent / "status.json", *parent.glob("*/status.json")} - {index}):
        if not old.is_file():
            continue
        try:
            handoff = read_json(old)["handoff"]
            commit = read_json(ROOT / handoff)["commit"][:7]
        except (ValueError, OSError, KeyError, TypeError):
            continue
        if commit != report.get("candidate", {}).get("commit", "")[:7] and commit not in history:
            history.append(commit)
    past = f'<p>其他候选 <code>{escape(" · ".join(history))}</code></p>' if history else ""
    return f'<p>CI 索引 <code>{escape(relative(index))}</code></p>{evidence}{past}'


def render_row(item, report, index):
    stage = report.get("next")
    owner = item.get("owner", STAGES.get(stage, ("", "—", ""))[1])
    if report["state"].endswith(("验收失败", "验收阻塞")):
        owner = "研发／总控"
    if report["state"] == "部署校验通过，待执行切换":
        owner = "运维"
    candidate = report.get("candidate", {})
    commit = candidate.get("commit", "")[:7]
    build = candidate.get("build", "")
    pending = [
        part["state"] for key, part in report.get("pending", {}).items() if key != stage
    ]
    reason = report.get("reason")
    if stage in STAGES and reason == f"需要填写 {stage}。":
        reason = f"等待{STAGES[stage][0]}记录"
    reason = " · ".join(filter(None, [reason, *pending])) or item.get("note_status", "—")
    state = report["state"]
    if state == "待部署校验" and "product-result" in report.get("verified", []):
        state = "本地产品验收通过 · 待部署校验"
    tone = "neutral"
    if report.get("unverified") or any(word in state for word in ("失败", "阻塞", "未执行")):
        tone = "blocked"
    elif stage in ("product-result", "ready", "qa-accept"):
        tone = "attention"
    elif stage == "deploy" or owner == "运维":
        tone = "passed"
    detail = render_details(item, report, index)
    return (
        f'<article class="item"><div class="item-name"><span class="ref">{escape(item["id"])}'
        f'</span><span class="type">{escape(item["type"])}</span><h2>{escape(item["title"])}</h2></div>'
        f'<div><span class="mobile-label">阶段</span><span class="stage {tone}">{escape(state)}</span></div>'
        f'<div><span class="mobile-label">下一位</span>{escape(owner)}</div>'
        f'<div class="reason"><span class="mobile-label">卡点</span>{escape(reason)}</div>'
        f'<div class="version"><span class="mobile-label">候选</span><code>{escape(commit or "—")}'
        f'</code><small>{escape(build)}</small></div>'
        f'<details><summary>查看依据</summary>{detail}</details></article>'
    )


def render_board(tab, wiki_running):
    items = load_items()
    active = [item for item in items if not item.get("ledger")]
    ledger = [item for item in items if item.get("ledger")]
    counts = {"all": len(active), "requirements": sum(i["type"] == "需求" for i in active),
              "bugs": sum(i["type"] == "Bug" for i in active), "ledger": len(ledger)}
    tabs = []
    for key, label in TABS.items():
        current = ' aria-current="page"' if key == tab else ""
        url = "/dev-board?" + urlencode({"view": key})
        tabs.append(f'<a href="{url}"{current}>{label} <span>{counts[key]}</span></a>')
    rows = []
    for item in ledger if tab == "ledger" else active:
        if tab in ("requirements", "bugs") and item["type"] != TABS[tab]:
            continue
        index = ROOT / item["index"] if "index" in item else None
        report = (
            check_status(index) if index else {
                "state": item["state"],
                "reason": item.get("note_status", "缺陷台账记录"),
            }
        )
        stage = report.get("next")
        rank = -1 if report.get("unverified") or any(
            word in report["state"] for word in ("失败", "阻塞", "未执行")
        ) else ORDER.index(stage)
        rows.append((rank, render_row(item, report, index)))
    template = TEMPLATE.read_text(encoding="utf-8")
    return template.format(
        tabs="".join(tabs),
        rows="".join(row for _, row in sorted(rows, key=lambda item: item[0])) or "<p>暂无事项</p>",
        time=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        refresh="/dev-board?" + urlencode({"view": tab}),
        wiki='<a href="/">开发 Wiki</a>' if wiki_running else "",
    ).encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = f"127.0.0.1:{self.server.server_port}"
        if (self.headers.get("Host") != host or
                self.headers.get("Origin") not in (None, f"http://{host}") or
                self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self.send_error(403)
            return
        request = urlsplit(self.path)
        if request.path == "/dev-board" or (request.path == "/" and not self.server.wiki_port):
            tab = parse_qs(request.query).get("view", ["all"])[0]
            try:
                body = render_board(tab if tab in TABS else "all", self.server.wiki_port)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"开发看板读取失败：{error}", file=sys.stderr)
                self.send_error(503, "Unable to read the board data. Refresh to retry.")
            else:
                self.send_page(body, "text/html; charset=utf-8")
        elif request.path == "/dev-board/evidence":
            self.show_evidence(parse_qs(request.query))
        elif request.path == "/dev-board.css":
            self.send_page(STYLES.read_bytes(), "text/css; charset=utf-8")
        elif self.server.wiki_port:
            self.forward_wiki(request.path)
        else:
            self.send_error(404)

    def show_evidence(self, query):
        issue = query.get("id", [""])[0]
        stage = query.get("stage", [""])[0]
        try:
            item = next(item for item in read_json(ITEMS) if item["id"] == issue)
            index = ROOT / item["index"]
            report = check_status(index)
            _, label, source, when, result = next(
                entry for entry in read_details(index, report) if entry[0] == stage
            )
            record = ROOT / read_json(index)[stage]
            data = read_json(record)
            lines = [item["title"], label, when, "候选 " + data.get("commit", "待固定"),
                     "范围 " + "、".join(data.get("scope", [])), "回执 " + source]
            if result:
                lines += ["结论 " + result]
            if stage == "product-result":
                path = ROOT / data["report"]
                if path.suffix == ".md" and path.resolve().is_relative_to(ROOT / "runs"):
                    lines += ["", path.read_text(encoding="utf-8")]
            if stage == "qa-result":
                lines += ["", *(f'{key}  {case["result"]}' for key, case in data["cases"].items())]
        except (StopIteration, OSError, ValueError, KeyError, TypeError):
            self.send_error(404, "No verified evidence for this stage.")
        else:
            self.send_page("\n".join(lines).encode(), "text/plain; charset=utf-8")

    def send_page(self, body, media_type):
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def forward_wiki(self, route):
        backend = HTTPConnection("127.0.0.1", self.server.wiki_port, timeout=10)
        try:
            backend.request("GET", self.path)
            response = backend.getresponse()
            body = response.read()
            if response.status == 200 and route in ("/", "/index.html"):
                marker = b'<p class="app-meta" data-app-meta></p>'
                link = '<a href="/dev-board">开发看板</a> · '.encode()
                body = body.replace(marker, marker + link, 1)
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in ("connection", "keep-alive", "transfer-encoding", "content-length"):
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.send_error(502, "The local Wiki is unavailable. Open /dev-board for the board.")
        finally:
            backend.close()


def start_wiki(resources):
    child = resources.enter_context(subprocess.Popen(
        [str(WIKI), str(ROOT / "runs/llm-wiki/root"),
         "--releases", str(ROOT / "runs/llm-wiki/versions"), "view", "--port", "0"],
        stdout=subprocess.PIPE, text=True,
    ))
    resources.callback(lambda: child.terminate() if child.poll() is None else None)
    startup = child.stdout.readline()
    address = re.search(r"http://127\.0\.0\.1:(\d+)", startup)
    if address is None:
        raise RuntimeError(f"Wiki 未启动：{startup.strip()}")
    return int(address[1])


def main():
    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=17437, type=int)
    parser.add_argument("--board-only", action="store_true", help="只打开看板，无需安装 Wiki")
    args = parser.parse_args()
    with ExitStack() as resources:
        server = resources.enter_context(ThreadingHTTPServer(("127.0.0.1", args.port), Handler))
        server.wiki_port = None if args.board_only else start_wiki(resources)
        print(f"开发看板 http://127.0.0.1:{server.server_port}/dev-board", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
