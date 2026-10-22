"""Backfill saved articles without rerunning classification or model enrichment.

Run from the project root: python -m backend.collector.backfill_content
"""
import asyncio

from dotenv import load_dotenv

load_dotenv()

from backend.collector.content import collect_article_content
from backend.collector.feeds import fetch_feed, load_sources
from backend.database import SessionLocal, init_db
from backend.models.article import Article


async def backfill_content():
    init_db()
    with SessionLocal() as db:
        articles = [article.to_dict() for article in db.query(Article).all()
                    if not article.content_text or article.content_source == "summary"]
    if not articles:
        print("All saved articles already have bodies.")
        return
    feeds = {}
    for source in load_sources():
        for article in await fetch_feed(source["name"], source["url"]):
            feeds[article["url"]] = article
    semaphore = asyncio.Semaphore(3)

    async def save(article):
        async with semaphore:
            fields = await collect_article_content(feeds.get(article["url"], article))
            with SessionLocal() as db:
                row = db.get(Article, article["id"])
                for name, value in fields.items():
                    setattr(row, name, value)
                db.commit()
            print(f"Article {article['id']}: {fields['content_source']}, {len(fields['content_text'])} characters", flush=True)

    await asyncio.gather(*(save(article) for article in articles))


if __name__ == "__main__":
    asyncio.run(backfill_content())
