"""Source-only model context and compatibility checks for staged tool revisions."""

from copy import deepcopy
import html
from html.parser import HTMLParser
import json
import re

from accounts import ApiError
import staged_blueprint as blueprint
import staged_program
import table_import


MAX_LEGACY_HTML = 24 * 1024
MAX_BASE_BYTES = 14 * 1024
THEME_NAMES = {
    "gray": ("灰白", "灰色", "浅灰", "淡灰"),
    "yellow": ("黄色", "浅黄", "淡黄"),
    "blue": ("蓝色", "浅蓝", "淡蓝"),
    "green": ("绿色", "浅绿", "淡绿"),
    "pink": ("粉色", "粉红", "浅粉", "淡粉"),
    "purple": ("紫色", "浅紫", "淡紫"),
}
CLAUSE_SEPARATOR = re.compile(r"[，,。；;！？!?\n]+")
LEDGER_RENAME = re.compile(
    r"^(?:请\s*)?(?:只|仅)?(?:把|将|修改|更改|改)?(?:当前|这个|该)?"
    r"(?:(?:台账|看板|工具)的?)?(?:标题|名称|名字)\s*(?:改成|改为|换成|换为|设为|设置为|为|改|换)\s*"
    r"|^(?:请\s*)?(?:只|仅)?(?:改名|重命名)(?:为|成)\s*"
)
LEDGER_REMOVE_CSV = re.compile(
    r"(?:请\s*)?(?:只|仅)?(?:删除|移除|去掉|取消|关闭|禁用)\s*(?:原有(?:的)?|现有(?:的)?)?"
    r"\s*(?:CSV\s*导出|导出\s*CSV)(?:的?(?:功能|按钮))?(?:即可|就好)?", re.I
)
LEDGER_ADD_FILTER = re.compile(
    r"^(?:请\s*)?(?:(?:给|为|在)(?:该|这个|当前)?(?:工具|台账|看板)(?:中|里)?)?"
    r"(?:新增|增加|添加|加上|加一个|加|补充)\s*.+?(?:筛选|过滤|搜索)"
)
LEDGER_THEME = re.compile(
    r"^(?:请\s*)?(?:只|仅)?(?:把|将)?(?:整体|页面|看板|台账)?的?"
    r"(?:背景|背景颜色|主题|配色|颜色)\s*(?:改|调|换|设|为|变)"
)
LEDGER_KEEP = re.compile(
    r"^(?:(?:其他|其它|其余|剩余).*(?:不变|保持|保留)|(?:保留|保持).*)$"
)
LEDGER_NEGATIVE = re.compile(r"不要|不许|不能|不加|不增加|别|禁止|取消|移除|删除")
VARIABLES = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"""(["'])([^"'\\\r\n]{1,160})\2\s*(?:;|$)""",
    re.M,
)
STORAGE_ARGUMENT = re.compile(r"\blocalStorage\s*\.\s*(?:getItem|setItem|removeItem)\s*\(\s*"
                              r"((?:\"[^\"\\\r\n]{1,160}\"|'[^'\\\r\n]{1,160}'|[A-Za-z_$][\w$]*))\s*[,)]")


def storage_keys(script):
    """Recover static key hints from code, never from a user's storage snapshot."""
    variables = {name: value for name, _, value in VARIABLES.findall(script)}
    return sorted({argument[1:-1] if argument[0] in {"'", '"'} else variables[argument]
                   for argument in STORAGE_ARGUMENT.findall(script)
                   if argument[0] in {"'", '"'} or argument in variables})


class LegacySource(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.body = False
        self.seen_body = False
        self.collect = None
        self.markup = []
        self.title = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            if attrs and (len(attrs) != 1 or attrs[0] not in {
                ("type", "text/javascript"), ("type", "application/javascript"),
            }):
                raise ValueError("legacy_external_script")
            self.collect = tag
        elif tag in {"title", "style"}:
            self.collect = tag
        elif tag == "body":
            self.body = self.seen_body = True
        elif self.body and self.collect is None:
            self.markup.append(self.tag(tag, attrs))

    def handle_startendtag(self, tag, attrs):
        if self.body and self.collect is None:
            self.markup.append(self.tag(tag, attrs, closing="/"))

    def handle_endtag(self, tag):
        if tag == self.collect:
            self.collect = None
            if tag == "script":
                self.scripts.append("\n;\n")
        elif tag == "body":
            self.body = False
        elif self.body and self.collect is None:
            self.markup.append("</" + ("h2" if tag == "h1" else tag) + ">")

    def handle_data(self, data):
        if self.collect == "script":
            self.scripts.append(data)
        elif self.collect == "title":
            self.title.append(data)
        elif self.body and self.collect is None:
            self.markup.append(html.escape(data, quote=False))

    def handle_entityref(self, name):
        self.handle_data(html.unescape("&" + name + ";"))

    def handle_charref(self, name):
        self.handle_data(html.unescape("&#" + name + ";"))

    @staticmethod
    def tag(tag, attrs, closing=""):
        rendered = []
        for name, value in attrs:
            if name == "style":
                continue
            rendered.append(name if value is None else name + '="' + html.escape(value) + '"')
        name = "h2" if tag == "h1" else tag
        return "<" + " ".join([name, *rendered]) + closing + ">"


def _legacy_artifacts(source):
    if not isinstance(source, bytes) or not 0 < len(source) <= MAX_LEGACY_HTML:
        raise ValueError("bad_legacy_source_size")
    parser = LegacySource()
    parser.feed(source.decode("utf-8"))
    parser.close()
    if not parser.seen_body or parser.body or parser.collect is not None:
        raise ValueError("bad_legacy_source")
    title = " ".join("".join(parser.title).split())[:48] or "已有工具"
    spec = {"kind": "program", "title": title, "goal": "保持已有工具的操作与数据格式，仅实现请求的修改。"}
    implementation = {"markup": "".join(parser.markup).strip(), "script": "".join(parser.scripts).strip() or ";"}
    staged_program.validate(implementation)
    return {"spec":spec, "implementation":implementation}


def prepare(prior_artifacts, legacy_html=None):
    """Prepare JSON-safe context using the fixed, authorized source of one existing version."""
    try:
        if bool(prior_artifacts) == (legacy_html is not None):
            raise ValueError("bad_revision_source")
        if legacy_html is not None:
            source = _legacy_artifacts(legacy_html)
            kind = "program"
        else:
            plan = blueprint.stage_plan(prior_artifacts.get("spec.json"))
            names = {stage + ".json" for stage in plan[:-1]}
            if not names <= prior_artifacts.keys() <= names | {"app.html"}:
                raise ValueError("bad_revision_artifacts")
            source = {}
            for stage in plan[:-1]:
                payload = prior_artifacts[stage + ".json"]
                blueprint.validate(stage, payload, prior_artifacts)
                limit = staged_program.MAX_ARTIFACT_BYTES if stage == "implementation" else blueprint.MAX_ARTIFACT_BYTES
                source[stage] = blueprint.read_artifact(payload, limit=limit)
            kind = source["spec"].get("kind", "ledger")
        keys = storage_keys(source["implementation"]["script"]) if kind == "program" else []
        base = {"kind":kind, "source":source, "storageKeys":keys}
        if len(json.dumps(base, ensure_ascii=False).encode()) > MAX_BASE_BYTES:
            raise ValueError("bad_revision_context_size")
        return base
    except (AttributeError, KeyError, RecursionError, TypeError, UnicodeError, ValueError):
        raise ApiError(409, "revision_source_unsupported", "旧工具的来源或结构无法安全修改，旧版仍可用。") from None


def table_columns(entries):
    """Extract the stored schema without adding record data to the revision context."""
    try:
        columns = table_import.read_saved_table(entries)["columns"]
        for column in columns:
            blueprint.keys(column, "name", "type")
        blueprint.require(len({column["name"] for column in columns}) == len(columns), "bad_table_columns")
        return columns
    except (KeyError, RecursionError, TypeError, ValueError):
        raise ApiError(409, "revision_source_unsupported", "旧表格的字段无法安全修改，原工具仍可用。") from None


def prepare_table(entries, *, prior_artifacts=None, html=None):
    columns = table_columns(entries)
    if prior_artifacts is None:
        configuration = table_import.tool_configuration(html.decode("utf-8"))
        spec = {"kind": "table", **configuration}
    else:
        blueprint.require(set(prior_artifacts) == {"spec.json", "app.html"}, "bad_table_artifacts")
        spec = blueprint.read_artifact(prior_artifacts["spec.json"])
        blueprint.require(spec.get("kind") == "table", "bad_table_source_kind")
    blueprint.validate_spec(spec)
    blueprint.require(all(index < len(columns) for index in spec["filters"]), "bad_table_column")
    return {"kind": "table", "source": {"spec": spec}, "columns": columns}


def _table_patch(payload, base):
    patch = blueprint.read_artifact(payload)
    current = base["source"]["spec"]
    if isinstance(patch, dict) and patch.keys() == {"addFilter"}:
        candidate = {**current, "filters": [*current["filters"], patch["addFilter"]]}
    else:
        blueprint.keys(patch, "title")
        candidate = {**current, "title": patch["title"]}
    return json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _check_table(candidate, base, requirement):
    current = base["source"]["spec"]
    if candidate.get("kind") != "table":
        raise _incompatible()
    title, filters = candidate["title"], candidate["filters"]
    clauses = [clause for clause in re.split(r"[，,。；;！？!?\n]+", requirement)
               if not re.search(r"不要|无需|不用|不必|不需要|不想|禁止|^别", clause)]
    rename = any(re.search(r"(?:标题|名称|名字)\s*(?:改|换|变|为|叫|设|更)|更名|重命名", clause)
                 for clause in clauses)
    if title != current["title"] and filters == current["filters"] and rename and title in requirement:
        return
    if title == current["title"] and filters[:-1] == current["filters"] and filters:
        index = filters[-1]
        if index >= len(base["columns"]):
            raise ValueError("bad_table_column")
        name = base["columns"][index]["name"]
        add_filter = any(name in clause and re.search(r"(?:加|添|补|按|支持|可以).*(?:筛选|过滤)", clause)
                         and not re.search(r"(?:取消|去掉|移除|删除).*(?:筛选|过滤)", clause)
                         for clause in clauses)
        if not rename and add_filter:
            return
    raise ApiError(422, "staged_unsupported", "表格工具仅支持新增指定列的精确筛选或只修改标题，原工具仍可用。")


def _incompatible():
    return ApiError(409, "incompatible_revision", "新版无法兼容原工具的数据结构，旧版仍可用。")


def _no_change():
    return ApiError(422, "revision_no_change", "这次修改没有产生可应用的变化。旧版仍可用，未扣除个人生成次数。")


def _theme(spec):
    return spec.get("theme", "gray")


def _active_modules(base):
    return [name for name in blueprint.OPTIONAL_MODULES if any(base["source"].get(name, {}).values())]


def _ledger_operation(requirement):
    requirement = requirement.strip()
    if rename := LEDGER_RENAME.match(requirement):
        title = requirement[rename.end():].strip()
        quoted = re.match(r'^(?:「([^」]+)」|『([^』]+)』|“([^”]+)”|"([^"]+)")', title)
        if quoted:
            trailing = CLAUSE_SEPARATOR.split(title[quoted.end():])
            if all(LEDGER_KEEP.fullmatch(part.strip()) for part in trailing if part.strip()):
                return "title", next(part for part in quoted.groups() if part is not None)
            return None, ""
    clauses = [part.strip() for part in CLAUSE_SEPARATOR.split(requirement) if part.strip()]
    if not clauses:
        return None, ""
    first, *rest = clauses
    if not all(LEDGER_KEEP.fullmatch(part) for part in rest):
        return None, ""
    if rename := LEDGER_RENAME.match(first):
        return "title", first[rename.end():].strip()
    if LEDGER_REMOVE_CSV.fullmatch(first) and not any(re.search(r"CSV|导出", part, re.I) for part in rest):
        return "remove_csv", first
    if LEDGER_ADD_FILTER.match(first) and not LEDGER_NEGATIVE.search(first):
        return "add_filter", first
    if LEDGER_THEME.match(first):
        return "theme", first
    return None, ""


def _requested_theme(theme, requirement):
    return any(name in requirement for name in THEME_NAMES[theme]) or re.search(
        r"(?<![a-z])" + theme + r"(?![a-z])", requirement, re.I
    )


def _out_of_scope():
    return ApiError(422, "staged_unsupported", "本次修改超出了可验证的范围，原工具和记录仍可用。")


def _ledger_patch(payload, base, active_modules, requirement):
    candidate = blueprint.read_artifact(payload)
    keys = candidate.keys() if isinstance(candidate, dict) else ()
    original = base["source"]["spec"]
    operation, _ = _ledger_operation(requirement)
    if keys == {"theme"}:
        theme = candidate["theme"]
        if not isinstance(theme, str) or theme not in blueprint.LEDGER_THEMES:
            raise ApiError(422, "staged_unsupported", "工作台暂不支持这个配色，旧版仍可用。")
        if operation != "theme" or not _requested_theme(theme, requirement):
            raise ApiError(422, "staged_unsupported", "工作台暂不支持请求的配色，旧版仍可用。")
        if theme == _theme(original):
            raise _no_change()
        candidate = {**original, "modules": active_modules, "theme": theme}
    elif operation == "title" and keys == {"title"}:
        candidate = {**original, "modules": active_modules, **candidate}
    elif operation == "remove_csv" and keys == {"removeCsv"} and candidate["removeCsv"] is True:
        candidate = {**original, "modules": [name for name in active_modules if name != "export"]}
    elif operation == "add_filter" and keys == {"addFilter"} and candidate["addFilter"] is True:
        modules = [name for name in blueprint.OPTIONAL_MODULES if name in active_modules or name == "filters"]
        candidate = {**original, "modules": modules}
    elif isinstance(candidate, dict):
        if "theme" in original:
            candidate["theme"] = original["theme"]
        else:
            candidate.pop("theme", None)
    return json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _effective_ledger(source):
    config = blueprint.ledger_config(source)
    spec, entry = config["spec"], config["entry"]
    config["spec"] = {"title": spec["title"], "fields": spec["fields"], "theme": _theme(spec)}
    if "columnThemes" in spec:
        config["spec"]["columnThemes"] = spec["columnThemes"]
    config["entry"] = {**entry, **{flag: entry.get(flag, False) for flag in ("allow_edit", "allow_delete", "backup")}}
    return config


def _check_ledger_spec(candidate, base, requirement):
    old = base["source"]["spec"]
    operation, target = _ledger_operation(requirement)
    if candidate["fields"] != old["fields"]:
        raise _out_of_scope()
    if operation == "title":
        if candidate["title"] != target or candidate["title"] == old["title"]:
            raise _out_of_scope()
    elif candidate["title"] != old["title"]:
        raise _out_of_scope()
    if operation == "theme":
        if not _requested_theme(_theme(candidate), target):
            raise _out_of_scope()
    elif _theme(candidate) != _theme(old):
        raise _out_of_scope()
    if candidate.get("columnThemes") != old.get("columnThemes"):
        raise _out_of_scope()

    modules = set(_active_modules(base))
    if operation == "remove_csv":
        modules.discard("export")
    elif operation == "add_filter":
        modules.add("filters")
    planned = set(candidate.get("modules", blueprint.OPTIONAL_MODULES))
    if not modules <= planned or ("modules" in candidate and planned != modules):
        raise _out_of_scope()


def _check_added_filter(original, candidate, fields, target):
    if candidate["search"][:len(original["search"])] != original["search"]:
        raise _out_of_scope()
    if candidate["exact"][:len(original["exact"])] != original["exact"]:
        raise _out_of_scope()
    search = candidate["search"][len(original["search"]):]
    exact = candidate["exact"][len(original["exact"]):]
    added = search + exact
    if candidate["date"] != original["date"]:
        if original["date"] is not None:
            raise _out_of_scope()
        added.append(candidate["date"])
    named = {field["id"] for field in fields if field["label"] in target or re.search(
        r"(?<![a-zA-Z0-9_])" + re.escape(field["id"]) + r"(?![a-zA-Z0-9_])", target, re.I
    )}
    if (not added or len(added) != len(set(added)) or set(added) != named
            or ("精确" in target and added != exact) or ("关键词" in target and added != search)):
        raise _out_of_scope()


def _check_ledger_assembly(source, base, requirement):
    current = _effective_ledger(base["source"])
    candidate = _effective_ledger(source)
    if candidate == current:
        raise _no_change()
    _check_ledger_spec(source["spec"], base, requirement)
    operation, target = _ledger_operation(requirement)
    if operation == "title":
        current["spec"]["title"] = candidate["spec"]["title"]
    elif operation == "theme":
        current["spec"]["theme"] = candidate["spec"]["theme"]
    elif operation == "remove_csv":
        current["export"] = blueprint.ledger_config({})["export"]
    elif operation == "add_filter":
        _check_added_filter(current["filters"], candidate["filters"], current["spec"]["fields"], target)
        current["filters"] = candidate["filters"]
    if candidate != current:
        raise _out_of_scope()



# Resolve these narrow edits against the source, then apply only the verified difference.
FIELD_LABEL = re.compile(r"^(?:请)?(?:把|将)?(?:字段)?[「“]?([^「」“”，,]+?)[」”]?\s*(?:字段|显示名)?\s*改(?:为|成)[「“]?([^「」“”，,]+)[」”]?$")
ADD_FIELD = re.compile(r"^(?:请)?(?:新增|添加|增加|加)(?:一个|个)?(?:可空(?:的)?)?(?:文本)?字段[\s，,：:]*[「“]?([^「」“”，,]+)[」”]?$")
COLUMN_STYLE = re.compile(r"^(?:请)?(?:把|将)?[「“]?([^「」“”，,]+?)[」”]?分栏(?:背景|颜色)?改(?:为|成)([^，,。]+)$")
WEBSITE_HEADING = re.compile(r"^(?:请)?(?:把|将)?首页主标题改(?:为|成)[「“]([^」”]+)[」”]$")
PROGRAM_TEXT = re.compile(r"^(?:请)?(?:把|将)(?:页面)?文案[「“]([^」”]+)[」”]改(?:为|成)[「“]([^」”]+)[」”]$")
DATA_TARGET = r"(?:字段|数据|记录|事项|表格|列)"
DESTRUCTIVE_ACTION = r"(?:删除|删掉|删|清除|清空|移除|去掉|去除|覆盖|重置|抹掉|合并|拆分|回填)"
RISKY_REVISION = re.compile(
    rf"{DESTRUCTIVE_ACTION}.*{DATA_TARGET}|{DATA_TARGET}.*{DESTRUCTIVE_ACTION}|"
    r"批量(?:修改|回填|更新)|回填|必填|改(?:成|为)(?:数字|日期|数值|整数|布尔|单选|多选|下拉)|"
    r"(?:改|修改|更改|转换)(?:字段)?类型|数据库|"
    r"\b(?:ALTER|DROP|TRUNCATE)\s+TABLE\b|"
    r"\b(?:delete|remove|clear|reset|overwrite)\b.*\b(?:records?|data|columns?|fields?)\b",
    re.I,
)


def local_revision(base, requirement):
    text = requirement.strip().rstrip("。")
    kind = base["kind"]
    if kind == "program" and (match := PROGRAM_TEXT.fullmatch(text)):
        before, after = match.groups()
        source = deepcopy(base["source"])
        original = ">" + html.escape(before, quote=False) + "<"
        markup = source["implementation"]["markup"]
        if not blueprint.label(after) or markup.count(original) != 1 or before == after:
            raise _out_of_scope()
        source["implementation"]["markup"] = markup.replace(original, ">" + html.escape(after, quote=False) + "<", 1)
        return source, f"页面文案「{before}」改为「{after}」。脚本与存储保持。"
    if kind == "website" and (match := WEBSITE_HEADING.fullmatch(text)):
        source = deepcopy(base["source"])
        heading = source["implementation"]["intro"]
        before, after = heading["heading"], match[1]
        if not blueprint.label(after) or before == after:
            raise _out_of_scope()
        heading["heading"] = after
        return source, f"首页主标题「{before}」改为「{after}」。导航、目录和问答保持。"
    if kind != "ledger":
        return None
    if match := ADD_FIELD.fullmatch(text):
        label = match[1].strip()
        if not blueprint.label(label) or any(field["label"] == label for field in base["source"]["spec"]["fields"]):
            raise _out_of_scope()
        source = deepcopy(base["source"])
        fields = source["spec"]["fields"]
        if len(fields) >= 12:
            raise ApiError(422, "staged_unsupported", "台账已达到字段数量上限，旧版仍可用。")
        used = {field["id"] for field in fields}
        identifier = next(f"field_{i}" for i in range(1, 14) if f"field_{i}" not in used)
        fields.append({"id":identifier, "label":label, "type":"text", "required":False, "options":[]})
        source["entry"]["form"].append(identifier)
        source["entry"]["columns"].append(identifier)
        change = f"新增可空文本字段「{label}」。已有记录在此字段显示为空。"
    elif match := COLUMN_STYLE.fullmatch(text):
        name, color = match.groups()
        board = base["source"]["entry"].get("board")
        field = next((field for field in base["source"]["spec"]["fields"] if field["id"] == board), None)
        theme = next((theme for theme, names in THEME_NAMES.items() if color in names or color == theme), None)
        if field is None or name not in field["options"] or theme is None:
            raise _out_of_scope()
        source = deepcopy(base["source"])
        index = str(field["options"].index(name))
        colors = source["spec"].setdefault("columnThemes", {})
        if colors.get(index, _theme(source["spec"])) == theme:
            raise _no_change()
        colors[index] = theme
        change = f"「{name}」分栏背景改为{color}。其他分栏和记录保持。"
    elif match := FIELD_LABEL.fullmatch(text):
        before, after = (value.strip() for value in match.groups())
        field = next((field for field in base["source"]["spec"]["fields"] if field["label"] == before), None)
        if field is None:
            return None
        if not blueprint.label(after) or after in field["options"] or any(item["label"] == after for item in base["source"]["spec"]["fields"]):
            raise _out_of_scope()
        source = deepcopy(base["source"])
        for item in source["spec"]["fields"]:
            if item["id"] == field["id"]:
                item["label"] = after
        change = f"字段「{before}」显示名改为「{after}」。字段键和已有值保持。"
        if field["id"] in source.get("export", {}).get("columns", []):
            change += "CSV 导出表头同步更新。"
    else:
        return None
    source["spec"]["modules"] = _active_modules(base)
    return source, change


def describe(base, files, requirement, entries):
    local = local_revision(base, requirement)
    if local:
        changes = [local[1]]
    else:
        spec = blueprint.read_artifact(files["spec.json"])
        old = base["source"]["spec"]
        changes = []
        if spec["title"] != old["title"]:
            changes.append(f"工具标题「{old['title']}」改为「{spec['title']}」。")
        if _theme(spec) != _theme(old):
            changes.append(f"整体背景改为{THEME_NAMES[_theme(spec)][0]}。")
        operation, _ = _ledger_operation(requirement)
        if operation == "remove_csv":
            changes.append("移除 CSV 导出。")
        if base["kind"] == "ledger" and operation == "add_filter":
            old_filters = base["source"].get("filters", {})
            new_filters = blueprint.read_artifact(files["filters.json"])
            names = {field["id"]: field["label"] for field in old["fields"]}
            for group, label in (("search", "关键词"), ("exact", "精确")):
                changes.extend(f"添加「{names[field]}」{label}筛选。"
                               for field in new_filters[group] if field not in old_filters.get(group, []))
            if new_filters["date"] != old_filters.get("date"):
                changes.append(f"添加「{names[new_filters['date']]}」日期筛选。")
        if base["kind"] == "table" and spec["filters"] != old["filters"]:
            column = base["columns"][spec["filters"][-1]]["name"]
            changes.append(f"添加「{column}」精确筛选。")
        if not changes:
            raise _out_of_scope()
    changes.append("保留已有记录和未要求修改的内容。")
    if local and base["kind"] == "ledger":
        try:
            records = json.loads(dict(entries).get("root-structured-records-v1", "[]"))
        except (TypeError, ValueError, RecursionError):
            raise _incompatible() from None
        if not isinstance(records, list):
            raise _incompatible()
        if not all(isinstance(record, dict) for record in records):
            raise _incompatible()
        previous_ids = {field["id"] for field in base["source"]["spec"]["fields"]}
        new_ids = {field["id"] for field in local[0]["spec"]["fields"]} - previous_ids
        if any(new_ids.intersection(record) for record in records):
            raise _incompatible()
        changes.append(f"已有记录 {len(records)} 条。")
    return changes


def check(stage, payload, base, requirement="", *, previous=None):
    """Check semantic data compatibility after normal stage validation."""
    local = local_revision(base, requirement)
    if local:
        source, _ = local
        if stage == "assemble":
            for name in blueprint.stage_plan(previous["spec.json"])[:-1]:
                check(name, previous[name + ".json"], base, requirement)
        elif stage in source:
            limit = staged_program.MAX_ARTIFACT_BYTES if stage == "implementation" else blueprint.MAX_ARTIFACT_BYTES
            if blueprint.read_artifact(payload, limit=limit) != source[stage]:
                raise _out_of_scope()
        return
    if stage == "spec":
        candidate = blueprint.read_artifact(payload)
        if base["kind"] == "table":
            _check_table(candidate, base, requirement)
            return
        if candidate.get("kind", "ledger") != base["kind"]:
            raise _incompatible()
        if base["kind"] == "website":
            if candidate["layout"] != base["source"]["spec"]["layout"]:
                raise _incompatible()
            return
        if base["kind"] == "program":
            return
        old = {field["id"]:field for field in base["source"]["spec"]["fields"]}
        new = {field["id"]:field for field in candidate["fields"]}
        for name, field in new.items():
            previous = old.get(name)
            if previous is None:
                if field["required"]:
                    raise _incompatible()
                continue
            if field["type"] != previous["type"] or (field["required"] and not previous["required"]):
                raise _incompatible()
            if [option for option in field["options"] if option in previous["options"]] != previous["options"]:
                raise _incompatible()
        if not old.keys() <= new.keys():
            raise _incompatible()
        if requirement:
            _check_ledger_spec(candidate, base, requirement)
    elif stage == "implementation":
        result = blueprint.read_artifact(payload, limit=staged_program.MAX_ARTIFACT_BYTES)
        if base["kind"] == "website":
            return
        if not set(base["storageKeys"]) <= set(storage_keys(result["script"])):
            raise _incompatible()
    elif stage == "assemble" and base["kind"] == "ledger":
        source = {name[:-5]: blueprint.read_artifact(data) for name, data in previous.items() if name != "app.html"}
        _check_ledger_assembly(source, base, requirement)
    elif stage == "assemble" and base["kind"] == "website":
        source = {name[:-5]: blueprint.read_artifact(data, limit=staged_program.MAX_ARTIFACT_BYTES)
                  for name, data in previous.items() if name != "app.html"}
        if (source["spec"]["title"] == base["source"]["spec"]["title"]
                and source["implementation"] == base["source"]["implementation"]):
            raise _no_change()


def planned_local_revision(base, requirement):
    if RISKY_REVISION.search(requirement):
        raise ApiError(422, "revision_risky", "这项操作可能删除、转换或改写已有数据，当前版本不执行。")
    local = local_revision(base, requirement)
    if local or base["kind"] == "table":
        return local
    if base["kind"] != "ledger" or not _ledger_operation(requirement)[0]:
        raise _out_of_scope()
    return None


def provide(stage, requirement, previous, key, admit, record, *, base):
    """Generate one revision stage from source-only context and check before success."""
    if re.fullmatch(r"(?:我想|请|帮我)?(?:改一下|换一下|调整)(?:颜色|配色)[。！？]?", requirement.strip()):
        raise ApiError(422, "revision_clarification_required", "要改整体背景还是某个分栏？请写明位置和颜色。")
    local = planned_local_revision(base, requirement)
    if local:
        return json.dumps(local[0][stage], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if base["kind"] == "table":
        spec = base["source"]["spec"]
        context = {**spec, "columns": [
            {"index": index, **column} for index, column in enumerate(base["columns"])
            if column["name"] in requirement
        ]}
        return blueprint.provide(stage, requirement, previous, key, admit, record, revision_context=context,
                                 spec_patch=lambda payload: _table_patch(payload, base),
                                 accept=lambda payload: check(stage, payload, base, requirement))
    context = {"kind":base["kind"], "storageKeys":base["storageKeys"]}
    spec_patch = None
    if stage == "spec" and base["kind"] == "ledger":
        active_modules = context["active_modules"] = _active_modules(base)
        spec_patch = lambda payload: _ledger_patch(payload, base, active_modules, requirement)
    elif base["kind"] == "ledger":
        operation, _ = _ledger_operation(requirement)
        if operation in ("theme", "title", "remove_csv") or (operation == "add_filter" and stage != "filters"):
            return json.dumps(base["source"][stage], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if stage in base["source"]:
        context["prior_stage"] = base["source"][stage]
    category = base["source"]["spec"]["layout"] if base["kind"] == "website" else "auto"
    return blueprint.provide(stage, requirement, previous, key, admit, record,
                             revision_context=context, spec_patch=spec_patch,
                             accept=lambda payload: check(stage, payload, base, requirement), category=category)
