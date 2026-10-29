"""Configured sources with persistent administrator overrides."""
import hashlib

from backend.collector.feeds import load_sources
from backend.collector.import_models_table import PUBLIC_FEED_URL
from backend.collector.source_fetch import read_record
from backend.models.operations import SourceSetting


def configured_sources(db):
    settings = {row.key: row.enabled for row in db.query(SourceSetting).all()}
    sources = [{"name": "Models Table", "url": PUBLIC_FEED_URL, "type": "models_table"}, *load_sources()]
    return [{**source, "key": (key := hashlib.sha256(source["url"].encode()).hexdigest()),
             "enabled": settings.get(key, True)} for source in sources]


def source_overview(db):
    return [{"key": source["key"], "name": source["name"], "type": source["type"], "enabled": source["enabled"],
             **{key: value for key, value in read_record(source["url"]).items() if key in
                ("status", "checked_at", "fetched_at", "error", "failure_kind", "diagnosis", "next_retry_at")}} for source in configured_sources(db)]
