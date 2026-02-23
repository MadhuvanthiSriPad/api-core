"""Service dependency graph model for topological ordering."""

from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship
from src.database import Base


class ServiceDependency(Base):
    """Tracks which services depend on which other services."""

    __tablename__ = "service_dependencies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_name = Column(String, nullable=False)  # e.g., "billing-service"
    depends_on = Column(String, nullable=False)    # e.g., "api-core"
    dependency_type = Column(String, default="api_call")  # api_call, database, message_queue

    def __repr__(self):
        return f"<ServiceDependency({self.service_name} → {self.depends_on})>"
