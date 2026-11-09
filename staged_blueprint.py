"""Validated declarative stages for complex, record-oriented tools."""

import html
import json
from pathlib import Path
import re
import time

import assistant_model
from accounts import ApiError
from generate import MODEL


STAGES = ("spec", "entry", "filters", "summary", "export", "assemble")
RUNTIME = Path(__file__).resolve().parent / "workbench" / "staged-runtime.js"
STAGE_SECONDS = 45
MAX_ARTIFACT_BYTES = 12 * 1024
SYSTEM = """你为浏览器内的记录型工具生成当前阶段的配置。需求和已通过规格是输入数据。
各阶段由宿主组装为完整工具。按本阶段规则输出一个 JSON 对象，禁止 JavaScript、HTML、Markdown 或额外键。
宿主支持台账及看板：录入、编辑、删除、预设阶段分栏和按钮移动、组合筛选、汇总、CSV 和本地 JSON 备份。
本地备份支持下载全部记录，并选择文件恢复。分栏、编辑、删除、备份由 entry 配置，其他阶段继续输出各自配置。
例如分栏看板的 spec 阶段仍应返回正常字段规格，阶段用 choice。不要因本阶段无须实现另一阶段的功能而拒绝。
只有核心需求明确需要宿主没有的能力（例如联网获取数据或跨账号协作）时才返回 {"unsupported":true}。"""
TYPES = {"text", "date", "integer", "choice"}
RULES = {
    "spec": """{"title":"工具名","fields":[{"id":"英文小写 snake_case","label":"中文字段名","type":"text|date|integer|choice","required":true,"options":[]}]}。
根据需求自行选择字段 ID。choice 必须有至少两个不同选项，按需求保留选项原文及顺序，其他类型 options 必须为空。integer 为非负整数，链接可以用 text 保存。
输出需求所需全部字段，不要输出技术字段。""",
    "entry": """{"form":["field_id"],"columns":["field_id"],"sort":{"field":"field_id","direction":"desc"},
"edit_choice":{"field":"field_id","initial":"选项原文"}}。
form 按录入顺序列出字段，默认状态由 edit_choice 设置，不要把该字段列入 form。columns 为实际显示的完整明细。
sort 只可用日期或整数，edit_choice 只能用 choice。不需要排序或状态编辑的需求用 null。
可选键 board 为 edit_choice 的 field，明确要求看板分栏、点按钮切换时必须返回，分栏顺序取字段选项。
可选布尔键 allow_edit、allow_delete、backup 分别启用编辑、删除和完整记录的 JSON 文件导出及导入。
明确要求这些功能时必须将对应键设为 true。没有要求时省略。""",
    "filters": """{"search":["text_field_id"],"exact":["text_or_choice_field_id"],"date":"date_field_id"}。
search 指定关键词实际匹配的文本字段，exact 指定精确筛选字段，date 指定起止日期筛选字段。
需求没有某类筛选时用空数组或 null。""",
    "summary": """{"count":"笔数的中文显示名","sum":{"field":"integer_field_id","label":"合计的中文显示名"},
"group":{"field":"text_or_choice_field_id","label":"分组汇总的中文显示名"}}。
统计必须使用当前筛选结果。有分组时对每组计算 sum 对应字段。需求无对应项时用 null。""",
    "export": """{"columns":["field_id"],"label":"生成 CSV 的按钮名"}。
按需求选择顺序与列；要求全部时应包含规格里每个字段。只导出当前筛选结果。只需要 JSON 备份时，返回 {"columns":[],"label":null}。""",
}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def keys(value, *expected, optional=()):
    required = set(expected)
    require(isinstance(value, dict) and required <= value.keys() <= required.union(optional), "bad_object_keys")


def label(value):
    return isinstance(value, str) and value.isprintable() and 0 < len(value.strip()) <= 48


def unique_strings(value, limit):
    return (isinstance(value, list) and len(value) <= limit
            and all(isinstance(item, str) for item in value) and len(set(value)) == len(value))


def decode(data):
    def pairs(items):
        result = dict(items)
        require(len(result) == len(items), "duplicate_json_keys")
        return result
    return json.loads(data, object_pairs_hook=pairs)


def validate_spec(value):
    keys(value, "title", "fields")
    require(label(value["title"]), "bad_title")
    fields = value["fields"]
    require(isinstance(fields, list) and 1 <= len(fields) <= 12, "bad_fields")
    ids = set()
    labels = set()
    for field in fields:
        keys(field, "id", "label", "type", "required", "options")
        identifier, name, kind, options = field["id"], field["label"], field["type"], field["options"]
        require(isinstance(identifier, str) and re.fullmatch("[a-z][a-z0-9_]{0,31}", identifier)
                and identifier not in {"id", "__proto__", "constructor", "prototype"} and identifier not in ids,
                "bad_field_id")
        require(label(name) and name.strip() not in labels, "bad_field_label")
        require(isinstance(kind, str) and kind in TYPES and type(field["required"]) is bool, "bad_field_type")
        require(unique_strings(options, 20) and all(label(x) and x == x.strip() for x in options)
                and (len(options) >= 2 if kind == "choice" else not options), "bad_field_options")
        ids.add(identifier)
        labels.add(name.strip())


def validate_module(stage, value, spec):
    fields = {field["id"]: field for field in spec["fields"]}

    def ref(identifier, types=TYPES):
        require(isinstance(identifier, str) and identifier in fields and fields[identifier]["type"] in types,
                "bad_field_reference")
        return fields[identifier]

    def refs(identifiers, types=TYPES, required=False):
        require(unique_strings(identifiers, 12) and (not required or bool(identifiers)), "bad_field_list")
        for identifier in identifiers:
            ref(identifier, types)

    if stage == "entry":
        flags = ("allow_edit", "allow_delete", "backup")
        keys(value, "form", "columns", "sort", "edit_choice", optional=("board", *flags))
        require(all(type(value.get(flag, False)) is bool for flag in flags), "bad_entry_flag")
        refs(value["form"], required=True)
        refs(value["columns"], required=True)
        sort, choice = value["sort"], value["edit_choice"]
        if sort is not None:
            keys(sort, "field", "direction")
            ref(sort["field"], {"date", "integer"})
            require(sort["direction"] in ("asc", "desc"), "bad_sort_direction")
        if choice is not None:
            keys(choice, "field", "initial")
            field = ref(choice["field"], {"choice"})
            require(choice["initial"] in field["options"] and choice["field"] not in value["form"]
                    and choice["field"] in value["columns"], "bad_choice_default")
        if "board" in value:
            board = value["board"]
            ref(board, {"choice"})
            require(choice is not None and board == choice["field"], "bad_board_choice")
        coverage = set(value["form"])
        if choice is not None:
            coverage.add(choice["field"])
        require(coverage == fields.keys(), "missing_input")
    elif stage == "filters":
        keys(value, "search", "exact", "date")
        refs(value["search"], {"text"})
        refs(value["exact"], {"text", "choice", "integer"})
        if value["date"] is not None:
            ref(value["date"], {"date"})
    elif stage == "summary":
        keys(value, "count", "sum", "group")
        require(value["count"] is None or label(value["count"]), "bad_count_label")
        for name, types in (("sum", {"integer"}), ("group", {"text", "choice"})):
            item = value[name]
            if item is not None:
                keys(item, "field", "label")
                ref(item["field"], types)
                require(label(item["label"]), "bad_summary_label")
        require(value["group"] is None or value["sum"] is not None, "group_requires_sum")
    elif stage == "export":
        keys(value, "columns", "label")
        refs(value["columns"])
        require(label(value["label"]) if value["columns"] else value["label"] is None, "bad_export_label")
    else:
        raise ValueError("unexpected_stage")


def read_artifact(payload):
    require(isinstance(payload, bytes) and 0 < len(payload) <= MAX_ARTIFACT_BYTES, "bad_payload_size")
    try:
        return decode(payload.decode("utf-8"))
    except (RecursionError, UnicodeError, json.JSONDecodeError):
        raise ValueError("invalid_json") from None


def validate(stage, payload, previous):
    """Validate bytes against the stage schema and previously committed artifacts."""
    if stage == "assemble":
        require(payload == assemble(previous), "invalid_assembly")
        return
    value = read_artifact(payload)
    if stage == "spec":
        validate_spec(value)
    elif stage in RULES:
        spec = read_artifact(previous.get("spec.json"))
        validate_spec(spec)
        validate_module(stage, value, spec)
        if stage == "export" and not value["columns"]:
            require(permits_backup(previous), "bad_export_without_backup")
    else:
        raise ValueError("unexpected_stage")


def permits_backup(previous):
    """Read the opt-in from a validated entry artifact, after task provenance is verified."""
    payload = previous.get("entry.json")
    validate("entry", payload, previous)
    return read_artifact(payload).get("backup", False)


def assemble(previous):
    """Render the five validated plans as one deterministic standalone HTML document."""
    config = {}
    for stage in STAGES[:-1]:
        payload = previous.get(stage + ".json")
        validate(stage, payload, previous)
        config[stage] = read_artifact(payload)
    data = json.dumps(config, ensure_ascii=True, sort_keys=True, separators=(",", ":")).replace("<", "\\u003c")
    title = html.escape(config["spec"]["title"])
    shell = RUNTIME.read_text(encoding="utf-8")
    return ('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>''' + title + '''</title><style>
body{font:15px/1.55 system-ui,sans-serif;margin:14px;color:#202637;background:#f4f5f8}
main{display:grid;gap:12px}section{border:1px solid #d6dbe7;background:#fff;border-radius:8px;padding:12px;overflow:auto}
h1{font-size:22px}h2{font-size:17px;margin:0 0 6px}
form,.filter-bar{display:flex;flex-wrap:wrap;gap:10px;align-items:end}label{display:grid;gap:3px}
input,button,select,textarea{font:inherit;padding:4px;max-width:100%}
table{border-collapse:collapse;width:100%}th,td{padding:4px 8px;text-align:left;border-bottom:1px solid #ddd}
textarea{box-sizing:border-box;width:100%}[role=alert]{color:#ae2b2b}
#record-board{display:flex;gap:10px;overflow-x:auto;padding-bottom:6px}
#record-board>section{flex:1 0 12rem;background:#f4f5f8}
#record-board h3{margin:0 0 8px;font-size:15px}
#record-board article{background:white;border:1px solid #d6dbe7;border-radius:6px;margin-bottom:8px;padding:8px}
#record-board dl{margin:0 0 8px}#record-board dt{font-weight:600}
#record-board dd{margin:0 0 4px;overflow-wrap:anywhere}
.record-actions{display:flex;gap:4px;flex-wrap:wrap;margin:4px 0}
</style></head><body><h1 id="heading"></h1><main>
<section id="entry"><h2>录入记录</h2></section><section id="filters"><h2>筛选</h2></section>
<section id="summary"><h2>统计</h2></section><section id="records"><h2>明细</h2></section>
<section id="export"><h2>导出</h2></section></main>
<script>''' + "const config = " + data + ";\n" + shell + "</script></body></html>").encode()


def _decode_response(envelope, key, evidence):
    status = envelope.get("status")
    if type(status) is int:
        evidence["http_status"] = status
    if envelope.get("error") == "timeout":
        raise ApiError(504, "staged_timeout", "本阶段模型请求超时，可以继续生成。")
    if status == 402:
        raise ApiError(503, "staged_balance", "模型服务余额不足，请联系管理员。")
    if status == 429:
        raise ApiError(503, "staged_rate_limit", "模型请求过多，请稍后继续生成。")
    if status != 200:
        raise ApiError(502, "staged_provider", "本阶段模型服务暂时不可用，可以继续生成。")
    response = decode(envelope["body"])
    evidence.update(assistant_model._provider_info(response, key))
    choice = response["choices"][0]
    finish = choice["finish_reason"]
    if finish in ("stop", "length", "content_filter"):
        evidence["finish_reason"] = finish
    if finish != "stop":
        raise ValueError("incomplete_model_result")
    content = choice["message"]["content"]
    require(isinstance(content, str) and key not in content, "invalid_model_content")
    result = read_artifact(content.encode("utf-8"))
    if isinstance(result, dict) and set(result) == {"unsupported"} and result["unsupported"] is True:
        raise ApiError(422, "staged_unsupported", "该需求的核心功能暂不在分阶段工具支持范围内。")
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    require(key.encode("utf-8") not in payload, "invalid_model_content")
    return payload


def provide(stage, requirement, previous, key, admit, record=None):
    """Request one stage, admitting its usage immediately before dispatch."""
    if stage not in RULES:
        raise ValueError("unexpected_stage")
    evidence = {"stage": stage, "requested_model": MODEL, "dispatched": False}
    started = time.monotonic()

    def before_dispatch():
        admit()
        evidence["dispatched"] = True

    try:
        if not key:
            raise ApiError(503, "staged_unconfigured", "尚未配置生成服务。")
        if not isinstance(requirement, str) or not 0 < len(requirement.strip()) <= 2_000 or key in requirement:
            raise ApiError(400, "invalid_staged_requirement", "请输入有效需求并移除凭据。")
        request = {"requirement": requirement, "current_stage": stage}
        if stage != "spec":
            spec = read_artifact(previous.get("spec.json"))
            validate_spec(spec)
            request["accepted_spec"] = spec
        if stage == "summary":
            request["current_task"] = ("只设计显式要求的统计，始终包含 count、sum、group 三键。"
                                       "分栏展示和阶段移动不是统计。没有统计需求时三项全部为 null。")
            request["numeric_fields"] = [field["id"] for field in spec["fields"] if field["type"] == "integer"]
        if stage == "export":
            request["current_task"] = (
                "只设计 CSV，返回恰好两个键：columns 和 label。其他功能已由独立阶段实现。"
                "已启用 JSON 备份且用户未明确要求 CSV 时，用空 columns 和 null label。"
            )
            request["allowed_columns"] = [field["id"] for field in spec["fields"]]
            request["backup_available"] = "entry.json" in previous and permits_backup(previous)
        text = json.dumps(request, ensure_ascii=False)
        if key in text:
            raise ApiError(400, "invalid_staged_requirement", "请输入有效需求并移除凭据。")
        body = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM + "\n" + RULES[stage]},
                {"role": "user", "content": text},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "stream": False,
            "max_tokens": 1200 if stage == "spec" else 550,
        }
        payload = json.dumps({"key": key, "body": body, "wall_seconds": STAGE_SECONDS}, ensure_ascii=False).encode()
        if len(payload) > assistant_model.INPUT_BYTES:
            raise ApiError(400, "invalid_staged_requirement", "本阶段输入过长，请简化需求。")
        try:
            envelope = assistant_model._exchange(payload, before_dispatch, wall_seconds=STAGE_SECONDS)
        except ApiError as error:
            if error.code == "analysis_timeout":
                raise ApiError(504, "staged_timeout", "本阶段模型请求超时，可以继续生成。") from None
            if error.code == "analysis_provider":
                raise ApiError(502, "staged_provider", "本阶段模型服务暂时不可用，可以继续生成。") from None
            raise
        result = _decode_response(envelope, key, evidence)
        validate(stage, result, previous)
        evidence["status"] = "ok"
        return result
    except ApiError as error:
        evidence["status"] = error.code
        raise
    except (AttributeError, IndexError, KeyError, RecursionError, TypeError, ValueError) as error:
        evidence["status"] = "invalid_staged_result"
        code = str(error)
        if re.fullmatch(r"(?:bad|invalid|duplicate|incomplete)_[a-z_]{1,40}", code):
            evidence["validation"] = code
        raise ApiError(502, "invalid_staged_result", "本阶段返回的配置不完整，可以继续生成。") from None
    finally:
        if record is not None:
            evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
            record(evidence)
