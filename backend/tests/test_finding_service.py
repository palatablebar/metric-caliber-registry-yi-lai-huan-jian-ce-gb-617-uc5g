"""问题清单与相邻运行对账（finding_service）纯逻辑单测。"""
from __future__ import annotations

from src.services import finding_service


def _conflict(cid: str, name: str, codes: list[str], severity: str = "high", score: int = 9) -> dict:
    return {
        "id": cid,
        "name": name,
        "kind": "filter_diff",
        "severity": severity,
        "score": score,
        "metric_codes": codes,
        "metrics": [{"code": item, "expression": f"{item} = sum(t.x)"} for item in codes],
        "diff": {"fields_only_first": [], "fields_only_others": []},
        "impact": {"reports": ["r01"], "report_count": 1, "core_count": 1},
        "suggestion": "对齐口径",
        "summary": f"「{name}」有 2 份不同口径",
    }


def _cycle(cid: str, members: list[str]) -> dict:
    path = members + [members[0]]
    return {
        "id": cid,
        "path": path,
        "length": len(members),
        "members": members,
        "metric_names": members,
        "severity": "medium",
        "score": 4,
        "impact": {"reports": [], "report_count": 0},
        "suggestion": "打断引用链",
        "summary": "检测到循环引用",
    }


def _parse_error(code: str, message: str = "DSL 为空") -> dict:
    return {
        "code": code,
        "name": code.upper(),
        "dsl_text": "",
        "error": {"code": "EMPTY_DSL", "message": message, "position": 0, "column": 1, "snippet": ""},
    }


def _summary(conflicts=None, cycles=None, parse_error_details=None) -> dict:
    conflicts = conflicts or []
    cycles = cycles or []
    parse_error_details = parse_error_details or []
    return {
        "metrics_total": 10,
        "conflicts": conflicts,
        "cycles": cycles,
        "conflicts_total": len(conflicts),
        "cycles_total": len(cycles),
        "parse_errors": [item["code"] for item in parse_error_details],
        "parse_error_count": len(parse_error_details),
        "parse_error_details": parse_error_details,
        "severity": {"high": 0, "medium": 0, "low": 0},
    }


def test_build_findings_keeps_three_types_separate_and_stable_keys():
    summary = _summary(
        conflicts=[_conflict("conflict-a-b", "GMV", ["a", "b"])],
        cycles=[_cycle("cycle-x-y-z", ["x", "y", "z"])],
        parse_error_details=[_parse_error("broken")],
    )
    findings = finding_service.build_findings(summary)
    assert [item["finding_type"] for item in findings] == ["conflict", "cycle", "parse_error"]
    keys = {item["finding_key"] for item in findings}
    assert keys == {"conflict:conflict-a-b", "cycle:x-y-z", "parse_error:broken"}
    # 同一输入两次构建逐字相等
    assert finding_service.build_findings(summary) == findings
    # 计数按类型分开
    assert finding_service.counts_by_type(findings) == {"conflict": 1, "cycle": 1, "parse_error": 1}


def test_cycle_key_stable_when_path_changes_but_members_unchanged():
    # 同一组指标构成的环，路径表示变化，身份键保持一致
    assert finding_service.cycle_finding_key(["b", "a", "c"]) == finding_service.cycle_finding_key(["a", "b", "c"])


def test_reconcile_distinguishes_new_resolved_continued():
    first = finding_service.build_findings(
        _summary(
            conflicts=[_conflict("conflict-a-b", "GMV", ["a", "b"])],
            cycles=[_cycle("cycle-x-y", ["x", "y"])],
        )
    )
    second = finding_service.build_findings(
        _summary(
            conflicts=[
                _conflict("conflict-a-b", "GMV", ["a", "b"]),  # 延续
                _conflict("conflict-c-d", "AOV", ["c", "d"]),  # 新增
            ],
            # 环 x-y 解除
            parse_error_details=[_parse_error("broken")],  # 新增解析失败
        )
    )
    result = finding_service.reconcile(second, first, baseline_run_no="V1")
    assert result["baseline_run_no"] == "V1"
    assert {item["finding_key"] for item in result["new"]} == {
        "conflict:conflict-c-d",
        "parse_error:broken",
    }
    assert {item["finding_key"] for item in result["resolved"]} == {"cycle:x-y"}
    assert {item["finding_key"] for item in result["continued"]} == {"conflict:conflict-a-b"}
    assert result["counts"] == {
        "new": 2,
        "resolved": 1,
        "continued": 1,
        "by_type": {"conflict": 2, "cycle": 0, "parse_error": 1},
    }
    # 解除项保留基线内容，且标成 resolved
    resolved = result["resolved"][0]
    assert resolved["finding_type"] == "cycle"
    assert resolved["reconcile_state"] == "resolved"
    assert all(item["reconcile_state"] == "new" for item in result["new"])
    assert all(item["reconcile_state"] == "continued" for item in result["continued"])


def test_same_total_count_but_replaced_problems_are_visible():
    # 两次问题总数相同（都是 1），但旧问题消失、新问题顶替：必须能区分
    first = finding_service.build_findings(_summary(conflicts=[_conflict("conflict-a-b", "GMV", ["a", "b"])]))
    second = finding_service.build_findings(_summary(conflicts=[_conflict("conflict-c-d", "AOV", ["c", "d"])]))
    result = finding_service.reconcile(second, first, baseline_run_no="V1")
    assert len(first) == len(second) == 1
    assert result["counts"]["new"] == 1
    assert result["counts"]["resolved"] == 1
    assert result["counts"]["continued"] == 0


def test_first_run_has_no_baseline_and_all_current_are_not_marked():
    findings = finding_service.build_findings(_summary(conflicts=[_conflict("conflict-a-b", "GMV", ["a", "b"])]))
    result = finding_service.reconcile(findings, None)
    assert result["baseline_run_no"] is None
    assert result["new"] == [] and result["resolved"] == [] and result["continued"] == []
    # 没有基线就没有任何对账结论（含类型计数），本次清单内容仍由 findings 承载
    assert result["counts"] == {"new": 0, "resolved": 0, "continued": 0, "by_type": {}}
    assert finding_service.empty_reconciliation()["baseline_run_no"] is None


def test_parse_error_finding_carries_structured_error():
    findings = finding_service.build_findings(_summary(parse_error_details=[_parse_error("broken", "语法不对")]))
    finding = findings[0]
    assert finding["finding_key"] == "parse_error:broken"
    assert finding["finding_type"] == "parse_error"
    assert finding["detail"]["error_code"] == "EMPTY_DSL"
    assert finding["detail"]["error_message"] == "语法不对"
    assert finding["severity"] is None  # 解析失败不参与严重度打分，与冲突/环分开
