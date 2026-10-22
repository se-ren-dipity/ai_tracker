"""Durable model tasks, field observations and article associations."""
import json
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from backend.database import Base


class ModelTask(Base):
    __tablename__ = "model_tasks"
    id = Column(Integer, primary_key=True)
    pending_id = Column(Integer, ForeignKey("pending_models.id"), unique=True, nullable=False)
    status = Column(String(20), default="queued", index=True)
    stage = Column(String(20), default="research")
    reason = Column(String(100))
    priority = Column(Integer, default=0)
    attempts = Column(Integer, default=0)
    next_run_at = Column(DateTime, default=datetime.utcnow)
    lease_until = Column(DateTime)
    owner = Column(String(80))
    checkpoint = Column(Text)
    error = Column(Text)
    requested_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)

    def to_dict(self):
        return {key: (value.isoformat() if isinstance(value, datetime) else value)
                for key in ("id", "pending_id", "status", "stage", "reason", "attempts", "next_run_at", "error", "started_at", "finished_at")
                for value in [getattr(self, key)]}


class ModelObservation(Base):
    __tablename__ = "model_observations"
    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=False, index=True)
    fingerprint = Column(String(64), unique=True, nullable=False)
    field = Column(String(40), nullable=False)
    value = Column(Text)
    previous_value = Column(Text)
    state = Column(String(25), default="known")
    status = Column(String(20), default="accepted", index=True)
    url = Column(Text)
    quote = Column(Text)
    observed_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime)

    def to_dict(self):
        return {"id": self.id, "model_id": self.model_id, "field": self.field,
                "value": json.loads(self.value or "null"), "previous_value": json.loads(self.previous_value or "null"),
                "state": self.state, "status": self.status, "url": self.url, "quote": self.quote,
                "observed_at": self.observed_at.isoformat(),
                "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None}


class ModelInsight(Base):
    __tablename__ = "model_insights"
    model_id = Column(Integer, ForeignKey("models.id"), primary_key=True)
    insight_id = Column(Integer, ForeignKey("insights.id"), primary_key=True)
    seen_at = Column(DateTime)
