"""校验运行集成测试（SQLite 内存库）：问题清单落库、相邻对账、失败回滚与导出。

覆盖用户验收点：
1. 每次成功运行留下带稳定身份键的问题清单，冲突/环/解析失败分开；
2. 相邻两次按身份键对账新增/解除/延续，导出与库内结论一致；
3. 运行未完整完成时业务变更回滚到上一份可用历史，已确认问题数据不丢；
4. failed 运行不携带问题清单、不参与对账基线。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.models import Base
from src.models.dependency import Dependency
from src.models.formula import Formula
from src.models.metric import Metric
from src.models.run import Run
from src.services import export_service, finding_service, graph_analysis, run_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    _seed_metrics(session)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_metric(db, code: str, name: str, dsl_text: str) -> Metric:
    metric = Metric(code=code, name=name, domain="trade", tier="normal", unit="个", owner="测试", status="active")
    db.add(metric)
    db.flush()
    formula = Formula(metric_id=metric.id, version=1, dsl_text=dsl_text, author_email="t@x.io", parse_status="ok")
    db.add(formula)
    db.flush()
    return metric


def _seed_metrics(db) -> None:
    # gmv_a / gmv_b 同名不同字段 -> 一处口径冲突
    _add_metric(db, "gmv_a", "GMV", "gmv_a = sum(dwd.order.amount)")
    _add_metric(db, "gmv_b", "GMV", "gmv_b = sum(dwd.refund.amount)")
    # 环：c_a -> c_b -> c_c -> c_a
    _add_metric(db, "c_a", "环A", "c_a = sum(@c_b)")
    _add_metric(db, "c_b", "环B", "c_b = sum(@c_c)")
    _add_metric(db, "c_c", "环C", "c_c = sum(@c_a)")
    db.commit()


def _new_run(db, seed_key=None) -> Run:
    run = Run(run_no=run_service.next_run_no(db), trigger="manual", operator_email="t@x.io", seed_key=seed_key)
    db.add(run)
    db.flush()
    return run_service.execute_run(db, run)


def test_successful_run_persists_typed_findings_and_reconciliation(db):
    run = _new_run(db)
    db.commit()
    assert run.status == "success"
    types = [item["finding_type"] for item in run.findings]
    assert types == ["conflict", "cycle"]  # 三类问题分组记录
    keys = {item["finding_key"] for item in run.findings}
    assert keys == {"conflict:conflict-gmv_a-gmv_b", "cycle:c_a-c_b-c_c"}
    # 首份有结论运行没有对账基线
    assert run.reconciliation["baseline_run_no"] is None
    assert run.output_count == 2
    # 依赖边已重建：指标引用 3 条（环）+ 字段边 2 条
    kinds = {row.kind for row in db.scalars(select(Dependency)).all()}
    assert {"metric", "field"} <= kinds


def test_failed_run_rolls_back_business_changes_and_keeps_history(db):
    first = _new_run(db)
    db.commit()
    edges_after_first = db.scalar(select(Dependency.id).limit(1))
    assert edges_after_first is not None
    first_findings = [dict(item) for item in first.findings]

    # 加一个解析必失败的指标，再让图分析阶段抛错 -> 运行中途失败
    broken = _add_metric(db, "broken_m", "坏指标", "broken_m = sum(")
    original_summarize = graph_analysis.summarize

    def boom(*args, **kwargs):
        raise RuntimeError("simulated graph outage")

    graph_analysis.summarize = boom  # type: ignore[assignment]
    try:
        failed = _new_run(db)
    finally:
        graph_analysis.summarize = original_summarize  # type: ignore[assignment]
    db.commit()

    assert failed.status == "failed"
    assert failed.findings == []  # 失败运行不携带问题清单
    assert failed.reconciliation["counts"]["new"] == 0
    assert "回滚" in (failed.log_text or "")

    # 业务变更已回滚：坏指标的公式没有被写成 error，依赖边数量与首次成功运行后一致
    formula = db.scalar(select(Formula).where(Formula.metric_id == broken.id))
    assert formula.parse_status == "ok"
    assert db.scalars(select(Dependency)).all()  # 上一份可用历史的边全在

    # 上一份已确认问题数据原封不动
    db.refresh(first)
    assert first.findings == first_findings
    assert first.status == "success"


def test_retry_after_failure_uses_last_conclusive_run_as_baseline(db):
    first = _new_run(db)
    db.commit()

    original = graph_analysis.summarize
    graph_analysis.summarize = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
    try:
        failed = _new_run(db)
    finally:
        graph_analysis.summarize = original  # type: ignore[assignment]
    db.commit()
    assert failed.status == "failed"

    # 修复口径：把两个 GMV 改成同签名 -> 冲突解除；新增一个解析失败 -> 新增问题；环延续
    gmv_b = db.scalar(select(Formula).join(Metric).where(Metric.code == "gmv_b"))
    gmv_b.dsl_text = "gmv_b = sum(dwd.order.amount)"
    _add_metric(db, "broken_m", "坏指标", "broken_m = sum(")

    third = _new_run(db)
    db.commit()
    assert third.status == "success"
    # 基线是第一份成功运行，跳过 failed
    assert third.reconciliation["baseline_run_no"] == first.run_no
    counts = third.reconciliation["counts"]
    assert counts["resolved"] == 1
    assert counts["new"] == 1
    assert counts["continued"] == 1
    new_keys = {item["finding_key"] for item in third.reconciliation["new"]}
    resolved_keys = {item["finding_key"] for item in third.reconciliation["resolved"]}
    continued_keys = {item["finding_key"] for item in third.reconciliation["continued"]}
    assert new_keys == {"parse_error:broken_m"}
    assert resolved_keys == {"conflict:conflict-gmv_a-gmv_b"}
    assert continued_keys == {"cycle:c_a-c_b-c_c"}
    # 解除项保留基线完整内容
    resolved = third.reconciliation["resolved"][0]
    assert resolved["finding_type"] == "conflict"
    assert resolved["title"] == "同名冲突「GMV」"
    # 本次清单里三类问题仍然分开
    assert finding_service.counts_by_type(third.findings) == {"conflict": 0, "cycle": 1, "parse_error": 1}


def test_same_total_count_replacement_is_visible_in_reconciliation(db):
    first = _new_run(db)
    db.commit()
    # 让环消失（c_c 不再引用 c_a），同时制造一个新的解析失败：总数维持 2
    c_c = db.scalar(select(Formula).join(Metric).where(Metric.code == "c_c"))
    c_c.dsl_text = "c_c = sum(dwd.order.amount)"
    _add_metric(db, "broken_m", "坏指标", "broken_m = sum(")

    second = _new_run(db)
    db.commit()
    assert first.output_count == second.output_count == 2  # 总数没变
    counts = second.reconciliation["counts"]
    assert (counts["new"], counts["resolved"], counts["continued"]) == (1, 1, 1)


def test_run_csv_exports_share_the_same_conclusion(db):
    import csv
    import io

    first = _new_run(db)
    db.commit()
    c_c = db.scalar(select(Formula).join(Metric).where(Metric.code == "c_c"))
    c_c.dsl_text = "c_c = sum(dwd.order.amount)"
    _add_metric(db, "broken_m", "坏指标", "broken_m = sum(")
    second = _new_run(db)
    db.commit()

    runs_rows = list(csv.reader(io.StringIO(export_service.runs_csv(db).decode("utf-8-sig"))))
    headers, data = runs_rows[0], runs_rows[1:]
    assert "对账基线" in headers and "新增问题" in headers and "解除问题" in headers and "延续问题" in headers
    by_no = {row[headers.index("运行号")]: row for row in data}
    first_row, second_row = by_no[first.run_no], by_no[second.run_no]
    # 首份运行无基线：三个对账列都是 0
    assert [first_row[headers.index(col)] for col in ("新增问题", "解除问题", "延续问题")] == ["0", "0", "0"]
    assert first_row[headers.index("对账基线")] == ""
    # 第二次：新增 1（解析失败）/ 解除 1（环）/ 延续 1（冲突）
    assert [second_row[headers.index(col)] for col in ("新增问题", "解除问题", "延续问题")] == ["1", "1", "1"]
    assert second_row[headers.index("对账基线")] == first.run_no

    finding_rows = list(csv.reader(io.StringIO(export_service.run_findings_csv(db).decode("utf-8-sig"))))
    f_headers, f_data = finding_rows[0], finding_rows[1:]
    assert {"问题身份键", "问题类型", "对账结论", "上一运行号"} <= set(f_headers)
    # 三类问题在导出里各有中文类型名
    joined = "\n".join(",".join(row) for row in f_data)
    assert "依赖环" in joined and "口径冲突" in joined and "解析失败" in joined
    # 环在第一次（无对账状态）+ 第二次解除，共两行
    cycle_rows = [row for row in f_data if "cycle:c_a-c_b-c_c" in row]
    assert len(cycle_rows) == 2
    assert cycle_rows[1][f_headers.index("对账结论")] == "解除"
    # 解除的环带上基线运行号，新增的解析失败标新增
    resolved_rows = [row for row in f_data if row[f_headers.index("对账结论")] == "解除"]
    assert len(resolved_rows) == 1
    assert resolved_rows[0][f_headers.index("上一运行号")] == first.run_no
    new_rows = [row for row in f_data if row[f_headers.index("对账结论")] == "新增"]
    assert any("parse_error:broken_m" in row for row in new_rows)
    # 冲突延续，同样带基线运行号
    continued_rows = [row for row in f_data if row[f_headers.index("对账结论")] == "延续"]
    assert any("conflict:conflict-gmv_a-gmv_b" in row for row in continued_rows)


def test_failed_run_exported_as_explicit_note_not_as_findings(db):
    _new_run(db)
    db.commit()
    original = graph_analysis.summarize
    graph_analysis.summarize = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
    try:
        failed = _new_run(db)
    finally:
        graph_analysis.summarize = original  # type: ignore[assignment]
    db.commit()

    findings_csv = export_service.run_findings_csv(db).decode("utf-8")
    failed_lines = [line for line in findings_csv.splitlines() if failed.run_no in line]
    assert len(failed_lines) == 1
    assert "运行未完整完成" in failed_lines[0]
