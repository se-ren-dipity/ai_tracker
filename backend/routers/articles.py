"""FastAPI routers for articles and daily digest."""
import json
import re
from datetime import date, datetime
from typing import Optional

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.article import Article
from backend.image_cache import cached_article_html, get_cached_image, image_key, image_nodes

router = APIRouter(prefix="/api/articles", tags=["articles"])


@router.get("")
def list_articles(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: Optional[str] = None,
    source: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Article)
    if category:
        query = query.filter(Article.category == category)
    if source:
        query = query.filter(Article.source == source)
    if q:
        query = query.filter(
            or_(Article.title.contains(q), Article.summary_zh.contains(q), Article.content_text.contains(q))
        )
    total = query.count()
    items = (
        query.order_by(Article.published.desc().nullslast(), Article.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"total": total, "page": page, "page_size": page_size, "items": [a.to_dict() for a in items]}


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    rows = db.query(Article.source, func.count(Article.id)).group_by(Article.source).all()
    return [{"source": r[0], "count": r[1]} for r in rows]


@router.get("/daily/{date_str}")
def daily_digest(date_str: str, db: Session = Depends(get_db)):
    """Return articles grouped by category for a given date (YYYY-MM-DD)."""
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        return {"error": "Invalid date format, use YYYY-MM-DD"}

    rows = (
        db.query(Article)
        .filter(func.date(Article.created_at) == target)
        .order_by(Article.relevance.desc())
        .all()
    )
    by_cat: dict = {}
    for r in rows:
        by_cat.setdefault(r.category or "other", []).append(r.to_dict())
    return {"date": date_str, "total": len(rows), "by_category": by_cat}


@router.get("/today")
def today_digest(db: Session = Depends(get_db)):
    return daily_digest(date.today().isoformat(), db)


@router.get("/{article_id}")
def get_article(article_id: int, db: Session = Depends(get_db)):
    article = db.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article.to_dict(include_content=True) | {"content_html": cached_article_html(article)}


@router.get("/{article_id}/images/{key}")
def get_article_image(article_id: int, key: str, db: Session = Depends(get_db)):
    article = db.get(Article, article_id) if re.fullmatch(r"[0-9a-f]{64}", key) else None
    if article is None:
        raise HTTPException(404, "Image not found")
    source = next((url for url in image_nodes(article.content_html).xpath(".//img/@src")
                   if image_key(url) == key), None)
    if source is None:
        raise HTTPException(404, "Image not found")
    db.close()  # Do not hold a SQLite connection while waiting for an image download.
    try:
        content, media_type = get_cached_image(source)
    except (httpx.HTTPError, OSError, ValueError):
        raise HTTPException(503, "图片暂时无法获取，请稍后重试", headers={"Retry-After": "60", "Cache-Control": "no-store"})
    return Response(content, media_type=media_type, headers={
        "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })
