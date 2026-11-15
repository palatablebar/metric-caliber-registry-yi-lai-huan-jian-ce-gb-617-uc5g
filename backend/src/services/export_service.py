"""导出服务：指标清单 / 冲突与影响面 / 变更单 / 运行记录（CSV，UTF-8 BOM）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CHANGE_KIND_LABELS,
    CHANGE_STATUS_LABELS,
    CONFLICT_KIND_LABELS,
    METRIC_DOMAIN_LABELS,
    METRIC_TIER_LABELS,
    RUN_STATUS_LABELS,
    SEVERITY_LABELS,
    TRIGGER_LABELS,
)
from src.models.change_request import ChangeRequest
from src.models.metric import Metric
from src.models.run import Run
from src.services import formula_service, graph_service, report_service
from src.utils.csv_export import to_csv


def metrics_csv(db: Session) -> bytes:
    metrics = db.scalars(select(Metric).order_by(Metric.code)).all()
    latest = formula_service.latest_map(db)
    reports_index = report_service.reports_by_metric(db)
    headers = [
        "指标编码", "指标名称", "业务域", "分级", "单位", "负责人", "当前版本",
        "解析状态", "解析错误", "表达式", "字段", "过滤条件", "依赖指标", "下游报表", "报表数", "状态",
    ]
    rows = []
    for metric in metrics:
        formula = latest.get(metric.id)
        parsed = (formula.parsed or {}) if formula else {}
        reports = reports_index.get(metric.code, [])
        rows.append(
            [
                metric.code,
                metric.name,
                METRIC_DOMAIN_LABELS.get(metric.domain, metric.domain),
                METRIC_TIER_LABELS.get(metric.tier, metric.tier),
                metric.unit,
                metric.owner,
                metric.current_version,
                formula.parse_status if formula else "error",
                (formula.parse_error if formula else "缺少口径公式") or "",
                parsed.get("expression") or (formula.dsl_text if formula else ""),
                " | ".join(parsed.get("fields") or []),
                " | ".join(parsed.get("filters") or []),
                " | ".join(parsed.get("deps") or []),
                " | ".join(report["name"] for report in reports),
                len(reports),
                metric.status,
            ]
        )
    return to_csv(headers, rows)


def conflicts_csv(db: Session) -> bytes:
    conflicts = graph_service.find_conflicts(db)
    headers = [
        "同名指标", "冲突编号", "差异类型", "严重度", "影响分", "口径成员", "字段差异",
        "过滤条件差异", "下游报表数", "核心报表数", "下游报表", "建议动作",
    ]
    rows = []
    for item in conflicts:
        diff = item.get("diff") or {}
        impact = item.get("impact") or {}
        rows.append(
            [
                item["name"],
                item["id"],
                CONFLICT_KIND_LABELS.get(item["kind"], item["kind"]),
                SEVERITY_LABELS.get(item["severity"], item["severity"]),
                item["score"],
                " | ".join(metric["code"] for metric in item["metrics"]),
                " | ".join(diff.get("fields_only_first", []) + diff.get("fields_only_others", [])),
                " | ".join(diff.get("filters_only_first", []) + diff.get("filters_only_others", [])),
                impact.get("report_count", 0),
                impact.get("core_count", 0),
                " | ".join(impact.get("report_names", [])),
                item.get("suggestion", ""),
            ]
        )
    return to_csv(headers, rows)


def change_requests_csv(db: Session) -> bytes:
    changes = db.scalars(select(ChangeRequest).order_by(ChangeRequest.id)).all()
    codes = {metric.id: metric.code for metric in db.scalars(select(Metric)).all()}
    headers = [
        "变更单号", "目标", "指标", "类型", "严重度", "影响分", "影响指标数",
        "影响报表数", "核心报表数", "状态", "申请人", "审批人", "原因", "申请时间", "决定时间",
    ]
    rows = [
        [
            change.cr_no,
            change.target_ref,
            codes.get(change.metric_id or -1, ""),
            CHANGE_KIND_LABELS.get(change.change_kind, change.change_kind),
            SEVERITY_LABELS.get(change.severity, change.severity),
            change.impact_score,
            (change.impact or {}).get("metric_count", len((change.impact or {}).get("metric_codes") or [])),
            (change.impact or {}).get("report_count", 0),
            (change.impact or {}).get("core_count", 0),
            CHANGE_STATUS_LABELS.get(change.status, change.status),
            change.requester_email,
            change.approver_email or "",
            change.reason,
            change.created_at.strftime("%Y-%m-%d %H:%M") if change.created_at else "",
            change.decided_at.strftime("%Y-%m-%d %H:%M") if change.decided_at else "",
        ]
        for change in changes
    ]
    return to_csv(headers, rows)


def runs_csv(db: Session) -> bytes:
    runs = db.scalars(select(Run).order_by(Run.id)).all()
    headers = [
        "运行号", "触发方式", "状态", "指标数", "问题数", "冲突数", "环数",
        "解析失败数", "对账基线", "新增问题", "解除问题", "延续问题",
        "操作人", "开始时间", "结束时间", "日志",
    ]
    rows = []
    for run in runs:
        reconciliation = run.reconciliation or {}
        counts = reconciliation.get("counts") or {}
        rows.append(
            [
                run.run_no,
                TRIGGER_LABELS.get(run.trigger, run.trigger),
                RUN_STATUS_LABELS.get(run.status, run.status),
                run.input_count,
                run.output_count,
                (run.metrics or {}).get("conflicts_total", 0),
                (run.metrics or {}).get("cycles_total", 0),
                (run.metrics or {}).get("parse_error_count", 0),
                reconciliation.get("baseline_run_no") or "",
                counts.get("new", 0),
                counts.get("resolved", 0),
                counts.get("continued", 0),
                run.operator_email,
                run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "",
                run.finished_at.strftime("%Y-%m-%d %H:%M") if run.finished_at else "",
                (run.log_text or "").replace("\n", " "),
            ]
        )
    return to_csv(headers, rows)


def run_findings_csv(db: Session) -> bytes:
    """逐运行 × 逐问题明细：页面与本导出消费同一份 findings / reconciliation 结论。

    解除（resolved）的问题同样保留在对账结论里，取上一份基线运行的完整内容；
    failed 运行没有问题清单，只占一行说明，避免与已确认问题混淆。
    """
    from src.services import finding_service

    runs = db.scalars(select(Run).order_by(Run.id)).all()
    headers = [
        "运行号", "运行状态", "问题身份键", "问题类型", "对账结论", "问题标题",
        "差异/错误类型", "严重度", "影响分", "涉及指标", "环路径", "明细摘要",
        "上一运行号",
    ]
    rows: list[list[object]] = []
    baseline_cache: dict[int, dict[str, dict]] = {}

    def baseline_index(run: Run) -> dict[str, dict]:
        baseline_no = (run.reconciliation or {}).get("baseline_run_no")
        if not baseline_no:
            return {}
        if run.id not in baseline_cache:
            baseline = db.scalar(select(Run).where(Run.run_no == baseline_no))
            baseline_cache[run.id] = {
                item["finding_key"]: item for item in (baseline.findings if baseline else [])
            }
        return baseline_cache[run.id]

    for run in runs:
        reconciliation = run.reconciliation or {}
        baseline_no = reconciliation.get("baseline_run_no") or ""
        state_by_key = {
            item["finding_key"]: state
            for state in ("new", "resolved", "continued")
            for item in reconciliation.get(state, [])
        }
        if run.status not in finding_service.CONCLUSIVE_STATUSES:
            rows.append(
                [
                    run.run_no,
                    RUN_STATUS_LABELS.get(run.status, run.status),
                    "", "", "",
                    "运行未完整完成，无问题清单（业务变更已回滚）",
                    "", "", "", "", "", "",
                    baseline_no,
                ]
            )
            continue
        previous = baseline_index(run)
        for finding in run.findings or []:
            rows.append(_finding_row(run, finding, state_by_key.get(finding["finding_key"]), baseline_no))
        # 解除的问题不在本次 findings 里，从上一份基线取完整内容补一行
        for item in reconciliation.get("resolved", []):
            finding = previous.get(item["finding_key"])
            if finding is not None:
                rows.append(_finding_row(run, finding, "resolved", baseline_no))
    return to_csv(headers, rows)


def _finding_row(run: Run, finding: dict, reconcile_state: str | None, baseline_no: str) -> list[object]:
    from src.services import finding_service

    detail = finding.get("detail") or {}
    if finding["finding_type"] == "cycle":
        summary_text = detail.get("path_text", "")
        path_text = detail.get("path_text", "")
    elif finding["finding_type"] == "parse_error":
        summary_text = " ".join(
            str(part)
            for part in (detail.get("error_code"), detail.get("error_message"))
            if part
        )
        path_text = ""
    else:
        summary_text = detail.get("summary", "")
        path_text = ""
    return [
        run.run_no,
        RUN_STATUS_LABELS.get(run.status, run.status),
        finding.get("finding_key", ""),
        finding_service.FINDING_TYPE_LABELS.get(finding["finding_type"], finding["finding_type"]),
        finding_service.RECONCILE_STATE_LABELS.get(reconcile_state or "", reconcile_state or ""),
        finding.get("title", ""),
        finding.get("kind_label") or "",
        finding.get("severity_label") or "",
        finding.get("score") if finding.get("score") is not None else "",
        " | ".join(finding.get("metric_codes") or []),
        path_text,
        summary_text,
        baseline_no,
    ]
