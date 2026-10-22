"""Daily digest table."""
import json
from datetime import datetime
from sqlalchemy import Column, Text, DateTime, Date
from backend.database import Base


class DailyDigest(Base):
    __tablename__ = "daily_digest"

    date = Column(Date, primary_key=True)
    content = Column(Text)      # JSON: {category: [article_dict, ...]}
    generated = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "date": self.date.isoformat() if self.date else None,
            "content": json.loads(self.content) if self.content else {},
            "generated": self.generated.isoformat() if self.generated else None,
        }
