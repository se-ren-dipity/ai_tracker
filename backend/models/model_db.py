"""model_db.py — v0.2: added hf_downloads, hf_likes, confidence fields."""
import json
from datetime import datetime
from sqlalchemy import Column, Integer, Text, Float, DateTime, String, Date
from backend.database import Base


class AIModel(Base):
    __tablename__ = "models"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, nullable=False)
    full_name = Column(Text)
    organization = Column(String(100))
    type = Column(String(50), nullable=False, index=True)
    params_b = Column(Float)
    context_length = Column(Integer)
    license = Column(String(100))
    languages = Column(Text)        # JSON array
    modality_in = Column(Text)      # JSON array
    modality_out = Column(Text)     # JSON array
    benchmarks = Column(Text)       # JSON object
    hf_url = Column(Text)
    hf_downloads = Column(Integer)
    hf_likes = Column(Integer)
    paper_url = Column(Text)
    github_url = Column(Text)
    source_url = Column(Text)
    release_date = Column(Date)
    release_precision = Column(String(10), default="day")
    existence_status = Column(String(20), default="confirmed")
    weight_access = Column(String(20), default="unknown")
    api_availability = Column(String(20), default="unknown")
    announced_date = Column(Date)
    available_date = Column(Date)
    field_states = Column(Text, default="{}")
    locked_fields = Column(Text, default="[]")
    last_checked_at = Column(DateTime)
    next_review_at = Column(DateTime, index=True)
    notes = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "full_name": self.full_name,
            "organization": self.organization,
            "type": self.type,
            "params_b": self.params_b,
            "context_length": self.context_length,
            "license": self.license,
            "languages": json.loads(self.languages) if self.languages else [],
            "modality_in": json.loads(self.modality_in) if self.modality_in else [],
            "modality_out": json.loads(self.modality_out) if self.modality_out else [],
            "benchmarks": json.loads(self.benchmarks) if self.benchmarks else {},
            "hf_url": self.hf_url,
            "hf_downloads": self.hf_downloads,
            "hf_likes": self.hf_likes,
            "paper_url": self.paper_url,
            "github_url": self.github_url,
            "source_url": self.source_url,
            "release_date": self.release_date.isoformat() if self.release_date else None,
            "release_precision": self.release_precision or "day",
            "existence_status": self.existence_status or "confirmed",
            "weight_access": self.weight_access or "unknown",
            "api_availability": self.api_availability or "unknown",
            "announced_date": self.announced_date.isoformat() if self.announced_date else None,
            "available_date": self.available_date.isoformat() if self.available_date else None,
            "field_states": json.loads(self.field_states or "{}"),
            "locked_fields": json.loads(self.locked_fields or "[]"),
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "next_review_at": self.next_review_at.isoformat() if self.next_review_at else None,
            "notes": self.notes,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
