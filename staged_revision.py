"""Source-only model context and compatibility checks for staged tool revisions."""

import html
from html.parser import HTMLParser
import json
import re

from accounts import ApiError
import staged_blueprint as blueprint
import staged_program


MAX_LEGACY_HTML = 24 * 1024
MAX_BASE_BYTES = 14 * 1024
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
            kind = "program" if plan == staged_program.PLAN else "ledger"
        keys = storage_keys(source["implementation"]["script"]) if kind == "program" else []
        base = {"kind":kind, "source":source, "storageKeys":keys}
        if len(json.dumps(base, ensure_ascii=False).encode()) > MAX_BASE_BYTES:
            raise ValueError("bad_revision_context_size")
        return base
    except (AttributeError, KeyError, RecursionError, TypeError, UnicodeError, ValueError):
        raise ApiError(409, "revision_source_unsupported", "旧工具的来源或结构无法安全修改，旧版仍可用。") from None


def _incompatible():
    return ApiError(409, "incompatible_revision", "新版无法兼容原工具的数据结构，旧版仍可用。")


def check(stage, payload, base):
    """Check semantic data compatibility after normal stage validation."""
    if stage == "spec":
        candidate = blueprint.read_artifact(payload)
        if (candidate.get("kind") == "program") != (base["kind"] == "program"):
            raise _incompatible()
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
    elif stage == "implementation":
        result = blueprint.read_artifact(payload, limit=staged_program.MAX_ARTIFACT_BYTES)
        if not set(base["storageKeys"]) <= set(storage_keys(result["script"])):
            raise _incompatible()


def provide(stage, requirement, previous, key, admit, record, *, base):
    """Generate one revision stage from source-only context and check before success."""
    context = {"kind":base["kind"], "storageKeys":base["storageKeys"]}
    if stage == "spec" and base["kind"] == "ledger":
        context["active_modules"] = [
            module for module in blueprint.OPTIONAL_MODULES
            if any(base["source"].get(module, {}).values())
        ]
    if stage in base["source"]:
        context["prior_stage"] = base["source"][stage]
    return blueprint.provide(stage, requirement, previous, key, admit, record,
                             revision_context=context, accept=lambda payload: check(stage, payload, base))
