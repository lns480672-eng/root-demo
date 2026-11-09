"""An isolated provider fixture for the formal staged workbench browser route."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from accounts import ApiError, Store
import assistant_model
import staged_blueprint
import staged_revision
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
theme_check = "--ledger-theme" in sys.argv[5:]
history_check = "--staged-history" in sys.argv[5:]


def history_program(stage, requirement, admit, record, base=None):
    admit()
    if stage == "implementation" and requirement.endswith("首次失败"):
        record({"stage":stage, "status":"invalid_staged_result", "dispatched":True})
        raise ApiError(502, "invalid_staged_result", "替身模拟无效实现")
    record({"stage":stage, "status":"ok", "dispatched":True})
    if base:
        content = dict(base["source"][stage])
        if stage == "implementation":
            content["markup"] += '<p id="history-revised">同一工具的新版本</p>'
    elif stage == "spec":
        content = {"kind":"program", "title":"历史复测工具", "goal":"验证明确重新提交后的保存"}
    else:
        content = {"markup":'<p id="history-saved">历史复测工具已保存</p>', "script":"void 0;"}
    return json.dumps(content, ensure_ascii=False).encode()


def staged(stage, requirement, prior, key, admit, record, *, category="auto"):
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
        return staged_blueprint.provide(stage, requirement, prior, key, admit, record, category=category)
    if history_check:
        return history_program(stage, requirement, admit, record)
    if not theme_check and stage == "entry" and not requirement.startswith("做一个公益活动雨具归还登记"):
        content = modules["entry"]
        if requirement not in failed:
            failed.add(requirement)
            content = {**content, "board": "unknown_status_field"}

        def exchange(payload, reserve, **_):
            reserve()
            return {"status": 200, "body": json.dumps({"choices": [{
                "finish_reason": "stop", "message": {"content": json.dumps(content, ensure_ascii=False)},
            }]})}

        with patch.object(assistant_model, "_exchange", exchange):
            return staged_blueprint.provide(stage, requirement, prior, key, admit, record)
    admit()
    if theme_check:
        content = {**modules["spec"], "kind":"ledger", "modules":[]} if stage == "spec" else modules[stage]
        record({"stage":stage, "status":"ok", "dispatched":True})
        return json.dumps(content, ensure_ascii=False).encode()
    if requirement.startswith("做一个公益活动雨具归还登记"):
        fields = [
            {"id":"item", "label":"物品编号", "type":"text", "required":True, "options":[]},
            {"id":"quantity", "label":"数量", "type":"integer", "required":True, "options":[]},
            {"id":"date", "label":"归还日期", "type":"date", "required":True, "options":[]},
        ]
        ids = [field["id"] for field in fields]
        registration = {
            "spec":{"kind":"ledger", "title":"雨具归还登记", "fields":fields, "modules":[]},
            "entry":{"form":ids, "columns":ids, "sort":None, "edit_choice":None},
        }
        record({"stage":stage,"status":"ok","dispatched":True})
        return json.dumps(registration[stage], ensure_ascii=False).encode()
    record({"stage":stage, "status":"ok", "dispatched":True})
    return json.dumps(modules[stage], ensure_ascii=False).encode()


live = os.environ.get("ROOT_STAGED_LIVE") == "1"
key = os.environ.get("DEEPSEEK_API_KEY", "").strip() if live else "fixture-key"
if not key:
    raise SystemExit("Missing configured model key")
runs = Path(sys.argv[3])
Store(runs / "workspace.sqlite3").create_invite("fixture-invite", uses=2)
def revise(stage, requirement, prior, key, admit, record, *, base):
    if history_check and not live:
        return history_program(stage, requirement, admit, record, base)
    if live or (base["kind"] != "table" and not theme_check):
        return staged_revision.provide(stage, requirement, prior, key, admit, record, base=base)

    def exchange(payload, reserve, **_):
        reserve()
        if theme_check:
            if requirement == "把颜色改成黄色":
                content = {"theme":"yellow"}
            elif requirement == "把颜色改成黑色":
                content = {"theme":"gray"}
            else:
                content = base["source"][stage]
        elif requirement.startswith("增加一个"):
            content = {"addFilter": 1}
        elif requirement.startswith("只把标题"):
            content = {"title": "交付行动看板"}
        else:
            content = {"addFilter": 999}
        return {"status": 200, "body": json.dumps({"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(content, ensure_ascii=False)},
        }]})}

    with patch.object(assistant_model, "_exchange", exchange):
        return staged_revision.provide(stage, requirement, prior, key, admit, record, base=base)


limits = (2, 2) if "--quota-feedback" in sys.argv[5:] else (5, 30)
provider = staged
if "--demo-site" in sys.argv[5:] and not live:
    from demo_site_fixture import demo_site_provider
    provider = demo_site_provider
workbench.serve(key, int(sys.argv[2]), runs, staged_enabled=True, generation_limits=limits,
                staged_provider=provider, staged_revision_provider=revise)
