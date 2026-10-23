"""Daily collection followed by the personal information analysis agent."""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.collector.content import collect_article_content
from backend.collector.feeds import fetch_feed, load_sources
from backend.collector.insight_agent import build_insights
from backend.database import SessionLocal
from backend.models.article import Article

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
MAX_PER_RUN = 30


async def collect_and_analyze():
    with SessionLocal() as db:
        known_urls = {url for url, in db.query(Article.url).all()}
    candidates = []
    for source in load_sources():
        for item in await fetch_feed(source["name"], source["url"]):
            if item["url"] in known_urls:
                continue
            known_urls.add(item["url"])
            candidates.append(item)
            if len(candidates) >= MAX_PER_RUN:
                break
        if len(candidates) >= MAX_PER_RUN:
            break
    for item in candidates:
        body = await collect_article_content(item)
        with SessionLocal() as db:
            if not db.query(Article.id).filter(Article.url == item["url"]).first():
                db.add(Article(url=item["url"], title=item["title"], source=item["source"],
                               published=item.get("published"), raw_content=item.get("raw_content"),
                               summary_zh=item.get("raw_content"), category="other", **body))
                db.commit()
    logger.info("Saved %d new source articles", len(candidates))
    await build_insights()


def start_scheduler():
    scheduler.add_job(collect_and_analyze, "cron", hour=10, minute=0,
                      id="daily_collect", replace_existing=True)
    scheduler.start()
    asyncio.create_task(collect_and_analyze())
    logger.info("Scheduler started — daily collection at 10:00 Asia/Shanghai")
