"""Independent durable model worker: python -m backend.collector.model_worker."""
import asyncio
import logging
import os
import uuid
from contextlib import suppress

from dotenv import load_dotenv
from pydantic import ValidationError
load_dotenv()

from backend.database import SessionLocal, init_db
from backend.collector.model_jobs import LeaseLost, claim_task, enqueue_due, fail_task, heartbeat, finish_task
from backend.collector.model_review import review_models

logger = logging.getLogger(__name__)


async def run_task(task_id, pending_id, owner):
    async def keep_lease():
        while True:
            await asyncio.sleep(20)
            with SessionLocal() as db:
                heartbeat(db, task_id, owner)
    pulse = asyncio.create_task(keep_lease())
    try:
        await review_models([pending_id], retry=True, include_existing=True, task_id=task_id, owner=owner)
        # The record may have been manually rejected while waiting in the queue.
        from backend.models.model_task import ModelTask
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
    while True:
        try:
            with SessionLocal() as db:
                enqueue_due(db)
                task = claim_task(db, owner)
                target = (task.id, task.pending_id) if task else None
            if target:
                await run_task(*target, owner)
            else:
                await asyncio.sleep(10)
        except Exception as exc:
            logger.warning("Model worker tick failed: %s", type(exc).__name__)
            await asyncio.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is required")
    asyncio.run(run_worker())
