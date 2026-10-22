"""RSS / HTTP feed collector."""
import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

import feedparser
import httpx

from backend.collector.content import content_fields, plain_text

logger = logging.getLogger(__name__)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _parse_date(entry) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6])
            except Exception:
                pass
    return None


async def fetch_feed(source_name: str, url: str) -> list[dict]:
    """Fetch and parse an RSS/Atom feed. Returns list of raw article dicts."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "AITracker/1.0"})
            resp.raise_for_status()
            raw = resp.text
    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return []

    feed = feedparser.parse(raw)
    articles = []
    for entry in feed.entries:
        link = getattr(entry, "link", None)
        title = getattr(entry, "title", "").strip()
        if not link or not title:
            continue

        summary = (
            getattr(entry, "summary", None)
            or getattr(entry, "description", None)
            or ""
        )
        summary = plain_text(summary)
        full_content = next((part.get("value") for part in entry.get("content", []) if part.get("value")), "")

        articles.append(
            {
                "url": link,
                "title": title,
                "source": source_name,
                "published": _parse_date(entry),
                "raw_content": summary,
                **(content_fields(full_content, link, "feed") if full_content else {}),
            }
        )
    return articles


def load_sources(path: str = "backend/collector/sources.yaml") -> list[dict]:
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])
