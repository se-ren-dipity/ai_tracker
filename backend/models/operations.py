"""Persistent collection progress, worker health and administrator state."""
import json
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from backend.database import Base


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, nullable=False)
    status = Column(String(20), default="queued", index=True)
    attempts = Column(Integer, default=0)
    stages = Column(Text, default="{}")
    error = Column(Text)
    requested_at = Column(DateTime, default=datetime.utcnow)
    next_run_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)

    def to_dict(self):
        return {key: value.isoformat() + "Z" if isinstance(value, datetime) else value
                for key in ("id", "key", "status", "attempts", "error", "requested_at", "next_run_at", "started_at", "finished_at")
                for value in [getattr(self, key)]} | {"stages": json.loads(self.stages)}


class SourceSetting(Base):
    __tablename__ = "source_settings"
    key = Column(String(64), primary_key=True)
    enabled = Column(Boolean, default=True, nullable=False)


class WorkerStatus(Base):
    __tablename__ = "worker_status"
    role = Column(String(30), primary_key=True)
    state = Column(String(80))
    last_seen = Column(DateTime, nullable=False)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token_hash = Column(String(64), primary_key=True)
    credential_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id = Column(Integer, primary_key=True)
    address = Column(String(80), index=True)
    attempted_at = Column(DateTime, default=datetime.utcnow, index=True)


class AdminEvent(Base):
    __tablename__ = "admin_events"
    id = Column(Integer, primary_key=True)
    action = Column(String(200), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
