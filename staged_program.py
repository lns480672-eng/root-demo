"""Validate a bounded tool fragment and place it inside a fixed document."""

from html import escape
from html.parser import HTMLParser
import re


PLAN = ("spec", "implementation", "assemble")
MAX_MARKUP_BYTES = 8 * 1024
MAX_SCRIPT_BYTES = 12 * 1024
MAX_ARTIFACT_BYTES = 24 * 1024
VOID_ELEMENTS = frozenset("br col hr input wbr".split())
ELEMENTS = VOID_ELEMENTS | frozenset("""
a abbr article aside b blockquote button canvas caption code colgroup dd details div dl dt em fieldset
footer form h1 h2 h3 h4 h5 h6 header i kbd label legend li main mark meter nav ol optgroup option output p pre
progress s samp section select small span strong sub summary sup table tbody td textarea tfoot th
thead time tr u ul var
""".split())
ATTRIBUTES = frozenset("""
accept aria-label autocomplete checked class cols colspan datetime disabled for height hidden high
href id inputmode label low max maxlength min minlength multiple name open optimum pattern placeholder
readonly required role rows rowspan selected size spellcheck step tabindex title type value width
""".split())
SAFE_ATTRIBUTE = re.compile(r"(?:aria|data)-[a-z][a-z0-9_-]{0,59}\Z")
SCRIPT_BOUNDARY = re.compile(r"</?script|<!--|-->", re.IGNORECASE)


class Fragment(HTMLParser):
    """Check that the model produced only inert markup under the generated tool root."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.open = []

    def handle_starttag(self, tag, attrs):
        if tag not in ELEMENTS:
            raise ValueError("bad_program_markup")
        seen = set()
        for name, value in attrs:
            if name in seen or (name not in ATTRIBUTES and not SAFE_ATTRIBUTE.fullmatch(name)):
                raise ValueError("bad_program_attribute")
            seen.add(name)
            if name == "href" and not (tag == "a" and value and value.startswith("#")):
                raise ValueError("bad_program_url")
            if tag == "input" and name == "type" and (value or "").lower() in {"file", "image"}:
                raise ValueError("bad_program_input")
        if tag not in VOID_ELEMENTS:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in VOID_ELEMENTS:
            raise ValueError("bad_program_markup")
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if not self.open or self.open[-1] != tag:
            raise ValueError("bad_program_markup")
        self.open.pop()

    def handle_data(self, data):
        # A fragment ending in an incomplete tag could consume the host's closing markup.
        if "<" in data:
            raise ValueError("bad_program_markup")

    def handle_decl(self, decl):
        raise ValueError("bad_program_markup")

    handle_pi = handle_decl
    unknown_decl = handle_decl
    handle_comment = handle_decl


def validate(value):
    if not isinstance(value, dict) or value.keys() != {"markup", "script"}:
        raise ValueError("bad_program_shape")
    markup, script = value["markup"], value["script"]
    for text, limit in ((markup, MAX_MARKUP_BYTES), (script, MAX_SCRIPT_BYTES)):
        if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text.encode()) > limit:
            raise ValueError("bad_program_size")
    if SCRIPT_BOUNDARY.search(script):
        raise ValueError("bad_program_script")
    parser = Fragment()
    parser.feed(markup)
    parser.close()
    if parser.open:
        raise ValueError("bad_program_markup")


def assemble(spec, implementation):
    validate(implementation)
    title = escape(spec["title"])
    markup, script = implementation["markup"], implementation["script"]
    alignment = ("#root-program{box-sizing:border-box;max-width:720px;margin-inline:auto}"
                 if spec.get("contentAlignment") == "center" else "")
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body{{font:15px/1.55 system-ui,sans-serif;margin:14px;color:#202637;background:#f4f5f8}}
main{{background:white;border:1px solid #d6dbe7;border-radius:8px;padding:12px;overflow:auto}}
h1{{font-size:22px}}label{{display:block;margin:4px 0}}
input,button,select,textarea{{font:inherit;padding:4px;max-width:100%;box-sizing:border-box}}
table{{border-collapse:collapse}}th,td{{padding:4px 8px;text-align:left}}
{alignment}</style></head><body><h1>{title}</h1><main id="root-program">{markup}
</main><script>
(() => {{
  'use strict';
{script}
}})();
</script></body></html>'''.encode()
