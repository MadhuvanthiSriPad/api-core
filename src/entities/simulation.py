"""ContractSimulation model — stores pre-merge blast radius predictions."""

from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Text, Integer, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class ContractSimulation(Base):
    __tablename__ = "contract_simulations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_id: Mapped[int] = mapped_column(Integer, ForeignKey("contract_changes.id"), nullable=False)
    service_name: Mapped[str] = mapped_column(String(200), nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="safe")
    breaking_issues_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    fields_affected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    routes_affected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    devin_analysis_id: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    change = relationship("ContractChange", back_populates="simulations")
