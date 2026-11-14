"""An isolated provider fixture for the formal staged workbench browser route."""
import json
import os
from pathlib import Path
import sys
import time
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
tool_actions_check = "--tool-actions" in sys.argv[5:]
standalone_check = "--standalone" in sys.argv[5:]


def history_program(stage, requirement, admit, record):
    admit()
    if stage == "implementation" and requirement.endswith("首次失败"):
        record({"stage":stage, "status":"invalid_staged_result", "dispatched":True})
        raise ApiError(502, "invalid_staged_result", "替身模拟无效实现")
    record({"stage":stage, "status":"ok", "dispatched":True})
    if stage == "spec":
        content = {"kind":"program", "title":"历史复测工具", "goal":"验证明确重新提交后的保存"}
    else:
        content = {"markup":'<p id="history-saved">历史复测工具已保存</p>', "script":"void 0;"}
    return json.dumps(content, ensure_ascii=False).encode()


def staged(stage, requirement, prior, key, admit, record, *, category="auto"):
    if live:
        return staged_blueprint.provide(stage, requirement, prior, key, admit, record, category=category)
    if "--running-layout" in sys.argv:
        time.sleep(6 if stage == "spec" else 10)
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
    if standalone_check and requirement.startswith("独立验收："):
        if "官网" in requirement:
            from demo_site_fixture import demo_site_provider
            return demo_site_provider(stage, requirement, prior, key, admit, record, category=category)
        admit()
        if "计算器" in requirement:
            content = {
                "spec": {"kind":"program", "title":"独立验收计算器", "goal":"验证两数之和", "storage":"none"},
                "implementation": {
                    "markup": '<label>甲<input id="left" type="number" value="2"></label>'
                              '<label>乙<input id="right" type="number" value="3"></label>'
                              '<button id="add" type="button">求和</button><output id="total"></output>',
                    "script": 'document.querySelector("#add").onclick = () => {'
                              'document.querySelector("#total").textContent = '
                              'Number(document.querySelector("#left").value) + Number(document.querySelector("#right").value);};',
                },
            }[stage]
        else:
            content = {**modules["spec"], "kind":"ledger", "title":"独立验收看板", "modules":[]} if stage == "spec" else modules[stage]
        record({"stage":stage, "status":"ok", "dispatched":True})
        return json.dumps(content, ensure_ascii=False).encode()
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
    if live or stage != "revision-plan":
        return staged_revision.provide(stage, requirement, prior, key, admit, record, base=base)

    def exchange(payload, reserve, **_):
        reserve()
        content = {"status":"unsupported","reason":"当前工具不支持此操作。"}
        if theme_check:
            if requirement == "验收改为白色":
                content = {"status":"clarify","question":"请明确分栏的颜色还是字段显示名？"}
            operations = {
                "把颜色改成黄色": {"kind":"theme","theme":"yellow"},
                "公司名称改为雇主": {"kind":"rename_field","field":"company","label":"雇主"},
                "新增一个可空文本字段「项目」": {"kind":"add_field","label":"项目","type":"text","required":None},
                "把「面试中」分栏背景改为浅蓝": {"kind":"column_theme","option":"面试中","theme":"blue"},
            }
            user = json.loads(json.loads(payload)["body"]["messages"][1]["content"])
            answers = user["revision_source"].get("clarification", [])
            selected = answers[-1]["answer"] if answers else requirement
            if selected in operations:
                content = {"status":"proposed","operation":operations[selected]}
        elif base["kind"] == "program" and requirement == "把按钮文案改为收藏，模拟补丁失配":
            before = ">加入书单<" if base.get("repair_feedback") else ">missing<"
            content = {"status":"proposed","edits":[{"part":"markup","before":before,"after":">收藏<"}]}
        elif tool_actions_check and base["kind"] == "program" and requirement.startswith("验收改按钮："):
            before, after = requirement.removeprefix("验收改按钮：").split(" → ", 1)
            content = {"status":"proposed","edits":[{"part":"markup","before":f">{before}<","after":f">{after}<"}]}
        elif base["kind"] == "program" and requirement == "把按钮文案从加入书单改为待取消":
            content = {"status":"proposed","edits":[{"part":"markup","before":">加入书单<","after":">待取消<"}]}
        elif base["kind"] == "program" and "不存在" not in requirement and "加入书单" in requirement:
            content = {"status":"proposed","edits":[{"part":"markup","before":">添加<","after":">加入书单<"}]}
        elif base["kind"] == "table" and requirement.startswith("增加一个"):
            content = {"status":"proposed","operation":{"kind":"table_filter","column":1}}
        elif base["kind"] == "table" and requirement.startswith("只把标题"):
            content = {"status":"proposed","operation":{"kind":"title","label":"交付行动看板"}}
        return {"status": 200, "body": json.dumps({"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(content, ensure_ascii=False)},
        }]})}

    with patch.object(assistant_model, "_exchange", exchange):
        return staged_revision.provide(stage, requirement, prior, key, admit, record, base=base)


limits = (2, 2) if "--quota-feedback" in sys.argv[5:] else (5, 30)
if tool_actions_check or standalone_check:
    limits = (30, 100)
provider = staged
if "--demo-site" in sys.argv[5:] and not live:
    from demo_site_fixture import demo_site_provider
    provider = demo_site_provider
workbench.serve(key, int(sys.argv[2]), runs, staged_enabled=True, generation_limits=limits,
                staged_provider=provider, staged_revision_provider=revise)
