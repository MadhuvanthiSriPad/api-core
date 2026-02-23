"""Team model — organizations using the AI agent platform."""

from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="pro")  # free, pro, enterprise
    monthly_budget: Mapped[float] = mapped_column(Float, default=1000.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    sessions = relationship("AgentSession", back_populates="team", lazy="selectin")
