"""Validated inputs, tool arguments and citation-based answers."""
from datetime import date
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(StrictModel):
    request_id: UUID
    version: int = Field(ge=1)
    message: str = Field(min_length=1, max_length=2000)


class Settings(StrictModel):
    enabled: bool = True
    web_enabled: bool = True
    session_limit: int = Field(20, ge=1, le=500)
    site_limit: int = Field(200, ge=1, le=10000)
    ip_limit: int = Field(100, ge=1, le=2000)
    minute_limit: int = Field(3, ge=1, le=30)
    max_concurrent: int = Field(3, ge=1, le=10)
    daily_calls: int = Field(600, ge=1, le=10000)
    input_budget: int = Field(2000000, ge=12000, le=100000000)
    output_budget: int = Field(200000, ge=2000, le=10000000)


class NewsQuery(StrictModel):
    terms: list[str] = Field(default_factory=list, max_length=6)
    date_from: date | None = None
    date_to: date | None = None
    category: Literal["model_release", "engineering", "industry"] | None = None
    offset: int = Field(0, ge=0, le=1000)
    limit: int = Field(8, ge=1, le=20)

    @model_validator(mode="after")
    def valid_range(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("开始日期不能晚于结束日期")
        if any(not word.strip() or len(word) > 100 for word in self.terms):
            raise ValueError("检索词需要 1–100 个字符")
        return self


class ModelQuery(StrictModel):
    query: str = Field(min_length=1, max_length=200)
    organization: str | None = Field(None, max_length=100)
    limit: int = Field(8, ge=1, le=20)


class ModelFilter(StrictModel):
    params_min: float | None = Field(None, ge=0)
    params_max: float | None = Field(None, ge=0)
    context_min: int | None = Field(None, ge=1)
    type: str | None = Field(None, max_length=30)
    weight_access: Literal["open", "closed"] | None = None
    offset: int = Field(0, ge=0, le=1000)
    limit: int = Field(8, ge=1, le=20)

    @model_validator(mode="after")
    def valid_range(self):
        if self.params_min is not None and self.params_max is not None and self.params_min > self.params_max:
            raise ValueError("参数范围颠倒")
        return self


class ModelID(StrictModel):
    model_id: int = Field(ge=1)


class NewsID(StrictModel):
    insight_id: int = Field(ge=1)


class EvidenceQuery(ModelID):
    fields: list[str] = Field(default_factory=list, max_length=13)


class WebQuery(StrictModel):
    query: str = Field(min_length=1, max_length=128)


class SourceID(StrictModel):
    source_id: str = Field(min_length=1, max_length=20)


class Block(StrictModel):
    text: str = Field(min_length=1, max_length=1800)
    citation_ids: list[str] = Field(default_factory=list, max_length=8)


class Card(StrictModel):
    kind: Literal["news", "model"]
    id: int = Field(ge=1)


class Answer(StrictModel):
    kind: Literal["news", "recommendation", "explanation", "chat"]
    blocks: list[Block] = Field(min_length=1, max_length=8)
    cards: list[Card] = Field(default_factory=list, max_length=5)
    limitations: list[str] = Field(default_factory=list, max_length=5)
    follow_up_question: str | None = Field(None, max_length=300)
