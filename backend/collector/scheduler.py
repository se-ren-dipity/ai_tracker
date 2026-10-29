"""Idempotent source archiving; scheduling lives in collection_jobs."""
import logging

from backend.collector.content import collect_article_content
from backend.collector.feeds import article_key, fetch_feed, load_sources
from backend.collector.insight_agent import build_insights
from backend.database import SessionLocal
from backend.models.article import Article

logger = logging.getLogger(__name__)


async def collect_news(sources=None, analyze=True):
    with SessionLocal() as db:
        known_urls = {article_key(url) for url, in db.query(Article.url).all()}
    candidates = []
    for source in load_sources() if sources is None else sources:
        for item in await fetch_feed(source["name"], source["url"]):
            key = article_key(item["url"])
            if key in known_urls:
                continue
            known_urls.add(key)
            candidates.append(item)
    for item in candidates:
        body = await collect_article_content(item)
        with SessionLocal() as db:
            if not db.query(Article.id).filter(Article.url == item["url"]).first():
                db.add(Article(url=item["url"], title=item["title"], source=item["source"],
                               published=item.get("published"), raw_content=item.get("raw_content"),
                               summary_zh=item.get("raw_content"), category="other", **body))
                db.commit()
    logger.info("Saved %d new source articles", len(candidates))
    if analyze:
        await build_insights()
    return len(candidates)
