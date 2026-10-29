import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.maintenance import merge_models
from backend.models.article import Article
from backend.models.insight import Insight
from backend.models.model_db import AIModel
from backend.models.model_event import ModelEvent
from backend.models.model_task import ModelInsight, ModelObservation
from backend.models.pending import PendingModel
from backend.registry import identity_matches
from backend.collector.model_profiles import record_research
from backend.collector.model_research import Findings, validate_findings


class MaintenanceTests(unittest.TestCase):
    def test_merge_preserves_links_aliases_conflicts_and_idempotent_evidence(self):
        engine = create_engine("sqlite://")
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine, autoflush=False)() as db:
            keep = AIModel(name="Kimi K3.1 Pro", organization="Moonshot AI", type="VLM", params_b=2800)
            old = AIModel(name="K3.1 Pro", organization="Moonshot AI", type="VLM")
            db.add_all([keep, old]); db.flush()
            old_id = old.id
            news = Insight(title="Release", category="model_release", event_key="one", related_model_id=old.id)
            db.add(news); db.flush()
            db.add_all([ModelInsight(model_id=keep.id, insight_id=news.id), ModelInsight(model_id=old.id, insight_id=news.id),
                        PendingModel(draft='{}', model_id=old.id)])
            research = {"status": "complete", "updates": [{"field": "params_b", "value": 3000, "url": "https://example.com/release", "quote": "3T parameters"}],
                        "events": [{"kind": "announced", "occurred_on": "2026-10-24", "summary": "发布", "url": "https://example.com/release", "quote": "release", "verified": True}]}
            record_research(db, old, research, initial=True); db.flush()
            merge_models(db, keep.id, old.id); db.commit()
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertEqual(db.query(ModelInsight).count(), 1)
            self.assertEqual(db.query(PendingModel).one().model_id, keep.id)
            self.assertEqual(news.related_model_id, keep.id)
            self.assertEqual(identity_matches(db, {"name": "K3.1 Pro", "organization": "月之暗面"})[0]["id"], keep.id)
            self.assertEqual(db.query(ModelObservation).one().status, "proposed")
            record_research(db, keep, research); db.flush()
            self.assertEqual(db.query(ModelObservation).count(), 1)
            self.assertEqual(db.query(ModelEvent).count(), 1)
            self.assertEqual(merge_models(db, keep.id, old_id).id, keep.id)
        engine.dispose()

    def test_future_weights_are_not_currently_open(self):
        for quote in ("Kimi K3.1 Pro 完整模型权重将于 11 月 4 日前发布。", "Kimi K3.1 Pro weights will be released soon."):
            findings = Findings.model_validate({"updates": [{"field": "weight_access", "value": "open", "source_id": 1, "quote": quote}]})
            result = validate_findings(findings, [{"id": 1, "url": "https://example.com", "content": quote}])
            self.assertEqual(result["updates"], [])
            self.assertEqual(len(result["rejected_updates"]), 1)
