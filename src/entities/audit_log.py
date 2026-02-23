"""AuditLog model — records state transitions for remediation jobs."""

from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Text, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("remediation_jobs.job_id"), nullable=False
    )
    old_status: Mapped[str] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str] = mapped_column(String(30), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    detail: Mapped[str] = mapped_column(Text, nullable=True)

    job = relationship("RemediationJob", back_populates="audit_entries")
