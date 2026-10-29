import csv
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import database
from backend.collector import import_models_table as table, insight_models, model_jobs
from backend.collector import source_fetch
from backend.collector.model_review import queue_existing_models
from backend.models.article import Article
from backend.models.insight import Insight, SourceItem
from backend.models.model_db import AIModel
from backend.models.model_table import ModelTableEntry
from backend.models.model_task import ModelInsight, ModelObservation, ModelTask
from backend.models.pending import PendingModel
from backend.routers.models import get_model


def row(name="Example-v2", **overrides):
    return {**dict.fromkeys(table.HEADERS, ""), "model": name, "lab": "Lab", "params_total": "7",
            "announced": "Oct/2026", "public": "🟢", "notes": "Public model notes.", **overrides}


def csv_text(rows, reverse_columns=False):
    keys = list(table.HEADERS)
    if reverse_columns:
        keys.reverse()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Models Table"])
    writer.writerow([table.HEADERS[key] for key in keys] + ["Count (rough)", "🔒License", "🔒Context window"])
    for index, item in enumerate(rows):
        writer.writerow([item[key] for key in keys] + [index, "LOCKED", "999999"])
    return output.getvalue()


class ModelsTableTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        database.Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, autoflush=False)
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "table.csv"
        self.cache_patch = patch.object(source_fetch, "cache_dir", return_value=Path(self.directory.name) / "cache")
        self.cache_patch.start()
        self.patch = patch.object(table, "SessionLocal", self.session)
        self.patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.patch.stop()
        self.engine.dispose()
        self.directory.cleanup()

    async def sync(self, rows):
        self.path.write_text(csv_text(rows), encoding="utf-8", newline="")
        return await table.sync_models_table(self.path)

    def add_news_source(self, db):
        from backend.collector.model_discovery import VERSION
        story = db.query(Insight).order_by(Insight.id.desc()).first()
        names = json.loads(story.model_names)
        body = "\n".join(f"{name} 模型新增能力已经发布。" for name in names)
        article = Article(url=f"https://example.com/news/{story.id}", title=story.title,
                          source="AIHOT 模型", content_html=f"<p>{body}</p>")
        db.add(article)
        db.flush()
        db.add(SourceItem(article_id=article.id, insight_id=story.id, section_key=f"{article.id}:body",
                          title=story.title, body_text=body, analysis_json=json.dumps({
                              "model_capture_version": VERSION, "model_candidates": [
                                  {"name": name, "quote": f"{name} 模型新增能力已经发布。"} for name in names]})))
        db.commit()

    async def test_organization_aliases_do_not_duplicate_existing_models(self):
        with self.session() as db:
            db.add(AIModel(name="Sage-A1.5", organization="上海人工智能实验室", type="LLM"))
            db.commit()
        counts = await self.sync([row("Sage-A1.5", lab="Shanghai AI Lab")])
        self.assertEqual(counts["new"], 0)
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)

    async def test_plus_versions_remain_distinct_and_legacy_mapping_is_reused(self):
        self.assertNotEqual(insight_models.model_key("Command-R+"), insight_models.model_key("Command-R"))
        await self.sync([row("Command-R+"), row("Command-R")])
        with self.session() as db:
            entry = db.query(ModelTableEntry).filter(ModelTableEntry.source_key.contains("plus")).one()
            entry.source_key = "old-normalization-key"
            original_id = entry.model_id
            db.commit()
        counts = await self.sync([row("Command-R+"), row("Command-R")])
        self.assertEqual(counts["new"], 0)
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 2)
            self.assertEqual(db.query(ModelTableEntry).filter(ModelTableEntry.source_key.contains("plus")).one().model_id, original_id)

    def test_header_mapping_future_dates_and_same_name_variants(self):
        rows = [row("Future", announced="Jan/2099"),
                row("MPT", playground="https://huggingface.co/lab/mpt-7b"),
                row("MPT", playground="https://huggingface.co/lab/mpt-1b", params_total="1.3")]
        parsed = table.parse_candidates(csv_text(rows, True), today=date(2026, 10, 26))
        self.assertEqual(len(parsed), 2)
        self.assertNotEqual(parsed[0]["source_key"], parsed[1]["source_key"])
        self.assertNotIn("license", parsed[0])
        with self.assertRaises(ValueError):
            table.parse_candidates(csv_text([row(), row()]))

    async def test_direct_admission_and_repeat_sync_are_idempotent(self):
        with patch("backend.collector.enricher.get_client", side_effect=AssertionError("No LLM required")):
            first = await self.sync([row(gpqa="0", paper="https://example.com/paper", playground="https://example.com/play")])
            with self.session() as db:
                model = db.query(AIModel).one()
                changed_at = model.updated_at
                observations = db.query(ModelObservation).count()
            second = await self.sync([row(gpqa="0", paper="https://example.com/paper", playground="https://example.com/play")])
        self.assertEqual(first, {"new": 1, "updated": 0, "unchanged": 0})
        self.assertEqual(second, {"new": 0, "updated": 0, "unchanged": 1})
        with self.session() as db:
            model = db.query(AIModel).one()
            self.assertEqual(model.params_b, 7)
            self.assertEqual(model.release_precision, "month")
            self.assertEqual(json.loads(model.benchmarks), {"GPQA": 0})
            self.assertIsNone(model.license)
            self.assertIsNone(model.context_length)
            self.assertEqual(model.type, "Unknown")
            self.assertEqual(model.updated_at, changed_at)
            self.assertEqual(db.query(ModelObservation).count(), observations)
            self.assertEqual(db.query(ModelTask).count(), 0)
            self.assertEqual(db.query(PendingModel).count(), 0)
            profile = get_model(model.id, db)
            self.assertIn(table.SOURCE_URL, profile["references"])
            self.assertIn({"label": "体验入口", "url": "https://example.com/play"}, profile["links"])
            released = next(item for item in profile["updates"] if item["field"] == "release_date")
            self.assertEqual(released["precision"], "month")

    async def test_revisions_update_only_source_owned_fields_and_keep_manual_edits(self):
        await self.sync([row(gpqa="60")])
        with self.session() as db:
            model = db.query(AIModel).one()
            model.notes = "Manually verified introduction"
            model.context_length = 131072
            model.release_date = date(2026, 10, 15)
            model.release_precision = "day"
            model.benchmarks = json.dumps({"GPQA": 60, "MMLU": 99})
            db.commit()
        changed = await self.sync([row(params_total="8", gpqa="70", mmlu="80", notes="Changed source note")])
        self.assertEqual(changed["updated"], 1)
        with self.session() as db:
            model = db.query(AIModel).one()
            self.assertEqual(model.params_b, 8)
            self.assertEqual(model.notes, "Manually verified introduction")
            self.assertEqual(model.context_length, 131072)
            self.assertEqual(model.release_date, date(2026, 10, 15))
            self.assertEqual(model.release_precision, "day")
            self.assertEqual(json.loads(model.benchmarks), {"GPQA": 70, "MMLU": 99})
        await self.sync([row(params_total="9", estimated_params="*", gpqa="70")])
        with self.session() as db:
            self.assertIsNone(db.query(AIModel).one().params_b)

    async def test_existing_facts_are_preserved_and_missing_fields_filled(self):
        with self.session() as db:
            db.add(AIModel(name="Example-v2", organization="Lab", type="VLM", params_b=12,
                           license="Apache-2.0", notes="Existing note"))
            db.commit()
        await self.sync([row(paper="https://example.com/report")])
        with self.session() as db:
            model = db.query(AIModel).one()
            self.assertEqual(model.id, 1)
            self.assertEqual(model.params_b, 12)
            self.assertEqual(model.type, "VLM")
            self.assertEqual(model.license, "Apache-2.0")
            self.assertEqual(model.paper_url, "https://example.com/report")

    async def test_same_name_variants_and_other_labs_do_not_merge(self):
        rows = [row("MPT", playground="https://huggingface.co/lab/mpt-7b"),
                row("MPT", playground="https://huggingface.co/lab/mpt-1b", params_total="1.3"),
                row("MPT", lab="Other Lab", params_total="3")]
        self.assertEqual((await self.sync(rows))["new"], 3)
        self.assertEqual((await self.sync(list(reversed(rows))))["unchanged"], 3)
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 3)

    async def test_old_rows_are_revised_and_removed_rows_are_not_deleted(self):
        await self.sync([row("Old", announced="Jan/2020"), row("New")])
        changed = await self.sync([row("Old", announced="Jan/2020", params_total="10")])
        self.assertEqual(changed["updated"], 1)
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 2)
            self.assertEqual(db.query(AIModel).filter_by(name="Old").one().params_b, 10)

    async def test_bad_payload_and_http_failure_leave_existing_data_untouched(self):
        await self.sync([row()])
        self.path.write_text(csv_text([row(params_total="99"), row("Broken", announced="bad")]), encoding="utf-8")
        with self.assertRaises(ValueError):
            await table.sync_models_table(self.path)
        with patch.object(source_fetch.httpx, "AsyncClient") as factory, patch.object(source_fetch.asyncio, "sleep", AsyncMock()):
            response = httpx.Response(503, request=httpx.Request("GET", table.PUBLIC_FEED_URL))
            factory.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
            with self.assertRaises(httpx.HTTPStatusError):
                await table.sync_models_table()
        with self.session() as db:
            self.assertEqual(db.query(AIModel).one().params_b, 7)
            self.assertEqual(db.query(ModelTableEntry).count(), 1)

    async def test_live_feed_uses_public_endpoint_and_preserves_provenance(self):
        with patch.object(source_fetch.httpx, "AsyncClient") as factory:
            response = httpx.Response(200, text=csv_text([row()]), request=httpx.Request("GET", table.PUBLIC_FEED_URL))
            get = AsyncMock(return_value=response)
            factory.return_value.__aenter__.return_value.get = get
            await table.sync_models_table()
            get.assert_awaited_once_with(table.PUBLIC_FEED_URL, headers={"Origin": "https://lifearchitect.ai", "Referer": table.SOURCE_URL})
        with self.session() as db:
            entry = db.query(ModelTableEntry).one()
            self.assertEqual(json.loads(entry.public_row)["notes"], "Public model notes.")
            self.assertEqual(db.query(AIModel).one().source_url, table.SOURCE_URL)

    async def test_adopts_old_automatic_task_but_preserves_manual_work(self):
        with self.session() as db:
            for _ in range(2):
                db.add(PendingModel(draft='{"name":"Example-v2","type":"Unknown"}', evidence="{}"))
            db.commit()
            model_jobs.enqueue(db, 1, "发现新模型")
            model_jobs.enqueue(db, 2)
            db.commit()
        await self.sync([row()])
        with self.session() as db:
            self.assertEqual(db.get(PendingModel, 1).status, "approved")
            self.assertEqual(db.get(PendingModel, 1).model_id, db.query(AIModel).one().id)
            self.assertEqual(db.query(ModelTask).filter_by(pending_id=1).one().status, "done")
            self.assertEqual(db.query(ModelTask).filter_by(pending_id=2).one().status, "queued")

    async def test_news_admission_for_both_table_and_other_models_without_research(self):
        await self.sync([row()])
        with self.session() as db:
            db.add(Insight(event_key="release", title="Models released", category="model_release",
                           model_names='["Example-v2", "Voice New"]'))
            db.commit()
            self.add_news_source(db)
            model_jobs.enqueue_due(db)
            self.assertEqual(db.query(ModelTask).count(), 0)
        with patch.object(insight_models, "SessionLocal", self.session):
            await insight_models.queue_model_drafts()
        with self.session() as db:
            self.assertEqual(db.query(ModelInsight).count(), 2)
            self.assertEqual(db.query(ModelTask).count(), 0)
            self.assertIsNotNone(db.query(AIModel).filter_by(name="Voice New").one())
            manual_id = queue_existing_models(db, 1)[0]
            model_jobs.enqueue(db, manual_id)
            db.commit()
            self.assertEqual(db.query(ModelTask).filter_by(pending_id=manual_id).one().status, "queued")

    async def test_table_sync_does_not_consume_new_reporting_for_existing_external_models(self):
        with self.session() as db:
            db.add(AIModel(name="Existing Voice", type="TTS"))
            db.add(Insight(event_key="voice-update", title="Voice update", category="engineering",
                           model_names='["Existing Voice"]'))
            db.commit()
            self.add_news_source(db)
        await self.sync([row()])
        with patch.object(insight_models, "SessionLocal", self.session):
            await insight_models.queue_model_drafts()
        with self.session() as db:
            pending = db.query(PendingModel).one()
            self.assertEqual(pending.model_id, 1)
            self.assertEqual(db.query(ModelTask).count(), 0)
            self.assertEqual(db.query(ModelInsight).filter_by(model_id=1).count(), 1)

    async def test_table_link_does_not_consume_news_discovery(self):
        with self.session() as db:
            db.add(AIModel(name="Existing short name", type="VLM", organization="Original Lab Name",
                           hf_url="https://huggingface.co/lab/example"))
            db.add(Insight(event_key="alias", title="New name in reporting", category="model_release",
                           model_names='["Example-v2"]'))
            db.commit()
            self.add_news_source(db)
        await self.sync([row(playground="https://huggingface.co/lab/example")])
        with patch.object(insight_models, "SessionLocal", self.session):
            await insight_models.queue_model_drafts()
        with self.session() as db:
            self.assertEqual(db.query(AIModel).count(), 1)
            self.assertEqual(db.query(ModelInsight).one().model_id, 1)
            self.assertEqual(db.query(ModelTask).count(), 0)

    async def test_repeated_discovery_reuses_latest_maintenance_record(self):
        with self.session() as db:
            db.add(AIModel(name="Existing Voice", type="TTS"))
            db.add(Insight(event_key="voice-news", title="Voice update", category="model_release",
                           model_names='["Existing Voice"]'))
            db.flush()
            for _ in range(2):
                db.add(PendingModel(status="approved", model_id=1, draft='{"name":"Existing Voice"}', evidence="{}"))
            db.commit()
            self.add_news_source(db)
        with patch.object(insight_models, "SessionLocal", self.session):
            await insight_models.queue_model_drafts()
            with self.session() as db:
                self.assertEqual(db.query(ModelTask).count(), 0)
                self.assertIn("source_versions", json.loads(db.get(PendingModel, 2).evidence))
                self.assertNotIn("source_versions", json.loads(db.get(PendingModel, 1).evidence))
            await insight_models.queue_model_drafts()
        with self.session() as db:
            self.assertEqual(db.query(ModelTask).count(), 0)
            self.assertEqual(db.query(PendingModel).count(), 2)


if __name__ == "__main__":
    unittest.main()
