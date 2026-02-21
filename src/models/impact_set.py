"""ImpactSet model — maps contract changes to affected caller services."""

from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Integer, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class ImpactSet(Base):
    __tablename__ = "impact_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_id: Mapped[int] = mapped_column(Integer, ForeignKey("contract_changes.id"), nullable=False)
    route_template: Mapped[str] = mapped_column(String(500), nullable=False)
    caller_service: Mapped[str] = mapped_column(String(100), nullable=False)
    calls_last_7d: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[str] = mapped_column(String(20), default="high")
    notes: Mapped[str] = mapped_column(Text, nullable=True)

    change = relationship("ContractChange", back_populates="impact_sets")
