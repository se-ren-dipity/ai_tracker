"""User-facing curated information; archived sources remain under /api/articles."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.collector.insight_agent import build_insights, load_profile, run_state
from backend.collector.insight_reader import reading_assets
from backend.database import get_db
from backend.models.article import Article
from backend.models.insight import Insight, SourceItem
from backend.models.model_task import ModelInsight
from backend.models.model_db import AIModel

router = APIRouter(prefix="/api/insights", tags=["insights"])


def source_refs(db, insight_id):
    rows = db.query(SourceItem, Article).join(Article, SourceItem.article_id == Article.id).filter(SourceItem.insight_id == insight_id).all()
    return [{"id": item.id, "article_id": article.id, "section_id": item.section_id,
             "title": item.title, "source": article.source, "url": article.url,
             "published": item.published.isoformat() if item.published else None}
            for item, article in rows]


@router.get("/status")
def status(db: Session = Depends(get_db)):
    counts = dict(db.query(SourceItem.status, func.count(SourceItem.id)).group_by(SourceItem.status).all())
    dates = [row[0] for row in db.query(func.date(SourceItem.published, "+8 hours")).distinct().order_by(func.date(SourceItem.published, "+8 hours").desc()).all() if row[0]]
    total = db.query(Insight).count()
    unwritten = db.query(Insight).filter(or_(Insight.body_paragraphs.is_(None), Insight.body_paragraphs == "[]")).count()
    articles = db.query(Article).count()
    prepared = db.query(SourceItem.article_id).distinct().count()
    return {**run_state, "profile": load_profile(), "articles": articles,
            "source_items": sum(counts.values()), "insights": total, "counts": counts, "unwritten": unwritten,
            "merged": counts.get("analyzed", 0) - total, "unprepared_articles": articles - prepared,
            "dates": dates}


@router.post("/refresh", status_code=202)
def refresh(background: BackgroundTasks):
    if not run_state["running"]:
        background.add_task(build_insights)
    return {"status": "processing"}


@router.get("/dates")
def publication_dates(db: Session = Depends(get_db)):
    days = db.query(func.date(SourceItem.published, "+8 hours")).filter(
        SourceItem.insight_id.isnot(None)).distinct().order_by(func.date(SourceItem.published, "+8 hours").desc()).all()
    return {"dates": [day for day, in days if day]}


@router.get("")
def list_insights(
    day: date | None = None,
    category: Literal["model_release", "engineering", "industry"] | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(Insight)
    if day:
        query = query.filter(Insight.id.in_(db.query(SourceItem.insight_id).filter(
            func.date(SourceItem.published, "+8 hours") == day.isoformat())))
    if category:
        query = query.filter(Insight.category == category)
    if q:
        query = query.filter(or_(Insight.title.contains(q), Insight.summary.contains(q),
                                Insight.analysis.contains(q), Insight.facts.contains(q)))
    total = query.count()
    order = [Insight.priority.desc(), Insight.id.desc()] if day else [Insight.published.desc(), Insight.priority.desc(), Insight.id.desc()]
    rows = query.order_by(*order).offset((page - 1) * page_size).limit(page_size).all()
    items = []
    for row in rows:
        data = row.to_dict()
        data["sources"] = source_refs(db, row.id)
        images = reading_assets(db, row.id)["images"]
        data["cover_image"] = images[0] if images else None
        data["cover_images"] = images
        items.append(data)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/{insight_id}")
def get_insight(insight_id: int, db: Session = Depends(get_db)):
    row = db.get(Insight, insight_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Insight not found")
    related = db.query(AIModel).filter(or_(AIModel.id.in_(db.query(ModelInsight.model_id).filter_by(insight_id=insight_id)),
                                         AIModel.id == row.related_model_id)).all()
    return {**row.to_dict(), "models": [{"id": model.id, "name": model.name} for model in related],
            "sources": source_refs(db, insight_id), **reading_assets(db, insight_id)}
