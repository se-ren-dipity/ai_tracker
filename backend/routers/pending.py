"""Model admission queue, automatic review and manual corrections."""
from datetime import datetime
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.pending import PendingModel
from backend.collector.model_review import promote, queue_existing_models
from backend.collector.model_jobs import enqueue, task_status
from backend.collector.model_profiles import resolve_observation
from backend.models.model_task import ModelTask, ModelObservation

router = APIRouter(prefix="/api/pending", tags=["pending"])


@router.get("")
def list_pending(status: Literal["draft", "approved", "rejected", "all"] = "draft", db: Session = Depends(get_db)):
    query = db.query(PendingModel)
    if status != "all":
        query = query.filter(PendingModel.status == status)
    counts = dict(db.query(PendingModel.status, func.count(PendingModel.id)).group_by(PendingModel.status).all())
    rows = query.order_by(PendingModel.reviewed_at.desc(), PendingModel.id.desc()).all()
    tasks = {row.pending_id: row.to_dict() for row in db.query(ModelTask).all()}
    return {"count": len(rows), "counts": counts, "review": task_status(db),
            "items": [r.to_dict() | {"task": tasks.get(r.id)} for r in rows]}


@router.post("/review")
async def start_review(pending_id: int | None = None, db: Session = Depends(get_db)):
    rows = db.query(PendingModel).filter(PendingModel.id == pending_id).all() if pending_id else db.query(PendingModel).filter_by(status="draft").all()
    if pending_id and not rows:
        raise HTTPException(404, "未找到模型记录")
    for row in rows:
        if row.status == "rejected":
            row.status = "draft"
        enqueue(db, row.id)
    db.commit()
    return {"status": "queued", "count": len(rows)}


@router.post("/enrich")
async def enrich_existing(model_id: int | None = None, db: Session = Depends(get_db)):
    ids = queue_existing_models(db, model_id)
    if not ids:
        return {"status": "empty"}
    for identifier in ids:
        enqueue(db, identifier)
    db.commit()
    return {"status": "queued", "count": len(ids)}


@router.get("/changes")
def proposed_changes(db: Session = Depends(get_db)):
    from backend.models.model_db import AIModel
    return {"items": [row.to_dict() | {"model_name": model.name} for row, model in
                     db.query(ModelObservation, AIModel).join(AIModel, AIModel.id == ModelObservation.model_id)
                     .filter(ModelObservation.status == "proposed").order_by(ModelObservation.id.desc()).all()]}


@router.post("/changes/{change_id}/{action}")
def review_change(change_id: int, action: Literal["accept", "dismiss"], db: Session = Depends(get_db)):
    row = db.get(ModelObservation, change_id)
    if not row:
        raise HTTPException(404, "未找到更正建议")
    try:
        resolve_observation(db, row, action == "accept")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return row.to_dict()


@router.post("/{pending_id}/approve")
def approve_pending(pending_id: int, db: Session = Depends(get_db)):
    row = db.get(PendingModel, pending_id)
    if not row:
        raise HTTPException(404, "未找到模型草稿")
    if row.status == "approved":
        model = promote(db, row)
        db.commit()
        return {"status": "approved", "model": model.to_dict()}
    try:
        m = promote(db, row)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    row.review_decision, row.review_reason = "approved", "人工确认入库"
    row.reviewer, row.reviewed_at = "manual", datetime.utcnow()
    db.commit()

    return {"status": "approved", "model": m.to_dict()}


@router.post("/{pending_id}/reject")
def reject_pending(pending_id: int, db: Session = Depends(get_db)):
    row = db.query(PendingModel).filter(PendingModel.id == pending_id).first()
    if not row:
        raise HTTPException(404, "未找到模型草稿")
    if row.status == "approved":
        raise HTTPException(409, "模型已入库，不能通过草稿丢弃操作移除")
    row.status, row.review_decision, row.review_reason = "rejected", "rejected", "人工丢弃"
    row.reviewer, row.reviewed_at = "manual", datetime.utcnow()
    db.commit()
    return {"status": "rejected"}
