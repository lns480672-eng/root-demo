"""Controlled DeepSeek responses for the CHG-046 browser flow."""
import json
from unittest.mock import patch

import assistant_model
import staged_blueprint


COFFEE = {
    "intro": {"heading": "山野咖啡", "body": "从山野选豆，在城市分享新鲜烘焙的咖啡。"},
    "catalog": {
        "title": "咖啡菜单",
        "filters": [{"id": "cold", "label": "冷饮"}, {"id": "hot", "label": "热饮"}],
        "items": [
            {"name": "山野冷萃", "description": "低温浸萃，清爽回甘。", "filter": "cold"},
            {"name": "燕麦冰拿铁", "description": "轻柔燕麦与浓缩咖啡。", "filter": "cold"},
            {"name": "手冲咖啡", "description": "小批次烘焙，现磨现冲。", "filter": "hot"},
        ],
    },
    "faq": [{"question": "可以选咖啡豆吗？", "answer": "手冲咖啡可选当日豆单。"}],
}


def demo_site_provider(stage, requirement, prior, key, admit, record, *, category="auto"):
    """Keep the real stage validation and assembly in the fixture browser run."""
    if category == "auto":
        layout = "landing" if "单页官网原型" in requirement else "brand"
    else:
        layout = category
    assert layout in {"landing", "brand"}
    spec = {"kind": "website", "layout": layout, "title": "山野咖啡", "goal": "品牌介绍和咖啡菜单"}
    content = {"spec": spec, "implementation": COFFEE}[stage]

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
