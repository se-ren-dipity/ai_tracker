import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import admin_auth as auth, database
from backend.collector import collection_jobs as jobs, collection_worker as worker
from backend.models.operations import CollectionRun, SourceSetting, WorkerStatus, AdminSession, AdminEvent
from backend.routers import admin, insights, pending
from backend.runtime import process_lock, worker_status


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        database.Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, autoflush=False)
        self.addCleanup(self.engine.dispose)


class CollectionJobTests(DatabaseCase):
    def test_online_backup_is_complete_and_never_overwrites_existing_file(self):
        import sqlite3
        from contextlib import closing
        from pathlib import Path
        from backend.backup import backup
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "live.db", Path(directory) / "copy.db"
            with closing(sqlite3.connect(source)) as db:
                db.execute("CREATE TABLE example (value TEXT)")
                db.execute("INSERT INTO example VALUES ('saved')")
                db.commit()
                backup(source, target)
                db.execute("INSERT INTO example VALUES ('later')")
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute("SELECT value FROM example").fetchall(), [("saved",)])
            with self.assertRaises(ValueError):
                backup(source, target)

    def test_daily_key_survives_restart_and_catches_up_only_today(self):
        for hour in (1, 2, 3, 15):
            with self.session() as db:
                jobs.ensure_daily(db, datetime(2026, 10, 27, hour, tzinfo=timezone.utc))
                self.assertEqual(db.query(CollectionRun).count(), int(hour >= 2))
        with self.session() as db:
            jobs.ensure_daily(db, datetime(2026, 10, 30, 2, tzinfo=timezone.utc))
            self.assertEqual([row.key for row in db.query(CollectionRun)], ["daily:2026-10-27", "daily:2026-10-30"])

    def test_consecutive_manual_requests_coalesce(self):
        for _ in range(3):
            with self.session() as db:
                self.assertEqual(jobs.enqueue(db).id, 1)

    def test_interruption_and_bounded_retry_preserve_completed_steps(self):
        with self.session() as db:
            row = jobs.enqueue(db)
            jobs.claim(db)
            jobs.checkpoint(db, row.id, "source", "Source", "done", "1")
        with self.session() as db:
            jobs.recover_interrupted(db)
            row = jobs.claim(db)
            self.assertEqual(row.attempts, 2)
            self.assertEqual(json.loads(row.stages)["source"]["status"], "done")
            jobs.checkpoint(db, row.id, "edit", "文章编排", "failed", "ValueError")
            jobs.finish(db, row.id)
            self.assertEqual(row.status, "retry")
            self.assertIsNone(jobs.claim(db))
            jobs.claim(db, datetime.utcnow() + timedelta(hours=1))
            jobs.finish(db, row.id)
            self.assertEqual(row.status, "failed")
            jobs.retry(db, row)
            self.assertEqual(row.attempts, 0)
            self.assertEqual(json.loads(row.stages)["source"]["status"], "done")
            jobs.claim(db)
            with self.assertRaises(ValueError):
                jobs.retry(db, row)

    def test_process_lock_excludes_another_worker_and_releases(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DB_PATH": directory + "/test.db"}):
            with process_lock("collector"):
                with self.assertRaises(OSError):
                    with process_lock("collector"):
                        self.fail("A second live worker entered")
            with process_lock("collector"):
                pass

    def test_stale_or_stopped_workers_are_not_reported_online(self):
        with self.session() as db:
            db.add(WorkerStatus(role="collector", state="running", last_seen=datetime.utcnow() - timedelta(seconds=100)))
            db.add(WorkerStatus(role="models", state="idle", last_seen=datetime.utcnow()))
            db.commit()
            self.assertEqual([row["online"] for row in worker_status(db)], [False, True])


class CollectionPipelineTests(unittest.IsolatedAsyncioTestCase, DatabaseCase):
    async def test_independent_sources_editor_retry_and_disabled_source(self):
        sources = [{"name": name, "key": name, "url": name, "type": kind, "enabled": enabled}
                   for name, kind, enabled in (("Table", "models_table", True), ("RSS", "rss", True), ("Off", "rss", False))]
        with self.session() as db:
            run_id = jobs.enqueue(db).id
            jobs.claim(db)
        with patch.object(worker, "SessionLocal", self.session), patch.object(worker, "configured_sources", return_value=sources), \
             patch.object(worker, "sync_models_table", AsyncMock(side_effect=[ValueError(), {}])) as table, \
             patch.object(worker, "collect_news", AsyncMock(return_value=1)) as news, \
             patch.object(worker, "read_record", return_value={"status": "ok"}), \
             patch.object(worker, "analyze", AsyncMock()) as analysis, \
             patch.object(worker, "compose_articles", AsyncMock(side_effect=[ValueError(), None])) as edit, \
             patch.object(worker, "queue_model_drafts", AsyncMock()) as discover, \
             patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}):
            await worker.run_collection(run_id, {})
            with self.session() as db:
                row = db.get(CollectionRun, run_id)
                self.assertEqual(row.status, "retry")
                self.assertEqual(json.loads(row.stages)["RSS"]["status"], "done")
                jobs.claim(db, datetime.utcnow() + timedelta(hours=1))
            await worker.run_collection(run_id, {})
            news.assert_awaited_once_with([sources[1]], analyze=False)
            self.assertEqual(table.await_count, 2)
            self.assertEqual(edit.await_count, 2)
            self.assertEqual(discover.await_count, 2)
            self.assertEqual(analysis.await_count, 2)
        with self.session() as db:
            self.assertEqual(db.get(CollectionRun, run_id).status, "done")

    async def test_no_key_still_archives_news_and_marks_analysis_blocked(self):
        sources = [{"name": "RSS", "key": "RSS", "url": "RSS", "type": "rss", "enabled": True}]
        with self.session() as db:
            run_id = jobs.enqueue(db).id
            jobs.claim(db)
        with patch.object(worker, "SessionLocal", self.session), patch.object(worker, "configured_sources", return_value=sources), \
             patch.object(worker, "collect_news", AsyncMock(return_value=2)) as news, \
             patch.object(worker, "read_record", return_value={"status": "ok"}), \
             patch.object(worker, "analyze", AsyncMock()) as analysis, \
             patch.object(worker, "compose_articles", AsyncMock()) as edit, \
             patch.object(worker, "queue_model_drafts", AsyncMock()), patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            await worker.run_collection(run_id, {})
            news.assert_awaited_once()
            analysis.assert_not_awaited()
            edit.assert_not_awaited()
        with self.session() as db:
            self.assertEqual(db.get(CollectionRun, run_id).status, "blocked")

    async def test_cached_source_keeps_run_degraded(self):
        sources = [{"name": "Table", "key": "Table", "url": "Table", "type": "models_table", "enabled": True}]
        with self.session() as db:
            run_id = jobs.enqueue(db).id
            jobs.claim(db)
        with patch.object(worker, "SessionLocal", self.session), patch.object(worker, "configured_sources", return_value=sources), \
             patch.object(worker, "sync_models_table", AsyncMock(return_value={})), \
             patch.object(worker, "read_record", return_value={"status": "cached", "error": "ConnectError"}), \
             patch.object(worker, "analyze", AsyncMock()), patch.object(worker, "compose_articles", AsyncMock()), \
             patch.object(worker, "queue_model_drafts", AsyncMock()), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}):
            await worker.run_collection(run_id, {})
        with self.session() as db:
            self.assertEqual(db.get(CollectionRun, run_id).status, "retry")


class AdminTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.password = "test-password-123456"
        setting = patch.dict(os.environ, {"ADMIN_PASSWORD_HASH": auth.password_hash(self.password), "ADMIN_COOKIE_SECURE": "false"})
        setting.start()
        self.addCleanup(setting.stop)
        sessions = patch.object(auth, "SessionLocal", self.session)
        sessions.start()
        self.addCleanup(sessions.stop)
        app = FastAPI()
        app.middleware("http")(auth.admin_guard)
        for router in (auth.router, admin.router, insights.router, pending.router):
            app.include_router(router)
        def get_db():
            with self.session() as db:
                yield db
        app.dependency_overrides[database.get_db] = get_db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def login(self):
        response = self.client.post("/api/admin/login", json={"password": self.password})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        return {"X-CSRF-Token": response.json()["csrf"]}

    def test_protected_reads_and_every_existing_mutation_require_login(self):
        for path in ("/api/admin/overview", "/api/pending", "/api/pending/changes"):
            self.assertEqual(self.client.get(path).status_code, 401)
        for path in ("/api/insights/refresh", "/api/pending/review", "/api/pending/enrich", "/api/pending/1/approve", "/api/pending/1/reject", "/api/pending/changes/1/accept"):
            self.assertEqual(self.client.post(path).status_code, 401)
        self.assertEqual(self.client.get("/api/insights").status_code, 200)

    def test_login_csrf_persistent_switch_queue_and_logout(self):
        headers = self.login()
        self.assertEqual(self.client.post("/api/insights/refresh").status_code, 403)
        info = self.client.get("/api/admin/overview").json()
        source = info["sources"][0]
        response = self.client.patch("/api/admin/sources/" + source["key"], json={"enabled": False}, headers=headers)
        self.assertEqual(response.status_code, 200)
        with self.session() as db:
            self.assertFalse(db.get(SourceSetting, source["key"]).enabled)
        for _ in range(2):
            response = self.client.post("/api/insights/refresh", headers=headers)
            self.assertEqual(response.status_code, 202)
        with self.session() as db:
            self.assertEqual(db.query(CollectionRun).count(), 1)
            self.assertEqual(db.query(CollectionRun).one().status, "queued")
            self.assertGreater(db.query(AdminEvent).count(), 0)
        self.assertEqual(self.client.post("/api/admin/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)

    def test_session_expiry_password_rotation_and_rate_limit(self):
        self.login()
        with self.session() as db:
            db.query(AdminSession).one().expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        self.assertFalse(self.client.get("/api/admin/session").json()["authenticated"])
        self.login()
        with patch.dict(os.environ, {"ADMIN_PASSWORD_HASH": auth.password_hash("a-different-password")}):
            self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)
        for _ in range(5):
            self.assertEqual(self.client.post("/api/admin/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/admin/login", json={"password": self.password}).status_code, 429)

    def test_unconfigured_and_cross_origin_login_fail_closed(self):
        with patch.dict(os.environ, {"ADMIN_PASSWORD_HASH": ""}):
            self.assertEqual(self.client.post("/api/admin/login", json={"password": self.password}).status_code, 503)
        self.assertEqual(self.client.post("/api/admin/login", json={"password": self.password}, headers={"Origin": "https://attacker.test"}).status_code, 403)
