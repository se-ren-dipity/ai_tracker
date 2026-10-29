"""Public Models Table snapshots and the fields they maintain."""
from sqlalchemy import Column, DateTime, ForeignKey, String, Text, Integer

from backend.database import Base

SOURCE_URL = "https://lifearchitect.ai/models-table/"


class ModelTableEntry(Base):
    __tablename__ = "model_table_entries"

    source_key = Column(String(500), primary_key=True)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=False, index=True)
    public_row = Column(Text, nullable=False)
    applied_values = Column(Text, nullable=False, default="{}")
    synced_at = Column(DateTime, nullable=False)
