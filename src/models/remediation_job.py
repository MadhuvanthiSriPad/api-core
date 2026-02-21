"""RemediationJob model — tracks Devin-dispatched fix jobs per impacted repo."""

import enum
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Text, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PR_OPENED = "pr_opened"
    CI_FAILED = "ci_failed"
    NEEDS_HUMAN = "needs_human"
    GREEN = "green"


class RemediationJob(Base):
    __tablename__ = "remediation_jobs"

    job_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_id: Mapped[int] = mapped_column(Integer, ForeignKey("contract_changes.id"), nullable=False)
    target_repo: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED.value)
    devin_run_id: Mapped[str] = mapped_column(String(200), nullable=True)
    pr_url: Mapped[str] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=True)
    error_summary: Mapped[str] = mapped_column(Text, nullable=True)

    change = relationship("ContractChange", back_populates="remediation_jobs")
    audit_entries = relationship("AuditLog", back_populates="job", lazy="selectin")
