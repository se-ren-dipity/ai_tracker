import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import database
from backend.collector import insight_agent as agent
from backend.collector.content import content_fields
from backend.collector.insight_models import queue_model_drafts
from backend.collector.model_agent import enrich_model
from backend.collector.source_items import prepare_source_items, split_article
from backend.models.article import Article
from backend.models.insight import Insight, SourceItem
from backend.models.model_db import AIModel
from backend.models.pending import PendingModel
from backend.routers.insights import router
from backend.routers.pending import router as pending_router


def result(item_id=1, **overrides):
    values = dict(item_id=item_id, keep=True, event_key="example-v2-release", event_kind="release",
                  title="Example v2 发布", category="model_release", summary="新模型增加流式接口。",
                  facts=[{"text": "支持流式接口", "quote": "增加流式接口"}],
                  analysis="可能降低接入延迟，仍需实测。", next_step="核对接口说明。",
                  model_names=["Example v2"], priority=80)
    return agent.Analysis(**(values | overrides))


class SplitTests(unittest.TestCase):
    def test_digest_splits_events_without_overview_and_keeps_evidence(self):
        article = {"id": 7, "title": "Daily", "source": "橘鸦早报", "published": datetime(2026, 10, 21),
                   **content_fields('<h2>今日速览</h2><p>Overview</p><h2>模型发布 #1</h2><p>增加流式接口 <a href="https://example.com/model">详情</a></p><h2>融资进展 #2</h2><p>新一轮融资。</p>', "https://example.com", "feed")}
        items = split_article(article)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "模型发布")
        self.assertNotIn("Overview", items[0]["body_text"])
        self.assertNotIn("融资", items[0]["body_text"])
        self.assertEqual(json.loads(items[0]["links"]), ["https://example.com/model"])
        self.assertEqual(items[0]["section_key"], "7:article-section-2")
        self.assertEqual(items, split_article(article))
        article["content_html"] = "<p>无可拆分的新闻章节</p>"
        with self.assertRaises(ValueError):
            split_article(article)

    def test_normal_article_keeps_entire_body(self):
        text = "正文" * 1000 + "尾部新增信息"
        items = split_article({"id": 8, "source": "AIHOT", "title": "News", "published": None, "content_html": f"<p>{text}</p>"})
        self.assertEqual(items[0]["body_text"], text)
        self.assertEqual(items[0]["section_key"], "8:body")


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def analyze(self, responses, existing=None, links=None):
        client = MagicMock()
        client.with_options.return_value = client
        client.chat.completions.create = AsyncMock(side_effect=[SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=value))]) for value in responses])
        with patch.object(agent, "get_client", return_value=client):
            output = await agent.analyze_batch([{"id": 1, "title": "Example", "body_text": "Example v2 增加流式接口。", "links": json.dumps(links or [])}], existing or [])
        return output, client

    async def test_retries_invalid_json_and_accepts_traceable_fact(self):
        output, client = await self.analyze(['{"items":fromjson("bad")}', json.dumps({"items": [result().model_dump()]})])
        self.assertEqual(output[0].facts[0].quote, "增加流式接口")
        self.assertEqual(client.chat.completions.create.await_count, 2)

    async def test_rejects_missing_ids_unknown_duplicate_and_unsupported_quotes(self):
        for values in [[], [result(item_id=2).model_dump()], [result(duplicate_of=99).model_dump()],
                       [result(facts=[{"text": "幻觉", "quote": "未经提供的性能翻倍"}]).model_dump()],
                       [result(facts=[{"text": "幻觉", "quote": " "}]).model_dump()],
                       [result(keep=False, reason="").model_dump()],
                       [result(keep=False, reason="与已有事件相同").model_dump()]]:
            raw = json.dumps({"items": values})
            with self.assertRaises(ValueError):
                await self.analyze([raw, raw])

    async def test_removes_only_unsupported_facts_with_explicit_caution(self):
        item = result(facts=[{"text": "流式", "quote": "增加流式接口"}, {"text": "翻倍", "quote": "性能翻倍"}])
        output, _ = await self.analyze([json.dumps({"items": [item.model_dump()]})])
        self.assertEqual(len(output[0].facts), 1)
        self.assertIn("移除", output[0].cautions[-1])

    async def test_same_official_announcement_is_merged_across_titles(self):
        url = "https://example.com/index/scientific-discoveries/"
        old = {"id": 12, "title": "OpenAI 宣布三项科学发现", "event_kind": "research", "links": [url]}
        value = result(title="OpenAI 模型取得三项科学发现", category="engineering", event_kind="research")
        output, _ = await self.analyze([json.dumps({"items": [value.model_dump()]})], [old], [url + "?utm_source=rss"])
        self.assertEqual(output[0].duplicate_of, 12)

    async def test_evaluation_cannot_merge_with_release_even_with_shared_links(self):
        url = "https://example.com/index/example-model"
        old = {"id": 12, "title": "Example v2 发布", "event_kind": "release", "links": [url]}
        value = result(title="Example v2 独立评测", event_kind="evaluation", duplicate_of=12)
        output, _ = await self.analyze([json.dumps({"items": [value.model_dump()]})], [old], [url])
        self.assertIsNone(output[0].duplicate_of)
        self.assertEqual(output[0].category, "engineering")

    async def test_shared_model_card_does_not_force_merge(self):
        url = "https://huggingface.co/lab/model"
        old = {"id": 12, "title": "Example v2 发布", "event_kind": "release", "links": [url]}
        value = result(event_kind="release")
        output, _ = await self.analyze([json.dumps({"items": [value.model_dump()]})], [old], [url])
        self.assertIsNone(output[0].duplicate_of)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        database.Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, autoflush=False)
        with self.session() as db:
            for index in range(1, 4):
                db.add(Article(id=index, url=f"https://example.com/{index}", title=f"News {index}", source="AIHOT",
                               published=datetime(2026, 10, 20, 18), content_html="<p>增加流式接口</p>"))
            db.commit()
            prepare_source_items(db)
        app = FastAPI()
        app.include_router(router)
        app.include_router(pending_router)
        def get_db():
            with self.session() as db:
                yield db
        app.dependency_overrides[database.get_db] = get_db
        self.client = TestClient(app)

    async def test_event_kind_fences_bad_duplicate_and_event_key_collision(self):
        with self.session() as db:
            agent.save_analysis(db, result(event_kind="release"))
            db.commit()
            agent.save_analysis(db, result(2, event_kind="evaluation", category="engineering", duplicate_of=1))
            db.commit()
            self.assertEqual(db.query(Insight).count(), 2)
            self.assertNotEqual(db.get(SourceItem, 1).insight_id, db.get(SourceItem, 2).insight_id)
            self.assertIsNotNone(db.get(SourceItem, 2).analysis_json)

    async def test_new_report_updates_title_summary_and_date(self):
        with self.session() as db:
            agent.save_analysis(db, result())
            db.commit()
            db.get(SourceItem, 2).published = datetime(2026, 10, 22)
            agent.save_analysis(db, result(2, duplicate_of=1, title="新版已开放", summary="开放情况已更新"))
            db.commit()
            row = db.get(Insight, 1)
            self.assertEqual(row.title, "新版已开放")
            self.assertEqual(row.summary, "开放情况已更新")
            self.assertEqual(row.published, datetime(2026, 10, 22))

    async def test_engineering_news_can_discover_a_new_model(self):
        with self.session() as db:
            db.get(SourceItem, 1).body_text = "Example v2 增加流式接口。"
            agent.save_analysis(db, result(category="engineering", event_kind="evaluation", model_candidates=[
                {"name": "Example v2", "quote": "Example v2 增加流式接口。"}]))
            db.commit()
        with patch("backend.collector.insight_models.SessionLocal", self.session):
            await queue_model_drafts()
            await queue_model_drafts()
        with self.session() as db:
            self.assertEqual(db.query(PendingModel).count(), 1)

    def test_source_failures_are_exposed_by_status_api(self):
        with patch("backend.collector.source_settings.source_overview", return_value=[{"name": "Models Table", "enabled": True, "status": "cached", "error": "ConnectError", "fetched_at": "2026-10-26"}]):
            status = self.client.get("/api/insights/status").json()
        self.assertIn("Models Table", status["last_error"])
        self.assertEqual(status["collection_sources"][0]["status"], "cached")

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        agent.run_state.update(running=False, last_error=None)

    def test_idempotent_split_merge_and_distinct_versions(self):
        with self.session() as db:
            prepare_source_items(db)
            self.assertEqual(db.query(SourceItem).count(), 3)
            agent.save_analysis(db, result())
            db.commit()
            agent.save_analysis(db, result())
            agent.save_analysis(db, result(2, duplicate_of=1, event_key="other-key"))
            agent.save_analysis(db, result(3, event_key="example-v3-release", title="Example v3 发布"))
            db.commit()
            self.assertEqual(db.query(Insight).count(), 2)
            self.assertEqual(db.query(SourceItem).filter_by(insight_id=1).count(), 2)
            self.assertEqual(db.query(Article).count(), 3)
            self.assertEqual(db.get(Article, 1).content_html, "<p>增加流式接口</p>")
        response = self.client.get("/api/insights", params={"day": "2026-10-21", "category": "model_release", "page_size": 1}).json()
        self.assertEqual(response["total"], 2)
        self.assertEqual(len(response["items"]), 1)
        self.assertEqual(self.client.get("/api/insights?day=2026-10-20").json()["total"], 0)
        self.assertEqual(self.client.get("/api/insights?q=流式").json()["total"], 2)
        self.assertEqual(self.client.get("/api/insights?category=industry").json()["total"], 0)
        detail = self.client.get("/api/insights/1").json()
        self.assertEqual(len(detail["sources"]), 2)
        self.assertEqual(detail["facts"][0]["source_item_id"], 1)
        self.assertEqual(self.client.get("/api/insights/999").status_code, 404)
        self.assertEqual(self.client.get("/api/insights?category=wrong").status_code, 422)
        state = self.client.get("/api/insights/status").json()
        self.assertEqual(state["merged"], 1)
        self.assertEqual(state["counts"]["analyzed"], 3)

    async def test_failure_is_retriable_and_never_published(self):
        with patch.object(agent, "SessionLocal", self.session), patch.object(agent, "analyze_batch", new_callable=AsyncMock) as analyze, patch("backend.collector.insight_models.queue_model_drafts", new_callable=AsyncMock), patch("backend.collector.insight_editor.compose_articles", new_callable=AsyncMock):
            analyze.side_effect = ValueError("invalid evidence")
            await agent.build_insights()
            with self.session() as db:
                self.assertEqual(db.query(Insight).count(), 0)
                self.assertEqual(db.query(SourceItem).filter_by(status="failed").count(), 3)
            analyze.side_effect = None
            analyze.return_value = [result(1), result(2, keep=False, reason="无实质信息"), result(3)]
            await agent.build_insights()
            with self.session() as db:
                self.assertEqual(db.query(Insight).count(), 1)
                self.assertEqual(db.query(SourceItem).filter_by(status="failed").count(), 0)
                self.assertEqual(db.get(SourceItem, 2).reason, "无实质信息")

    def test_public_dates_and_cover_only_use_published_event_sections(self):
        with self.session() as db:
            article = db.get(Article, 1)
            article.source = "橘鸦早报"
            article.content_html = '<div><img src="https://test/cover/day.png"><h3 id="article-section-1">Model #1</h3><img src="https://test/model.png"><h3 id="article-section-2">Other #2</h3><img src="https://test/other.png"></div>'
            db.get(SourceItem, 1).section_id = "article-section-1"
            db.get(SourceItem, 2).published = datetime(2026, 10, 25)
            agent.save_analysis(db, result())
            db.commit()
        self.assertEqual(self.client.get("/api/insights/dates").json(), {"dates": ["2026-10-21"]})
        item = self.client.get("/api/insights").json()["items"][0]
        self.assertEqual(item["cover_image"]["url"], "https://test/model.png")
        with self.session() as db:
            db.get(Article, 1).content_html = "<div>无配图</div>"
            db.commit()
        self.assertIsNone(self.client.get("/api/insights").json()["items"][0]["cover_image"])

    def test_list_includes_alternate_covers_from_the_same_event(self):
        with self.session() as db:
            db.get(Article, 1).content_html = '<p><img src="https://test/first.png"></p>'
            db.get(Article, 2).content_html = '<p><img src="https://test/second.png"></p>'
            db.get(Article, 3).content_html = '<p><img src="https://test/unrelated.png"></p>'
            agent.save_analysis(db, result())
            db.commit()
            agent.save_analysis(db, result(2, duplicate_of=1))
            db.commit()
        item = self.client.get("/api/insights").json()["items"][0]
        self.assertEqual([image["url"] for image in item["cover_images"]],
                         ["https://test/first.png", "https://test/second.png"])
        self.assertEqual(item["cover_image"], item["cover_images"][0])
        self.assertEqual(item["cover_images"], self.client.get("/api/insights/1").json()["images"])

    async def test_direct_model_admission_keeps_event_link_without_paid_task(self):
        with self.session() as db:
            db.get(SourceItem, 1).body_text = "Example v2 增加流式接口。"
            agent.save_analysis(db, result(model_candidates=[
                {"name": "Example v2", "quote": "Example v2 增加流式接口。"}]))
            db.commit()
        with patch("backend.collector.insight_models.SessionLocal", self.session), patch("backend.collector.model_review.review_models", new_callable=AsyncMock) as review:
            await queue_model_drafts()
            await queue_model_drafts()
            review.assert_not_awaited()
            from backend.models.model_task import ModelTask
            with self.session() as db:
                self.assertEqual(db.query(ModelTask).count(), 0)
            with self.session() as db:
                db.get(Insight, 1).model_names = '["EXAMPLE-v2"]'
                db.commit()
            await queue_model_drafts()
        with self.session() as db:
            draft = db.query(PendingModel).one()
            self.assertEqual(draft.source_article_id, 1)
            self.assertEqual(draft.source_insight_id, 1)
            self.assertEqual(draft.status, "approved")
        with self.session() as db:
            self.assertEqual(db.get(Insight, 1).related_model_id, db.query(AIModel).one().id)

    async def test_unavailable_metadata_does_not_invent_model_type(self):
        with patch("backend.collector.model_agent._hf_api", new_callable=AsyncMock, return_value={}), patch("backend.collector.model_agent._hf_readme", new_callable=AsyncMock, return_value=""), patch("backend.collector.model_agent.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value.get = AsyncMock(side_effect=httpx.ConnectError("offline"))
            draft = await enrich_model("Voice Example")
        self.assertEqual(draft["type"], "Unknown")
        self.assertIsNone(draft["params_b"])
        self.assertEqual(draft["modality_in"], "[]")
        self.assertIsNone(draft["hf_url"])

    async def test_collection_deduplicates_urls_and_resumes_analysis_without_new_articles(self):
        from backend.collector import scheduler
        feed = [{"url": "https://example.com/new", "title": "New event", "source": "Test", "raw_content": "New body"}] * 2
        with patch.object(scheduler, "SessionLocal", self.session), patch.object(scheduler, "load_sources", return_value=[{"name": "Test", "url": "https://example.com/feed"}]), patch.object(scheduler, "fetch_feed", new_callable=AsyncMock, return_value=feed), patch.object(scheduler, "collect_article_content", new_callable=AsyncMock, return_value={"content_html": "<p>New body</p>"}) as collect, patch.object(scheduler, "build_insights", new_callable=AsyncMock) as analyze:
            await scheduler.collect_news()
            await scheduler.collect_news()
            self.assertEqual(analyze.await_count, 2)
            collect.assert_awaited_once()
        with self.session() as db:
            self.assertEqual(db.query(Article).count(), 4)

    async def test_name_match_without_weights_is_not_model_metadata(self):
        with patch("backend.collector.model_agent._hf_api", new_callable=AsyncMock, return_value={"id": "unofficial/Closed-Model", "pipeline_tag": "text-generation"}), patch("backend.collector.model_agent._hf_readme", new_callable=AsyncMock, return_value="Unofficial notes"), patch("backend.collector.model_agent._extract_benchmarks", new_callable=AsyncMock) as benchmarks:
            draft = await enrich_model("Closed Model")
            benchmarks.assert_not_called()
        self.assertIsNone(draft["organization"])
        self.assertIsNone(draft["hf_url"])
        self.assertEqual(draft["type"], "Unknown")


if __name__ == "__main__":
    unittest.main()
