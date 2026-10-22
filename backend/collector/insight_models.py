"""Feed model-release events into the automatic admission queue."""
import json
import logging
import re

from backend.collector.model_agent import enrich_model
from backend.database import SessionLocal
from backend.models.insight import Insight, SourceItem
from backend.models.model_db import AIModel
from backend.models.pending import PendingModel
from backend.collector.model_profiles import link_model

logger = logging.getLogger(__name__)


def model_key(name):
    return re.sub(r"[\W_]+", "", name.casefold())


async def queue_model_drafts():
    added_ids = []
    with SessionLocal() as db:
        events = [(row.id, row.category, json.loads(row.model_names or "[]")) for row in db.query(Insight).all()]
    for insight_id, category, names in events:
        for name in names:
            with SessionLocal() as db:
                model = next((row for row in db.query(AIModel).all() if model_key(row.name) == model_key(name)), None)
                if model:
                    from backend.collector.model_review import queue_existing_models
                    from backend.collector.model_jobs import enqueue
                    if link_model(db, model.id, db.get(Insight, insight_id)):
                        ids = queue_existing_models(db, model.id)
                        enqueue(db, ids[0], "相关报道更新", 10)
                    db.commit()
                    continue
                pending = next((row for row in db.query(PendingModel).all() if model_key(json.loads(row.draft).get("name", "")) == model_key(name)), None)
                if pending:
                    from backend.collector.model_jobs import enqueue
                    evidence = json.loads(pending.evidence or "{}")
                    versions = evidence.setdefault("insight_versions", {})
                    insight = db.get(Insight, insight_id)
                    version = insight.generated_at.isoformat() if insight.generated_at else ""
                    if versions.get(str(insight_id)) != version:
                        versions[str(insight_id)] = version
                        evidence["insight_ids"] = sorted(set(evidence.get("insight_ids", [])) | {insight_id})
                        pending.evidence = json.dumps(evidence, ensure_ascii=False)
                        if pending.status == "rejected":
                            pending.status = "draft"
                        enqueue(db, pending.id, "相关报道更新", 10)
                        db.commit()
                    continue
                if category != "model_release":
                    continue
                source = db.query(SourceItem).filter(SourceItem.insight_id == insight_id).first()
                article_id = source.article_id if source else None
            try:
                draft = await enrich_model(name)
                with SessionLocal() as db:
                    evidence = draft.pop("_evidence", {})
                    insight = db.get(Insight, insight_id)
                    evidence["insight_ids"] = [insight_id]
                    evidence["insight_versions"] = {str(insight_id): insight.generated_at.isoformat() if insight.generated_at else ""}
                    row = PendingModel(draft=json.dumps(draft, ensure_ascii=False),
                                        evidence=json.dumps(evidence, ensure_ascii=False),
                                        confidence=draft.get("confidence", "{}"),
                                        source_article_id=article_id, source_insight_id=insight_id)
                    db.add(row)
                    db.commit()
                    added_ids.append(row.id)
            except Exception as exc:
                logger.warning("Model enrichment failed for %s: %s", name, exc)
    from backend.collector.model_jobs import enqueue
    if added_ids:
        with SessionLocal() as db:
            for identifier in added_ids:
                enqueue(db, identifier, "发现新模型", 10)
            db.commit()
