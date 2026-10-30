"""Daily jobs and stage checkpoints for one collector on a single host."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.sqlite import insert
from backend.models.operations import CollectionRun

MAX_ATTEMPTS = 3


def ensure_daily(db, now=None):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if local.hour < 10:
        return
    db.execute(insert(CollectionRun).values(key="daily:" + local.date().isoformat())
               .on_conflict_do_nothing(index_elements=["key"]))
    db.commit()


def enqueue(db):
    # Serialize concurrent clicks before checking for unfinished work.
    from sqlalchemy import text
    db.execute(text("BEGIN IMMEDIATE"))
    row = db.query(CollectionRun).filter(CollectionRun.status.in_(["queued", "running", "retry"])).order_by(CollectionRun.id).first()
    if row is None:
        row = CollectionRun(key="manual:" + uuid.uuid4().hex)
        db.add(row)
    elif row.status == "retry":
        row.next_run_at = datetime.utcnow()
    db.commit()
    return row


def recover_interrupted(db):
    # Only called after acquiring the OS process lock; the previous owner is gone.
    for row in db.query(CollectionRun).filter_by(status="running").all():
        row.status = "retry" if row.attempts < MAX_ATTEMPTS else "failed"
        row.error = "执行进程中断，已保留完成步骤" if row.status == "retry" else "任务连续中断，请手动重试"
        row.next_run_at = datetime.utcnow()
    db.commit()


def claim(db, now=None):
    from sqlalchemy import update
    now = now or datetime.utcnow()
    row = db.query(CollectionRun).filter(CollectionRun.status.in_(["queued", "retry"]),
                                          CollectionRun.next_run_at <= now).order_by(CollectionRun.id).first()
    if row:
        identifier = row.id
        changed = db.execute(update(CollectionRun).where(CollectionRun.id == identifier,
                             CollectionRun.status.in_(["queued", "retry"]), CollectionRun.next_run_at <= now).values(
                                 status="running", error=None, attempts=CollectionRun.attempts + 1,
                                 started_at=row.started_at or now))
        db.commit()
        if not changed.rowcount:
            return None
        db.expire_all()
        return db.get(CollectionRun, identifier)
    return row


def retry(db, row):
    from sqlalchemy import update
    result = db.execute(update(CollectionRun).where(CollectionRun.id == row.id,
                        CollectionRun.status.in_(["failed", "blocked", "retry", "needs_evidence"])).values(
                            status="queued", attempts=0, error=None, finished_at=None, next_run_at=datetime.utcnow()))
    if not result.rowcount:
        db.rollback()
        raise ValueError("只有失败、待补证据、等待配置或等待重试的任务可以重试")
    db.commit()


def checkpoint(db, run_id, key, label, status, detail=None):
    row = db.get(CollectionRun, run_id)
    stages = json.loads(row.stages)
    stages[key] = {"label": label, "status": status, "detail": detail,
                   "updated_at": datetime.utcnow().isoformat() + "Z"}
    row.stages = json.dumps(stages, ensure_ascii=False)
    db.commit()


def finish(db, run_id):
    row = db.get(CollectionRun, run_id)
    stages = json.loads(row.stages).values()
    failed = [stage for stage in stages if stage["status"] in ("failed", "cached", "blocked", "needs_evidence")]
    row.error = "；".join(f"{stage['label']}：{stage['detail']}" for stage in failed) or None
    retryable = any(stage["status"] in {"failed", "cached"} for stage in failed)
    row.status = (("retry" if row.attempts < MAX_ATTEMPTS else "failed") if retryable else
                  "blocked" if any(stage["status"] == "blocked" for stage in failed) else
                  "needs_evidence" if failed else "done")
    row.next_run_at = datetime.utcnow() + timedelta(minutes=30)
    row.finished_at = datetime.utcnow() if row.status in ("done", "failed", "blocked", "needs_evidence") else None
    db.commit()
