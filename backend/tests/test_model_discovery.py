import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import database
from backend.collector import model_discovery as discovery, model_jobs as jobs
from backend.collector.insight_agent import Analysis
from backend.collector.source_items import prepare_source_items
from backend.models.article import Article
from backend.models.insight import Insight, SourceItem
from backend.models.model_db import AIModel
from backend.models.model_task import ModelInsight, ModelObservation, ModelTask
from backend.models.pending import PendingModel


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        database.Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, autoflush=False)
        self.fallback = AsyncMock(side_effect=AssertionError("Normal admission must not call an LLM"))
        self.mock_fallback = patch.object(discovery, "extract_batch", self.fallback)
        self.mock_fallback.start()

    def tearDown(self):
        self.mock_fallback.stop()
        self.engine.dispose()

    def source(self, names=("Example v2",), body=None, candidates=None, cached=True, old=False):
        body = body or "\n".join(f"{name} 模型正式发布，支持长文本理解。" for name in names)
        candidates = candidates if candidates is not None else [
            {"name": name, "quote": f"{name} 模型正式发布，支持长文本理解。", "type": "LLM"} for name in names]
        with self.session() as db:
            article = Article(url=f"https://example.com/{db.query(Article).count()}", title="新模型发布",
                              source="AIHOT 模型", content_html=f"<p>{body}</p>",
                              published=datetime.utcnow() - timedelta(days=2))
            if old:
                article.created_at = datetime.utcnow() - timedelta(days=2)
            db.add(article)
            db.commit()
            prepare_source_items(db)
            source = db.query(SourceItem).filter_by(article_id=article.id).one()
            insight = Insight(event_key=f"release-{article.id}", title=article.title, category="model_release")
            db.add(insight)
            db.flush()
            source.insight_id = insight.id
            if cached:
                source.analysis_json = json.dumps({"model_candidates": candidates, "model_capture_version": discovery.VERSION})
            db.commit()
            return source.id

    async def reconcile(self):
        await discovery.reconcile_releases(session_factory=self.session)

    async def test_all_versions_direct_admission_is_idempotent_and_zero_extra_calls(self):
        names = ["Example-1B-Base", "Example-1B-SFT", "Example-7B", "Example-14B", "Example-32B", "Example-70B"]
        self.source(names)
        await self.reconcile()
        await self.reconcile()
        self.fallback.assert_not_awaited()
        with self.session() as db:
            self.assertEqual({m.name for m in db.query(AIModel)}, set(names))
            self.assertEqual(db.query(ModelTask).count(), 0)
            self.assertEqual(db.query(ModelInsight).count(), 6)
            self.assertTrue(all(m.params_b is None for m in db.query(AIModel)))
            report = discovery.coverage(db)
            self.assertEqual((report["sources"], report["checked"], report["pending_candidates"]), (1, 1, 0))

    async def test_multiple_reports_fill_empty_fields_and_preserve_conflicts(self):
        with self.session() as db:
            db.add(AIModel(name="Example v2", type="LLM", context_length=32768, locked_fields='["license"]'))
            db.commit()
        quote = "Example v2 模型开放权重，采用 MIT 许可证，支持 128K 上下文。"
        updates = [{"field": field, "value": value, "quote": quote} for field, value in
                   [("weight_access", "open"), ("license", "MIT"), ("context_length", 131072)]]
        for _ in range(2):
            self.source(body=quote, candidates=[{"name": "Example v2", "quote": quote, "updates": updates}])
        await self.reconcile()
        self.fallback.assert_not_awaited()
        with self.session() as db:
            model = db.query(AIModel).one()
            self.assertEqual((model.weight_access, model.context_length, model.license), ("open", 32768, None))
            self.assertEqual(db.query(ModelObservation).filter_by(status="proposed").count(), 4)
            self.assertEqual(db.query(ModelInsight).count(), 2)
            self.assertEqual(db.query(PendingModel).count(), 1)
            self.assertEqual(db.query(ModelTask).count(), 0)

    async def test_ambiguous_identity_and_manual_rejection_remain_visible(self):
        with self.session() as db:
            db.add_all([AIModel(name="Echo", organization="Lab A", type="LLM"),
                        AIModel(name="Echo", organization="Lab B", type="LLM"),
                        PendingModel(draft='{"name":"Rejected v2"}', status="rejected", reviewer="manual")])
            db.commit()
        self.source(["Echo", "Rejected v2"])
        await self.reconcile()
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 2)
            self.assertEqual(db.get(PendingModel, 1).status, "rejected")
            self.assertEqual(discovery.coverage(db)["pending_candidates"], 2)
            self.assertEqual(db.query(ModelTask).count(), 0)

    async def test_short_name_resolves_only_with_explicit_full_repository_name(self):
        with self.session() as db:
            db.add(AIModel(name="Nemotron 3.5 Transcribe 30B-A4B", organization="NVIDIA", type="Speech"))
            db.commit()
        source_id = self.source(["Nemotron 3.5 Transcribe"], candidates=[{
            "name": "Nemotron 3.5 Transcribe", "organization": "NVIDIA",
            "quote": "NVIDIA 发布模型 Nemotron 3.5 Transcribe，支持转录。"}],
            body="NVIDIA 发布模型 Nemotron 3.5 Transcribe，支持转录。")
        with self.session() as db:
            db.get(SourceItem, source_id).links = '["https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B"]'
            db.commit()
        await self.reconcile()
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertEqual(db.query(PendingModel).one().model_id, 1)

    async def test_known_publisher_alias_does_not_create_duplicate_or_false_conflict(self):
        with self.session() as db:
            db.add(AIModel(name="Clover-35B-A3B-Preview", organization="DiffuSpace（扩散智能）", type="LLM"))
            db.commit()
        quote = "扩散智能 发布模型 Clover-35B-A3B-Preview，权重开放。"
        self.source(body=quote, candidates=[{"name": "Clover-35B-A3B-Preview", "organization": "扩散智能", "quote": quote,
            "updates": [{"field": "organization", "value": "扩散智能", "quote": quote}]}])
        await self.reconcile()
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertEqual(db.query(PendingModel).one().model_id, 1)
            self.assertEqual(db.query(ModelObservation).count(), 0)

    async def test_short_name_without_matching_publisher_repository_defers_instead_of_duplicating(self):
        with self.session() as db:
            db.add(AIModel(name="Nemotron 3.5 Transcribe 30B-A4B", organization="NVIDIA", type="Speech"))
            db.commit()
        source_id = self.source(["Nemotron 3.5 Transcribe"])
        with self.session() as db:
            db.get(SourceItem, source_id).links = '["https://huggingface.co/unrelated/Nemotron-3.5-Transcribe-30B-A4B"]'
            db.commit()
        await self.reconcile()
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertEqual(db.query(PendingModel).one().review_decision, "deferred")
            self.assertEqual(db.query(ModelTask).count(), 0)

    async def test_old_legacy_sources_skipped_but_new_capture_recovers_after_day_boundary(self):
        self.source(["Legacy v1"], cached=False, old=True)
        self.source(["Captured v2"], old=True)
        await self.reconcile()
        self.fallback.assert_not_awaited()
        with self.session() as db:
            self.assertEqual([m.name for m in db.query(AIModel)], ["Captured v2"])

    async def test_legacy_today_fallback_once_and_failures_not_marked_complete(self):
        first = self.source(cached=False)
        self.fallback.side_effect = ValueError("bad extraction")
        with self.assertRaises(RuntimeError):
            await self.reconcile()
        with self.session() as db:
            self.assertEqual(discovery.coverage(db)["items"][0]["status"], "failed")
            self.assertEqual(db.query(AIModel).count(), 0)
        self.fallback.side_effect = None
        self.fallback.return_value = [discovery.Discovery(source_item_id=first, candidates=[
            {"name": "Example v2", "quote": "Example v2 模型正式发布，支持长文本理解。"}], reason="已提取")]
        await self.reconcile()
        await self.reconcile()
        self.assertEqual(self.fallback.await_count, 2)
        with self.session() as db:
            self.assertEqual(discovery.coverage(db)["checked"], 1)

    async def test_unsupported_quote_only_rolls_back_its_source(self):
        self.source(["Good v1"])
        bad_id = self.source(["Bad v2"], candidates=[{"name": "Bad v2", "quote": "Bad v2 这是编造的原文证据。"}])
        with self.assertRaises(RuntimeError):
            await self.reconcile()
        with self.session() as db:
            self.assertEqual([m.name for m in db.query(AIModel)], ["Good v1"])
            self.assertEqual(discovery.coverage(db)["unresolved_sources"], 1)
            self.assertEqual(json.loads(db.get(SourceItem, bad_id).model_discovery)["status"], "failed")

    async def test_concurrent_writer_does_not_break_news_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, "tracker.db")
            self.engine.dispose()
            self.engine = create_engine(f"sqlite:///{filename}", connect_args={"timeout": 2})
            database.Base.metadata.create_all(self.engine)
            self.session = sessionmaker(bind=self.engine, autoflush=False)
            with self.engine.begin() as connection:
                connection.execute(text("CREATE TABLE heartbeat_probe (value INTEGER)"))
            self.source(["Concurrent v1"])
            reserved, errors = threading.Event(), []
            def heartbeat():
                try:
                    with closing(sqlite3.connect(filename, timeout=2)) as db:
                        db.execute("INSERT INTO heartbeat_probe VALUES (1)")
                        reserved.set()
                        time.sleep(0.05)
                        db.commit()
                except Exception as exc:
                    errors.append(exc)
            writer = threading.Thread(target=heartbeat)
            admit = discovery.admit_candidate
            def contend(db, source, candidate):
                db.query(AIModel).all()
                writer.start()
                reserved.wait(0.15)
                return admit(db, source, candidate)
            try:
                with patch.object(discovery, "admit_candidate", side_effect=contend):
                    await self.reconcile()
                writer.join(timeout=3)
                self.assertFalse(writer.is_alive())
                self.assertEqual(errors, [])
                with self.session() as db:
                    self.assertEqual(db.query(AIModel).one().name, "Concurrent v1")
                    self.assertEqual(discovery.coverage(db)["checked"], 1)
            finally:
                if writer.ident is not None:
                    writer.join(timeout=3)
                self.engine.dispose()

    async def test_database_error_keeps_other_sources_and_its_original_reason(self):
        self.source(["Good before"])
        bad_id = self.source(["Bad database"])
        self.source(["Good after"])
        admit = discovery.admit_candidate
        def fail_one(db, source, candidate):
            if source.id == bad_id:
                db.execute(text("SELECT * FROM missing_admission_table"))
            return admit(db, source, candidate)
        with patch.object(discovery, "admit_candidate", side_effect=fail_one):
            with self.assertRaises(RuntimeError):
                await self.reconcile()
        with self.session() as db:
            self.assertEqual({m.name for m in db.query(AIModel)}, {"Good before", "Good after"})
            error = json.loads(db.get(SourceItem, bad_id).model_discovery)["error"]
            self.assertIn("no such table", error)
            self.assertNotIn("PendingRollbackError", error)

    async def test_empty_release_capture_is_flagged_without_automatic_research(self):
        self.source(candidates=[])
        await self.reconcile()
        await self.reconcile()
        with self.session() as db:
            self.assertEqual(discovery.coverage(db)["unresolved_sources"], 1)
            self.assertEqual(db.query(ModelTask).count(), 0)

    async def test_unnamed_companion_remains_unresolved_even_when_main_model_admitted(self):
        source_id = self.source()
        with self.session() as db:
            source = db.get(SourceItem, source_id)
            captured = json.loads(source.analysis_json)
            captured["model_discovery_issue"] = "另有配套判别模型，但输入没有给出正式名称"
            source.analysis_json = json.dumps(captured)
            db.commit()
        await self.reconcile()
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertEqual(discovery.coverage(db)["unresolved_sources"], 1)

    def test_digest_parent_section_backfilled_without_overview_duplication(self):
        with self.session() as db:
            article = Article(title="日报", source="橘鸦早报", url="https://example.com/daily", content_html=
                '<div><h2>今日速览</h2><p>重复提及</p><h2>模型发布</h2><h3 id="a">Example v2 #1</h3><p>发布</p><h2>工程</h2><h3 id="b">Tool #2</h3><p>工具</p></div>')
            db.add(article)
            db.commit()
            prepare_source_items(db)
            source = db.query(SourceItem).filter_by(section_id="a").one()
            source.source_section = None
            db.commit()
            prepare_source_items(db)
            self.assertEqual(source.source_section, "模型发布")
            self.assertEqual(db.query(SourceItem).count(), 2)

    @patch.dict(os.environ, {"MODEL_AUTO_REVIEW_ENABLED": "false"})
    def test_old_automatic_tasks_pause_including_expired_lease_manual_tasks_still_run(self):
        with self.session() as db:
            for name in ["Auto", "Expired", "Manual"]:
                row = PendingModel(draft=json.dumps({"name": name}))
                db.add(row)
                db.flush()
                jobs.enqueue(db, row.id, "手动复查" if name == "Manual" else "相关报道更新")
            db.commit()
            expired = db.get(ModelTask, 2)
            expired.status, expired.lease_until = "running", datetime.utcnow() - timedelta(minutes=2)
            db.add(AIModel(name="Old model", type="LLM"))
            db.commit()
            jobs.enqueue_due(db)
            jobs.pause_automatic_tasks(db)
            self.assertEqual(db.query(ModelTask).count(), 3)
            self.assertEqual([db.get(ModelTask, n).status for n in [1, 2]], ["paused", "paused"])
            self.assertEqual(jobs.claim_task(db, "worker").pending_id, 3)
            jobs.enqueue(db, 1)  # Administrator can explicitly resume a paused task.
            db.commit()
            self.assertEqual((db.get(ModelTask, 1).status, db.get(ModelTask, 1).reason), ("queued", "手动复查"))


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_source_id_response_is_retried_then_flagged(self):
        client = MagicMock()
        client.with_options.return_value = client
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"items":[]}'))]))
        with patch.object(discovery, "get_client", return_value=client):
            rows = await discovery.extract_batch([{"source_item_id": 1, "title": "Model", "body": "Release"}])
        self.assertTrue(rows[0].needs_review)
        self.assertEqual(rows[0].candidates, [])
        self.assertEqual(client.chat.completions.create.await_count, 2)

    async def test_invalid_source_does_not_discard_valid_sources_or_retry_them(self):
        valid = {"source_item_id": 1, "candidates": [{"name": "Example v2", "quote": "Example v2 is released."}], "reason": "发布模型"}
        invalid = {"source_item_id": 2, "candidates": [{"name": "Invented v9", "quote": "Invented v9 is released."}], "reason": "错误引用"}
        client = MagicMock()
        client.with_options.return_value = client
        client.chat.completions.create = AsyncMock(side_effect=[SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps({"items": rows})))])
            for rows in ([valid, invalid], [invalid])])
        with patch.object(discovery, "get_client", return_value=client):
            rows = await discovery.extract_batch([
                {"source_item_id": 1, "title": "Release", "body": "Example v2 is released."},
                {"source_item_id": 2, "title": "Release", "body": "Something else."}])
        self.assertEqual(rows[0].candidates[0].name, "Example v2")
        self.assertFalse(rows[0].needs_review)
        self.assertTrue(rows[1].needs_review)
        retry = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        self.assertEqual([item["source_item_id"] for item in retry["items"]], [2])

    def test_analysis_no_longer_limits_names_to_five(self):
        self.assertEqual(len(Analysis(item_id=1, keep=False, event_kind="release", model_names=[f"Model {i}" for i in range(8)]).model_names), 8)
