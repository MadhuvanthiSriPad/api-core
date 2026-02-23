"""ContractChange model — records detected API contract changes."""

from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Text, Integer, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class ContractChange(Base):
    __tablename__ = "contract_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_ref: Mapped[str] = mapped_column(String(40), nullable=True)
    head_ref: Mapped[str] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    is_breaking: Mapped[bool] = mapped_column(Boolean, default=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="low")
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    changed_routes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    changed_fields_json: Mapped[str] = mapped_column(Text, nullable=True)

    impact_sets = relationship("ImpactSet", back_populates="change", lazy="selectin")
    remediation_jobs = relationship("RemediationJob", back_populates="change", lazy="selectin")
