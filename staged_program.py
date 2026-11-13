"""Validate a bounded tool fragment and place it inside a fixed document."""

from html import escape
from html.parser import HTMLParser
import re


PLAN = ("spec", "implementation", "assemble")
NO_STORAGE_MARKER = "<!--root:storage=none-->"
MAX_MARKUP_BYTES = 8 * 1024
MAX_SCRIPT_BYTES = 12 * 1024
MAX_ARTIFACT_BYTES = 24 * 1024
VOID_ELEMENTS = frozenset("br col hr input wbr".split())
ELEMENTS = VOID_ELEMENTS | frozenset("""
a abbr article aside b blockquote button canvas caption code colgroup dd details div dl dt em fieldset
footer form h2 h3 h4 h5 h6 header i kbd label legend li mark meter ol optgroup option output p pre
progress s samp section select small span strong sub summary sup table tbody td textarea tfoot th
thead time tr u ul var
""".split())
ATTRIBUTES = frozenset("""
accept aria-label autocomplete checked class cols colspan datetime disabled for height hidden high
href id inputmode label low max maxlength min minlength multiple name open optimum pattern placeholder
readonly required role rows rowspan selected size spellcheck step tabindex title type value width
""".split())
ELEMENT_ATTRIBUTES = {
    ("th", "scope"): frozenset({"row", "col", "rowgroup", "colgroup"}),
    ("form", "novalidate"): frozenset({None, "", "novalidate"}),
}
SAFE_ATTRIBUTE = re.compile(r"(?:aria|data)-[a-z][a-z0-9_-]{0,59}\Z")
SCRIPT_BOUNDARY = re.compile(r"</?script|<!--|-->", re.IGNORECASE)


class Fragment(HTMLParser):
    """Check that the model produced only inert markup under the generated tool root."""

    def __init__(self, elements=ELEMENTS):
        super().__init__(convert_charrefs=False)
        self.elements = elements
        self.open = []
        self.ids = set()
        self.fragment_links = set()

    def handle_starttag(self, tag, attrs):
        if tag not in self.elements:
            raise ValueError("bad_program_markup")
        seen = set()
        for name, value in attrs:
            allowed_values = ELEMENT_ATTRIBUTES.get((tag, name), ())
            if (name in seen or (value not in allowed_values and name not in ATTRIBUTES
                                 and not SAFE_ATTRIBUTE.fullmatch(name))):
                raise ValueError("bad_program_attribute")
            seen.add(name)
            if name == "href" and not (tag == "a" and value and value.startswith("#")):
                raise ValueError("bad_program_url")
            if name == "href":
                self.fragment_links.add(value[1:])
            if name == "id" and value:
                self.ids.add(value)
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


def validate(value, *, elements=ELEMENTS):
    if not isinstance(value, dict) or value.keys() != {"markup", "script"}:
        raise ValueError("bad_program_shape")
    markup, script = value["markup"], value["script"]
    for text, limit in ((markup, MAX_MARKUP_BYTES), (script, MAX_SCRIPT_BYTES)):
        if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text.encode()) > limit:
            raise ValueError("bad_program_size")
    if SCRIPT_BOUNDARY.search(script):
        raise ValueError("bad_program_script")
    parser = Fragment(elements)
    parser.feed(markup)
    parser.close()
    if parser.open:
        raise ValueError("bad_program_markup")
    return parser


def assemble(spec, implementation):
    validate(implementation)
    title = escape(spec["title"])
    markup, script = implementation["markup"], implementation["script"]
    alignment = ("#root-program{box-sizing:border-box;max-width:720px;margin-inline:auto}"
                 if spec.get("contentAlignment") == "center" else "")
    storage = NO_STORAGE_MARKER if spec.get("storage") == "none" else ""
    return f'''<!doctype html>{storage}<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
*{{box-sizing:border-box}}
body{{font:15px/1.55 system-ui,sans-serif;margin:clamp(12px,3vw,28px);color:#202637;background:#f4f5f8}}
h1{{font-size:clamp(22px,4vw,30px);line-height:1.2;overflow-wrap:anywhere;margin:0 0 16px}}
main{{background:#fff;border:1px solid #d6dbe7;border-radius:12px;padding:clamp(12px,3vw,24px);overflow:auto}}
label{{display:block;margin:6px 0}}
input,button,select,textarea{{font:inherit;max-width:100%;padding:6px 10px;border:1px solid #a6afc0;border-radius:6px;color:inherit}}
input,select,textarea{{background:#fff}}
button{{background:#f5f7fa;cursor:pointer;white-space:normal}}
button[type=submit],form button:not([type]){{background:#24499b;color:white;border-color:#24499b}}
:focus-visible{{outline:2px solid #24499b;outline-offset:2px}}
output{{font-weight:650;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}}
table{{border-collapse:collapse}}th,td{{padding:6px 9px;text-align:left}}
{alignment}</style></head><body><h1>{title}</h1><main id="root-program">{markup}
</main><script>
(() => {{
  'use strict';
{script}
}})();
</script></body></html>'''.encode()
