"""Model identity aliases and dated lifecycle evidence."""
import json
from datetime import datetime

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from backend.database import Base


class ModelAlias(Base):
    __tablename__ = "model_aliases"
    __table_args__ = (UniqueConstraint("model_id", "key"),)
    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=False, index=True)
    name = Column(Text, nullable=False)
    key = Column(String(300), nullable=False, index=True)


class ModelEvent(Base):
    __tablename__ = "model_events"
    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=False, index=True)
    fingerprint = Column(String(64), unique=True, nullable=False)
    kind = Column(String(30), nullable=False)
    occurred_on = Column(Date, nullable=False)
    summary = Column(Text, nullable=False)
    sources = Column(Text, default="[]")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "date": self.occurred_on.isoformat(),
                "summary": self.summary, "sources": json.loads(self.sources or "[]")}
