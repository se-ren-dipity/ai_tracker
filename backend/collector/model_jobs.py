"""SQLite-backed queue. A lease fences stale workers; research is checkpointed."""
import json
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, update, func, case
from sqlalchemy.dialects.sqlite import insert

from backend.models.model_db import AIModel
from backend.models.model_task import ModelTask
from backend.models.pending import PendingModel

LEASE_SECONDS = 90
MAX_ATTEMPTS = 3


class LeaseLost(RuntimeError):
    pass


def enqueue(db, pending_id, reason="手动复查", priority=20, force=True):
    now = datetime.utcnow()
    db.execute(insert(ModelTask).values(pending_id=pending_id, reason=reason, priority=priority, requested_at=now)
               .on_conflict_do_nothing(index_elements=["pending_id"]))
    task = db.query(ModelTask).filter_by(pending_id=pending_id).one()
    if task.status in ("queued", "retry", "running"):
        if reason == "相关报道更新":
            task.requested_at = now
        task.priority = max(task.priority, priority)
    elif force:
        task.status, task.stage, task.reason = "queued", "research", reason
        task.attempts, task.priority = 0, priority
        task.next_run_at, task.requested_at = now, now
        task.checkpoint = task.error = task.owner = task.lease_until = task.finished_at = task.started_at = None
    return task


def enqueue_due(db, now=None):
    from backend.collector.model_review import queue_existing_models
    now = now or datetime.utcnow()
    for row in db.query(PendingModel).filter_by(status="draft").all():
        task = db.query(ModelTask).filter_by(pending_id=row.id).first()
        due = row.reviewed_at is None or row.reviewed_at + timedelta(days=7) <= now
        if due and (task is None or task.status == "done"):
            enqueue(db, row.id, "待确认模型复查", 0)
    for model in db.query(AIModel).filter(or_(AIModel.next_review_at.is_(None), AIModel.next_review_at <= now)).all():
        row = db.query(PendingModel).filter_by(model_id=model.id, status="approved").order_by(PendingModel.id.desc()).first()
        task = db.query(ModelTask).filter_by(pending_id=row.id).first() if row else None
        if task and task.status != "done":
            continue
        ids = queue_existing_models(db, model.id)
        enqueue(db, ids[0], "定期资料复查", -5)
    db.commit()


def claim_task(db, owner, now=None):
    now = now or datetime.utcnow()
    available = or_(and_(ModelTask.status.in_(["queued", "retry"]), ModelTask.next_run_at <= now),
                    and_(ModelTask.status == "running", ModelTask.lease_until <= now))
    row = db.query(ModelTask).filter(available).order_by(
        case((ModelTask.status == "running", 0), else_=1),
        ModelTask.priority.desc(), ModelTask.next_run_at, ModelTask.id).first()
    if row is None:
        return None
    if row.attempts >= MAX_ATTEMPTS:
        db.execute(update(ModelTask).where(ModelTask.id == row.id, available).values(status="failed", error="任务连续中断，可手动重试", owner=None, lease_until=None))
        db.commit()
        return None
    # Compare state and deadline in the write itself, not only in the earlier read.
    result = db.execute(update(ModelTask).where(ModelTask.id == row.id, available).values(
        status="running", owner=owner, lease_until=now + timedelta(seconds=LEASE_SECONDS),
        started_at=func.coalesce(ModelTask.started_at, now), attempts=ModelTask.attempts + 1, error=None).execution_options(synchronize_session=False))
    db.commit()
    if not result.rowcount:
        return None
    db.expire_all()
    return db.get(ModelTask, row.id)


def assert_owner(db, task_id, owner):
    task = db.get(ModelTask, task_id)
    if task is None or task.status != "running" or task.owner != owner or task.lease_until <= datetime.utcnow():
        raise LeaseLost("任务已由其他执行进程接手")
    return task


def heartbeat(db, task_id, owner):
    task = assert_owner(db, task_id, owner)
    task.lease_until = datetime.utcnow() + timedelta(seconds=LEASE_SECONDS)
    db.commit()


def checkpoint_task(db, task_id, owner, research):
    task = assert_owner(db, task_id, owner)
    task.checkpoint = json.dumps(research, ensure_ascii=False)
    task.stage = "decision"


def finish_task(db, task_id, owner):
    task = assert_owner(db, task_id, owner)
    if task.requested_at > task.started_at:
        task.status, task.stage, task.checkpoint = "queued", "research", None
        task.next_run_at, task.attempts = datetime.utcnow(), 0
        task.started_at = None
    else:
        task.status, task.stage = "done", "complete"
    task.finished_at = datetime.utcnow()
    task.owner = task.lease_until = None


def fail_task(db, task_id, owner, error):
    task = assert_owner(db, task_id, owner)
    task.error = f"处理失败（{type(error).__name__}）"
    task.status = "failed" if task.attempts >= MAX_ATTEMPTS else "retry"
    task.next_run_at = datetime.utcnow() + timedelta(minutes=2 ** task.attempts)
    task.owner = task.lease_until = None
    db.commit()


def task_status(db):
    rows = db.query(ModelTask).all()
    counts = {status: sum(row.status == status for row in rows) for status in ("queued", "running", "retry", "done", "failed")}
    current = next((row for row in rows if row.status == "running"), None)
    pending = db.get(PendingModel, current.pending_id) if current else None
    failed = next((row for row in reversed(rows) if row.status == "failed"), None)
    return {"running": bool(current), "queued": counts["queued"] + counts["retry"], "counts": counts,
            "phase": ("搜索与补充资料" if current.stage == "research" else "判断入库") if current else None,
            "current_model": json.loads(pending.draft)["name"] if pending else None,
            "completed": counts["done"], "total": len(rows), "last_error": failed.error if failed else None}
