"""Private visitor conversations and durable, atomically reserved assistant budgets."""
from datetime import datetime
from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint
from backend.database import Base


class AssistantSession(Base):
    __tablename__ = "assistant_sessions"
    id = Column(String(64), primary_key=True)
    version = Column(Integer, default=1, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class AssistantRequest(Base):
    __tablename__ = "assistant_requests"
    __table_args__ = (UniqueConstraint("session_id", "client_id"),)
    id = Column(String(36), primary_key=True)
    session_id = Column(String(64), nullable=False, index=True)
    client_id = Column(String(36), nullable=False)
    version = Column(Integer, nullable=False)
    day = Column(String(10), nullable=False, index=True)
    address = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default="running")
    phase = Column(String(20), default="thinking")
    message = Column(Text, nullable=False)
    result = Column(Text)
    error = Column(Text)
    trace = Column(Text, default="[]")
    model = Column(String(100))
    prompt_version = Column(String(20), default="0.3")
    calls = Column(Integer, default=0, nullable=False)
    input_tokens = Column(Integer, default=0, nullable=False)
    output_tokens = Column(Integer, default=0, nullable=False)
    web_calls = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at = Column(DateTime)


class AssistantUsage(Base):
    __tablename__ = "assistant_usage"
    key = Column(String(150), primary_key=True)
    count = Column(Integer, default=0, nullable=False)
    calls = Column(Integer, default=0, nullable=False)
    input_tokens = Column(Integer, default=0, nullable=False)
    output_tokens = Column(Integer, default=0, nullable=False)
    web_calls = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class AssistantConfig(Base):
    __tablename__ = "assistant_config"
    id = Column(Integer, primary_key=True)
    value = Column(Text, nullable=False)
