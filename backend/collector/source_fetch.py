"""Bounded source downloads with validated caches and persistent health status."""
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx


def cache_dir():
    return Path(os.getenv("DB_PATH", "data/tracker.db")).parent / "source-cache"


def paths(url):
    key = hashlib.sha256(url.encode()).hexdigest()
    return cache_dir() / f"{key}.json"


def save_record(url, record):
    path = paths(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_record(url):
    path = paths(url)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        return record if isinstance(record, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def source_status():
    return [{key: value for key, value in json.loads(path.read_text(encoding="utf-8")).items()
             if key not in {"content", "credential_version"}} for path in sorted(cache_dir().glob("*.json"))]


async def fetch_source(name, url, parse, headers=None):
    """Cache only parsed data. A failed refresh remains visible even with a cache."""
    previous = read_record(url)
    from backend.collector.source_health import cooling_down, diagnosis, ServiceBlocked
    if cooling_down(previous):
        if previous.get("content"):
            return parse(previous["content"].encode("utf-8"))
        raise ServiceBlocked(previous)
    checked = datetime.now(timezone.utc).isoformat()
    record = {**previous, "name": name, "checked_at": checked}
    error = None
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 proxy=os.getenv("COLLECTOR_PROXY") or None) as client:
        for attempt in range(3):
            try:
                response = await client.get(url, headers=headers or {})
                response.raise_for_status()
                result = parse(response.content)
                record.update(status="ok", error=None, fetched_at=checked,
                              failure_kind=None, diagnosis=None, next_retry_at=None, http_status=None,
                              content=response.content.decode("utf-8-sig"))
                save_record(url, record)
                return result
            except (httpx.HTTPError, ValueError) as exc:
                error = exc
                code = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = isinstance(exc, httpx.RequestError) or code == 429 or (code and code >= 500)
                if code == 429:
                    break  # Retry-After is persisted rather than sleeping inside the collection worker.
                if not retryable or attempt == 2:
                    break
                await asyncio.sleep(attempt + 1)
    code = getattr(getattr(error, "response", None), "status_code", None)
    record.update(status="failed", error=str(code or type(error).__name__), **diagnosis(error))
    if previous.get("content"):
        try:
            result = parse(previous["content"].encode("utf-8"))
        except ValueError:
            pass
        else:
            record["status"] = "cached"
            save_record(url, record)
            return result
    save_record(url, record)
    raise error
