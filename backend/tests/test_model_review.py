import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import database
from backend.collector import model_review as review
from backend.collector.import_models_table import make_draft
from backend.models.model_db import AIModel
from backend.models.pending import PendingModel
from backend.models.model_task import ModelTask
from backend.routers.pending import router


def decision(identifier=1, **overrides):
    return review.Decision(**({"item_id": identifier, "decision": "approved", "type": "LLM",
                              "reason": "来源明确给出模型身份与公开发布信息", "summary": "用于文本生成与工具调用的语言模型。"} | overrides))


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.research_patch = patch.object(review, "research_model", AsyncMock(return_value={"status": "complete", "sources": [], "updates": [], "identity": []}))
        self.research_patch.start()
        self.addCleanup(self.research_patch.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        database.Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, autoflush=False)
        with self.session() as db:
            db.add(PendingModel(draft=json.dumps({"name": "Example-v2", "type": "Unknown"}), evidence='{"released":true}'))
            db.commit()
        app = FastAPI()
        app.include_router(router)
        def get_db():
            with self.session() as db:
                yield db
        app.dependency_overrides[database.get_db] = get_db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        review.review_state.update(running=False, last_error=None)

    async def test_automatic_approval_is_idempotent_and_preserves_audit(self):
        with patch.object(review, "SessionLocal", self.session), patch.object(review, "judge_batch", AsyncMock(return_value=[decision()])) as judge:
            self.assertEqual((await review.review_models([1]))["approved"], 1)
            await review.review_models([1])
            judge.assert_awaited_once()
        with self.session() as db:
            model = db.query(AIModel).one()
            row = db.get(PendingModel, 1)
            self.assertEqual(model.type, "LLM")
            self.assertIsNone(model.params_b)
            self.assertIsNotNone(row.reviewed_at)
            self.assertEqual(row.reviewer, "deepseek-chat")
            self.assertEqual(row.model_id, model.id)
        data = self.client.get("/api/pending?status=approved").json()
        self.assertEqual(data["counts"]["approved"], 1)
        self.assertTrue(data["items"][0]["review_reason"])
        self.assertEqual(self.client.post("/api/pending/1/reject").status_code, 409)

    async def test_failed_response_stays_retryable_and_deferred_does_not_publish(self):
        with patch.object(review, "SessionLocal", self.session), patch.object(review, "judge_batch", AsyncMock(side_effect=ValueError("invalid"))):
            result = await review.review_models([1])
        self.assertEqual(result["failed"], 1)
        with self.session() as db:
            self.assertIsNone(db.get(PendingModel, 1).reviewed_at)
            self.assertEqual(db.query(AIModel).count(), 0)
        with patch.object(review, "SessionLocal", self.session), patch.object(review, "judge_batch", AsyncMock(return_value=[decision(decision="deferred", type="Unknown", summary="", reason="缺少模型身份依据")])):
            await review.review_models([1])
        with self.session() as db:
            self.assertEqual(db.get(PendingModel, 1).status, "draft")
            self.assertEqual(db.get(PendingModel, 1).review_decision, "deferred")
            self.assertEqual(db.query(AIModel).count(), 0)

    def test_manual_approval_and_name_alias_cannot_duplicate_model(self):
        first = self.client.post("/api/pending/1/approve").json()["model"]["id"]
        self.assertEqual(self.client.post("/api/pending/1/approve").json()["model"]["id"], first)
        with self.session() as db:
            db.add(PendingModel(draft='{"name":"EXAMPLE v2","type":"LLM"}'))
            db.commit()
        self.assertEqual(self.client.post("/api/pending/2/approve").json()["model"]["id"], first)
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)
        self.assertEqual(self.client.post("/api/pending/999/approve").status_code, 404)

    async def test_invalid_ids_and_duplicates_are_rejected(self):
        for values in ([decision(2).model_dump()], [decision(duplicate_of=99).model_dump()],
                       [decision().model_dump()] * 2, [decision(summary="").model_dump()]):
            response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps({"items": values})))])
            client = MagicMock()
            client.chat.completions.create = AsyncMock(return_value=response)
            with patch.object(review, "get_client", return_value=client):
                with self.assertRaises(ValueError):
                    await review.judge_batch([{"item_id": 1}], [])
            self.assertEqual(client.chat.completions.create.await_count, 2)

    def test_review_endpoint_persists_work_without_running_in_request(self):
        with patch.object(review, "SessionLocal", self.session), patch.object(review, "judge_batch", AsyncMock(return_value=[decision()])):
            response = self.client.post("/api/pending/review")
        self.assertEqual(response.json()["status"], "queued")
        self.assertFalse(review.review_state["running"])
        with self.session() as db:
            self.assertEqual(db.query(ModelTask).one().status, "queued")
            self.assertEqual(db.query(AIModel).count(), 0)

    async def test_closed_model_can_be_admitted_without_parameters_license_or_known_type(self):
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps({"items": [decision(type="Unknown").model_dump()]})))])
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=response)
        with patch.object(review, "get_client", return_value=client):
            decisions = await review.judge_batch([{"item_id": 1, "draft": {"name": "Example-v2", "params_b": None, "license": None}, "evidence": {"research": {"identity": [{"source_id": 1, "quote": "Example-v2 is our privately hosted model."}]}}}], [])
        with patch.object(review, "SessionLocal", self.session), patch.object(review, "judge_batch", AsyncMock(return_value=decisions)):
            await review.review_models([1])
        with self.session() as db:
            model = db.query(AIModel).one()
            self.assertEqual(model.type, "Unknown")
            self.assertIsNone(model.params_b)
            self.assertIsNone(model.license)

    async def test_existing_model_is_enriched_in_place_and_research_failure_keeps_it(self):
        model_id = self.client.post("/api/pending/1/approve").json()["model"]["id"]
        with self.session() as db:
            db.get(AIModel, model_id).params_b = 9
            db.commit()
            ids = review.queue_existing_models(db, model_id)
        findings = {"status": "complete", "sources": [], "updates": [{"field": "params_b", "value": 90}, {"field": "context_length", "value": 131072}]}
        with patch.object(review, "SessionLocal", self.session), patch.object(review, "research_model", AsyncMock(return_value=findings)), patch.object(review, "judge_batch", AsyncMock(return_value=[decision(duplicate_of=model_id)])):
            await review.review_models(ids, retry=True, include_existing=True)
        with self.session() as db:
            model = db.query(AIModel).one()
            self.assertEqual(model.id, model_id)
            self.assertEqual(model.params_b, 9)
            self.assertEqual(model.context_length, 131072)
        with patch.object(review, "SessionLocal", self.session), patch.object(review, "research_model", AsyncMock(side_effect=ValueError("offline"))), patch.object(review, "judge_batch", AsyncMock(return_value=[decision(decision="deferred", reason="联网资料暂不可用")])):
            await review.review_models(ids, retry=True, include_existing=True)
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertEqual(db.get(PendingModel, 1).status, "approved")
            self.assertEqual(db.query(AIModel).one().context_length, 131072)

    def test_enrich_endpoint_scopes_to_selected_model_and_reuses_review_record(self):
        with self.session() as db:
            db.add_all([AIModel(name="Seed model", type="LLM"), AIModel(name="Other model", type="LLM")])
            db.commit()
        findings = {"status": "complete", "sources": [], "updates": [{"field": "context_length", "value": 131072}]}
        with patch.object(review, "SessionLocal", self.session), patch.object(review, "research_model", AsyncMock(return_value=findings)), patch.object(review, "judge_batch", AsyncMock(return_value=[decision(2, duplicate_of=1)])):
            for _ in range(2):
                response = self.client.post("/api/pending/enrich?model_id=1")
                self.assertEqual(response.json(), {"status": "queued", "count": 1})
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 2)
            self.assertIsNone(db.get(AIModel, 1).context_length)
            self.assertIsNone(db.get(AIModel, 2).context_length)
            self.assertEqual(db.query(PendingModel).filter_by(model_id=1).count(), 1)
            self.assertEqual(db.query(ModelTask).count(), 1)

    async def test_estimates_and_locked_fields_are_not_imported_as_facts(self):
        row = dict(model="New Model", lab="Lab", playground="https://example.com", paper="https://example.com/report",
                   estimated_params="*", params_total="600", mmlu="", mmlu_pro="", gpqa="90", hle="",
                   release_date="2026-10-01", announced="Oct/2026", notes="estimated parameters",
                   license="🔒", context_window="🔒")
        draft, evidence = await make_draft(row, None)
        self.assertIsNone(draft["params_b"])
        self.assertIsNone(draft["license"])
        self.assertIsNone(draft["context_length"])
        self.assertEqual(draft["release_precision"], "month")
        self.assertEqual(evidence["public_row"]["params_total"], "600")

        row.update(estimated_params="", playground="https://huggingface.co/Lab/New-Model", params_total="552")
        client = MagicMock()
        client.get = AsyncMock(return_value=MagicMock(json=lambda: {"safetensors": {"total": 763205000000}}))
        draft, _ = await make_draft(row, client)
        self.assertEqual(draft["params_b"], 552)


if __name__ == "__main__":
    unittest.main()
