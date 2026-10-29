"""Incrementally sync the public LifeArchitect table without per-model API calls."""
import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


from backend.collector.insight_models import model_key, model_lookup
from backend.collector.model_profiles import EMPTY, FIELD_LABELS, link_model, set_fact
from backend.database import SessionLocal, init_db
from backend.models.insight import Insight
from backend.models.model_db import AIModel
from backend.models.model_table import ModelTableEntry, SOURCE_URL
from backend.models.model_task import ModelObservation, ModelTask
from backend.models.pending import PendingModel
from backend.collector.source_fetch import fetch_source
from backend.runtime import process_lock

PUBLIC_FEED_URL = "https://models-table-data.adt-360.workers.dev/"
logger = logging.getLogger(__name__)
# Only public columns. Locked columns and the changing row counter are ignored.
HEADERS = {"model": "Model", "lab": "Lab", "playground": "Playground",
           "params_total": "Params (total, B)", "estimated_params": "Estimated total params?",
           "architecture": "Arch", "mmlu": "MMLU", "mmlu_pro": "MMLU-Pro",
           "gpqa": "GPQA", "hle": "HLE", "announced": "Announced▼",
           "public": "Public?", "paper": "Paper / Repo", "tags": "Tags", "notes": "Notes"}


def number(value, allow_zero=False):
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) and (result >= 0 if allow_zero else result > 0) else None
    except (ValueError, TypeError):
        return None


def public_link(value):
    url = urlsplit(value)
    return value if url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password else None


def hf_repository(row):
    for value in (row["playground"], row["paper"]):
        url = urlsplit(value)
        parts = url.path.strip("/").split("/")
        if url.hostname == "huggingface.co" and len(parts) == 2 and parts[0] not in {"collections", "blog", "spaces", "datasets"}:
            return "/".join(parts)
    return None


def parse_candidates(text, today=None):
    rows = [row for row in csv.reader(io.StringIO(text.lstrip("\ufeff"))) if any(cell.strip() for cell in row)]
    normalize = lambda value: re.sub(r"\s+", "", value).casefold()
    expected = {key: normalize(value) for key, value in HEADERS.items()}
    if len(rows) < 3:
        raise ValueError("Models Table 公开数据为空或不完整")
    header = [normalize(value) for value in rows[1]]
    if any(value not in header for value in expected.values()):
        raise ValueError("Models Table 的公开 CSV 列结构已变化，请核对后再同步")
    indexes = {key: header.index(value) for key, value in expected.items()}
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    candidates = []
    for values in rows[2:]:
        if len(values) <= max(indexes.values()):
            raise ValueError("Models Table 存在不完整的数据行，本轮未写入")
        row = {key: values[index].strip() for key, index in indexes.items()}
        if not row["model"] or not row["lab"]:
            raise ValueError("Models Table 存在缺少模型名或机构的数据行")
        try:
            announced = datetime.strptime(row["announced"], "%b/%Y").date()
        except ValueError as exc:
            raise ValueError(f"Models Table 日期格式无法识别：{row['announced']}") from exc
        if announced > today:
            continue
        row["release_date"] = announced.isoformat()
        candidates.append(row)
    if not candidates:
        raise ValueError("Models Table 没有可同步的已公布模型")
    names = Counter((model_key(row["lab"]), model_key(row["model"])) for row in candidates)
    seen = set()
    for row in candidates:
        name = (model_key(row["lab"]), model_key(row["model"]))
        key = "|".join(name)
        if names[name] > 1:
            # The table includes same-name variants, e.g. MPT 7B and MPT 1.3B.
            key += "|" + (hf_repository(row) or row["paper"] or row["playground"])
        if key in seen:
            raise ValueError(f"Models Table 存在无法区分的同名模型：{row['model']}")
        seen.add(key)
        row["source_key"] = key
    return sorted(candidates, key=lambda row: row["release_date"], reverse=True)


def read_candidates(path, limit=None):
    return parse_candidates(Path(path).read_text(encoding="utf-8-sig"))[:limit]


def make_draft(row):
    """Map public facts only. No HF requests or LLM guesses are needed."""
    hf_id = hf_repository(row)
    paper = public_link(row["paper"])
    github = paper if paper and urlsplit(paper).hostname == "github.com" else None
    draft = {"name": row["model"], "full_name": hf_id or f"{row['lab']}/{row['model']}",
             "organization": row["lab"], "type": "Unknown",
             "params_b": None if row["estimated_params"] else number(row["params_total"]),
             "license": None, "context_length": None, "languages": [],
             "benchmarks": {label: number(row[key], True) for key, label in
                            [("mmlu", "MMLU"), ("mmlu_pro", "MMLU-Pro"), ("gpqa", "GPQA"), ("hle", "HLE")]
                            if number(row[key], True) is not None},
             "hf_url": f"https://huggingface.co/{hf_id}" if hf_id else None,
             "paper_url": paper if not github else None, "github_url": github,
             "source_url": SOURCE_URL, "release_date": row["release_date"],
             "release_precision": "month", "notes": row.get("notes") or None}
    return draft, {"source_url": SOURCE_URL, "public_row": row}


def organization_key(name):
    key = model_key(re.split(r"[（(]", name or "")[0])
    return {"deepseekai": "deepseek", "stepfunai": "stepfun", "上海人工智能实验室": "shanghaiailab",
            "alibabaqwenteam": "alibaba"}.get(key, key)


def match_existing(models, draft, assigned):
    candidates = [model for model in models if model.id not in assigned]
    if draft.get("hf_url"):
        exact = [model for model in candidates if model.hf_url == draft["hf_url"] or model.full_name == draft["full_name"]]
        if len(exact) == 1:
            return exact[0]
    keys = {model_key(draft[key]) for key in ("name", "full_name")}
    matches = [model for model in candidates if keys & {model_key(value) for value in (model.name, model.full_name) if value}
               and (not model.organization or organization_key(model.organization) == organization_key(draft["organization"]))]
    return matches[0] if len(matches) == 1 else None


def apply_public_fields(db, model, entry, draft, row, now):
    owned = json.loads(entry.applied_values or "{}")
    current = model.to_dict()
    values = {key: draft[key] for key in ("full_name", "organization", "params_b", "hf_url", "paper_url", "github_url", "notes", "release_date")}
    values.update({f"benchmarks.{name}": draft["benchmarks"].get(name) for name in ("MMLU", "MMLU-Pro", "GPQA", "HLE")})
    changed = False
    states = json.loads(model.field_states or "{}")
    for field, value in values.items():
        if field in json.loads(model.locked_fields or "[]"):
            continue
        benchmark = field.startswith("benchmarks.")
        metric = field.split(".", 1)[1] if benchmark else None
        old = current["benchmarks"].get(metric) if benchmark else current[field]
        if old not in EMPTY and (field not in owned or old != owned[field]):
            continue
        # A manual refinement to an exact day must not revert to month precision.
        if field == "release_date" and old and model.release_precision != "month":
            continue
        if old == value:
            continue
        if benchmark:
            if value is None:
                current["benchmarks"].pop(metric, None)
            else:
                current["benchmarks"][metric] = value
            model.benchmarks = json.dumps(current["benchmarks"], ensure_ascii=False)
        else:
            set_fact(model, field, value)
            if field == "release_date":
                model.release_precision = "month"
        owned[field] = value
        changed = True
        if field in FIELD_LABELS:
            states[field] = "known" if value is not None else "not_found"
            fingerprint = hashlib.sha256(json.dumps([entry.source_key, row, field, old, value], sort_keys=True).encode()).hexdigest()
            if not db.query(ModelObservation.id).filter_by(fingerprint=fingerprint).first():
                db.add(ModelObservation(model_id=model.id, fingerprint=fingerprint, field=field,
                    value=json.dumps(value, ensure_ascii=False), previous_value=json.dumps(old, ensure_ascii=False),
                    state=states[field], status="accepted", url=SOURCE_URL,
                    quote=json.dumps(row, ensure_ascii=False), observed_at=now, resolved_at=now))
    if changed:
        model.field_states = json.dumps(states, ensure_ascii=False)
        model.last_checked_at = now
    if not model.source_url:
        model.source_url = SOURCE_URL
    model.next_review_at = None
    entry.applied_values = json.dumps(owned, ensure_ascii=False)
    return changed


def adopt_pending(db, model, draft, row, now, pending_by_model, pending_by_name):
    keys = {model_key(draft[key]) for key in ("name", "full_name")}
    candidates = {pending.id: pending for pending in pending_by_model.get(model.id, [])}
    for key in keys:
        candidates.update({pending.id: pending for pending in pending_by_name.get(key, [])})
    for pending in candidates.values():
        prior = json.loads(pending.draft)
        matches = keys & {model_key(prior[key]) for key in ("name", "full_name") if prior.get(key)}
        if pending.model_id != model.id and (pending.model_id or not matches):
            continue
        if pending.model_id is None and prior.get("organization") and model_key(prior["organization"]) != model_key(draft["organization"]):
            continue
        evidence = json.loads(pending.evidence or "{}")
        evidence["models_table"] = {"source_url": SOURCE_URL, "public_row": row}
        pending.evidence = json.dumps(evidence, ensure_ascii=False)
        if pending.status != "approved" or pending.model_id is None:
            pending.status, pending.model_id = "approved", model.id
            pending.draft = json.dumps(model.to_dict(), ensure_ascii=False)
            pending.review_decision, pending.review_reason = "approved", "由 LifeArchitect Models Table 公开表同步入库"
            pending.reviewer, pending.reviewed_at = "lifearchitect", now
        task = db.query(ModelTask).filter_by(pending_id=pending.id).first()
        if task and task.reason in {"发现新模型", "定期资料复查", "待确认模型复查"} and not evidence.get("insight_ids") and task.status != "done":
            task.status, task.stage, task.reason = "done", "complete", "公开模型表同步"
            task.owner = task.lease_until = task.error = None
            task.finished_at = now


async def sync_models_table(path=None, limit=None):
    if path:
        rows = read_candidates(path, limit)
    else:
        rows = (await fetch_source("Models Table", PUBLIC_FEED_URL,
            lambda raw: parse_candidates(raw.decode("utf-8-sig")),
            headers={"Origin": "https://lifearchitect.ai", "Referer": SOURCE_URL}))[:limit]
    # Initial backfill must not block the web server's event loop.
    return await asyncio.to_thread(apply_rows, rows)


def apply_rows(rows):
    # Parse and validate the whole response before changing any records.
    drafts = [(row, make_draft(row)[0]) for row in rows]
    now = datetime.utcnow()
    counts = {"new": 0, "updated": 0, "unchanged": 0}
    with process_lock("registry-write", wait=True), SessionLocal() as db:
        models = db.query(AIModel).all()
        pending_by_model, pending_by_name = {}, {}
        for pending in db.query(PendingModel).all():
            pending_by_model.setdefault(pending.model_id, []).append(pending)
            draft = json.loads(pending.draft)
            for key in {model_key(draft[field]) for field in ("name", "full_name") if draft.get(field)}:
                pending_by_name.setdefault(key, []).append(pending)
        entries = {entry.source_key: entry for entry in db.query(ModelTableEntry).all()}
        # Preserve mappings when identity normalization is corrected (e.g. Command-R vs Command-R+).
        snapshots = [(entry, json.loads(entry.public_row)) for entry in entries.values()]
        for row in rows:
            if row["source_key"] not in entries:
                previous = [(entry, old) for entry, old in snapshots if old["lab"] == row["lab"] and old["model"] == row["model"]]
                if len(previous) > 1:
                    previous = [(entry, old) for entry, old in previous if old["paper"] == row["paper"] and old["playground"] == row["playground"]]
                if len(previous) == 1:
                    entry = previous[0][0]
                    entries.pop(entry.source_key)
                    entry.source_key = row["source_key"]
                    entries[entry.source_key] = entry
        assigned = {entries[row["source_key"]].model_id for row in rows if row["source_key"] in entries}
        for row, draft in drafts:
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
            entry = entries.get(row["source_key"])
            if entry and entry.public_row == payload:
                entry.synced_at = now
                counts["unchanged"] += 1
                continue
            model = db.get(AIModel, entry.model_id) if entry else match_existing(models, draft, assigned)
            is_new = model is None
            if is_new:
                model = AIModel(name=draft["name"], type="Unknown", release_precision="month")
                db.add(model)
                db.flush()
                models.append(model)
            assigned.add(model.id)
            if entry is None:
                entry = ModelTableEntry(source_key=row["source_key"], model_id=model.id, public_row=payload, applied_values="{}", synced_at=now)
                db.add(entry)
                entries[row["source_key"]] = entry
            changed = apply_public_fields(db, model, entry, draft, row, now)
            entry.public_row, entry.synced_at = payload, now
            adopt_pending(db, model, draft, row, now, pending_by_model, pending_by_name)
            counts["new" if is_new else "updated" if changed else "unchanged"] += 1
        # Associate archived reporting too, including when news analysis is disabled.
        by_name = model_lookup([model for model in models if model.id in assigned],
                               [entry for entry in entries.values() if entry.model_id in assigned])
        for insight in db.query(Insight).all():
            for name in json.loads(insight.model_names or "[]"):
                if model_key(name) in by_name:
                    link_model(db, by_name[model_key(name)], insight)
        db.commit()
    logger.info("Models Table synced: %s", counts)
    return counts


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, nargs="?", help="可选：从公开 CSV 快照同步，默认拉取最新公开表")
    parser.add_argument("--limit", type=int, help="仅用于手动小批量同步，默认处理全部模型")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    init_db()
    print(json.dumps(asyncio.run(sync_models_table(args.csv_path, args.limit)), ensure_ascii=False))
