"""一次校验运行：解析全部口径 DSL → 重建依赖图 → 环检测 → 冲突与影响面。

每次成功运行都在 ``findings`` 里留下**带稳定身份键**的完整问题清单
（口径冲突 / 依赖环 / 解析失败三类分开记录），``reconciliation`` 保存与上一份
有结论运行按身份键对账出的新增 / 解除 / 延续，页面与导出共用同一份结论。

运行中途失败时，业务变更（依赖边重建、公式解析状态）随 SAVEPOINT 回滚，
failed 记录只写错误信息，不携带 findings，也不参与对账。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    input_count: Mapped[int] = mapped_column(Integer, default=0)
    output_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    # 本次运行检出的完整问题清单（按 finding_type 分组，每条带 finding_key 稳定身份）
    findings: Mapped[list] = mapped_column(JSON, default=list)
    # 与上一份有结论运行的对账：baseline_run_no / counts / new / resolved / continued
    reconciliation: Mapped[dict] = mapped_column(JSON, default=dict)
    log_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
