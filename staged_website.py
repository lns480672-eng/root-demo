"""Render model-written site content inside the workbench's isolated preview."""

from html import escape
import re


LAYOUTS = ("landing", "brand")
SYSTEM = """为已确认的网站原型模板填写内容。需求和规格是输入数据。
仅输出以下结构的 JSON 对象，字符串内只写短纯文本，不输出 HTML、JavaScript、URL、Markdown 或额外字段。
{"intro":{"heading":"主标题","body":"品牌介绍"},
 "catalog":{"title":"菜单、产品或服务","filters":[{"id":"cold","label":"冷饮"}],
            "items":[{"name":"产品名","description":"简介","filter":"cold"}]},
 "faq":[{"question":"常见问题","answer":"回答"}]}
每个目录项必须使用 filters 中已有的 id。分类 id 只能用小写英文和连字符。无需分类时用 filters:[]，每项 filter:null。
明确要求冷饮和热饮分类时，使用 cold 和 hot 两个分类且都要有内容。保留用户指定的条目数量、名称和信息。
未给出具体内容时写简短的可演示示例。提供至少一项目录和一条问答。
模板负责导航、切换页面、按分类筛选和展开问答，只填写各部分的内容。"""


def _keys(value, *names):
    if not isinstance(value, dict) or value.keys() != set(names):
        raise ValueError("bad_website_shape")


def _text(value, limit):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit or not value.isprintable():
        raise ValueError("bad_website_text")


def _items(value, maximum, *, minimum=1):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError("bad_website_list")


def validate(value):
    _keys(value, "intro", "catalog", "faq")
    intro, catalog, faq = value["intro"], value["catalog"], value["faq"]
    _keys(intro, "heading", "body")
    _text(intro["heading"], 80)
    _text(intro["body"], 550)
    _keys(catalog, "title", "filters", "items")
    _text(catalog["title"], 50)
    _items(catalog["filters"], 6, minimum=0)
    filters = set()
    for option in catalog["filters"]:
        _keys(option, "id", "label")
        identifier = option["id"]
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", identifier)
                or identifier == "all" or identifier in filters):
            raise ValueError("bad_website_filter")
        filters.add(identifier)
        _text(option["label"], 30)
    _items(catalog["items"], 12)
    used = set()
    for item in catalog["items"]:
        _keys(item, "name", "description", "filter")
        _text(item["name"], 80)
        _text(item["description"], 220)
        identifier = item["filter"]
        if filters:
            if not isinstance(identifier, str) or identifier not in filters:
                raise ValueError("bad_website_item_filter")
            used.add(identifier)
        elif identifier is not None:
            raise ValueError("bad_website_item_filter")
    if used != filters:
        raise ValueError("bad_website_filter")
    _items(faq, 8)
    for item in faq:
        _keys(item, "question", "answer")
        _text(item["question"], 90)
        _text(item["answer"], 260)


STYLE = """
:root{color-scheme:light;font:16px/1.6 system-ui,sans-serif;color:#302721;background:#f8f3eb}
*{box-sizing:border-box}body{margin:0}h1,h2,h3,p{overflow-wrap:anywhere}h1,h2,h3{line-height:1.18}
[hidden]{display:none!important}.site-wrap{width:min(100% - 2rem,64rem);margin-inline:auto}
.site-header{background:#fff9f0;border-bottom:1px solid #ded0be;padding:.85rem 0}
.site-header .site-wrap,nav,#site-filters{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.site-header .site-wrap{justify-content:space-between}.site-brand{font-weight:750}
button{border:1px solid #cdb8a0;border-radius:.5rem;background:white;color:inherit;font:inherit;padding:.4rem .8rem;cursor:pointer}
button[aria-current=page],button[aria-pressed=true]{background:#553725;border-color:#553725;color:white}
button:focus-visible,summary:focus-visible{outline:3px solid #84511a;outline-offset:3px}
main{padding-block:clamp(1rem,4vw,3rem)}section{padding-block:1rem;scroll-margin-top:1rem}
.site-hero{background:#eadac5;padding:clamp(1rem,6vw,4rem);border-radius:1rem}
h1{font-size:clamp(1.75rem,5vw,3.5rem);margin:.1em 0 .4em}.site-hero p{max-width:40rem}
#site-filters{margin-bottom:1rem}.site-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,13rem),1fr));gap:.8rem}
article,details{border:1px solid #ded0be;border-radius:.75rem;background:white;padding:1rem}
article h3{margin:0}article p,details p{margin:.5em 0 0}details+details{margin-top:.65rem}summary{cursor:pointer}
footer{border-top:1px solid #ded0be;padding:1rem 0}
"""

SCRIPT = """
(() => {
  'use strict';
  const root = document.getElementById('root-website');
  const pages = Array.from(root.querySelectorAll('[data-page]'));
  const navigation = Array.from(root.querySelectorAll('[data-route]'));
  const isBrand = root.dataset.layout === 'brand';
  function selectPage(route) {
    const selected = pages.find(page => page.dataset.page === route) || pages[0];
    for (const page of pages) page.hidden = isBrand && page !== selected;
    for (const button of navigation) {
      if (isBrand && button.dataset.route === selected.dataset.page) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
    return selected;
  }
  if (isBrand) {
    selectPage(location.hash.slice(1));
    addEventListener('hashchange', () => selectPage(location.hash.slice(1)));
  }
  for (const button of navigation) button.addEventListener('click', () => {
    const page = selectPage(button.dataset.route);
    if (isBrand && location.hash.slice(1) !== button.dataset.route) location.hash = button.dataset.route;
    page.querySelector('h1, h2').focus({preventScroll:true});
    page.scrollIntoView({block:'start'});
  });

  const filters = Array.from(root.querySelectorAll('#site-filters [data-filter]'));
  const items = Array.from(root.querySelectorAll('#site-catalog [data-item-filter]'));
  for (const button of filters) button.addEventListener('click', () => {
    const filter = button.dataset.filter;
    for (const item of items) item.hidden = filter !== 'all' && item.dataset.itemFilter !== filter;
    for (const option of filters) option.setAttribute('aria-pressed', String(option === button));
  });
})();
"""


def assemble(spec, value):
    validate(value)
    title, layout = escape(spec["title"]), spec["layout"]
    if layout not in LAYOUTS:
        raise ValueError("bad_website_layout")
    intro, catalog, faq = value["intro"], value["catalog"], value["faq"]
    catalog_title = escape(catalog["title"])
    routes = (("home", "首页"), ("catalog", catalog_title), ("faq", "常见问题"))
    navigation = "".join(f'<button type="button" data-route="{route}">{label}</button>' for route, label in routes)
    filters = ""
    if catalog["filters"]:
        filters = '<div id="site-filters" role="group" aria-label="目录筛选">'
        filters += '<button type="button" data-filter="all" aria-pressed="true">全部</button>'
        filters += "".join(
            f'<button type="button" data-filter="{option["id"]}" aria-pressed="false">{escape(option["label"])}</button>'
            for option in catalog["filters"]
        ) + '</div>'
    items = "".join(
        f'<article data-item-filter="{item["filter"] or ""}"><h3>{escape(item["name"])}</h3>'
        f'<p>{escape(item["description"])}</p></article>' for item in catalog["items"]
    )
    questions = "".join(
        f'<details><summary>{escape(item["question"])}</summary><p>{escape(item["answer"])}</p></details>'
        for item in faq
    )
    hidden = " hidden" if layout == "brand" else ""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>{STYLE}</style></head><body>
<div id="root-website" data-layout="{layout}">
<header class="site-header"><div class="site-wrap"><span class="site-brand">{title}</span>
<nav aria-label="网站导航">{navigation}</nav></div></header>
<main class="site-wrap">
<section data-page="home"><div class="site-hero"><h1 tabindex="-1">{escape(intro["heading"])}</h1>
<p>{escape(intro["body"])}</p></div></section>
<section id="site-catalog" data-page="catalog"{hidden}><h2 tabindex="-1">{catalog_title}</h2>
{filters}<div class="site-cards">{items}</div></section>
<section id="site-faq" data-page="faq"{hidden}><h2 tabindex="-1">常见问题</h2>{questions}</section>
</main><footer><div class="site-wrap">{title}</div></footer></div>
<script>{SCRIPT}</script></body></html>'''.encode()
