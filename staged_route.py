"""Classify explicit requirements before any model dispatch."""
import re

RECORDS = r"台账|登记|记录|看板|清单|跟踪|列表|申请|投递|卡片|tracker|ledger|kanban|records?"
FEATURES = (
    r"筛选|过滤|检索|搜索|filter|search",
    r"统计|汇总|合计|总计|聚合|summary|aggregate",
    r"导出|下载|备份|恢复文件|保存成文件|保存为文件|export|csv",
    r"分栏|阶段.{0,5}分组|移动卡片|阶段切换|切换到|阶段流转|状态.{0,12}(更新|修改|切换|流转)|(?:更新|修改|切换|流转).{0,12}状态|审核|审批",
    r"编辑.{0,12}删除|删除.{0,12}编辑",
)
UNSUPPORTED = r"外部接口|外部系统|飞书接口|联网同步|自动发邮件|多人实时协同|多人实时编辑|webhook"
NEGATIVE = r"不(?:需要|要|用|支持|做|含)|无需|不用|禁止|without|\bno "


def choose_route(requirement):
    text = requirement.lower()
    clauses = re.split(r"[，,。；;！!？?\n]", text)
    positive = " ".join(re.split(NEGATIVE, clause, maxsplit=1)[0] for clause in clauses)
    if not re.search(RECORDS, positive) or sum(bool(re.search(feature, positive)) for feature in FEATURES) < 2:
        return "single"
    return "unsupported" if re.search(UNSUPPORTED, positive) else "staged"
