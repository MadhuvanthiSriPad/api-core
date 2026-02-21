"""AgentSession model — tracks individual AI agent runs."""

from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Float, Integer, ForeignKey, Text, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from src.database import Base


class SessionStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(50), ForeignKey("teams.id"), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "gpt-4o"
    status: Mapped[str] = mapped_column(String(20), default=SessionStatus.RUNNING.value)
    priority: Mapped[str] = mapped_column(String(20), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=True)

    # Token usage
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)

    # Cost (calculated from tokens)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)

    # Timing
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    # Metadata
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    tags: Mapped[str] = mapped_column(String(500), nullable=True)  # comma-separated

    team = relationship("Team", back_populates="sessions", lazy="selectin")
    token_events = relationship("TokenUsage", back_populates="session", lazy="selectin")
