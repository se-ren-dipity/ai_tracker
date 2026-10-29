"""RSS / HTTP feed collector."""
import hashlib
import json
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import feedparser

from backend.collector.content import content_fields, plain_text
from backend.collector.source_fetch import fetch_source

logger = logging.getLogger(__name__)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def article_key(url: str) -> str:
    """Identify a WeChat article independently of tracking parameters."""
    parsed = urlsplit(url)
    if parsed.hostname == "mp.weixin.qq.com" and parsed.path.rstrip("/") == "/s":
        query = parse_qs(parsed.query)
        if all(query.get(key) for key in ("__biz", "mid", "idx")):
            return "wechat:" + json.dumps([query[key][0] for key in ("__biz", "mid", "idx")])
    return url


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
        return await fetch_source(source_name, url, lambda raw: parse_feed(source_name, raw),
                                  headers={"User-Agent": "AITracker/1.0"})
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        logger.warning("Failed to fetch %s: %s", source_name, status or type(e).__name__)
        return []


def parse_feed(source_name, raw):
    feed = feedparser.parse(raw)
    if not feed.version or (feed.bozo and not feed.entries):
        raise ValueError("返回内容不是有效的 RSS/Atom")
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
    if len(articles) != len(feed.entries):
        raise ValueError("RSS 条目缺少标题或链接，本轮不覆盖缓存")
    return articles


def load_sources(path: str = "backend/collector/sources.yaml") -> list[dict]:
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])
