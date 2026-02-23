"""ContractSnapshot model — stores versioned OpenAPI contract snapshots."""

from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class ContractSnapshot(Base):
    __tablename__ = "contract_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    git_sha: Mapped[str] = mapped_column(String(40), nullable=True)
