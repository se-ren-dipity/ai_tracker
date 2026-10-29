import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend import registry
from backend.collector import model_research, model_review, model_jobs, source_fetch, source_health
from backend.collector.model_profiles import record_research, resolve_observation, bootstrap_profiles
from backend.models.model_db import AIModel
from backend.models.model_event import ModelEvent
from backend.models.model_task import ModelObservation, ModelInsight
from backend.models.insight import Insight
from backend.models.pending import PendingModel
from backend.mcp_server import create_server, create_http_app


URL = "https://example.com/models/Example-v2"
QUOTE = "On 2026-10-20, Example v2 weights are now publicly available for download."


def evidence(day="2026-10-20", url=URL, quote=QUOTE, value="open", kind="weights_opened"):
    return {"status": "complete", "checked_at": "2026-10-21T00:00:00", "identity": [{"quote": quote}],
            "updates": [{"field": "weight_access", "value": value, "url": url, "quote": quote}],
            "events": [{"kind": kind, "occurred_on": day, "summary": "Example v2 开放权重", "url": url,
                        "quote": quote, "verified": True}]}


class RegistryAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, autoflush=False)
        with self.session() as db:
            db.add(AIModel(name="Example v2", organization="Lab", type="LLM", source_url=URL,
                           weight_access="closed", last_checked_at=datetime(2026, 10, 1)))
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_exact_identity_is_scoped_and_ambiguous_names_never_choose_first(self):
        with self.session() as db:
            db.add_all([AIModel(name="Example v2", organization="Other", type="VLM"),
                        AIModel(name="Example v20", organization="Lab", type="LLM"),
                        AIModel(name="Example v2 Instruct", organization="Lab", type="LLM")])
            db.commit()
            self.assertIsNone(registry.find_model(db, {"name": "Example v2"}))
            self.assertEqual(registry.find_model(db, {"name": "EXAMPLE-v2", "organization": "Lab"}).id, 1)
            self.assertEqual(registry.find_model(db, {"name": "Example v20"}).id, 3)
            self.assertFalse(registry.mentions("Example v2", "Example v20 is released"))
            self.assertFalse(registry.compatible_versions("Example 7B", "Example 32B"))

    def test_verified_alias_is_searchable_and_unverified_similarity_is_only_a_candidate(self):
        with self.session() as db:
            registry.remember_alias(db, db.get(AIModel, 1), "示例模型 2")
            db.commit()
            self.assertEqual(registry.find_model(db, {"name": "示例模型 2"}).id, 1)
            result = registry.search_models(db, "Example v3")
            self.assertFalse(result["items"][0]["exact_name"])
            self.assertIsNone(registry.find_model(db, {"name": "Example v3"}))

    def test_startup_never_links_ambiguous_names_to_an_arbitrary_model(self):
        with self.session() as db:
            db.add(AIModel(name="Example v2", organization="Other", type="LLM"))
            db.add(Insight(event_key="ambiguous", title="Example v2 released", category="model_release",
                           model_names='["Example v2"]'))
            db.commit()
            bootstrap_profiles(db)
            self.assertEqual(db.query(ModelInsight).count(), 0)

    def test_open_weights_updates_existing_model_and_records_one_event(self):
        with self.session() as db:
            model = db.get(AIModel, 1)
            record_research(db, model, evidence())
            db.commit()
            record_research(db, model, evidence())
            db.commit()
            self.assertEqual(model.weight_access, "open")
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertEqual(db.query(ModelEvent).count(), 1)
            self.assertEqual(db.query(ModelObservation).count(), 1)
            self.assertEqual(json.loads(db.query(ModelObservation).one().previous_value), "closed")

    def test_repeated_reports_share_lifecycle_event_and_keep_each_source(self):
        with self.session() as db:
            model = db.get(AIModel, 1)
            record_research(db, model, evidence())
            db.commit()
            record_research(db, model, evidence(url="https://news.example.com/release"))
            db.commit()
            self.assertEqual(db.query(ModelEvent).count(), 1)
            self.assertEqual(len(json.loads(db.query(ModelEvent).one().sources)), 2)
            self.assertEqual(model.weight_access, "open")

    def test_blocked_service_does_not_exhaust_model_retry_budget(self):
        with self.session() as db:
            db.add(PendingModel(draft='{"name":"Example v2"}'))
            db.commit()
            model_jobs.enqueue(db, 1)
            db.commit()
            task = model_jobs.claim_task(db, "worker")
            response = httpx.Response(401, request=httpx.Request("POST", "https://example.com"))
            error = httpx.HTTPStatusError("unauthorized", request=response.request, response=response)
            model_jobs.fail_task(db, task.id, "worker", source_health.ServiceBlocked(source_health.diagnosis(error)))
            self.assertEqual(task.status, "blocked")
            self.assertEqual(task.attempts, 0)

    def test_chat_authentication_error_also_pauses_shared_service(self):
        with self.session() as db:
            db.add(PendingModel(draft='{"name":"Example v2"}'))
            db.commit()
            model_jobs.enqueue(db, 1)
            db.commit()
            task = model_jobs.claim_task(db, "worker")
            error = RuntimeError("provider authentication error")
            error.status_code = 401
            with patch.object(source_health, "block_research", return_value=source_health.diagnosis(error)) as blocked:
                model_jobs.fail_task(db, task.id, "worker", error)
            blocked.assert_called_once_with(error)
            self.assertEqual(task.status, "blocked")
            self.assertEqual(task.attempts, 0)

    def test_unanchored_and_older_state_cannot_overwrite_current_value(self):
        with self.session() as db:
            model = db.get(AIModel, 1)
            record_research(db, model, evidence(url="https://news.example.com/article"))
            db.commit()
            self.assertEqual(model.weight_access, "closed")
            self.assertEqual(db.query(ModelObservation).one().status, "proposed")
            record_research(db, model, evidence(day="2026-09-20"))
            db.commit()
            self.assertEqual(model.weight_access, "closed")

    def test_manual_resolution_locks_field_against_automatic_overwrite(self):
        with self.session() as db:
            model = db.get(AIModel, 1)
            record_research(db, model, evidence(url="https://news.example.com/article"))
            db.commit()
            resolve_observation(db, db.query(ModelObservation).one(), True)
            db.commit()
            self.assertIn("weight_access", json.loads(model.locked_fields))
            record_research(db, model, {"status":"complete", "updates":[{"field":"weight_access", "value":"closed", "url":URL, "quote":"Example v2 is closed"}]})
            db.commit()
            self.assertEqual(model.weight_access, "open")

    def test_event_quote_requires_actual_date_correct_version_and_completed_action(self):
        for quote, name, day, expected in [(QUOTE, "Example v2", "2026-10-20", 1),
                (QUOTE, "Example v2", "2026-10-19", 0), (QUOTE, "Example v20", "2026-10-20", 0),
                ("Example v2 will release weights on 2026-10-20.", "Example v2", "2026-10-20", 0)]:
            result = model_research.validate_findings(model_research.Findings(events=[{
                "kind":"weights_opened", "occurred_on":day, "summary":"开放权重", "source_id":1, "quote":quote}]),
                [{"id":1, "url":URL, "content":quote}], name)
            self.assertEqual(len(result["events"]), expected)

    async def test_registry_is_read_before_research_and_duplicate_updates_in_place(self):
        with self.session() as db:
            db.add(PendingModel(draft='{"name":"Example-v2","organization":"Lab","type":"LLM"}', evidence="{}"))
            db.commit()
        async def research(draft, supplied):
            self.assertEqual(supplied["registry_lookup"]["items"][0]["id"], 1)
            return evidence()
        decision = model_review.Decision(item_id=1, decision="approved", reason="官方证据对应同一版本", type="LLM", summary="文本模型", duplicate_of=1)
        with patch.object(model_review, "SessionLocal", self.session), patch.object(model_review, "research_model", research), \
             patch.object(model_review, "judge_batch", AsyncMock(return_value=[decision])):
            result = await model_review.review_models([1])
        self.assertEqual(result["approved"], 1)
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertEqual(db.get(AIModel, 1).weight_access, "open")

    async def test_llm_cannot_merge_a_different_version_by_id_alone(self):
        with self.session() as db:
            db.add(PendingModel(draft='{"name":"Example-v3","organization":"Lab","type":"LLM"}', evidence="{}"))
            db.commit()
        decision = model_review.Decision(item_id=1, decision="approved", reason="错误合并示例", type="LLM", summary="文本模型", duplicate_of=1)
        with patch.object(model_review, "SessionLocal", self.session), patch.object(model_review, "research_model", AsyncMock(return_value=evidence())), \
             patch.object(model_review, "judge_batch", AsyncMock(return_value=[decision])):
            result = await model_review.review_models([1])
        self.assertEqual(result["deferred"], 1)
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertIsNone(db.get(PendingModel, 1).model_id)

    async def test_mcp_protocol_exposes_only_read_tools_and_real_search(self):
        async with Client(create_server(self.session)) as client:
            tools = await client.list_tools()
            self.assertEqual({tool.name for tool in tools.tools}, {"search_models", "get_model", "get_evidence", "get_source_health", "search_news", "find_models"})
            self.assertTrue(all(tool.annotations.read_only_hint for tool in tools.tools))
            result = await client.call_tool("search_models", {"query":"Example v2"})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["items"][0]["id"], 1)
            result = await client.call_tool("get_model", {"model_id":999})
            self.assertTrue(result.is_error)

    def test_http_mcp_requires_separate_bearer_token(self):
        with self.assertRaises(ValueError):
            create_http_app(create_server(self.session), "")
        with TestClient(create_http_app(create_server(self.session), "x" * 32), base_url="http://127.0.0.1:8001") as client:
            self.assertEqual(client.post("/mcp", json={}).status_code, 401)
            self.assertEqual(client.post("/mcp", json={}, headers={"Authorization":"Bearer wrong"}).status_code, 401)
            response = client.post("/mcp", json={"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{
                "protocolVersion":"2025-11-25", "capabilities":{}, "clientInfo":{"name":"test","version":"1"}}},
                headers={"Authorization":"Bearer " + "x" * 32, "Accept":"application/json, text/event-stream"})
            self.assertEqual(response.status_code, 200)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = patch.object(source_fetch, "cache_dir", return_value=Path(directory.name))
        setting.start()
        self.addCleanup(setting.stop)

    async def test_401_stops_other_research_requests_and_preserves_no_secret(self):
        client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(401, request=httpx.Request("POST", "https://api.deepseek.com/v1/tools/search"))))
        with patch.object(model_research, "get_client", return_value=SimpleNamespace(api_key="private-key")):
            for _ in range(2):
                session = model_research.ResearchSession(client, {"name":"Example"}, {})
                with self.assertRaises(source_health.ServiceBlocked):
                    await session.search("Example model")
        self.assertEqual(client.post.await_count, 1)
        self.assertNotIn("private-key", json.dumps(source_fetch.source_status()))
        self.assertTrue(source_health.cooling_down(source_health.research_health()))
        source_health.clear_research_block()
        self.assertFalse(source_health.cooling_down(source_health.research_health()))

    async def test_rate_limit_respects_retry_after_and_uses_only_validated_cache(self):
        url = "https://example.com/feed?token=private"
        source_fetch.save_record(url, {"content":"[1]", "status":"ok", "fetched_at":"old"})
        response = httpx.Response(429, headers={"Retry-After":"120"}, request=httpx.Request("GET", url))
        with patch.object(source_fetch.httpx, "AsyncClient") as factory:
            get = AsyncMock(return_value=response)
            factory.return_value.__aenter__.return_value.get = get
            self.assertEqual(await source_fetch.fetch_source("feed", url, json.loads), [1])
            self.assertEqual(await source_fetch.fetch_source("feed", url, json.loads), [1])
            get.assert_awaited_once()
        status = source_fetch.source_status()[0]
        self.assertEqual(status["status"], "cached")
        self.assertEqual(status["failure_kind"], "rate_limit")
        self.assertEqual(status["fetched_at"], "old")
