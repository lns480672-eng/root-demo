"""Validate generated record-tool code and wrap it without adding business UI."""

from datetime import date
from html import escape
import json

from accounts import ApiError
import staged_website


FORMAT = "record-code-v1"
MARKER = "<!--root:implementation=record-code-v1-->"
STORAGE_KEY = "root-structured-records-v1"
SYSTEM = """根据需求及已验收字段，为台账或看板生成可操作的 HTML、CSS、JavaScript。
返回 JSON：{"format":"record-code-v1","markup":"HTML 片段","style":"CSS","script":"普通 JavaScript"}。
自己编写布局、响应式样式和交互，宿主不提供表单、卡片、看板或事件处理器。
用 JSON 字符串分别序列化 HTML、CSS 和 JavaScript，双引号、反斜杠和换行必须按 JSON 规则转义。
启动时直接调用 window.rootRecords.list()；例如 window.rootRecords.list().then(render).catch(showError)。
写入时直接调用 window.rootRecords.create(values)、window.rootRecords.update(id, patch)。
API 只在 window.rootRecords 上，不在 DOM 节点上。方法名只有 list、create、update，不使用 listRecords、createRecord 或 updateRecord。
三种方法均返回 Promise，结果都是最新记录数组，每条是 {id, ...values}。create 和 update 成功后，用返回的数组替换页面的记录状态再渲染；切换看板列时不要用旧数组重绘。按 accepted_spec 中的稳定字段 ID 读写。新增和更新只可发送已声明的字段 ID，choice 只能发送规格列出的原始选项。
必须为用户要求保存的状态使用规格中已声明的字段。若缺少必需状态字段或选项，返回 {"unsupported":true}，不要自行添加 ID 或把状态只留在页面内存。
create 接受字段对象，update 只提交当前记录的已变动字段。整数字段用数值，日期为 YYYY-MM-DD。
只在用户操作时写入，每次操作立即调用一个写入方法。初次加载读取并展示已有记录，写入成功后刷新显示，失败提示并保留输入。
操作结束后先解除忙碌状态，再渲染可操作的记录和按钮，避免新增的卡片因渲染时的忙碌状态无法操作。
用 textContent 显示记录。不要用 localStorage、网络、外部资源、跳转或依赖。
CSS 不写 style 标签、URL、注释或转义；仅允许 @media 响应式规则。适配 390px，确保主要操作可以用键盘。
不写文档外壳，不重复创建 root-record-tool。仅输出 JSON，不用 Markdown。"""


def matches(spec):
    return spec.get("implementation") == FORMAT


def validate(code):
    if not isinstance(code, dict) or code.keys() != {"format", "markup", "style", "script"}:
        raise ValueError("bad_record_code_shape")
    if code["format"] != FORMAT:
        raise ValueError("bad_record_code_format")
    staged_website.validate_fragment(code)


def assemble(spec, code):
    validate(code)
    return f"""<!doctype html>{MARKER}<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(spec['title'])}</title><style>{code['style']}</style></head><body>
<div id="root-record-tool">{code['markup']}</div>
<script>(() => {{
'use strict';
{code['script']}
}})();</script></body></html>""".encode()


def valid_value(field, value):
    if value == "":
        return not field["required"]
    kind = field["type"]
    if kind == "text":
        return isinstance(value, str) and 0 < len(value.strip()) and len(value) <= 300
    if kind == "integer":
        return type(value) is int and 0 <= value <= 1_000_000_000
    if kind == "choice":
        return value in field["options"]
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def verification_entries(fields):
    values = {"text": "验证记录", "integer": 7, "date": "2026-01-15"}
    row = {"id": 1}
    for field in fields:
        kind = field["type"]
        row[field["id"]] = field["options"][0] if kind == "choice" else values[kind]
    return [[STORAGE_KEY, json.dumps([row], ensure_ascii=False)]]


def mutate(fields, records, operation):
    """Compute one record write from the latest host-owned rows."""
    error = ApiError(400, "invalid_record_operation", "记录操作、字段或值无效。")
    if not isinstance(operation, dict):
        raise error
    action = operation.get("action")
    if action == "create":
        if set(operation) != {"action", "values"}:
            raise error
    elif action == "update":
        if set(operation) != {"action", "id", "values"} or type(operation["id"]) is not int:
            raise error
    else:
        raise error
    by_id = {field["id"]: field for field in fields}
    values = operation["values"]
    if not isinstance(values, dict) or not values or not values.keys() <= by_id.keys():
        raise error
    values = {key: "" if value is None and not by_id[key]["required"] else value
              for key, value in values.items()}
    if any(not valid_value(by_id[key], value) for key, value in values.items()):
        raise error
    if action == "create":
        if any(field["required"] and field["id"] not in values for field in fields):
            raise error
        identifier = max((record["id"] for record in records), default=0) + 1
        return [*records, {"id": identifier, **values}]
    for index, record in enumerate(records):
        if record["id"] == operation["id"]:
            return [*records[:index], {**record, **values}, *records[index + 1:]]
    raise error
