"""Article ORM model — v0.2: added keywords and related_model_id."""
import json
from datetime import datetime
from sqlalchemy import Column, Integer, Text, Float, DateTime, String
from backend.database import Base


class Article(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(Text, unique=True, nullable=False, index=True)
    title = Column(Text, nullable=False)
    source = Column(String(100))
    published = Column(DateTime)
    raw_content = Column(Text)          # 原文摘要（橘鸦/AIHOT 的原文，不重写）
    summary_zh = Column(Text)           # 保留字段，存原文摘要
    content_html = Column(Text)         # 清理后的正文结构，使用本站样式渲染
    content_text = Column(Text)         # 完整正文，用于搜索
    content_source = Column(String(20)) # feed | page | summary
    content_fetched_at = Column(DateTime)
    category = Column(String(50))       # model_release | engineering | research | product | other
    tags = Column(Text)                 # JSON array — 粗分类标签
    keywords = Column(Text)             # JSON array — 技术关键词，用于前端高亮
    relevance = Column(Float, default=0.0)
    related_model_id = Column(Integer, nullable=True)  # 关联模型库条目
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self, include_content=False):
        result = {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "source": self.source,
            "published": self.published.isoformat() if self.published else None,
            "raw_content": self.raw_content,
            "summary_zh": self.summary_zh,
            "has_content": bool(self.content_text) and self.content_source != "summary",
            "content_source": self.content_source,
            "content_fetched_at": self.content_fetched_at.isoformat() if self.content_fetched_at else None,
            "category": self.category,
            "tags": json.loads(self.tags) if self.tags else [],
            "keywords": json.loads(self.keywords) if self.keywords else [],
            "relevance": self.relevance,
            "related_model_id": self.related_model_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_content:
            result.update(content_html=self.content_html, content_text=self.content_text)
        return result
