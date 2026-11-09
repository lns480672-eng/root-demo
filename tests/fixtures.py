"""Predictable provider responses for browser regression; never used by workbench.py CLI."""

import json
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate
from accounts import Store

reading = """<!doctype html><html lang="zh-CN"><head><title>读书清单</title></head>
<body><h1>读书清单</h1><form><label>书名 <input id="book" required></label>
<button type="submit">添加书籍</button></form><ul id="list"></ul>
<script>
const books = JSON.parse(localStorage.getItem('books') || '[]');
const render = () => {
    list.replaceChildren(...books.map(book => {
        const item = document.createElement('li');
        item.textContent = book;
        return item;
    }));
};
document.querySelector('form').onsubmit = event => {
    event.preventDefault();
    books.push(book.value);
    localStorage.setItem('books', JSON.stringify(books));
    book.value = '';
    render();
};
render();
try { parent.document.body; document.body.dataset.parentAccess = 'allowed'; }
catch { document.body.dataset.parentAccess = 'blocked'; }
const probe = PROBE_URL;
fetch(probe + '/fetch', { mode: 'no-cors' })
    .then(() => document.body.dataset.network = 'allowed')
    .catch(() => document.body.dataset.network = 'blocked');
const image = document.createElement('img');
image.hidden = true;
image.src = probe + '/image';
document.body.append(image);
const nested = document.createElement('iframe');
nested.hidden = true;
nested.src = probe + '/nested';
document.body.append(nested);
</script></body></html>"""

budget = """<!doctype html><html lang="zh-CN"><head><title>搬家预算计算器</title></head>
<body><h1>搬家预算计算器</h1><label>租车费 <input id="truck" type="number" value="800"></label>
<label>纸箱费 <input id="boxes" type="number" value="180"></label>
<button id="calculate">计算总额</button><p id="total" role="status"></p>
<script>
const saved = JSON.parse(localStorage.getItem('budget-values') || 'null');
if (saved) {
    truck.value = saved[0];
    boxes.value = saved[1];
    total.textContent = Number(truck.value) + Number(boxes.value);
}
calculate.onclick = () => {
    localStorage.setItem('budget-values', JSON.stringify([truck.value, boxes.value]));
    total.textContent = Number(truck.value) + Number(boxes.value);
};
document.body.dataset.readingData = localStorage.getItem('books') === null ? 'absent' : 'present';
</script></body></html>"""

budget_attempt = 0

def fixture(requirement, key):
    global budget_attempt
    if requirement == "回归：模型超时":
        raise TimeoutError("Fixture provider timeout")
    if requirement in {"回归：模型限流", "回归：余额不足"}:
        status = 429 if requirement == "回归：模型限流" else 402
        raise urllib.error.HTTPError(generate.ENDPOINT, status, "Fixture HTTP error", None, None)
    name = "reading"
    if requirement.startswith("修改现有工具"):
        name = "reading-revision"
        if "修改要求：回归：修改输出失败\n" in requirement:
            html = "fixture failure"
        elif "修改要求：回归：不兼容数据\n" in requirement:
            html = reading.replace("'books'", "'other-books'").replace("PROBE_URL", json.dumps(sys.argv[4]))
        else:
            html = reading.replace(
                '<ul id="list">',
                '<label>筛选 <select id="read-filter"><option value="all">全部</option>'
                '<option value="read">已读</option></select></label><ul id="list">',
            ).replace(
                'books.map(book => {', 'books.filter(book => readFilter.value === "all" || book.startsWith("已读：")).map(book => {',
            ).replace(
                "const books = ", 'const readFilter = document.querySelector("#read-filter");\nconst books = ',
            ).replace(
                "render();\ntry", "readFilter.onchange = render;\nrender();\ntry",
            ).replace("PROBE_URL", json.dumps(sys.argv[4]))
    elif "读书清单" in requirement:
        html = reading.replace("PROBE_URL", json.dumps(sys.argv[4]))
    elif "搬家预算计算器" in requirement:
        budget_attempt += 1
        name = f"budget-{budget_attempt}"
        if budget_attempt == 1:
            html = "fixture failure"
        elif budget_attempt == 2:
            html = budget.replace("calculate.onclick", """
                localStorage.setItem('staged', 'candidate only');
                throw new Error('fixture initialization failure');
                calculate.onclick""")
        else:
            if budget_attempt == 4:
                release = Path(sys.argv[3]).parent / "release-model"
                release.with_name("model-started").touch()
                deadline = time.monotonic() + 8
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(0.025)
                if not release.exists():
                    raise TimeoutError("Fixture model gate was not released")
            html = budget.replace("calculate.onclick", """
                localStorage.setItem('budget-version', '1');
                calculate.onclick""")
    else:
        html = "fixture failure"
    result = {
        "id": "fixture-" + name,
        "model": generate.MODEL,
        "choices": [{"message": {"content": html}, "finish_reason": "stop"}],
    }
    return 200, json.dumps(result).encode()


generate.request_completion = fixture
import workbench
import assistant_fixture

runs = Path(sys.argv[3])
store = Store(runs / "workspace.sqlite3")
store.create_invite("fixture-invite", uses=2)
store.create_invite("fixture-expired-invite", hours=-1)
store.create_invite("fixture-used-invite", uses=0, hours=-1)
workbench.serve(sys.argv[1], int(sys.argv[2]), runs, generation_limits=(100, 500),
                analysis_limits=(100, 500), analysis_provider=assistant_fixture.provider)
