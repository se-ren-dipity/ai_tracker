"""Curated events and the individual source sections supporting them."""
import json
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from backend.database import Base


class Insight(Base):
    __tablename__ = "insights"

    id = Column(Integer, primary_key=True)
    event_key = Column(String(200), unique=True, nullable=False)
    title = Column(Text, nullable=False)
    category = Column(String(30), nullable=False, index=True)
    event_kind = Column(String(30), default="other")
    summary = Column(Text)
    body_paragraphs = Column(Text, nullable=True)
    facts = Column(Text, default="[]")
    analysis = Column(Text)
    cautions = Column(Text, default="[]")
    next_step = Column(Text)
    keywords = Column(Text, default="[]")
    model_names = Column(Text, default="[]")
    priority = Column(Integer, default=50)
    published = Column(DateTime, index=True)
    related_model_id = Column(Integer, nullable=True)
    generated_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "category": self.category,
            "summary": self.summary, "facts": json.loads(self.facts or "[]"),
            "body_paragraphs": json.loads(self.body_paragraphs or "[]"),
            "analysis": self.analysis, "cautions": json.loads(self.cautions or "[]"),
            "next_step": self.next_step, "keywords": json.loads(self.keywords or "[]"),
            "model_names": json.loads(self.model_names or "[]"), "priority": self.priority,
            "published": self.published.isoformat() if self.published else None,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "related_model_id": self.related_model_id,
        }


class SourceItem(Base):
    __tablename__ = "source_items"

    id = Column(Integer, primary_key=True)
    article_id = Column(Integer, ForeignKey("articles.id"), nullable=False, index=True)
    section_key = Column(String(150), unique=True, nullable=False)
    section_id = Column(String(80))
    source_section = Column(String(100))
    title = Column(Text, nullable=False)
    body_text = Column(Text, nullable=False)
    links = Column(Text, default="[]")
    published = Column(DateTime)
    status = Column(String(20), default="pending", index=True)
    reason = Column(Text)
    insight_id = Column(Integer, ForeignKey("insights.id"), nullable=True, index=True)
    analyzed_at = Column(DateTime)
    analysis_json = Column(Text)
    model_discovery = Column(Text)
