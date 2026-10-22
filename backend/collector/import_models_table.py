"""Import recent public LifeArchitect Models Table rows, then run DeepSeek review."""
import argparse
import asyncio
import csv
import json
import math
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from backend.collector.insight_models import model_key
from backend.collector.model_review import find_existing
from backend.database import SessionLocal, init_db
from backend.models.pending import PendingModel

SOURCE_URL = "https://lifearchitect.ai/models-table/"
# Public CSV layout published by the table's own renderer. Locked columns are ignored.
COLUMNS = {"model": 0, "lab": 1, "playground": 2, "params_total": 3, "params_active": 4,
           "architecture": 5, "estimated_params": 7, "mmlu": 11, "mmlu_pro": 12,
           "gpqa": 13, "hle": 14, "announced": 16, "public": 17, "paper": 21,
           "tags": 22, "notes": 23}


def number(value):
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) and result > 0 else None
    except ValueError:
        return None


def read_candidates(path, limit):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 3 or rows[1][0:3] != ["Model", "Lab", "Playground"] or "Announced" not in rows[1][16]:
        raise ValueError("Models Table 的公开 CSV 列结构已变化，请核对后再导入")
    candidates = []
    for values in rows[2:]:
        if len(values) <= max(COLUMNS.values()):
            continue
        row = {key: values[index].strip() for key, index in COLUMNS.items()}
        try:
            announced = datetime.strptime(row["announced"], "%b/%Y").date()
        except ValueError:
            continue
        if announced > date.today() or not row["model"]:
            continue
        row["release_date"] = announced.isoformat()
        candidates.append(row)
    candidates.sort(key=lambda row: row["release_date"], reverse=True)
    seen, selected = set(), []
    for row in candidates:
        key = model_key(row["model"])
        if key not in seen:
            seen.add(key)
            selected.append(row)
        if len(selected) == limit:
            break
    return selected


async def make_draft(row, client):
    hf_id, hf = None, {}
    for value in (row["playground"], row["paper"]):
        url = urlsplit(value)
        parts = url.path.strip("/").split("/")
        if url.hostname == "huggingface.co" and len(parts) == 2 and parts[0] not in {"collections", "blog", "spaces"}:
            hf_id = "/".join(parts)
            break
    if hf_id:
        try:
            response = await client.get(f"https://huggingface.co/api/models/{hf_id}")
            response.raise_for_status()
            hf = response.json()
        except (httpx.HTTPError, ValueError):
            hf = {}
    card = hf.get("cardData") or {}
    # Weight-file element counts can include quantization auxiliaries or extra heads.
    # Keep the table's architectural parameter count instead of mixing definitions.
    params = None if row["estimated_params"] else number(row["params_total"])
    benchmarks = {label: number(row[key]) for key, label in [("mmlu", "MMLU"), ("mmlu_pro", "MMLU-Pro"), ("gpqa", "GPQA"), ("hle", "HLE")] if number(row[key]) is not None}
    draft = {"name": row["model"], "full_name": hf_id or f"{row['lab']}/{row['model']}",
             "organization": row["lab"], "type": "Unknown", "params_b": params,
             "license": card.get("license"), "context_length": None,
             "languages": card.get("language") or [], "benchmarks": benchmarks,
             "hf_url": f"https://huggingface.co/{hf_id}" if hf_id else None,
             "paper_url": row["paper"] if row["paper"].startswith("https://") else None,
             "source_url": SOURCE_URL, "release_date": row["release_date"], "release_precision": "month"}
    if isinstance(draft["languages"], str):
        draft["languages"] = [draft["languages"]]
    evidence = {"source_url": SOURCE_URL, "public_row": row,
                "hf_metadata": {key: hf[key] for key in ("id", "pipeline_tag", "cardData", "safetensors") if key in hf},
                "field_notes": "日期只精确到月份；参数量采用公开表的非估算总参数列，不用 HF 文件元素统计覆盖，标为估算的参数量留空；评测条件见 notes。许可证只取匹配 HF 元数据，公开表的付费列不读取。"}
    return draft, evidence


async def import_table(path, limit=20):
    init_db()
    rows = read_candidates(path, limit)
    ids, skipped = [], []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for row in rows:
            with SessionLocal() as db:
                if find_existing(db, {"name": row["model"]}):
                    skipped.append(row["model"])
                    continue
            draft, evidence = await make_draft(row, client)
            with SessionLocal() as db:
                pending = next((p for p in db.query(PendingModel).all()
                                if model_key(json.loads(p.draft).get("name", "")) == model_key(draft["name"])), None)
                if pending and pending.status != "draft":
                    skipped.append(row["model"])
                    continue
                pending = pending or PendingModel()
                pending.draft = json.dumps(draft, ensure_ascii=False)
                pending.evidence = json.dumps(evidence, ensure_ascii=False)
                pending.confidence = json.dumps({"params_b": "medium" if draft["params_b"] else "none", "release_date": "medium"})
                pending.reviewed_at = None
                pending.review_decision, pending.review_reason = None, None
                db.add(pending)
                db.commit()
                ids.append(pending.id)
            print(f"候选：{draft['name']}", flush=True)
    from backend.collector.model_jobs import enqueue
    with SessionLocal() as db:
        for identifier in ids:
            enqueue(db, identifier, "公开模型表导入", 10)
        db.commit()
    result = {"queued": len(ids)}
    report = {"source": SOURCE_URL, "snapshot": str(path), "imported_at": datetime.now().isoformat(),
              "pending_ids": ids, "skipped": skipped, "review": result}
    report_path = Path(path).with_suffix(".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit 必须在 1 到 100 之间")
    asyncio.run(import_table(args.csv_path, args.limit))
