"""Read operations and explicit admin controls; execution stays in workers."""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StrictBool
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.operations import AdminEvent, CollectionRun, SourceSetting
from backend.collector.collection_jobs import retry
from backend.collector.source_settings import configured_sources, source_overview
from backend.collector.model_jobs import task_status
from backend.runtime import worker_status
from backend.assistant.schemas import Settings as AssistantSettings
from backend.assistant import session as assistant_session
from backend.models.assistant import AssistantConfig

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/assistant")
def assistant_overview():
    return assistant_session.admin_overview()


@router.put("/assistant")
def assistant_settings(body: AssistantSettings):
    with assistant_session.transaction() as db:
        db.merge(AssistantConfig(id=1, value=body.model_dump_json()))
    return assistant_session.admin_overview()


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    from backend.collector.source_health import research_health
    from backend.collector.model_discovery import coverage
    return {"workers": worker_status(db), "sources": source_overview(db),
            "runs": [row.to_dict() for row in db.query(CollectionRun).order_by(CollectionRun.id.desc()).limit(30)],
            "model_tasks": task_status(db), "model_coverage": coverage(db), "research_service": research_health(), "analysis_configured": bool(os.getenv("DEEPSEEK_API_KEY")),
            "schedule": "每天 10:00 · Asia/Shanghai",
            "events": [{"action": row.action, "at": row.created_at.isoformat() + "Z"}
                       for row in db.query(AdminEvent).order_by(AdminEvent.id.desc()).limit(20)]}


class SourceSwitch(BaseModel):
    enabled: StrictBool


@router.patch("/sources/{key}")
def toggle_source(key: str, body: SourceSwitch, db: Session = Depends(get_db)):
    source = next((source for source in configured_sources(db) if source["key"] == key), None)
    if source is None:
        raise HTTPException(404, "未找到采集源")
    db.merge(SourceSetting(key=key, enabled=body.enabled))
    db.add(AdminEvent(action=f"{source['name']}：{'启用' if body.enabled else '停用'}"))
    db.commit()
    return {"key": key, "enabled": body.enabled}


@router.post("/runs/{run_id}/retry", status_code=202)
def retry_run(run_id: int, db: Session = Depends(get_db)):
    row = db.get(CollectionRun, run_id)
    if row is None:
        raise HTTPException(404, "未找到任务")
    try:
        retry(db, row)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return row.to_dict()


@router.post("/research/retry", status_code=202)
def retry_research(db: Session = Depends(get_db)):
    from backend.collector.source_health import clear_research_block
    clear_research_block()
    db.add(AdminEvent(action="重新检测模型补查服务"))
    db.commit()
    return {"status": "queued"}
