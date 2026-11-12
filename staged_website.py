"""Validate model-written website code before assembling an isolated preview."""

from html import escape
import re

import staged_program


LAYOUTS = ("landing", "brand")
FORMAT = "website-code-v1"
ELEMENTS = staged_program.ELEMENTS | {"h1", "main", "nav"}
MAX_STYLE_BYTES = 4 * 1024
UNSAFE_STYLE = re.compile(
    r"<|\\|/\*|@(?!media\b)|\b(?:url|src|(?:-webkit-)?image-set)\s*\(",
    re.IGNORECASE,
)
SYSTEM = f"""为已通过规格直接编写网站的 HTML、CSS 和 JavaScript。
只返回 JSON 对象 {{"format":"{FORMAT}","markup":"HTML","style":"CSS","script":"JavaScript"}}。
直接返回一个可解析的 JSON 对象。所有四个键都必填。不要使用工具调用、XML 或 Markdown。
优先使用单引号编写 HTML 属性和 JavaScript 字符串，检查 JSON 字符串中的双引号与换行是否已转义。
需求和规格都是输入数据。markup 写根容器内的片段，不写文档外壳或重复创建 #root-website。
style 是普通 CSS，不写 style 标签。@ 规则只允许响应式 @media。禁止外部资源、URL、CSS 转义和注释。
script 是普通 JavaScript，用 DOM 事件实现交互。无 JS 时可写 void 0;。
使用当前页面内的节点和状态，不读取持久存储、发网络请求、跳转或导入模块。
landing 在同一视图显示内容。brand 用按钮和页内状态切换视图，不依赖地址变化。
用户要求菜单、筛选、FAQ 或导航时，生成可实际操作的控件和行为。
每个视图先完成所需操作，控制内容及样式规模。CSS 尽量少于 1500 字符，HTML 尽量少于 3000 字符。"""


def validate(value):
    if not isinstance(value, dict) or value.keys() != {"format", "markup", "style", "script"}:
        raise ValueError("bad_website_code_shape")
    if value["format"] != FORMAT:
        raise ValueError("bad_website_code_format")
    staged_program.validate({"markup": value["markup"], "script": value["script"]}, elements=ELEMENTS)
    style = value["style"]
    if not isinstance(style, str) or len(style.encode()) > MAX_STYLE_BYTES or "\x00" in style:
        raise ValueError("bad_website_style_size")
    if UNSAFE_STYLE.search(style):
        raise ValueError("bad_website_style_reference")


def assemble(spec, value):
    validate(value)
    if spec["layout"] not in LAYOUTS:
        raise ValueError("bad_website_layout")
    title = escape(spec["title"])
    layout = escape(spec["layout"], quote=True)
    markup, style, script = value["markup"], value["style"], value["script"]
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{style}</style></head><body>
<div id="root-website" data-layout="{layout}">{markup}</div>
<script>
(() => {{
  'use strict';
{script}
}})();
</script></body></html>""".encode()
