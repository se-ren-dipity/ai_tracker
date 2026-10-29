"""Single-host process locks and database-backed worker liveness."""
import asyncio
import os
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path


@contextmanager
def process_lock(name, wait=False):
    directory = Path(os.getenv("DB_PATH", "data/tracker.db")).resolve().parent
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f".{name}.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def beat(role, state):
    from backend.database import SessionLocal
    from backend.models.operations import WorkerStatus
    with SessionLocal() as db:
        db.merge(WorkerStatus(role=role, state=state, last_seen=datetime.utcnow()))
        db.commit()


@asynccontextmanager
async def worker_heartbeat(role):
    state = {"value": "idle"}

    async def pulse():
        while True:
            await asyncio.to_thread(beat, role, state["value"])
            await asyncio.sleep(20)

    async with asyncio.TaskGroup() as tasks:
        task = tasks.create_task(pulse())
        try:
            yield state
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await asyncio.to_thread(beat, role, "stopped")


def worker_status(db):
    from backend.models.operations import WorkerStatus
    result = []
    for role in ("collector", "models"):
        row = db.get(WorkerStatus, role)
        online = bool(row and row.state != "stopped" and row.last_seen > datetime.utcnow() - timedelta(seconds=90))
        result.append({"role": role, "online": online, "state": row.state if row else "not_started",
                       "last_seen": row.last_seen.isoformat() + "Z" if row else None})
    return result


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    from backend.database import SessionLocal
    with SessionLocal() as db:
        healthy = any(row["role"] == sys.argv[1] and row["online"] for row in worker_status(db))
    raise SystemExit(0 if healthy else 1)
