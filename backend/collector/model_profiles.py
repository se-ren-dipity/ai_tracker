"""Published model facts, proposed corrections and related reporting."""
import hashlib
import json
from datetime import date, datetime, timedelta

from backend.models.insight import Insight
from backend.models.model_db import AIModel
from backend.models.model_task import ModelInsight, ModelObservation

FIELD_LABELS = {"organization": "机构", "params_b": "参数量", "context_length": "上下文",
                "license": "许可证", "languages": "支持语言", "modality_in": "输入", "modality_out": "输出",
                "release_date": "发布日期", "announced_date": "公布日期", "available_date": "开放日期",
                "weight_access": "模型权重", "api_availability": "API 服务"}
EMPTY = (None, "", [], "[]", "Unknown", "unknown")


def set_fact(model, field, value):
    if field.endswith("_date"):
        value = date.fromisoformat(value) if value else None
    elif isinstance(value, list):
        value = json.dumps(value, ensure_ascii=False)
    setattr(model, field, value)
    if field == "release_date":
        model.release_precision = "day"


def schedule_review(model, research):
    checked = datetime.fromisoformat(research["checked_at"]).replace(tzinfo=None) if research.get("checked_at") else datetime.utcnow()
    states = json.loads(model.field_states or "{}")
    model.last_checked_at = checked
    slow = model.weight_access == "closed" or "undisclosed" in states.values()
    model.next_review_at = checked + timedelta(days=30 if slow else 7)


def record_research(db, model, research, initial=False):
    if research.get("status") != "complete":
        return
    checked = datetime.fromisoformat(research["checked_at"]).replace(tzinfo=None) if research.get("checked_at") else datetime.utcnow()
    states = json.loads(model.field_states or "{}")
    current = model.to_dict()
    seen = set()
    entries = [dict(item, state="known") for item in research.get("updates", [])] + research.get("field_statuses", [])
    for item in entries:
        field, state = item["field"], item["state"]
        if field not in FIELD_LABELS:
            continue
        value, old = item.get("value"), current.get(field)
        signature = json.dumps([model.id, field, state, value, item.get("url"), item.get("quote")], ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(signature.encode()).hexdigest()
        if fingerprint in seen or db.query(ModelObservation.id).filter_by(fingerprint=fingerprint).first():
            continue
        seen.add(fingerprint)
        precise = field == "release_date" and old and model.release_precision == "month" and str(old)[:7] == str(value)[:7]
        conflict = state == "conflict" or (state == "known" and old not in EMPTY and old != value and not precise)
        observation = ModelObservation(model_id=model.id, fingerprint=fingerprint, field=field,
            value=json.dumps(value, ensure_ascii=False), previous_value=json.dumps(None if initial else old, ensure_ascii=False),
            state=state, status="proposed" if conflict else "accepted", url=item.get("url"), quote=item.get("quote"), observed_at=checked)
        db.add(observation)
        if conflict:
            states[field] = "conflict"
        elif state == "known":
            set_fact(model, field, value)
            current[field] = value
            states[field] = "known"
            observation.resolved_at = checked
        elif old in EMPTY:
            # A failed search must not erase an earlier explicit nondisclosure.
            if states.get(field) != "undisclosed" or state != "not_found":
                states[field] = state
            observation.resolved_at = checked
    model.field_states = json.dumps(states, ensure_ascii=False)
    schedule_review(model, research)


def resolve_observation(db, row, accept):
    if row.status != "proposed":
        return
    model = db.get(AIModel, row.model_id)
    states = json.loads(model.field_states or "{}")
    value = json.loads(row.value or "null")
    if accept:
        if row.state != "known" or value is None:
            raise ValueError("这条记录没有可采用的确定值，请补查资料")
        current = model.to_dict()[row.field]
        previous = json.loads(row.previous_value or "null")
        if current != previous and current != value:
            raise ValueError("该字段已有其他更新，请重新补查后处理")
        row.previous_value = json.dumps(current, ensure_ascii=False)
        set_fact(model, row.field, value)
        row.status = "accepted"
    else:
        row.status = "dismissed"
    row.resolved_at = datetime.utcnow()
    db.flush()
    others = db.query(ModelObservation.id).filter_by(model_id=model.id, field=row.field, status="proposed").first()
    states[row.field] = "conflict" if others else ("known" if model.to_dict()[row.field] not in EMPTY else "not_found")
    model.field_states = json.dumps(states, ensure_ascii=False)


def link_model(db, model_id, insight):
    link = db.get(ModelInsight, (model_id, insight.id))
    changed = link is None or link.seen_at != insight.generated_at
    if link is None:
        db.add(ModelInsight(model_id=model_id, insight_id=insight.id, seen_at=insight.generated_at))
        db.flush()
    else:
        link.seen_at = insight.generated_at
    if not insight.related_model_id:
        insight.related_model_id = model_id
    return changed


def bootstrap_profiles(db):
    """Idempotently retain existing associations and earlier research evidence."""
    from backend.collector.insight_models import model_key
    from backend.models.pending import PendingModel
    models = db.query(AIModel).all()
    by_name = {model_key(name): model for model in models for name in (model.name, model.full_name) if name}
    for insight in db.query(Insight).all():
        ids = {insight.related_model_id} if insight.related_model_id else set()
        ids.update(by_name[model_key(name)].id for name in json.loads(insight.model_names or "[]") if model_key(name) in by_name)
        for model_id in ids:
            if not db.get(ModelInsight, (model_id, insight.id)):
                link_model(db, model_id, insight)
    for row in db.query(PendingModel).filter_by(status="approved").all():
        model = db.get(AIModel, row.model_id) if row.model_id else None
        if model and model.last_checked_at is None:
            record_research(db, model, json.loads(row.evidence or "{}").get("research", {}), initial=True)
    db.commit()
