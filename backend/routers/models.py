"""FastAPI router for the AI model registry."""
import json
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.model_db import AIModel
from backend.models.insight import Insight
from backend.models.model_task import ModelInsight, ModelObservation
from backend.collector.insight_reader import reading_assets, web_url
from backend.collector.model_profiles import FIELD_LABELS

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
def list_models(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    type: Optional[str] = None,
    organization: Optional[str] = None,
    license: Optional[str] = None,
    params_min: Optional[float] = None,
    params_max: Optional[float] = None,
    context_min: Optional[int] = None,
    language: Optional[str] = None,
    q: Optional[str] = None,
    sort: Optional[str] = "release_date",
    db: Session = Depends(get_db),
):
    query = db.query(AIModel)

    if type:
        query = query.filter(AIModel.type == type)
    if organization:
        query = query.filter(AIModel.organization == organization)
    if license:
        query = query.filter(AIModel.license == license)
    if params_min is not None:
        query = query.filter(AIModel.params_b >= params_min)
    if params_max is not None:
        query = query.filter(AIModel.params_b <= params_max)
    if context_min is not None:
        query = query.filter(AIModel.context_length >= context_min)
    if language:
        query = query.filter(AIModel.languages.contains(language))
    if q:
        query = query.filter(
            or_(AIModel.name.contains(q), AIModel.organization.contains(q), AIModel.notes.contains(q))
        )

    # Sorting
    sort_col = {
        "release_date": AIModel.release_date.desc().nullslast(),
        "params_b": AIModel.params_b.desc().nullslast(),
        "name": AIModel.name,
    }.get(sort, AIModel.release_date.desc().nullslast())

    total = query.count()
    items = query.order_by(sort_col).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [m.to_dict() for m in items]}


@router.get("/filters")
def get_filter_options(db: Session = Depends(get_db)):
    """Return distinct values for filter dropdowns."""
    types = [r[0] for r in db.query(AIModel.type).distinct().all() if r[0]]
    orgs = [r[0] for r in db.query(AIModel.organization).distinct().order_by(AIModel.organization).all() if r[0]]
    licenses = [r[0] for r in db.query(AIModel.license).distinct().all() if r[0]]
    return {"types": sorted(types), "organizations": orgs, "licenses": sorted(licenses)}


@router.get("/{model_id}")
def get_model(model_id: int, db: Session = Depends(get_db)):
    m = db.query(AIModel).filter(AIModel.id == model_id).first()
    if not m:
        raise HTTPException(404, "未找到这个模型")
    stories = db.query(Insight).filter(or_(Insight.id.in_(db.query(ModelInsight.insight_id).filter_by(model_id=model_id)),
                                          Insight.related_model_id == model_id)).order_by(Insight.published.desc(), Insight.id.desc()).limit(20).all()
    articles = []
    for story in stories:
        images = reading_assets(db, story.id)["images"]
        articles.append({"id": story.id, "title": story.title, "summary": story.summary,
                         "category": story.category, "published": story.published.isoformat() if story.published else None,
                         "cover_image": images[0] if images else None})
    observations = db.query(ModelObservation).filter_by(model_id=model_id, status="accepted").order_by(ModelObservation.observed_at.desc(), ModelObservation.id.desc()).all()
    updates = [{"field": row.field, "label": FIELD_LABELS[row.field], "value": json.loads(row.value),
                "previous_value": json.loads(row.previous_value or "null"),
                "date": (row.resolved_at or row.observed_at).isoformat()}
               for row in observations if row.state == "known" and row.value != row.previous_value][:12]
    links = []
    for label, url in [("模型权重", m.hf_url), ("技术报告 / 介绍", m.paper_url), ("项目代码", m.github_url)]:
        if web_url(url):
            links.append({"label": label, "url": url})
    references = list(dict.fromkeys(row.url for row in observations if web_url(row.url)))[:6]
    return m.to_dict() | {"articles": articles, "updates": updates, "links": links, "references": references}


@router.post("")
def create_model(data: dict, db: Session = Depends(get_db)):
    m = AIModel(
        name=data["name"],
        full_name=data.get("full_name"),
        organization=data.get("organization"),
        type=data["type"],
        params_b=data.get("params_b"),
        context_length=data.get("context_length"),
        license=data.get("license"),
        languages=json.dumps(data.get("languages", []), ensure_ascii=False),
        modality_in=json.dumps(data.get("modality_in", ["text"]), ensure_ascii=False),
        modality_out=json.dumps(data.get("modality_out", ["text"]), ensure_ascii=False),
        benchmarks=json.dumps(data.get("benchmarks", {}), ensure_ascii=False),
        hf_url=data.get("hf_url"),
        paper_url=data.get("paper_url"),
        github_url=data.get("github_url"),
        release_date=date.fromisoformat(data["release_date"]) if data.get("release_date") else None,
        notes=data.get("notes"),
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m.to_dict()
