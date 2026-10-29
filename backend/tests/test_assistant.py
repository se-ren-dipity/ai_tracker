import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from openai.types.chat import ChatCompletionMessage

from backend import admin_auth
from backend.database import Base
from backend.assistant import session, tools, agent
from backend.assistant.schemas import Message, NewsQuery, ModelFilter
from backend.models.assistant import AssistantRequest, AssistantUsage, AssistantConfig
from backend.models.article import Article
from backend.models.insight import Insight, SourceItem
from backend.models.model_db import AIModel
from backend.routers import assistant as router, admin


class AssistantCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.engine = create_engine("sqlite:///" + str(Path(folder.name) / "test.db"), connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)
        self.addCleanup(self.engine.dispose)
        for target in (session, tools, admin_auth):
            patcher = patch.object(target, "SessionLocal", self.db)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test-key", "ADMIN_COOKIE_SECURE":"false"})
        patcher.start(); self.addCleanup(patcher.stop)

    def visitor(self):
        request = Request({"type":"http", "method":"GET", "scheme":"http", "path":"/api/assistant/session",
                           "headers":[(b"host", b"testserver")], "server":("testserver", 80), "client":("127.0.0.1", 123)})
        return session.open_session(request)[0]

    def admit(self, token, message="你好", request_id=None, version=1):
        return session.admit(token, "address", Message(request_id=request_id or uuid4(), version=version, message=message))[0]

    def test_idempotency_and_concurrent_admission_are_durable(self):
        token, rid = self.visitor(), uuid4()
        def attempt(_):
            try:
                return self.admit(token, request_id=rid)["id"]
            except HTTPException as exc:
                self.assertIn(exc.status_code, (409, 429))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        identifiers = {v for v in results if v}
        self.assertEqual(len(identifiers), 1)
        with self.db() as db:
            self.assertEqual(db.query(AssistantRequest).count(), 1)
            self.assertEqual(db.get(AssistantUsage, "site:" + session.day_key()).count, 1)
        self.assertEqual(self.admit(token, request_id=rid)["id"], next(iter(identifiers)))

    def test_ownership_clear_late_completion_and_quota(self):
        token, other = self.visitor(), self.visitor()
        item = self.admit(token)
        with self.assertRaises(HTTPException) as exc:
            session.owned_request(other, item["id"])
        self.assertEqual(exc.exception.status_code, 404)
        session.reserve_call(item["id"], 12000, 2000)
        identifiers, summary = session.clear_conversation(token)
        self.assertEqual(identifiers, [item["id"]])
        self.assertEqual(summary["remaining"], 19)
        self.assertEqual(summary["version"], 2)
        session.finish(item["id"], "done", {"secret":"late completion"})
        with self.db() as db:
            row = db.get(AssistantRequest, item["id"])
            self.assertEqual(row.status, "cancelled")
            self.assertIsNone(row.result)
            self.assertEqual(row.message, "")
        with self.assertRaises(HTTPException):
            session.owned_request(token, item["id"])

    def test_restart_retains_paid_budget_and_refunds_unpaid_failure_once(self):
        token = self.visitor()
        first = self.admit(token)
        session.finish(first["id"], "failed")
        session.finish(first["id"], "failed")
        with self.db() as db:
            self.assertEqual(db.get(AssistantUsage, "site:" + session.day_key()).count, 0)
        second = self.admit(token)
        session.reserve_call(second["id"], 12000, 2000)
        session.settle_call(second["id"], 12000, 2000, 600, 100, "test-model")
        session.recover_and_cleanup(True)
        with self.db() as db:
            row = db.get(AssistantRequest, second["id"])
            self.assertEqual(row.status, "interrupted")
            total = db.get(AssistantUsage, "site:" + session.day_key())
            self.assertEqual((total.count, total.calls, total.input_tokens, total.output_tokens), (1, 1, 600, 100))

    def test_expiry_and_budget_do_not_reset_on_reopen(self):
        token = self.visitor()
        item = self.admit(token)
        for _ in range(4):
            session.reserve_call(item["id"], 12000, 2000)
        with self.assertRaises(HTTPException):
            session.reserve_call(item["id"], 12000, 2000)
        with self.db() as db:
            row = db.get(AssistantRequest, item["id"])
            row.created_at = datetime.utcnow() - timedelta(hours=25)
            db.commit()
        session.recover_and_cleanup()
        with self.db() as db:
            self.assertEqual(db.query(AssistantRequest).count(), 0)
            self.assertEqual(db.get(AssistantUsage, "site:" + session.day_key()).calls, 4)

    def test_news_source_day_boundary_deduplicates_and_never_uses_generation_time(self):
        with self.db() as db:
            db.add(Article(id=1, url="https://example.com/a", title="daily"))
            db.add_all([Insight(id=1,event_key="one",title="OpenAI feature",category="engineering", published=datetime(2026,10,20),generated_at=datetime(2026,10,28)),
                        Insight(id=2,event_key="two",title="old OpenAI",category="engineering",published=datetime(2026,10,10),generated_at=datetime(2026,10,28))])
            db.add_all([SourceItem(article_id=1,section_key="a",title="a",body_text="a",insight_id=1,published=datetime(2026,10,27,16)),
                        SourceItem(article_id=1,section_key="b",title="b",body_text="b",insight_id=1,published=datetime(2026,10,28,1))])
            db.commit()
            result = tools.news_search(db, NewsQuery(terms=["OpenAI"],date_from="2026-10-28",date_to="2026-10-28"))
            self.assertEqual(result["total"], 1)
            self.assertEqual([item["id"] for item in result["items"]], [1])
            self.assertEqual(tools.news_search(db, NewsQuery(terms=["%"],date_from="2026-10-28",date_to="2026-10-28"))["total"], 0)

    def test_model_filter_unknown_is_separate_and_moe_total_not_active_params(self):
        with self.db() as db:
            db.add_all([AIModel(name="Good 32B",type="LLM",params_b=32,context_length=131072,weight_access="open"),
                        AIModel(name="Unknown 30B",type="LLM",params_b=30),
                        AIModel(name="300B-A30B",type="LLM",params_b=300,context_length=131072,weight_access="open"),
                        AIModel(name="Closed 30B",type="LLM",params_b=30,context_length=131072,weight_access="closed")])
            db.commit()
            result = tools.find_models(db, ModelFilter(params_min=24,params_max=40,context_min=100000,weight_access="open"))
            self.assertEqual([row["name"] for row in result["items"]], ["Good 32B"])
            self.assertEqual([row["name"] for row in result["unknown"]], ["Unknown 30B"])
            self.assertEqual(result["unknown"][0]["unverified_fields"], ["context_length","weight_access"])

    def test_guest_routes_require_own_csrf_and_never_unlock_administration(self):
        app = FastAPI()
        app.middleware("http")(admin_auth.admin_guard)
        app.include_router(router.router); app.include_router(admin.router)
        with TestClient(app) as client:
            data = client.get("/api/assistant/session").json()
            self.assertIn("httponly", client.get("/api/assistant/session").headers["set-cookie"].lower())
            self.assertEqual(client.delete("/api/assistant/conversation").status_code, 403)
            headers = {"X-CSRF-Token":data["csrf"]}
            self.assertEqual(client.delete("/api/assistant/conversation",headers=headers | {"Origin":"https://evil.test"}).status_code,403)
            self.assertEqual(client.delete("/api/assistant/conversation",headers=headers).status_code,200)
            self.assertEqual(client.post("/api/assistant/admin",headers=headers).status_code,401)
            self.assertEqual(client.post("/api/pending/1/approve",headers=headers).status_code,401)
            self.assertEqual(client.get("/api/admin/assistant").status_code,401)
            self.assertEqual(client.put("/api/admin/assistant",json={},headers=headers).status_code,401)

    def test_fake_citations_cards_and_unread_links_rejected(self):
        evidence = tools.ToolSession("test")
        source = evidence.source("Official", "https://openai.com/test", "unread", "link")
        raw = {"kind":"news", "blocks":[{"text":"test", "citation_ids":[source["id"]]}]}
        with self.assertRaises(ValueError): agent.validate_answer(json.dumps(raw), evidence)
        raw["blocks"][0]["citation_ids"] = ["E999"]
        with self.assertRaises(ValueError): agent.validate_answer(json.dumps(raw), evidence)
        raw["blocks"][0]["citation_ids"] = []
        raw["cards"] = [{"kind":"model","id":999}]
        with self.assertRaises(ValueError): agent.validate_answer(json.dumps(raw), evidence)
        with self.assertRaises(ValueError): agent.validate_answer('{"kind":"chat","blocks":[{"text":"记忆中的新品"}]}', tools.ToolSession("x"), True)

    def test_unshown_search_results_cannot_be_cited_and_compaction_preserves_json(self):
        evidence = tools.ToolSession("test")
        item = evidence.add_card("model", {"id":1,"name":"Visible 32B"})
        raw = json.dumps({"kind":"recommendation", "blocks":[{"text":"候选", "citation_ids":[item["citation_id"]]}], "cards":[{"kind":"model","id":1}]})
        with self.assertRaises(ValueError): agent.validate_answer(raw,evidence)
        evidence.exposed.add(item["citation_id"])
        self.assertEqual(agent.validate_answer(raw,evidence)["cards"][0]["id"],1)
        output = {"total":25,"items":[{"id":i,"summary":"长文"*3000} for i in range(25)]}
        reduced = json.loads(agent.tool_content(output))
        self.assertEqual(reduced["total"],25)
        self.assertIn("truncated",reduced)
        self.assertLessEqual(len(reduced["items"]),4)

    def test_zero_configured_budget_and_rate_limit_do_not_admit_extra_requests(self):
        token = self.visitor()
        item = self.admit(token)
        session.reserve_call(item["id"],12000,2000)
        session.finish(item["id"],"done",{"blocks":[]})
        with session.transaction() as db:
            config = session.settings(db)
            config.session_limit = 1
            db.merge(AssistantConfig(id=1,value=config.model_dump_json()))
        for _ in range(3):
            with self.assertRaises(HTTPException) as exc: self.admit(token)
            self.assertEqual(exc.exception.status_code,429)
        with self.db() as db:
            self.assertEqual(db.query(AssistantRequest).count(),1)
            self.assertEqual(db.get(AssistantUsage,"site:"+session.day_key()).count,1)

    def test_each_turn_owns_a_separate_provider_client(self):
        with patch.object(agent,"AsyncOpenAI",side_effect=[object(),object()]) as constructor:
            first, second = agent.get_client(), agent.get_client()
        self.assertIsNot(first,second)
        self.assertEqual(constructor.call_count,2)

    def test_unknown_or_private_url_cannot_be_fetched(self):
        for url in ("http://127.0.0.1/admin", "http://localhost", "https://openai.com.evil.test", "https://user:pass@openai.com"):
            self.assertFalse(tools.official_url(url))
        evidence = tools.ToolSession(self.admit(self.visitor())["id"])
        with patch.object(tools, "research_health", return_value={}):
            result = asyncio.run(evidence.execute("read_source", '{"source_id":"E99"}'))
        self.assertIn("error", result)
        result = asyncio.run(evidence.execute("delete_model", '{}'))
        self.assertIn("error", result)

    def test_bounded_agent_chat_success_and_one_repair_failure(self):
        token = self.visitor()
        message = ChatCompletionMessage(role="assistant",content='{"kind":"chat","blocks":[{"text":"你好，我是囤囤。"}]}')
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)],usage=SimpleNamespace(prompt_tokens=1000,completion_tokens=50),model="test")
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))),close=AsyncMock())
        with patch.object(agent,"get_client",return_value=client), patch.object(agent,"research_health",return_value={}):
            item = self.admit(token)
            asyncio.run(agent.run(item["id"]))
            self.assertEqual(session.owned_request(token,item["id"])["status"],"done")
            message.content = '{"kind":"news","blocks":[{"text":"fabricated","citation_ids":["E999"]}]}'
            item = self.admit(token,"最近新闻")
            asyncio.run(agent.run(item["id"]))
            self.assertEqual(session.owned_request(token,item["id"])["status"],"failed")
            with self.db() as db: self.assertEqual(db.get(AssistantRequest,item["id"]).calls,2)


if __name__ == "__main__":
    unittest.main()
