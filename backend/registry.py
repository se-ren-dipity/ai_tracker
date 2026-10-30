"""Shared, read-only identity retrieval for the web app, model agent and MCP."""
import json
import re
from difflib import SequenceMatcher

from backend.models.model_db import AIModel
from backend.models.model_event import ModelAlias, ModelEvent
from backend.models.model_task import ModelObservation
from backend.models.model_table import ModelTableEntry
from backend.models.pending import PendingModel


def key(value):
    return re.sub(r"[\W_]+", "", (value or "").casefold().replace("+", "plus"))


def organization_key(value):
    value = key(re.split(r"[（(]", value or "")[0])
    return {"deepseekai": "deepseek", "stepfunai": "stepfun", "alibabaqwenteam": "alibaba",
            "google": "googledeepmind", "openmoss": "openmossteam", "扩散智能": "diffuspace",
            "面壁智能": "openbmb", "月之暗面": "moonshotai", "moonshot": "moonshotai",
            "meta": "metaai", "metasuperintelligencelab": "metaai",
            "腾讯混元": "tencent", "tencenthunyuan": "tencent",
            "蚂蚁百灵": "inclusionai", "antgroup": "inclusionai",
            "蚂蚁数科": "antdigital", "上海人工智能实验室": "shanghaiailab",
            "百度": "baidu", "商汤": "sensetime", "数美万物": "mathmagic",
            "科大讯飞": "iflytek", "sparkllm": "iflytek", "sparkllm团队": "iflytek", "xhtoken": "iflytek",
            "新加坡国立大学xmllab": "nationaluniversityofsingapore",
            "智源研究院": "beijingacademyofartificialintelligence",
            "米哈游崩坏ai团队": "mihoyohonkaiairdteam"}.get(value, value)


def search_models(db, query, organization=None, limit=10, offset=0, include_pending=False):
    needle = key(query)
    if not needle:
        return {"total": 0, "items": [], "pending": []}
    aliases = {}
    for row in db.query(ModelAlias).all():
        aliases.setdefault(row.model_id, []).append(row.name)
    for row in db.query(ModelTableEntry).all():
        aliases.setdefault(row.model_id, []).append(json.loads(row.public_row).get("model", ""))
    matches = []
    for model in db.query(AIModel).all():
        names = [v for v in [model.name, model.full_name, *aliases.get(model.id, [])] if v]
        exact = any(key(name) == needle for name in names)
        score = max(SequenceMatcher(None, needle, key(name)).ratio() for name in names)
        contains = any(needle in key(name) for name in names)
        org_match = not organization or organization_key(organization) == organization_key(model.organization)
        if exact or contains or score >= .65:
            matches.append({**model.to_dict(), "aliases": aliases.get(model.id, []),
                            "exact_name": exact, "organization_match": org_match,
                            "match_reason": "名称或已确认别名一致" if exact else "名称相近，仅作为候选",
                            "score": round(score, 3)})
    matches.sort(key=lambda row: (not row["organization_match"], not row["exact_name"], -row["score"], row["id"]))
    pending = []
    if include_pending:
        for row in db.query(PendingModel).filter(PendingModel.model_id.is_(None)).all():
            draft = json.loads(row.draft)
            if needle in key(draft.get("name")) or SequenceMatcher(None, needle, key(draft.get("name"))).ratio() >= .65:
                pending.append({"id": row.id, "name": draft.get("name"), "organization": draft.get("organization"),
                                "status": row.status, "reason": row.review_reason})
    return {"total": len(matches), "items": matches[offset:offset + min(max(limit, 1), 50)], "pending": pending[:20]}


def identity_matches(db, draft):
    matches = {}
    for name in (draft.get("name"), draft.get("full_name")):
        if name:
            # Exact matching must not be truncated by the public search pagination.
            offset = 0
            while True:
                result = search_models(db, name, draft.get("organization"), limit=50, offset=offset)
                for row in result["items"]:
                    if row["exact_name"] and row["organization_match"] and compatible_versions(draft.get("name", ""), row["name"]):
                        matches[row["id"]] = row
                offset += 50
                if offset >= result["total"]:
                    break
    return list(matches.values())


def mentions(name, text):
    parts = re.findall(r"[a-z]+|\d+(?:\.\d+)?|[\u4e00-\u9fff]+", name.casefold())
    return bool(parts and re.search(r"(?<![a-z0-9])" + r"[\W_]*".join(re.escape(p) for p in parts) + r"(?![a-z0-9])", text.casefold()))


def mentions_model_version(name, text):
    """Accept an explicit family/size list without joining unrelated model names."""
    if mentions(name, text):
        return True
    variant = re.fullmatch(r"(.+?)[\s_-]+(\d+(?:\.\d+)?[BM]|Base|SFT|Instruct|Chat)", name, re.I)
    if not variant:
        return False
    family, size = variant.groups()
    parts = re.findall(r"[a-z]+|\d+(?:\.\d+)?|[\u4e00-\u9fff]+", family.casefold())
    family_pattern = r"(?<![a-z0-9])" + r"[\W_]*".join(re.escape(p) for p in parts) + r"(?![a-z0-9])"
    bridge = r"(?:[\s，,：:（）()]|视觉|语言|多模态|模型|系列|版本|家族|models?|series|family)*"
    listing = re.search(family_pattern + bridge + r"(?:包括|包含|提供|includes?|contains?|offers?)\s*([^。；;\n!?！？]{1,200})", text, re.I)
    if not listing:
        return False
    values = listing[1]
    # Another list or a comparison is not evidence that its sizes belong to this family.
    if re.search(r"包括|包含|提供|相比|对比|优于|另有|另外|includes?|contains?|offers?|versus|\bvs\b", values, re.I):
        return False
    sizes = re.findall(r"(?<![a-z0-9])\d+(?:\.\d+)?[bm](?![a-z0-9])", values, re.I)
    if len(set(s.casefold() for s in sizes)) < 2 and not re.search(r"版本|型号|尺寸|\b(?:variants?|versions?|sizes?)\b", values, re.I):
        return False
    return mentions(size, values)


def compatible_versions(left, right):
    def signature(name):
        return set(re.findall(r"\d+(?:\.\d+)?(?:b|m)?|\b(?:base|instruct|chat)\b", name.casefold()))
    a, b = signature(left), signature(right)
    return not a or not b or a == b


def find_model(db, draft):
    matches = identity_matches(db, draft)
    return db.get(AIModel, matches[0]["id"]) if len(matches) == 1 else None


def remember_alias(db, model, name):
    if name and key(name) not in {key(model.name), key(model.full_name)}:
        if not db.query(ModelAlias).filter_by(model_id=model.id, key=key(name)).first():
            db.add(ModelAlias(model_id=model.id, name=name, key=key(name)))


def get_model(db, model_id):
    model = db.get(AIModel, model_id)
    if model is None:
        raise ValueError("未找到模型")
    return model.to_dict() | {
        "aliases": [row.name for row in db.query(ModelAlias).filter_by(model_id=model_id)],
        "events": [row.to_dict() for row in db.query(ModelEvent).filter_by(model_id=model_id)
                   .order_by(ModelEvent.occurred_on.desc(), ModelEvent.id.desc()).limit(30)],
        "conflicts": [row.to_dict() for row in db.query(ModelObservation).filter_by(model_id=model_id, status="proposed").limit(30)]}


def get_evidence(db, model_id, field=None, limit=30):
    query = db.query(ModelObservation).filter_by(model_id=model_id)
    if field:
        query = query.filter_by(field=field)
    return [row.to_dict() for row in query.order_by(ModelObservation.id.desc()).limit(min(max(limit, 1), 100))]
