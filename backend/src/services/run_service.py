"""校验运行服务：算法 A 解析全部口径 → 重建依赖图 → 算法 B 环检测 + 冲突 → 落统计。

事务与失败恢复：
- 解析状态回写、依赖边重建等**业务变更全部包在 SAVEPOINT（嵌套事务）里**；
- 运行未完整完成时 SAVEPOINT 回滚，公式解析状态与依赖边恢复到上一份可用历史，
  已确认的问题数据（历史运行的 findings）不受影响；
- SAVEPOINT 之外只保留一条 failed 运行记录（错误信息），它不携带问题清单、
  不参与对账，下一次成功运行仍以最近一份有结论运行为基线。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.models.formula import Formula
from src.models.metric import Metric
from src.models.run import Run
from src.services import (
    dependency_service,
    dsl_parser,
    finding_service,
    formula_service,
    graph_analysis,
    graph_service,
)
from src.utils.errors import not_found
from src.utils.timeutil import iso_utc, local_now, utcnow


def serialize(run: Run) -> dict:
    return {
        "id": run.id,
        "run_no": run.run_no,
        "trigger": run.trigger,
        "status": run.status,
        "operator_email": run.operator_email,
        "input_count": run.input_count,
        "output_count": run.output_count,
        "metrics": run.metrics or {},
        "findings": run.findings or [],
        "reconciliation": run.reconciliation or {},
        "log_text": run.log_text,
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
    }


def next_run_no(db: Session) -> str:
    prefix = f"V{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(Run).where(Run.run_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:04d}"


def latest_conclusive_run(db: Session, *, exclude_id: int | None = None) -> Run | None:
    """最近一份有结论（success / partial）的运行，即对账基线。"""
    stmt = select(Run).where(Run.status.in_(finding_service.CONCLUSIVE_STATUSES))
    if exclude_id is not None:
        stmt = stmt.where(Run.id != exclude_id)
    return db.scalar(stmt.order_by(Run.id.desc()).limit(1))


def _build_parse_error_details(metrics: list[Metric], latest: dict[int, Formula], parsed_by_code: dict[str, dict]) -> list[dict]:
    details = []
    for metric in metrics:
        parsed = parsed_by_code.get(metric.code) or {}
        if parsed.get("status") == "ok":
            continue
        formula = latest.get(metric.id)
        details.append(
            {
                "code": metric.code,
                "name": metric.name,
                "dsl_text": formula.dsl_text if formula else "",
                "error": parsed.get("error") or {},
            }
        )
    return details


def execute_run(db: Session, run: Run) -> Run:
    run.status = "running"
    baseline = latest_conclusive_run(db, exclude_id=run.id)
    scanned = 0
    try:
        # 所有业务变更放进嵌套事务：异常时整体回滚到运行前状态
        with db.begin_nested():
            metrics = db.scalars(select(Metric).order_by(Metric.code)).all()
            scanned = len(metrics)
            latest = formula_service.latest_map(db)
            parsed_by_code: dict[str, dict] = {}
            for metric in metrics:
                formula: Formula | None = latest.get(metric.id)
                parsed = dsl_parser.parse_dsl(formula.dsl_text if formula else "", metric.code)
                parsed_by_code[metric.code] = parsed
                if formula is not None:
                    formula_service.apply_parse(formula, parsed)
            db.flush()

            dependency_stats = dependency_service.rebuild(db, metrics, parsed_by_code)
            payloads, reports_index, _ = graph_service.collect_inputs(db)
            summary = graph_analysis.summarize(payloads, reports_index)
            summary["parse_error_details"] = _build_parse_error_details(metrics, latest, parsed_by_code)

            findings = finding_service.build_findings(summary)
            if baseline is not None:
                reconciliation = finding_service.reconcile(
                    findings,
                    baseline.findings,
                    baseline_run_no=baseline.run_no,
                )
            else:
                # 首份有结论运行没有对账基线：不产生新增/解除/延续结论
                reconciliation = finding_service.empty_reconciliation()

            run.input_count = len(metrics)
            run.output_count = len(findings)
            run.findings = findings
            run.reconciliation = reconciliation
            run.metrics = {
                "metrics_total": summary["metrics_total"],
                "parsed_ok": len(metrics) - summary["parse_error_count"],
                "parse_error_count": summary["parse_error_count"],
                "parse_errors": summary["parse_errors"],
                "conflicts_total": summary["conflicts_total"],
                "cycles_total": summary["cycles_total"],
                "severity": summary["severity"],
                "dep_edges": dependency_stats["counts"],
                "reconciliation": reconciliation["counts"],
                "high_conflicts": [item["name"] for item in summary["conflicts"] if item["severity"] == "high"],
                "cycle_paths": [" → ".join(item["path"]) for item in summary["cycles"]],
            }
            run.status = "success" if metrics else "partial"
            run.log_text = (
                f"解析 {len(metrics)} 个指标口径，失败 {summary['parse_error_count']} 个；"
                f"依赖边 {dependency_stats['total']} 条（指标 {dependency_stats['counts']['metric']} / "
                f"字段 {dependency_stats['counts']['field']} / 报表 {dependency_stats['counts']['report']}）；"
                f"同名冲突 {summary['conflicts_total']} 处，依赖环 {summary['cycles_total']} 个；"
                f"对账：新增 {reconciliation['counts']['new']} / 解除 {reconciliation['counts']['resolved']} / "
                f"延续 {reconciliation['counts']['continued']}；"
                f"新增边 {dependency_stats['added']}，清理边 {dependency_stats['removed']}"
            )
    except Exception as exc:  # noqa: BLE001 - 运行失败要留痕且回滚业务变更，不抛 500
        # SAVEPOINT 已回滚：公式解析状态、依赖边恢复到运行前；历史 findings 原样保留
        run.status = "failed"
        run.input_count = scanned  # 扫描口径数保留，问题清单因未完成而作废
        run.output_count = 0
        run.findings = []
        run.reconciliation = finding_service.empty_reconciliation()
        run.metrics = {"error": str(exc)[:300]}
        run.log_text = f"校验运行失败，业务变更已回滚（保留上一份可用历史）：{exc}"
    run.finished_at = utcnow()
    db.flush()
    return run


def create_and_execute(
    db: Session,
    *,
    trigger: str,
    operator_email: str,
    seed_key: str | None = None,
) -> Run:
    if seed_key:
        existing = db.scalar(select(Run).where(Run.seed_key == seed_key))
        if existing is not None:
            return existing
    run = Run(
        run_no=next_run_no(db),
        trigger=trigger,
        status="running",
        operator_email=operator_email,
        seed_key=seed_key,
    )
    db.add(run)
    db.flush()
    return execute_run(db, run)


def list_runs(db: Session, *, status: str | None = None, limit: int, offset: int) -> dict:
    stmt = select(Run)
    count_stmt = select(func.count()).select_from(Run)
    if status:
        stmt = stmt.where(Run.status == status)
        count_stmt = count_stmt.where(Run.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Run.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [serialize(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_run(db: Session, run_id: int) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise not_found("run", run_id)
    return run


def app_name() -> str:
    return settings.app_name
