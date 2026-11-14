"""Check ROOT's kickoff, handoff, QA acceptance and deploy transitions."""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
HANDOFF_CHECKS = {
    "unit": "Python 单测",
    "fixture": "模型替身浏览器回归",
    "live": "适用的真实模型自测",
}
DEPLOY_CHECKS = {
    "independent": "本地独立验收",
    "product": "本地产品验收",
    "target": "G4 目标环境预检",
    "rollback": "备份与回退",
}
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
AC = re.compile(r"AC-(?:[0-9]{3}(?:-[A-Z][0-9]*)?|[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*-[0-9]+)")
RECEIPT_SUFFIXES = {
    "ready": ".ready.json", "qa-design-ack": ".design-ack.json",
    "kickoff": ".kickoff.json", "handoff": ".sent.json",
    "qa-accept": ".accepted.json", "qa-result": ".result.json",
    "product-start": ".product-start.json", "product-result": ".product-result.json",
}


def field(record, name):
    value = record.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"需要填写 {name}。")
    return value.strip()


def local_file(value):
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file() or not path.stat().st_size:
        raise ValueError(f"文件不存在或为空：{path}")
    return path


def write_receipt(path, suffix, data):
    destination = path.with_name(path.name + suffix)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                            delete=False) as temporary:
        staging = Path(temporary.name)
        json.dump(data, temporary, ensure_ascii=False)
        temporary.write("\n")
    try:
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)


def check_candidate(record):
    commit = field(record, "commit")
    build = field(record, "build")
    if not SHA.fullmatch(commit):
        raise ValueError("commit 需要填写完整 Git SHA。")
    result = subprocess.run(
        ["git", "cat-file", "-t", commit], cwd=ROOT, capture_output=True, text=True, timeout=5,
    )
    if result.returncode or result.stdout.strip() != "commit":
        raise ValueError(f"本地 Git 找不到候选提交 {commit}。")
    check_scope(record.get("scope"), "scope")
    return commit, build


def check_scope(scope, name):
    if not isinstance(scope, list) or not scope or any(
        not isinstance(ac, str) or not AC.fullmatch(ac) for ac in scope
    ):
        raise ValueError(f"{name} 需要填写至少一个 AC 编号，例如 AC-018-B2 或 AC-AGENT-01。")
    return set(scope)


def check_ready(record):
    if field(record, "status") != "Ready":
        raise ValueError("需求须先确认 Ready。")
    field(record, "change")
    check_scope(record.get("scope"), "scope")
    field(record, "out_of_scope")
    local_file(field(record, "spec"))


def read_ready(record):
    path = local_file(field(record, "ready"))
    ready, receipt = checked_receipt(path, ".ready.json")
    check_ready(ready)
    if receipt.get("spec_sha256") != hashlib.sha256(local_file(ready["spec"]).read_bytes()).hexdigest():
        raise ValueError("需求快照已变化，请重新通知 QA。")
    return path, ready, receipt


def check_design_ack(record):
    _, _, receipt = read_ready(record)
    field(record, "qa_owner")
    local_file(field(record, "test_design"))
    field(record, "questions")
    return {"ready_version": receipt["ready_version"]}


def check_handoff_ready(record):
    _, ready, receipt = read_ready(record)
    scope = set(record["scope"] + record.get("trial_scope", []))
    if not scope <= set(ready["scope"]):
        raise ValueError("提测包含未通知 QA 的验收范围。")
    kickoff, kickoff_receipt = checked_receipt(local_file(field(record, "kickoff")), ".kickoff.json")
    if kickoff_receipt.get("ready_version") != receipt["ready_version"]:
        raise ValueError("开工回执与当前需求版本不一致。")
    if not scope <= check_scope(kickoff.get("scope"), "开工 scope"):
        raise ValueError("提测包含本次开工未认领的范围。")
    return receipt


def check_evidence(record, checks, candidate, *, optional_live=False, artifact_sha=None):
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("需要填写 evidence。")
    for key, label in checks.items():
        item = evidence.get(key)
        if not isinstance(item, dict):
            raise ValueError(f"缺少「{label}」回执（evidence.{key}）。")
        if (item.get("commit"), item.get("build")) != candidate:
            raise ValueError(f"「{label}」的 SHA 或构建与本次候选不一致。")
        if key in {"independent", "product"}:
            covered = item.get("scope")
            if not isinstance(covered, list):
                raise ValueError(f"「{label}」需要填写覆盖的 AC。")
            missing = [ac for ac in record["scope"] if ac not in covered]
            if missing:
                raise ValueError(f"「{label}」缺少范围 {'、'.join(missing)}。")
        if artifact_sha and key in {"target", "rollback"}:
            if item.get("environment") != record["environment"]:
                raise ValueError(f"「{label}」的环境与本次部署目标不一致。")
            if item.get("artifact_sha256") != artifact_sha:
                raise ValueError(f"「{label}」的 SHA-256 与本次部署制品不一致。")
        if key == "live" and optional_live and item.get("result") == "不适用":
            field(item, "reason")
        elif item.get("result") != "通过":
            raise ValueError(f"「{label}」需要通过回执。")
        local_file(field(item, "path"))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def check_running_build(entry, build):
    url = urlsplit(entry)
    if url.scheme not in {"http", "https"} or url.username or url.password or not url.hostname:
        raise ValueError("entry 需要填写本机工作台 HTTP 或 HTTPS 入口。")
    if not ipaddress.ip_address(url.hostname).is_loopback or url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("entry 需要指向回环地址的工作台根路径，例如 http://127.0.0.1:8773/。")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(f"{url.scheme}://{url.netloc}/api/config", timeout=5) as response:
        config = json.loads(response.read(65536))
    if not isinstance(config, dict) or config.get("build") != build:
        raise ValueError(f"工作台运行构建与候选 {build} 不一致。")


def check_handoff(record, candidate):
    kind = field(record, "kind")
    if kind not in {"阶段", "整版"}:
        raise ValueError("kind 应为「阶段」或「整版」。")
    if "trial_scope" in record:
        check_scope(record["trial_scope"], "trial_scope")
    checks = dict(HANDOFF_CHECKS)
    if kind == "整版":
        checks["code_review"] = "整版独立代码 Review"
    check_evidence(record, checks, candidate, optional_live=kind == "阶段")
    field(record, "known_issues")
    check_running_build(field(record, "entry"), candidate[1])


def check_qa_accept(record, candidate):
    handoff_path = local_file(field(record, "handoff"))
    handoff_bytes = handoff_path.read_bytes()
    handoff = json.loads(handoff_bytes)
    if not isinstance(handoff, dict) or check_candidate(handoff) != candidate:
        raise ValueError("QA 接测记录与提测候选不一致。")
    scope = check_scope(handoff["scope"], "提测 scope")
    trial_scope = handoff.get("trial_scope")
    if trial_scope is not None:
        scope.update(check_scope(trial_scope, "提测 trial_scope"))
    if scope != set(record["scope"]):
        raise ValueError("QA 接测范围与提测范围不一致，须纳入试用 AC。")
    if not handoff_path.with_name(handoff_path.name + ".sent.json").is_file():
        raise ValueError("缺少正式提测发送回执，请开发通过 handoff --send-to 提测。")
    _, sent = checked_receipt(handoff_path, ".sent.json", candidate)
    if sent["sha256"] != hashlib.sha256(handoff_bytes).hexdigest():
        raise ValueError("正式提测记录已变化，请重新检查并发送。")
    owner = field(record, "qa_owner")
    ready = check_handoff_ready(handoff)
    ack, ack_receipt = checked_receipt(
        local_file(field(record, "design_ack")), ".design-ack.json",
    )
    if (ack_receipt.get("ready_version") != ready["ready_version"]
            or sent.get("ready_version") != ready["ready_version"]
            or sent.get("thread") != ready.get("thread") or ack.get("qa_owner") != owner):
        raise ValueError("QA 认领、需求版本或提测接收方不一致。")
    plan_bytes = local_file(field(record, "test_plan")).read_bytes()
    plan = json.loads(plan_bytes)
    if not isinstance(plan, dict) or (plan.get("commit"), plan.get("build")) != candidate:
        raise ValueError("QA 用例表与提测候选不一致。")
    if plan.get("qa_owner") != owner or plan.get("status") != "可执行":
        raise ValueError("QA 负责人须提交状态为「可执行」的用例表。")
    cases = plan.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("QA 用例表缺少可执行用例。")
    covered = set()
    ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("每条用例须为 JSON 对象。")
        case_id = field(case, "id")
        if case_id in ids:
            raise ValueError(f"用例编号重复：{case_id}")
        ids.add(case_id)
        covered.update(check_scope(case.get("scope"), f"{case_id}.scope"))
        for name in ("preconditions", "steps", "expected", "environment", "evidence"):
            try:
                field(case, name)
            except ValueError as error:
                raise ValueError(f"{case_id} {error}") from error
    if covered != scope:
        missing = "、".join(sorted(scope - covered)) or "无"
        extra = "、".join(sorted(covered - scope)) or "无"
        raise ValueError(f"QA 用例覆盖与本次范围不一致。缺少：{missing}。范围外：{extra}。")
    review = record.get("test_review")
    if not isinstance(review, dict) or review.get("result") != "通过":
        raise ValueError("缺少通过的 QA 用例复核回执。")
    if field(review, "reviewer") == owner:
        raise ValueError("用例复核人须与用例负责人不同。")
    local_file(field(review, "path"))
    if review.get("plan_sha256") != hashlib.sha256(plan_bytes).hexdigest():
        raise ValueError("用例表内容已变化，须复核当前版本。")
    check_handoff(handoff, candidate)
    return {"handoff_sha256": hashlib.sha256(handoff_bytes).hexdigest(),
            "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
            "design_ack_sha256": ack_receipt["sha256"],
            "case_ids": sorted(ids)}


def checked_receipt(path, suffix, candidate=None):
    payload = path.read_bytes()
    receipt_path = path.with_name(path.name + suffix)
    if not receipt_path.is_file():
        raise ValueError(f"缺少正式回执：{receipt_path.name}")
    receipt = json.loads(receipt_path.read_bytes())
    if (not isinstance(receipt, dict)
            or (candidate is not None and (receipt.get("commit"), receipt.get("build")) != candidate)
            or receipt.get("sha256") != hashlib.sha256(payload).hexdigest()):
        raise ValueError(f"回执与当前候选或记录内容不一致：{receipt_path.name}")
    record = json.loads(payload)
    if not isinstance(record, dict):
        raise ValueError(f"回执对应的记录须为 JSON 对象：{path.name}")
    return record, receipt


def check_qa_result(record, candidate):
    accepted, receipt = checked_receipt(
        local_file(field(record, "acceptance")), ".accepted.json", candidate,
    )
    handoff = local_file(field(accepted, "handoff"))
    handoff_record, sent = checked_receipt(handoff, ".sent.json", candidate)
    ready = check_handoff_ready(handoff_record)
    _, ack = checked_receipt(local_file(field(accepted, "design_ack")), ".design-ack.json")
    if (sent.get("ready_version") != ready["ready_version"] or ack.get("ready_version") != ready["ready_version"]
            or receipt.get("design_ack_sha256") != ack["sha256"]):
        raise ValueError("需求或 QA 认领已变化，须重新接测。")
    plan = local_file(field(accepted, "test_plan"))
    for path, key in ((handoff, "handoff_sha256"), (plan, "plan_sha256")):
        if hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get(key):
            raise ValueError("提测或用例内容已变化，QA 须重新接测。")
    if set(record["scope"]) != set(accepted["scope"]):
        raise ValueError("QA 结果范围与接测范围不一致。")
    cases = record.get("cases")
    if not isinstance(cases, dict) or set(cases) != set(receipt["case_ids"]):
        raise ValueError("QA 结果须逐条对应正式接测的用例编号。")
    outcomes = {"通过", "失败", "阻塞", "未执行"}
    for case_id, item in cases.items():
        if not isinstance(item, dict) or item.get("result") not in outcomes:
            raise ValueError(f"{case_id} 缺少有效测试结果。")
        local_file(field(item, "path"))
    if record.get("result") not in outcomes:
        raise ValueError("需要填写有效的 QA 总体结论。")
    if record["result"] == "通过" and any(item["result"] != "通过" for item in cases.values()):
        raise ValueError("仍有未通过用例，无法提交 QA 通过结论。")
    return {key: receipt[key] for key in
            ("sha256", "handoff_sha256", "plan_sha256", "design_ack_sha256")}


def check_product(record, candidate):
    qa_result, receipt = checked_receipt(
        local_file(field(record, "qa_result")), ".result.json", candidate,
    )
    acceptance = check_qa_result(qa_result, candidate)
    if receipt.get("qa_acceptance") != acceptance:
        raise ValueError("正式 QA 结论对应的接测或用例版本已变化，请重新回传。")
    if qa_result["result"] != "通过" or not set(record["scope"]) <= set(qa_result["scope"]):
        raise ValueError("QA 正式结论尚未通过或未覆盖本次范围。")
    version = hashlib.sha256(json.dumps([receipt["sha256"], acceptance], sort_keys=True).encode()).hexdigest()
    return {"qa_sha256": version}


def check_product_result(record, candidate):
    start, receipt = checked_receipt(
        local_file(field(record, "product_start")), ".product-start.json", candidate,
    )
    qa = check_product(start, candidate)
    if receipt.get("qa_sha256") != qa["qa_sha256"] or set(record["scope"]) != set(start["scope"]):
        raise ValueError("产品结果与正式交接的 QA 版本或范围不一致。")
    if record.get("result") not in ("通过", "失败", "阻塞", "未执行"):
        raise ValueError("需要填写有效的产品验收结论。")
    report = local_file(field(record, "report"))
    return {"product_start_sha256": receipt["sha256"],
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()}


def check_deploy(record, candidate):
    field(record, "environment")
    local_file(field(record, "authorization"))
    artifact = local_file(field(record, "artifact"))
    expected = field(record, "artifact_sha256")
    if not DIGEST.fullmatch(expected):
        raise ValueError("artifact_sha256 需要填写制品的完整 SHA-256。")
    digest = hashlib.sha256()
    with artifact.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(f"部署制品 SHA-256 不匹配：{artifact}")
    check_evidence(record, DEPLOY_CHECKS, candidate, artifact_sha=expected)
    qa = check_product(record, candidate)
    product, receipt = checked_receipt(
        local_file(field(record, "product_start")), ".product-start.json", candidate,
    )
    if receipt.get("qa_sha256") != qa["qa_sha256"] or not set(record["scope"]) <= set(product["scope"]):
        raise ValueError("产品验收交接与本次 QA 结果或部署范围不一致。")
    product_result, result_receipt = checked_receipt(
        local_file(field(record, "product_result")), ".product-result.json", candidate,
    )
    current = check_product_result(product_result, candidate)
    if (product_result["result"] != "通过"
            or result_receipt.get("product_start_sha256") != receipt["sha256"]):
        raise ValueError("部署需要本次产品交接对应的正式通过结论。")
    if result_receipt.get("report_sha256") != current["report_sha256"]:
        raise ValueError("产品验收报告已变化，请重新回传正式结论。")
    if local_file(product_result["report"]).resolve() != local_file(record["evidence"]["product"]["path"]).resolve():
        raise ValueError("部署引用的产品验收报告与正式通过回执不一致。")


def status_report(records):
    """Read a task's selected records and verify the receipts that advance it."""
    stages = (
        ("ready", "待需求就绪通知", {}),
        ("kickoff", "待开工交接", {"ready": "ready"}),
        ("handoff", "待正式提测", {"ready": "ready", "kickoff": "kickoff"}),
        ("qa-design-ack", "待 QA 认领测试设计", {"ready": "ready"}),
        ("qa-accept", "待 QA 接测", {"handoff": "handoff", "design_ack": "qa-design-ack"}),
        ("qa-result", "待 QA 结论", {"acceptance": "qa-accept"}),
        ("product-start", "待产品验收交接", {"qa_result": "qa-result"}),
        ("product-result", "待产品验收结论", {"product_start": "product-start"}),
        ("deploy", "待部署校验", {"qa_result": "qa-result", "product_start": "product-start",
                               "product_result": "product-result"}),
    )
    if records.keys() - {stage for stage, _, _ in stages}:
        raise ValueError("状态索引包含未知节点。")
    paths = {}
    verified = []
    candidate = None
    ready = None
    pending_parallel = {}
    for stage, pending, links in stages:
        if stage == "qa-accept" and pending_parallel:
            next_stage = next(iter(pending_parallel))
            return {**pending_parallel[next_stage], "next": next_stage,
                    "pending": pending_parallel, "verified": verified}
        try:
            path = local_file(field(records, stage)).resolve()
            if stage == "deploy":
                record = json.loads(path.read_bytes())
                receipt = {}
            else:
                record, receipt = checked_receipt(path, RECEIPT_SUFFIXES[stage])
            for key, previous in links.items():
                if previous not in paths:
                    raise ValueError(f"前置节点 {previous} 尚未核验。")
                if local_file(field(record, key)).resolve() != paths[previous]:
                    raise ValueError(f"{key} 指向另一条交接链。")
            if stage == "ready":
                _, _, ready = read_ready({"ready": str(path)})
            elif stage in ("kickoff", "handoff", "qa-design-ack"):
                if receipt.get("ready_version") != ready["ready_version"]:
                    raise ValueError("回执与当前需求版本不一致。")
                if stage == "handoff":
                    candidate = check_candidate(record)
                    check_handoff_ready(record)
                    if (receipt.get("commit"), receipt.get("build"), receipt.get("thread")) != (
                            *candidate, ready["thread"]):
                        raise ValueError("提测候选或 QA 收件方与回执不一致。")
            else:
                if check_candidate(record) != candidate:
                    raise ValueError("记录与本轮提测候选不一致。")
                if stage != "deploy":
                    checked_receipt(path, RECEIPT_SUFFIXES[stage], candidate)
                if stage == "qa-accept":
                    _, handoff = checked_receipt(paths["handoff"], ".sent.json", candidate)
                    _, design = checked_receipt(paths["qa-design-ack"], ".design-ack.json")
                    if (handoff["sha256"] != receipt.get("handoff_sha256")
                            or design["sha256"] != receipt.get("design_ack_sha256")):
                        raise ValueError("提测或 QA 认领回执已变化，须重新接测。")
                    plan = local_file(field(record, "test_plan"))
                    if hashlib.sha256(plan.read_bytes()).hexdigest() != receipt.get("plan_sha256"):
                        raise ValueError("用例内容已变化，QA 须重新接测。")
                elif stage == "qa-result":
                    if check_qa_result(record, candidate) != receipt.get("qa_acceptance"):
                        raise ValueError("QA 结论对应的接测或用例版本已变化。")
                elif stage == "product-start":
                    if check_product(record, candidate)["qa_sha256"] != receipt.get("qa_sha256"):
                        raise ValueError("产品交接引用的 QA 结论已变化。")
                elif stage == "product-result":
                    if any(receipt.get(key) != value for key, value in check_product_result(record, candidate).items()):
                        raise ValueError("产品报告或交接已变化。")
                else:
                    check_deploy(record, candidate)
            paths[stage] = path
            verified.append(stage)
            if stage in ("qa-result", "product-result") and record["result"] != "通过":
                return {"state": f"{'QA' if stage == 'qa-result' else '产品'}验收{record['result']}",
                        "next": stage, "verified": verified}
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            if stage in ("kickoff", "handoff", "qa-design-ack") and "ready" in paths:
                pending_parallel[stage] = {"state": pending, "reason": str(error)}
                continue
            return {"state": pending, "next": stage, "reason": str(error), "verified": verified}
    return {"state": "部署校验通过，待执行切换", "next": None, "verified": verified}


def queue_handoff(stage, thread, path, record, *, resume):
    if stage == "ready":
        message = (f"ROOT 需求已就绪，请独立设计测试\n变更：{record['change']}\n"
                   f"验收范围：{'、'.join(record['scope'])}\n排除项：{record['out_of_scope']}\n"
                   f"需求及设计入口：{local_file(record['spec']).resolve()}\n请用 qa-design-ack 回传认领。")
    elif stage == "qa-design-ack":
        message = (f"ROOT QA 已认领测试设计\n负责人：{record['qa_owner']}\n"
                   f"用例入口：{local_file(record['test_design']).resolve()}\n待澄清：{record['questions']}")
    elif stage == "kickoff":
        message = f"ROOT 开工交接\n变更：{record['change']}\n已核对 QA 通知和适用的开工检查。"
    elif stage == "handoff":
        scope = "、".join(record["scope"] + record.get("trial_scope", []))
        message = (
            f"ROOT 独立测试交接\n候选：{record['commit']} / {record['build']}\n"
            f"入口：{record['entry']}\n验收范围：{scope}\n已知问题：{record['known_issues']}\n"
            "开发已提交候选，待 QA 准备和复核用例。请通过 qa-accept 回传正式接测。"
        )
    elif stage == "qa-accept":
        message = (f"ROOT QA 已正式接测\n候选：{record['commit']} / {record['build']}\n"
                   f"验收范围：{'、'.join(record['scope'])}\nQA 用例：{record['test_plan']}")
    elif stage == "qa-result":
        message = (f"ROOT QA 验收结论：{record['result']}\n"
                   f"候选：{record['commit']} / {record['build']}\n"
                   f"验收范围：{'、'.join(record['scope'])}")
    elif stage == "product-start":
        message = (f"ROOT 产品验收交接\n候选：{record['commit']} / {record['build']}\n"
                   f"验收范围：{'、'.join(record['scope'])}\n已核对正式 QA 通过回执。")
    else:
        message = (f"ROOT 产品验收结论：{record['result']}\n候选：{record['commit']} / {record['build']}\n"
                   f"验收范围：{'、'.join(record['scope'])}")
    message += f"\n本地交接记录：{path.resolve()}"
    command = ["codex", "queue", "--thread", thread, "--message", message]
    if resume:
        command.append("--resume")
    subprocess.run(command, cwd=ROOT, check=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=(*RECEIPT_SUFFIXES, "deploy", "status"))
    parser.add_argument("record", type=Path, help="当前阶段的 JSON 记录")
    parser.add_argument("--send-to", help="检查通过后交接给指定的 Codex 聊天")
    parser.add_argument("--queue-only", action="store_true", help="只排入消息队列，等接收方继续会话")
    parser.add_argument("--expected-release", nargs=4,
                        metavar=("COMMIT", "BUILD", "ENVIRONMENT", "SHA256"),
                        help="部署入口提供实际使用的候选、环境和制品摘要")
    args = parser.parse_args()

    if args.stage in ("deploy", "status") and args.send_to:
        parser.error("此阶段不发送聊天消息。")
    if args.queue_only and not args.send_to:
        parser.error("--queue-only 需要 --send-to。")
    if args.stage != "deploy" and args.expected_release:
        parser.error("--expected-release 仅适用于部署。")

    try:
        record_bytes = args.record.read_bytes()
        record = json.loads(record_bytes)
        if not isinstance(record, dict):
            raise ValueError("阶段记录应为 JSON 对象。")
        if args.stage == "status":
            report = status_report(record)
            if "handoff" in report["verified"]:
                _, handoff = checked_receipt(local_file(record["handoff"]), ".sent.json")
                report["candidate"] = {key: handoff[key] for key in ("commit", "build")}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        candidate = None
        details = {}
        if args.stage == "ready":
            check_ready(record)
            spec = local_file(record["spec"]).read_bytes()
            details["spec_sha256"] = hashlib.sha256(spec).hexdigest()
            details["ready_version"] = hashlib.sha256(record_bytes + b"\0" + spec).hexdigest()
        elif args.stage == "qa-design-ack":
            details = check_design_ack(record)
        elif args.stage == "kickoff":
            field(record, "change")
            if args.send_to or "ready" in record:
                _, ready, receipt = read_ready(record)
                scope = check_scope(record.get("scope"), "开工 scope")
                if not scope <= set(ready["scope"]):
                    raise ValueError("开工范围须包含在已通知 QA 的 Ready 范围内。")
                details["ready_version"] = receipt["ready_version"]
            if "checks" in record or {"AC-018-C", "AC-018-B2"} & set(record.get("scope", [])):
                checks = runpy.run_path(str(ROOT / "tests/check-related-change.py"))
                checks["check_record"](record)
                checks["check_references"]()
                checks["check_browser"]()
        else:
            candidate = check_candidate(record)
            if args.expected_release:
                actual = (*candidate, field(record, "environment"), field(record, "artifact_sha256"))
                if actual != tuple(args.expected_release):
                    raise ValueError("部署记录与实际切换的候选、目标或制品不一致。")
            check = {"handoff": check_handoff, "qa-accept": check_qa_accept,
                     "qa-result": check_qa_result, "product-start": check_product,
                     "product-result": check_product_result, "deploy": check_deploy}[args.stage]
            details = check(record, candidate) or {}
            if args.stage == "qa-result":
                details = {"qa_acceptance": details}
            if args.stage == "handoff" and (args.send_to or "ready" in record):
                ready = check_handoff_ready(record)
                if args.send_to and args.send_to != ready.get("thread"):
                    raise ValueError("提测须发给需求就绪时通知的 QA 聊天。")
                details["ready_version"] = ready["ready_version"]
        if args.send_to:
            if args.record.read_bytes() != record_bytes:
                raise ValueError("交接记录在检查期间已变化，请重跑。")
            queue_handoff(args.stage, args.send_to, args.record, record, resume=not args.queue_only)
            receipt = {"sha256": hashlib.sha256(record_bytes).hexdigest(),
                       "thread": args.send_to, "queued_at": datetime.now(timezone.utc).isoformat(),
                       **details}
            if candidate:
                receipt.update(commit=candidate[0], build=candidate[1])
            write_receipt(args.record, RECEIPT_SUFFIXES[args.stage], receipt)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"{args.stage} 检查失败：{error}", file=sys.stderr)
        return 1
    version = f" · {candidate[0]} · {candidate[1]}" if candidate else ""
    print(f"{args.stage} 检查通过{version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
