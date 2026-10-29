"""Pending models table — Model Enrich Agent draft queue."""
import json
from datetime import datetime
from sqlalchemy import Column, Integer, Text, DateTime, String
from backend.database import Base


class PendingModel(Base):
    __tablename__ = "pending_models"

    id = Column(Integer, primary_key=True, index=True)
    draft = Column(Text, nullable=False)        # JSON: full model draft dict
    confidence = Column(Text)                   # JSON: per-field confidence levels
    source_article_id = Column(Integer, nullable=True)
    source_insight_id = Column(Integer, nullable=True)
    # draft | approved | rejected
    status = Column(String(20), default="draft")
    evidence = Column(Text)
    review_decision = Column(String(20))
    review_reason = Column(Text)
    reviewer = Column(String(30))
    reviewed_at = Column(DateTime)
    model_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        evidence = json.loads(self.evidence or "{}")
        research = evidence.get("research", {})
        return {
            "id": self.id,
            "draft": json.loads(self.draft) if self.draft else {},
            "confidence": json.loads(self.confidence) if self.confidence else {},
            "source_article_id": self.source_article_id,
            "source_insight_id": self.source_insight_id,
            "status": self.status,
            "review_decision": self.review_decision,
            "review_reason": self.review_reason,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "model_id": self.model_id,
            "maintenance": evidence.get("maintenance"),
            "research": {key: research.get(key) for key in ("status", "checked_at", "unresolved", "errors")}
                        | {"source_count": len(research.get("sources", [])),
                           "fields": [item["field"] for item in research.get("applied", [])]},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
