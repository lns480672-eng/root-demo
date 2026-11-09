"""An isolated provider fixture for the formal staged workbench browser route."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from accounts import ApiError, Store
import staged_blueprint
import workbench

stages = ["待投递", "已投递", "简历筛选", "面试中", "已发offer", "已结束"]
fields = [
    ("company", "公司名称", "text", True, []),
    ("role", "岗位名称", "text", True, []),
    ("date", "投递时间", "date", True, []),
    ("link", "链接", "text", False, []),
    ("note", "备注", "text", False, []),
    ("stage", "投递阶段", "choice", True, stages),
]
modules = {
    "spec":{"title":"求职投递看板","fields":[dict(zip(("id","label","type","required","options"), field)) for field in fields]},
    "entry":{"form":[field[0] for field in fields[:-1]],"columns":[field[0] for field in fields],"sort":None,
             "edit_choice":{"field":"stage","initial":stages[0]}, "board":"stage",
             "allow_edit":True,"allow_delete":True,"backup":True},
    "filters":{"search":[],"exact":[],"date":None},
    "summary":{"count":None,"sum":None,"group":None},
    "export":{"columns":[],"label":None},
}
failed = set()


def staged(stage, requirement, prior, key, admit, record):
    if requirement.startswith("做一份简单读书清单"):
        admit()
        content = {"kind":"program","title":"旧清单","goal":"录入并保存书名"} if stage == "spec" else {
            "markup":'<label for="book">书名</label><input id="book"><button id="add" type="button">添加</button><ul id="list"></ul>',
            "script":"""
const old = JSON.parse(localStorage.getItem("books") || "[]");
const render = () => document.querySelector("#list").textContent = old.join("，");
document.querySelector("#add").onclick = () => {
  old.push(document.querySelector("#book").value);
  localStorage.setItem("books", JSON.stringify(old));
  render();
};
render();
"""}
        record({"stage":stage,"status":"ok","dispatched":True})
        return json.dumps(content, ensure_ascii=False).encode()
    if live:
        return staged_blueprint.provide(stage, requirement, prior, key, admit, record)
    admit()
    if stage == "entry" and requirement not in failed:
        failed.add(requirement)
        record({"stage":stage, "status":"fixture_failed", "dispatched":True})
        raise ApiError(502, "fixture_failed", "替身模拟录入阶段失败")
    record({"stage":stage, "status":"ok", "dispatched":True})
    return json.dumps(modules[stage], ensure_ascii=False).encode()


live = os.environ.get("ROOT_STAGED_LIVE") == "1"
key = os.environ.get("DEEPSEEK_API_KEY", "").strip() if live else "fixture-key"
if not key:
    raise SystemExit("Missing configured model key")
runs = Path(sys.argv[3])
Store(runs / "workspace.sqlite3").create_invite("fixture-invite", uses=2)
workbench.serve(key, int(sys.argv[2]), runs, staged_enabled=True,
                staged_provider=staged)
