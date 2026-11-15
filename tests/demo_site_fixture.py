"""Controlled DeepSeek responses for the CHG-046 browser flow."""
from html import escape
import json
from unittest.mock import patch

import assistant_model
import staged_blueprint


RECOVERY_REQUIREMENT = "做一个单页官网原型，有上午和下午可切换的日程。"
recovery_generated = False


def website_code(layout):
    navigation = ""
    if layout == "brand":
        navigation = """<nav aria-label="网站导航">
<button data-route="home" aria-current="page">首页</button>
<button data-route="catalog">菜单</button>
<button data-route="faq">问答</button>
</nav>"""
    hidden = " hidden" if layout == "brand" else ""
    return {
        "format": "website-code-v1",
        "markup": f"""{navigation}
<section data-page="home"><h1>山野咖啡</h1><p>从山野选豆，在城市分享咖啡。</p></section>
<section id="site-catalog" data-page="catalog"{hidden}>
<h2>咖啡菜单</h2><div id="site-filters">
<button data-filter="cold">冷饮</button><button data-filter="hot">热饮</button>
</div><article data-item-filter="cold">山野冷萃</article>
<article data-item-filter="cold">燕麦冰拿铁</article>
<article data-item-filter="hot">手冲咖啡</article></section>
<section id="site-faq" data-page="faq"{hidden}><h2>常见问题</h2>
<details><summary>可以选咖啡豆吗？</summary><p>手冲咖啡可选当日豆单。</p></details></section>""",
        "style": ("body { margin: 0; } main { padding: 16px; } button { margin: 4px; } "
                  "#root-website [data-item-filter] { display: flex !important; } "
                  "#root-website [data-page] { display: grid; }"),
        "script": """
const root = document.getElementById('root-website');
const navigation = root.querySelectorAll('[data-route]');
const pages = root.querySelectorAll('[data-page]');
for (const button of navigation) {
  button.addEventListener('click', () => {
    for (const page of pages) page.hidden = page.dataset.page !== button.dataset.route;
    for (const item of navigation) item.removeAttribute('aria-current');
    button.setAttribute('aria-current', 'page');
  });
}
const items = root.querySelectorAll('[data-item-filter]');
for (const button of root.querySelectorAll('#site-filters button')) {
  button.addEventListener('click', () => {
    for (const item of items) item.hidden = item.dataset.itemFilter !== button.dataset.filter;
  });
}
""",
    }


def demo_site_provider(stage, requirement, prior, key, admit, record, *, category="auto"):
    """Keep the real stage validation and assembly in the fixture browser run."""
    if category == "auto":
        layout = "landing" if "单页官网原型" in requirement else "brand"
    else:
        layout = category
    assert layout in {"landing", "brand"}
    spec = {"kind": "website", "layout": layout, "title": "山野咖啡", "goal": "品牌介绍和咖啡菜单"}
    content = {"spec": spec, "implementation": website_code(layout)}[stage]
    if stage == "implementation" and requirement.startswith("转义标签回归："):
        content["markup"] = escape(content["markup"])
        content["script"] = "void 0;"
    if stage == "implementation" and requirement == RECOVERY_REQUIREMENT:
        global recovery_generated
        content["markup"] += """
<div role="tablist">
  <button id="morning-tab" role="tab" aria-selected="true" aria-controls="morning">上午</button>
  <button id="afternoon-tab" role="tab" aria-selected="false" aria-controls="afternoon">下午</button>
</div>
<p id="morning" role="tabpanel">签到</p>
<p id="afternoon" role="tabpanel" hidden>制作</p>"""
        if recovery_generated:
            content["script"] += """
const tabs = root.querySelectorAll('[role=tab]');
for (const tab of tabs) tab.addEventListener('click', () => {
  for (const item of tabs) {
    item.setAttribute('aria-selected', String(item === tab));
    document.getElementById(item.getAttribute('aria-controls')).hidden = item !== tab;
  }
});
"""
        else:
            content["script"] = "void 0;"
            recovery_generated = True

    def exchange(payload, reserve, **_):
        reserve()
        return {"status": 200, "body": json.dumps({
            "id": "fixture-coffee-" + stage,
            "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(content, ensure_ascii=False),
            }}],
        })}

    with patch.object(assistant_model, "_exchange", exchange):
        return staged_blueprint.provide(stage, requirement, prior, key, admit, record, category=category)
