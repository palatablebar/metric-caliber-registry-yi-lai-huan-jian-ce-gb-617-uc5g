"""问题清单与相邻运行对账（纯逻辑，不依赖 ORM，便于单测）。

一次校验运行检出的问题分三类，**分开记录、各有稳定身份键**：

- ``conflict`` 口径冲突：同名指标签名不同，身份键 ``conflict:<冲突编号>``
  （编号由成员指标编码组成，成员不变则身份不变）；
- ``cycle`` 依赖环：Tarjan SCC 检出的环，身份键 ``cycle:<成员编码排序后拼接>``
  （同一组指标构成的环，即使路径变化也视为同一条问题）；
- ``parse_error`` 解析失败：单个指标 DSL 解析不过，身份键 ``parse_error:<指标编码>``。

对账只比较身份键集合：

- ``new``       上一份没有、本次新检出；
- ``resolved``  上一份有、本次消失（解除）；
- ``continued`` 两份都有（延续，内容可能已变化）。

只有有结论的运行（success / partial）才参与对账；failed / running 运行
既不产生 findings，也不会成为对账基线，因此未完整完成的校验不会污染历史结论。
"""
from __future__ import annotations

from src.constants.enums import CONFLICT_KIND_LABELS, SEVERITY_LABELS

FINDING_TYPES = ("conflict", "cycle", "parse_error")

FINDING_TYPE_LABELS: dict[str, str] = {
    "conflict": "口径冲突",
    "cycle": "依赖环",
    "parse_error": "解析失败",
}

# 对账状态
STATE_NEW = "new"
STATE_RESOLVED = "resolved"
STATE_CONTINUED = "continued"

RECONCILE_STATE_LABELS: dict[str, str] = {
    STATE_NEW: "新增",
    STATE_RESOLVED: "解除",
    STATE_CONTINUED: "延续",
}

# 结论性运行状态：只有这些运行携带可对账的问题清单
CONCLUSIVE_STATUSES = ("success", "partial")


def conflict_finding_key(conflict_id: str) -> str:
    return f"conflict:{conflict_id}"


def cycle_finding_key(members: list[str]) -> str:
    return "cycle:" + "-".join(sorted(members))


def parse_error_finding_key(metric_code: str) -> str:
    return f"parse_error:{metric_code}"


def _conflict_findings(conflicts: list[dict]) -> list[dict]:
    findings = []
    for item in conflicts:
        findings.append(
            {
                "finding_key": conflict_finding_key(item["id"]),
                "finding_type": "conflict",
                "type_label": FINDING_TYPE_LABELS["conflict"],
                "title": f"同名冲突「{item['name']}」",
                "ref_id": item["id"],
                "kind": item["kind"],
                "kind_label": CONFLICT_KIND_LABELS.get(item["kind"], item["kind"]),
                "severity": item["severity"],
                "severity_label": SEVERITY_LABELS.get(item["severity"], item["severity"]),
                "score": item["score"],
                "metric_codes": list(item.get("metric_codes") or []),
                "path": None,
                "detail": {
                    "name": item["name"],
                    "expression": [metric.get("expression", "") for metric in item.get("metrics", [])],
                    "diff": item.get("diff") or {},
                    "reports": (item.get("impact") or {}).get("reports", []),
                    "report_count": (item.get("impact") or {}).get("report_count", 0),
                    "suggestion": item.get("suggestion", ""),
                    "summary": item.get("summary", ""),
                },
            }
        )
    return findings


def _cycle_findings(cycles: list[dict]) -> list[dict]:
    findings = []
    for item in cycles:
        findings.append(
            {
                "finding_key": cycle_finding_key(item["members"]),
                "finding_type": "cycle",
                "type_label": FINDING_TYPE_LABELS["cycle"],
                "title": f"依赖环（{item['length']} 节点）",
                "ref_id": item["id"],
                "kind": None,
                "kind_label": "—",
                "severity": item["severity"],
                "severity_label": SEVERITY_LABELS.get(item["severity"], item["severity"]),
                "score": item["score"],
                "metric_codes": list(item.get("members") or []),
                "path": list(item.get("path") or []),
                "detail": {
                    "path_text": " → ".join(item.get("path") or []),
                    "metric_names": list(item.get("metric_names") or []),
                    "reports": (item.get("impact") or {}).get("reports", []),
                    "report_count": (item.get("impact") or {}).get("report_count", 0),
                    "suggestion": item.get("suggestion", ""),
                    "summary": item.get("summary", ""),
                },
            }
        )
    return findings


def _parse_error_findings(parse_errors: list[dict]) -> list[dict]:
    findings = []
    for item in parse_errors:
        error = item.get("error") or {}
        code = item["code"]
        findings.append(
            {
                "finding_key": parse_error_finding_key(code),
                "finding_type": "parse_error",
                "type_label": FINDING_TYPE_LABELS["parse_error"],
                "title": f"解析失败 · {code}",
                "ref_id": code,
                "kind": error.get("code") or "SYNTAX",
                "kind_label": error.get("code") or "SYNTAX",
                "severity": None,
                "severity_label": "—",
                "score": None,
                "metric_codes": [code],
                "path": None,
                "detail": {
                    "metric_code": code,
                    "metric_name": item.get("name") or code,
                    "error_code": error.get("code"),
                    "error_message": error.get("message"),
                    "column": error.get("column"),
                    "snippet": error.get("snippet"),
                    "dsl_text": item.get("dsl_text", ""),
                },
            }
        )
    return findings


def build_findings(summary: dict) -> list[dict]:
    """把算法 B 的 summarize 结果 + 解析失败明细组装成稳定、可对账的问题清单。

    顺序固定：冲突（按影响分/编号）→ 环（按长度/编号）→ 解析失败（按指标编码），
    保证同一输入两次输出逐字相等。
    """
    findings = _conflict_findings(summary.get("conflicts") or [])
    findings += _cycle_findings(summary.get("cycles") or [])
    findings += _parse_error_findings(summary.get("parse_error_details") or [])
    return findings


def counts_by_type(findings: list[dict]) -> dict[str, int]:
    counts = {finding_type: 0 for finding_type in FINDING_TYPES}
    for finding in findings:
        finding_type = finding.get("finding_type")
        if finding_type in counts:
            counts[finding_type] += 1
    return counts


def reconcile(
    current_findings: list[dict],
    baseline_findings: list[dict] | None,
    *,
    baseline_run_no: str | None = None,
) -> dict:
    """相邻两次有结论运行按身份键对账。

    返回结构同时给出键集合（供序列化时回填解除问题的完整内容）与计数，
    页面、详情接口、CSV 导出都消费这同一份结论。没有基线（首份运行）时
    不产生新增/解除/延续结论，避免把首次检出误报成“新增”。
    """
    if not baseline_findings:
        return empty_reconciliation()

    current_by_key = {item["finding_key"]: item for item in current_findings}
    baseline_by_key = {item["finding_key"]: item for item in baseline_findings}
    current_keys = set(current_by_key)
    baseline_keys = set(baseline_by_key)

    new_keys = sorted(current_keys - baseline_keys)
    resolved_keys = sorted(baseline_keys - current_keys)
    continued_keys = sorted(current_keys & baseline_keys)

    def mark(keys: list[str], state: str) -> list[dict]:
        marked = []
        for key in keys:
            finding = dict(current_by_key.get(key) or baseline_by_key[key])
            finding["reconcile_state"] = state
            finding["reconcile_state_label"] = RECONCILE_STATE_LABELS[state]
            marked.append(finding)
        return marked

    new_items = mark(new_keys, STATE_NEW)
    resolved_items = mark(resolved_keys, STATE_RESOLVED)
    continued_items = mark(continued_keys, STATE_CONTINUED)

    return {
        "baseline_run_no": baseline_run_no,
        "counts": {
            "new": len(new_items),
            "resolved": len(resolved_items),
            "continued": len(continued_items),
            "by_type": counts_by_type(current_findings),
        },
        "new": new_items,
        "resolved": resolved_items,
        "continued": continued_items,
    }


def empty_reconciliation() -> dict:
    """首份有结论运行没有对账基线。"""
    return {
        "baseline_run_no": None,
        "counts": {"new": 0, "resolved": 0, "continued": 0, "by_type": {}},
        "new": [],
        "resolved": [],
        "continued": [],
    }


def state_of(finding_key: str, reconciliation: dict | None) -> str | None:
    """问题键在本次对账中的状态（new / resolved / continued），不在对账范围返回 None。"""
    if not reconciliation:
        return None
    for state in (STATE_NEW, STATE_RESOLVED, STATE_CONTINUED):
        if any(item["finding_key"] == finding_key for item in reconciliation.get(state, [])):
            return state
    return None
