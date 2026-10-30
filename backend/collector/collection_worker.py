"""Run with python -m backend.collector.collection_worker; no web dependency."""
import asyncio
import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from backend.database import SessionLocal, init_db
from backend.runtime import process_lock, worker_heartbeat
from backend.models.operations import CollectionRun
from backend.collector import collection_jobs as jobs
from backend.collector.scheduler import collect_news
from backend.collector.source_settings import configured_sources
from backend.collector.source_fetch import read_record
from backend.collector.import_models_table import sync_models_table
from backend.collector.insight_agent import build_insights, run_state
from backend.collector.insight_editor import compose_articles
from backend.collector.insight_models import queue_model_drafts
from backend.collector.model_discovery import release_outcome

logger = logging.getLogger(__name__)


async def run_collection(run_id, state):
    with SessionLocal() as db:
        sources = configured_sources(db)
    for source in sources:
        key, name = source["key"], source["name"]
        with SessionLocal() as db:
            enabled = next(row["enabled"] for row in configured_sources(db) if row["key"] == key)
            previous = json.loads(db.get(CollectionRun, run_id).stages).get(key, {})
            if not enabled:
                jobs.checkpoint(db, run_id, key, name, "disabled", "管理员已停用")
                continue
            if previous.get("status") == "done":
                continue
            jobs.checkpoint(db, run_id, key, name, "running")
        state["value"] = name
        try:
            result = await sync_models_table() if source["type"] == "models_table" else await collect_news([source], analyze=False)
            health = read_record(source["url"])
            status = "done" if health.get("status") == "ok" else health.get("status", "failed")
            detail = json.dumps(result, ensure_ascii=False) if status == "done" else health.get("error", "未取得有效来源状态")
        except Exception as exc:
            status, detail = "failed", type(exc).__name__
            logger.warning("Source %s failed: %s", name, detail)
        with SessionLocal() as db:
            jobs.checkpoint(db, run_id, key, name, status, detail)

    # Re-scan idempotent stages on retry: late source items must not be omitted
    # after a crash. Processed items/written bodies/news versions are skipped.
    for key, label, operation in (("analyze", "新闻分析", analyze), ("discover", "模型收录与对账", queue_model_drafts),
                                  ("edit", "文章编排", compose_articles)):
        state["value"] = label
        with SessionLocal() as db:
            jobs.checkpoint(db, run_id, key, label, "running")
        try:
            if not os.getenv("DEEPSEEK_API_KEY"):
                status, detail = "blocked", "未配置 DEEPSEEK_API_KEY，原始资料已保存"
            else:
                await operation()
                status, detail = "done", None
                if key == "discover":
                    with SessionLocal() as db:
                        run_key = db.get(CollectionRun, run_id).key
                        status, detail = release_outcome(db, run_key[6:] if run_key.startswith("daily:") else None)
        except Exception as exc:
            status, detail = "failed", type(exc).__name__
            logger.warning("Stage %s failed: %s", key, detail)
        with SessionLocal() as db:
            jobs.checkpoint(db, run_id, key, label, status, detail)
    with SessionLocal() as db:
        jobs.finish(db, run_id)


async def analyze():
    await build_insights(followups=False)
    if run_state["last_error"]:
        raise RuntimeError("新闻分析部分失败，失败条目保留待重试")


async def run_worker():
    init_db()
    with process_lock("collector"):
        with SessionLocal() as db:
            jobs.recover_interrupted(db)
        async with worker_heartbeat("collector") as state:
            while True:
                with SessionLocal() as db:
                    jobs.ensure_daily(db)
                    row = jobs.claim(db)
                    target = row.id if row else None
                if target:
                    await run_collection(target, state)
                else:
                    state["value"] = "idle"
                    await asyncio.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
