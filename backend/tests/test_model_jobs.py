import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import database
from backend.collector import model_jobs as jobs, model_review as review, model_worker as worker
from backend.collector.model_profiles import record_research, resolve_observation, link_model
from backend.models.model_db import AIModel
from backend.models.model_task import ModelTask, ModelObservation, ModelInsight
from backend.models.pending import PendingModel
from backend.models.insight import Insight
from backend.routers.models import router as models_router
from backend.routers.insights import router as insights_router
from backend.routers.pending import router as pending_router


def findings(**extra):
    return {"status": "complete", "identity": [{"source_id": 1, "quote": "Example v2 is a model"}], "checked_at": datetime.utcnow().isoformat(), "updates": [
        {"field": "context_length", "value": 131072, "url": "https://example.com/model", "quote": "Example v2 has 128K context"}], **extra}


class ModelJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        database.Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, autoflush=False)
        with self.session() as db:
            db.add(PendingModel(draft='{"name":"Example v2","type":"LLM"}'))
            db.commit()
            jobs.enqueue(db, 1)
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_queue_is_idempotent_and_only_one_worker_can_claim(self):
        with self.session() as db:
            jobs.enqueue(db, 1)
            db.commit()
            self.assertEqual(db.query(ModelTask).count(), 1)
            self.assertEqual(jobs.claim_task(db, "first").id, 1)
        with self.session() as other:
            self.assertIsNone(jobs.claim_task(other, "second"))

    def test_expired_lease_preserves_checkpoint_and_fences_old_owner(self):
        with self.session() as db:
            task = jobs.claim_task(db, "old")
            jobs.checkpoint_task(db, task.id, "old", findings())
            task.lease_until = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        with self.session() as db:
            resumed = jobs.claim_task(db, "new")
            self.assertEqual(resumed.stage, "decision")
            self.assertTrue(resumed.checkpoint)
            with self.assertRaises(jobs.LeaseLost):
                jobs.checkpoint_task(db, resumed.id, "old", {})
            jobs.heartbeat(db, resumed.id, "new")

    def test_interrupted_task_resumes_before_new_high_priority_work(self):
        with self.session() as db:
            task = jobs.claim_task(db, "old")
            jobs.checkpoint_task(db, task.id, "old", findings())
            task.lease_until = datetime.utcnow() - timedelta(seconds=1)
            db.add(PendingModel(draft='{"name":"New model","type":"LLM"}'))
            db.commit()
            jobs.enqueue(db, 2, priority=100)
            db.commit()
            resumed = jobs.claim_task(db, "new")
            self.assertEqual(resumed.pending_id, 1)
            self.assertEqual(resumed.stage, "decision")

    async def test_failed_decision_resumes_without_repeating_paid_research(self):
        decision = review.Decision(item_id=1, decision="approved", type="LLM", reason="官方确认模型身份", summary="一个文本模型")
        with patch.object(review, "SessionLocal", self.session), patch.object(worker, "SessionLocal", self.session), patch.object(review, "research_model", AsyncMock(return_value=findings())) as research, patch.object(review, "judge_batch", AsyncMock(side_effect=[ValueError("bad JSON"), [decision]])):
            with self.session() as db:
                jobs.claim_task(db, "first")
            await worker.run_task(1, 1, "first")
            with self.session() as db:
                task = db.get(ModelTask, 1)
                self.assertEqual(task.status, "retry")
                self.assertEqual(task.stage, "decision")
                task.next_run_at = datetime.utcnow() - timedelta(seconds=1)
                db.commit()
            with self.session() as db:
                jobs.claim_task(db, "restart")
            await worker.run_task(1, 1, "restart")
            research.assert_awaited_once()
        with self.session() as db:
            self.assertEqual(db.get(ModelTask, 1).status, "done")
            self.assertEqual(db.query(AIModel).one().context_length, 131072)
            self.assertEqual(db.query(ModelObservation).count(), 1)

    def test_retries_are_bounded_and_manual_retry_resets_budget(self):
        for attempt in range(3):
            with self.session() as db:
                task = db.get(ModelTask, 1)
                task.next_run_at = datetime.utcnow() - timedelta(seconds=1)
                db.commit()
                jobs.claim_task(db, "worker")
                jobs.fail_task(db, 1, "worker", ValueError())
        with self.session() as db:
            self.assertEqual(db.get(ModelTask, 1).status, "failed")
            self.assertIsNone(jobs.claim_task(db, "another"))
            jobs.enqueue(db, 1)
            db.commit()
            self.assertEqual(db.get(ModelTask, 1).attempts, 0)
            self.assertIsNotNone(jobs.claim_task(db, "retry"))

    async def test_manual_rejection_wins_over_inflight_automatic_decision(self):
        async def manual_reject_then_return_decision(items, existing):
            with self.session() as db:
                row = db.get(PendingModel, 1)
                row.status, row.reviewer, row.reviewed_at = "rejected", "manual", datetime.utcnow()
                row.review_reason = "人工丢弃"
                db.commit()
            return [review.Decision(item_id=1, decision="approved", type="LLM", reason="自动通过", summary="模型")]
        with self.session() as db:
            jobs.claim_task(db, "worker")
        with patch.object(review, "SessionLocal", self.session), patch.object(worker, "SessionLocal", self.session), \
             patch.object(review, "research_model", AsyncMock(return_value=findings())), \
             patch.object(review, "judge_batch", AsyncMock(side_effect=manual_reject_then_return_decision)):
            await worker.run_task(1, 1, "worker")
        with self.session() as db:
            row = db.get(PendingModel, 1)
            self.assertEqual((row.status, row.reviewer, row.review_reason), ("rejected", "manual", "人工丢弃"))
            self.assertEqual(db.query(AIModel).count(), 0)
            self.assertEqual(db.get(ModelTask, 1).status, "done")

    @patch.dict(os.environ, {"MODEL_AUTO_REVIEW_ENABLED": "true"})
    def test_opt_in_due_models_and_backlog_are_queued_but_not_recently_checked_models(self):
        now = datetime.utcnow()
        with self.session() as db:
            db.add_all([AIModel(name="Due", type="LLM", next_review_at=now-timedelta(days=1)),
                        AIModel(name="Fresh", type="LLM", next_review_at=now+timedelta(days=10))])
            db.add(PendingModel(draft='{"name":"Old draft"}'))
            db.commit()
            jobs.enqueue_due(db, now)
            jobs.enqueue_due(db, now)
            self.assertEqual(db.query(ModelTask).count(), 3)
            self.assertEqual(db.query(PendingModel).filter_by(model_id=2).count(), 0)

    def test_news_arriving_during_research_runs_again_after_completion(self):
        with self.session() as db:
            task = jobs.claim_task(db, "worker")
            task.started_at = datetime.utcnow() - timedelta(seconds=10)
            jobs.enqueue(db, 1, "相关报道更新", 10)
            jobs.finish_task(db, 1, "worker")
            db.commit()
            self.assertEqual(task.status, "queued")
            self.assertIsNone(task.checkpoint)
            self.assertIsNone(task.started_at)

    def test_conflicting_fact_requires_explicit_resolution_and_retains_history(self):
        with self.session() as db:
            model = AIModel(name="Example", type="LLM", context_length=32768)
            db.add(model)
            db.flush()
            evidence = findings()
            record_research(db, model, evidence)
            db.commit()
            change = db.query(ModelObservation).one()
            self.assertEqual(model.context_length, 32768)
            self.assertEqual(change.status, "proposed")
            resolve_observation(db, change, True)
            db.commit()
            self.assertEqual(model.context_length, 131072)
            self.assertEqual(json.loads(change.previous_value), 32768)
            record_research(db, model, evidence)
            db.commit()
            self.assertEqual(db.query(ModelObservation).count(), 1)

    def test_closed_models_remain_in_library_with_unknown_parameters_and_slower_review(self):
        with self.session() as db:
            model = AIModel(name="Private", type="Unknown")
            db.add(model)
            db.flush()
            data = findings(updates=[{"field":"weight_access", "value":"closed", "url":"https://example.com", "quote":"Private is a closed model"}],
                            field_statuses=[{"field":"params_b", "state":"undisclosed", "url":"https://example.com", "quote":"Parameters are not disclosed"}])
            record_research(db, model, data)
            db.commit()
            self.assertEqual(model.existence_status, "confirmed")
            self.assertIsNone(model.params_b)
            self.assertEqual(json.loads(model.field_states)["params_b"], "undisclosed")
            self.assertEqual((model.next_review_at-model.last_checked_at).days, 30)

    async def test_historical_insight_changes_do_not_queue_paid_research(self):
        from backend.collector.insight_models import queue_model_drafts
        with self.session() as db:
            model = AIModel(name="Example v2", type="LLM")
            story = Insight(event_key="engineering", title="框架适配 Example v2", category="engineering", model_names='["Example v2"]', generated_at=datetime.utcnow())
            db.add_all([model, story])
            db.commit()
        with patch('backend.collector.insight_models.SessionLocal', self.session):
            await queue_model_drafts()
            await queue_model_drafts()
            with self.session() as db:
                db.get(Insight, 1).generated_at = datetime.utcnow() + timedelta(seconds=1)
                db.commit()
            await queue_model_drafts()
        with self.session() as db:
            self.assertEqual(db.query(ModelTask).count(), 1)  # Only the manually queued fixture.
            self.assertEqual(db.query(PendingModel).filter_by(model_id=1).count(), 0)
            self.assertEqual(db.query(AIModel).count(), 1)

    def test_admin_can_accept_or_dismiss_corrections_and_stale_updates_are_blocked(self):
        app = FastAPI()
        app.include_router(pending_router)
        def get_db():
            with self.session() as db:
                yield db
        app.dependency_overrides[database.get_db] = get_db
        with self.session() as db:
            model = AIModel(name="Example", type="LLM", context_length=32768)
            db.add(model)
            db.flush()
            record_research(db, model, findings())
            db.commit()
        with TestClient(app) as client:
            item = client.get('/api/pending/changes').json()['items'][0]
            self.assertEqual(item['previous_value'], 32768)
            with self.session() as db:
                db.get(AIModel, 1).context_length = 65536
                db.commit()
            self.assertEqual(client.post(f"/api/pending/changes/{item['id']}/accept").status_code, 409)
            self.assertEqual(client.post(f"/api/pending/changes/{item['id']}/dismiss").json()['status'], 'dismissed')
            self.assertEqual(client.get('/api/pending/changes').json()['items'], [])
        with self.session() as db:
            self.assertEqual(db.get(AIModel, 1).context_length, 65536)

    async def test_inconclusive_existing_review_schedules_later_instead_of_looping(self):
        with self.session() as db:
            model = AIModel(name="Example v2", type="LLM", next_review_at=datetime.utcnow()-timedelta(days=1))
            db.add(model)
            db.flush()
            row = db.get(PendingModel, 1)
            row.model_id, row.status = model.id, "approved"
            db.commit()
            jobs.claim_task(db, "worker")
        decision = review.Decision(item_id=1, decision="deferred", type="LLM", reason="本轮资料不足", summary="")
        with patch.object(review, "SessionLocal", self.session), patch.object(worker, "SessionLocal", self.session), patch.object(review, "research_model", AsyncMock(return_value=findings(updates=[]))), patch.object(review, "judge_batch", AsyncMock(return_value=[decision])):
            await worker.run_task(1, 1, "worker")
        with self.session() as db:
            jobs.enqueue_due(db)
            self.assertEqual(db.get(ModelTask, 1).status, "done")
            self.assertGreater(db.get(AIModel, 1).next_review_at, datetime.utcnow())
            self.assertEqual(db.get(PendingModel, 1).status, "approved")

    def test_model_and_article_readers_support_multiple_models_and_real_missing_pages(self):
        app = FastAPI()
        app.include_router(models_router)
        app.include_router(insights_router)
        def get_db():
            with self.session() as db:
                yield db
        app.dependency_overrides[database.get_db] = get_db
        with self.session() as db:
            db.add_all([AIModel(name="One", type="LLM"), AIModel(name="Two", type="LLM")])
            story = Insight(event_key="both", title="两个模型的新进展", category="engineering", summary="框架适配", generated_at=datetime.utcnow())
            db.add(story)
            db.flush()
            link_model(db, 1, story)
            link_model(db, 2, story)
            db.commit()
            self.assertEqual(db.query(ModelInsight).count(), 2)
        with TestClient(app) as client:
            self.assertEqual(len(client.get('/api/insights/1').json()['models']), 2)
            for model_id in (1,2):
                self.assertEqual(client.get(f'/api/models/{model_id}').json()['articles'][0]['id'], 1)
            self.assertEqual(client.get('/api/models/999').status_code, 404)


if __name__ == "__main__":
    unittest.main()
