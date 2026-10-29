"""Offline, explicit identity repair and read-only database checks.

Run checks with ``python -m backend.maintenance [database]``. Merges are deliberately
not inferred from similar names; callers must back up, stop writers, and verify IDs.
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from backend.models.article import Article
from backend.models.insight import Insight
from backend.models.model_db import AIModel
from backend.models.model_event import ModelAlias, ModelEvent
from backend.models.model_table import ModelTableEntry
from backend.models.model_task import ModelInsight, ModelObservation
from backend.models.pending import PendingModel
from backend.registry import remember_alias


def merge_models(db, keep_id, remove_id):
    """Preserve evidence and relationships in the caller's transaction; never commit."""
    if keep_id == remove_id:
        raise ValueError("合并目标必须为两个不同模型")
    keep, old = db.get(AIModel, keep_id), db.get(AIModel, remove_id)
    if keep is None:
        raise ValueError("保留模型不存在")
    if old is None:
        return keep  # Replaying an already verified plan is harmless.
    for field in json.loads(old.locked_fields or "[]"):
        if getattr(keep, field) not in (None, "", "[]", "{}", "unknown", "Unknown", getattr(old, field)):
            raise ValueError(f"人工锁定字段 {field} 冲突，请先人工解决")
    locked = set(json.loads(keep.locked_fields or "[]")) | set(json.loads(old.locked_fields or "[]"))
    for column in AIModel.__table__.columns:
        field = column.name
        if field in {"id", "name", "full_name", "field_states", "locked_fields", "updated_at"}:
            continue
        if getattr(keep, field) in (None, "", "[]", "{}", "unknown", "Unknown"):
            setattr(keep, field, getattr(old, field))
    keep.locked_fields = json.dumps(sorted(locked))
    for name in (old.name, old.full_name):
        remember_alias(db, keep, name)
    db.flush()
    for row in db.query(ModelAlias).filter_by(model_id=old.id).all():
        remember_alias(db, keep, row.name)
        db.delete(row)
        db.flush()
    for row in db.query(ModelInsight).filter_by(model_id=old.id).all():
        existing = db.get(ModelInsight, (keep.id, row.insight_id))
        if existing:
            existing.seen_at = max(filter(None, [existing.seen_at, row.seen_at]), default=None)
            db.delete(row)
        else:
            row.model_id = keep.id
    for cls, field in ((PendingModel, "model_id"), (ModelTableEntry, "model_id"),
                       (Article, "related_model_id"), (Insight, "related_model_id")):
        db.query(cls).filter(getattr(cls, field) == old.id).update({field: keep.id}, synchronize_session="fetch")
    states = json.loads(keep.field_states or "{}")
    for row in db.query(ModelObservation).filter_by(model_id=old.id).all():
        value = json.loads(row.value or "null")
        signature = json.dumps([keep.id, row.field, row.state, value, row.url, row.quote], ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(signature.encode()).hexdigest()
        existing = db.query(ModelObservation).filter_by(fingerprint=fingerprint).first()
        if existing:
            db.delete(row)
        else:
            row.model_id, row.fingerprint = keep.id, fingerprint
            current = keep.to_dict().get(row.field)
            if row.state == "known" and row.status == "accepted" and current != value:
                row.status, row.resolved_at = "proposed", None
                row.previous_value = json.dumps(current, ensure_ascii=False)
                states[row.field] = "conflict"
        db.flush()
    keep.field_states = json.dumps(states, ensure_ascii=False)
    from backend.collector.model_events import EVENT_FIELDS
    for row in db.query(ModelEvent).filter_by(model_id=old.id).all():
        sources = json.loads(row.sources or "[]")
        canonical = sources[0]["url"].split("#")[0] if sources else None
        signature = json.dumps([keep.id, row.kind, row.occurred_on.isoformat(),
                                None if row.kind in EVENT_FIELDS or row.kind == "announced" else canonical])
        fingerprint = hashlib.sha256(signature.encode()).hexdigest()
        existing = db.query(ModelEvent).filter_by(fingerprint=fingerprint).first()
        if existing:
            merged = json.loads(existing.sources or "[]")
            existing.sources = json.dumps(merged + [s for s in sources if s not in merged], ensure_ascii=False)
            db.delete(row)
        else:
            row.model_id, row.fingerprint = keep.id, fingerprint
        db.flush()
    db.flush()
    db.delete(old)
    db.flush()
    return keep


def audit(path):
    """Inspect declared and soft references without creating or changing a database."""
    path = Path(path).resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        references = [(t, r[3], r[2], r[4]) for t in tables for r in db.execute(f'PRAGMA foreign_key_list("{t}")')]
        references += [("articles", "related_model_id", "models", "id"), ("insights", "related_model_id", "models", "id"),
                       ("pending_models", "model_id", "models", "id"), ("pending_models", "source_article_id", "articles", "id"),
                       ("pending_models", "source_insight_id", "insights", "id")]
        dangling = {}
        for table, field, target, key in references:
            count = db.execute(f'SELECT count(*) FROM "{table}" a LEFT JOIN "{target}" b ON a."{field}"=b."{key}" WHERE a."{field}" IS NOT NULL AND b."{key}" IS NULL').fetchone()[0]
            if count:
                dangling[f"{table}.{field}"] = count
        import re
        normalize = lambda value: re.sub(r"\s+", "", value or "").casefold()
        sources = {r[0]: r for r in db.execute("SELECT id,insight_id,title,body_text FROM source_items")}
        fact_issues = []
        for identifier, facts in db.execute("SELECT id,facts FROM insights"):
            for fact in json.loads(facts or "[]"):
                source = sources.get(fact.get("source_item_id"))
                if not source or source[1] != identifier or not normalize(fact.get("quote")) or normalize(fact.get("quote")) not in normalize(source[2] + source[3]):
                    fact_issues.append({"insight_id": identifier, "source_item_id": fact.get("source_item_id")})
        return {"integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
                "foreign_keys": db.execute("PRAGMA foreign_key_check").fetchall(), "dangling": dangling,
                "fact_issues": fact_issues,
                "counts": {t: db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables},
                "articles_without_sources": db.execute("SELECT count(*) FROM articles a WHERE NOT EXISTS (SELECT 1 FROM source_items s WHERE s.article_id=a.id)").fetchone()[0],
                "insights_without_sources": db.execute("SELECT count(*) FROM insights i WHERE NOT EXISTS (SELECT 1 FROM source_items s WHERE s.insight_id=i.id)").fetchone()[0]}


if __name__ == "__main__":
    from backend.database import DB_PATH
    result = audit(sys.argv[1] if len(sys.argv) > 1 else DB_PATH)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["integrity"] == "ok" and not result["foreign_keys"] and not result["dangling"] and not result["fact_issues"]
             and not result["articles_without_sources"] and not result["insights_without_sources"] else 1)
