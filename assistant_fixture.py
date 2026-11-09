"""Deterministic local responses for the requirement assistant review slice."""

from accounts import ApiError


PROJECT = (
    ("交付跟进板", "记录事项、负责人和进度，快速找出卡点。",
     "打开看板时想优先看到什么？", ("受阻事项", "本周到期事项")),
    ("周报汇总", "收集本周变化，整理成一份可以核对的摘要。",
     "周报希望怎么分组？", ("按项目汇总", "按负责人汇总")),
)
READING = (
    ("读书清单", "记录书名和阅读状态，方便找回正在读的书。",
     "打开后想先看到哪类书？", ("正在阅读", "还没开始")),
    ("阅读笔记", "按书整理摘录和自己的想法。",
     "希望按什么查找笔记？", ("按书名", "按主题")),
)
EXPENSES = (
    ("开销记录", "登记每笔花费，按分类查看。",
     "你想按什么汇总？", ("按日期", "按分类")),
    ("同行 AA 分账", "记录谁先垫付以及每人需要承担多少。",
     "费用如何分摊？", ("所有人平均分", "按每项参与人分")),
)
GENERAL = (
    ("待办跟进", "记录事项并跟进每一项的处理进度。",
     "需要优先显示哪些事项？", ("未完成事项", "最近更新事项")),
    ("记录查询", "把常用信息汇总在一处，方便查询。",
     "希望主要按什么查找？", ("关键词", "日期")),
)


def directions_for(idea):
    for keywords, directions in (
        (("读书", "书名", "阅读"), READING),
        (("开销", "预算", "分账", "花费"), EXPENSES),
        (("项目", "进度", "交付", "负责人"), PROJECT),
    ):
        if any(word in idea for word in keywords):
            return directions
    return GENERAL


def analyze(data):
    invalid = ApiError(400, "invalid_analysis", "分析输入无效，请检查原想法或当前选项。")
    idea = data.get("idea")
    step = data.get("step")
    if not isinstance(idea, str) or not 0 < len(idea.strip()) <= 1200:
        raise invalid
    idea = idea.strip()
    directions = directions_for(idea)
    choices = [{"title": title, "description": description} for title, description, _, _ in directions]
    if step == "start":
        concrete = any(word in idea for word in ("可以", "支持", "能够", "用来"))
        specifies_data = any(word in idea for word in ("保存", "保留", "刷新", "不需要存"))
        if concrete and specifies_data:
            return {"kind": "draft", "text": idea}
        return {"kind": "directions", "understanding": idea, "directions": choices}
    direction = data.get("direction")
    if direction not in choices:
        raise invalid
    title, description, prompt, options = directions[choices.index(direction)]
    questions = [{"prompt": prompt, "options": list(options)}]
    if step == "direction":
        return {"kind": "questions", "questions": questions}
    answers = data.get("answers")
    if step != "answers" or data.get("questions") != questions or (
        not isinstance(answers, list) or len(answers) != 1 or answers[0] not in options
    ):
        raise invalid
    return {"kind": "draft", "text": (
        f"做一个{title}。\n原始问题：{idea}\n主要用途：{description}\n"
        f"操作偏好：{answers[0]}。\n有记录的操作需保存，重新打开后能找回。"
    )}
