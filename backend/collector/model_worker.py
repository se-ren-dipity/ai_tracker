"""Independent durable model worker: python -m backend.collector.model_worker."""
import asyncio
import logging
import os
import uuid
from datetime import datetime
from contextlib import suppress

from dotenv import load_dotenv
from pydantic import ValidationError
load_dotenv()

from backend.database import SessionLocal, init_db
from backend.collector.model_jobs import LeaseLost, claim_task, enqueue_due, fail_task, heartbeat, finish_task, pause_automatic_tasks
from backend.collector.model_review import review_models
from backend.runtime import process_lock, worker_heartbeat
from backend.collector.source_health import research_health, cooling_down
from backend.models.model_task import ModelTask

logger = logging.getLogger(__name__)


async def run_task(task_id, pending_id, owner):
    async def keep_lease():
        while True:
            await asyncio.sleep(20)
            with SessionLocal() as db:
                heartbeat(db, task_id, owner)
    pulse = asyncio.create_task(keep_lease())
    try:
        async with asyncio.timeout(900):
            await review_models([pending_id], retry=True, include_existing=True, task_id=task_id, owner=owner)
        # The record may have been manually rejected while waiting in the queue.
        with SessionLocal() as db:
            task = db.get(ModelTask, task_id)
            if task.status == "running" and task.owner == owner:
                finish_task(db, task_id, owner)
                db.commit()
    except LeaseLost:
        logger.warning("Task %s lease lost", task_id)
    except Exception as exc:
        if isinstance(exc, ValidationError):
            logger.warning("Validation details: %s", exc.errors(include_input=False, include_url=False))
        with SessionLocal() as db:
            with suppress(LeaseLost):
                fail_task(db, task_id, owner, exc)
        logger.warning("Task %s failed: %s", task_id, type(exc).__name__)
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError, LeaseLost):
            await pulse


async def run_worker():
    init_db()
    owner = f"{os.getpid()}-{uuid.uuid4().hex}"
    with process_lock("models"):
        async with worker_heartbeat("models") as state:
            while True:
                if not os.getenv("DEEPSEEK_API_KEY"):
                    state["value"] = "disabled_no_api_key"
                    await asyncio.sleep(10)
                    continue
                with SessionLocal() as db:
                    enqueue_due(db)
                    pause_automatic_tasks(db)
                    if cooling_down(research_health()):
                        state["value"] = "blocked"
                        target = None
                    else:
                        for blocked in db.query(ModelTask).filter_by(status="blocked"):
                            blocked.status = "queued"
                            blocked.next_run_at = datetime.utcnow()
                        db.commit()
                        task = claim_task(db, owner)
                        target = (task.id, task.pending_id) if task else None
                if target:
                    state["value"] = f"task:{target[0]}"
                    await run_task(*target, owner)
                else:
                    if state["value"] != "blocked":
                        state["value"] = "idle"
                    await asyncio.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
