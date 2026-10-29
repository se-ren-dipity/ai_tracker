"""Dated lifecycle events; only anchored evidence can advance current state."""
import hashlib
import json
import re
from datetime import date
from urllib.parse import urlsplit

from backend.models.model_event import ModelEvent
from backend.models.model_task import ModelObservation

EVENT_LABELS = {"announced": "首次公布", "weights_opened": "开放权重", "code_released": "代码发布",
                "api_available": "API 开放", "api_discontinued": "API 停止服务", "license_changed": "许可证调整"}
EVENT_FIELDS = {"weights_opened": ("weight_access", "open"), "api_available": ("api_availability", "available"),
                "api_discontinued": ("api_availability", "discontinued")}


def anchored_url(model, value):
    """Trust known document/repository paths, never a whole shared hosting domain."""
    target = urlsplit(value or "")
    for field in ("hf_url", "github_url", "paper_url", "source_url"):
        url = urlsplit(model.get(field) or "")
        path = url.path.rstrip("/")
        if not path or url.scheme != "https" or target.scheme != "https":
            continue
        if url.hostname in {"lifearchitect.ai", "models-table-data.adt-360.workers.dev"}:
            continue
        if url.hostname == target.hostname and (target.path.rstrip("/") == path or target.path.startswith(path + "/")):
            return True
    return False


def date_supported(value, quote):
    day = date.fromisoformat(value)
    formats = [value, f"{day.year}年{day.month}月{day.day}日", day.strftime("%B %d, %Y"), day.strftime("%b %d, %Y"),
               f"{day.strftime('%B')} {day.day}, {day.year}", f"{day.strftime('%b')} {day.day}, {day.year}"]
    compact = re.sub(r"\s+", "", quote).casefold()
    return any(re.sub(r"\s+", "", item).casefold() in compact for item in formats)


def record_events(db, model, research):
    for event in research.get("events", []):
        # Validation normally happens before persistence; this guard also protects replayed checkpoints.
        if event.get("kind") not in EVENT_LABELS or not event.get("verified"):
            continue
        canonical = event["url"].split("#")[0]
        # Reports of the same dated state transition share one event and retain all sources.
        signature = json.dumps([model.id, event["kind"], event["occurred_on"],
                                None if event["kind"] in EVENT_FIELDS or event["kind"] == "announced" else canonical])
        fingerprint = hashlib.sha256(signature.encode()).hexdigest()
        row = db.query(ModelEvent).filter_by(fingerprint=fingerprint).first()
        source = {"url": event["url"], "quote": event["quote"]}
        if row is None:
            row = ModelEvent(model_id=model.id, fingerprint=fingerprint, kind=event["kind"],
                             occurred_on=date.fromisoformat(event["occurred_on"]), summary=event["summary"], sources="[]")
            db.add(row)
        sources = json.loads(row.sources or "[]")
        if source not in sources:
            sources.append(source)
        row.sources = json.dumps(sources, ensure_ascii=False)
        db.flush()


def transition_supported(db, model, item, research):
    if item["field"] in json.loads(model.locked_fields or "[]"):
        return False
    for event in research.get("events", []):
        if (not event.get("verified") or EVENT_FIELDS.get(event["kind"]) != (item["field"], item["value"])
                or event["url"] != item.get("url") or not anchored_url(model.to_dict(), event["url"])):
            continue
        # Date and state must be supported by the same statement.
        if item.get("quote") not in event["quote"] and event["quote"] not in item.get("quote", ""):
            continue
        previous = db.query(ModelEvent).filter_by(model_id=model.id).filter(
            ModelEvent.kind.in_([kind for kind, pair in EVENT_FIELDS.items() if pair[0] == item["field"]]))
        previous_dates = [row.occurred_on.isoformat() for row in previous
                          if any(anchored_url(model.to_dict(), source["url"]) for source in json.loads(row.sources or "[]"))]
        if any(value >= event["occurred_on"] for value in previous_dates):
            continue
        # An undated current state may be newer than a late-arriving article.
        if not previous_dates and (not model.last_checked_at or model.last_checked_at.date().isoformat() >= event["occurred_on"]):
            continue
        return True
    return False
