"""Model Enrich Agent — queries HuggingFace API to auto-fill model metadata."""
import asyncio
import json
import logging
import re
from typing import Optional

import httpx

from backend.collector.enricher import get_client

logger = logging.getLogger(__name__)

HF_API = "https://huggingface.co/api"
HF_RAW = "https://huggingface.co"

# Map HF pipeline_tag → our type system
PIPELINE_TYPE_MAP = {
    "text-generation": "LLM",
    "text2text-generation": "LLM",
    "conversational": "LLM",
    "automatic-speech-recognition": "ASR",
    "text-to-speech": "TTS",
    "text-to-audio": "TTS",
    "text-to-video": "VideoGen",
    "text-to-image": "ImageGen",
    "image-to-text": "VLM",
    "visual-question-answering": "VLM",
    "feature-extraction": "Embedding",
    "sentence-similarity": "Embedding",
    "fill-mask": "LLM",
    "question-answering": "LLM",
}


async def _hf_api(model_id: str) -> dict:
    """Query HuggingFace model API endpoint."""
    url = f"{HF_API}/models/{model_id}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"User-Agent": "AITracker/1.0"})
        if resp.status_code != 200:
            return {}
        return resp.json()


async def _hf_readme(model_id: str) -> str:
    """Fetch model card README from HuggingFace."""
    url = f"{HF_RAW}/{model_id}/raw/main/README.md"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"User-Agent": "AITracker/1.0"})
        if resp.status_code != 200:
            return ""
        return resp.text[:6000]  # cap to save tokens


async def _extract_benchmarks(readme: str, model_name: str) -> dict:
    """Use DeepSeek to extract benchmark numbers from model card."""
    if not readme or len(readme) < 200:
        return {}
    client = get_client()
    prompt = f"""从以下模型卡片中提取 benchmark 评测数据，返回 JSON 对象（键为 benchmark 名，值为数字）。
如果找不到任何 benchmark 数据，返回空对象 {{}}。不要添加额外说明。

模型：{model_name}
---
{readme[:4000]}"""
    try:
        resp = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=800,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return {key: value for key, value in json.loads(raw).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)}
    except Exception as e:
        logger.debug("Benchmark extraction failed: %s", e)
        return {}


def _parse_params(hf_data: dict) -> Optional[float]:
    """Try to extract parameter count (billions) from HF metadata."""
    # safetensors metadata
    try:
        total = hf_data.get("safetensors", {}).get("total")
        if total and total > 0:
            return round(total / 1e9, 2)
    except Exception:
        pass
    # cardData num_parameters
    try:
        n = hf_data.get("cardData", {}).get("model-index", [{}])[0].get("results", [{}])[0].get("metrics", [{}])[0]
        pass  # too nested, skip
    except Exception:
        pass
    # tags like "7B", "70B", "0.5B"
    tags = hf_data.get("tags", [])
    for tag in tags:
        m = re.match(r"^(\d+\.?\d*)[Bb]$", str(tag))
        if m:
            return float(m.group(1))
    return None


async def enrich_model(model_name: str, hf_model_id: Optional[str] = None) -> dict:
    """
    Run the Model Enrich Agent for a given model name.
    Returns a draft dict ready to insert into pending_models.
    """
    # If no explicit HF id, try a search
    search_id = hf_model_id or model_name

    logger.info("Enriching model: %s (HF id hint: %s)", model_name, search_id)

    # Parallel fetch: HF API + README
    try:
        hf_data, readme = await asyncio.gather(_hf_api(search_id), _hf_readme(search_id))
    except httpx.HTTPError as exc:
        logger.info("HF lookup unavailable for %s: %s", model_name, type(exc).__name__)
        hf_data, readme = {}, ""

    # If HF API failed, try a search
    if not hf_data and "/" not in search_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{HF_API}/models",
                    params={"search": model_name, "limit": 5, "sort": "downloads"},
                    headers={"User-Agent": "AITracker/1.0"},
                )
                r.raise_for_status()
                normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
                results = [item for item in r.json()
                           if normalize(item.get("id", "").split("/")[-1]) == normalize(model_name)]
                if results:
                    search_id = results[0].get("id", search_id)
                    hf_data, readme = await asyncio.gather(
                        _hf_api(search_id),
                        _hf_readme(search_id),
                    )
        except Exception as e:
            logger.debug("HF search failed: %s", e)

    # Name-only search may return unofficial placeholders for closed models.
    # Automatic enrichment requires a repository containing actual model weights.
    if not hf_model_id and not hf_data.get("safetensors", {}).get("total"):
        hf_data, readme = {}, ""

    # Async benchmark extraction (only if we have a README)
    benchmarks = await _extract_benchmarks(readme, model_name) if readme else {}

    # Build draft with confidence levels
    card = hf_data.get("cardData", {}) or {}
    pipeline = hf_data.get("pipeline_tag", "")
    model_type = PIPELINE_TYPE_MAP.get(pipeline, "Unknown")

    # Detect CodeLM / MathLM from tags
    tags = [str(t).lower() for t in hf_data.get("tags", [])]
    if any(t in tags for t in ["code", "coding", "codegen"]):
        model_type = "CodeLM"
    elif any(t in tags for t in ["math", "mathematics"]):
        model_type = "MathLM"

    params_b = _parse_params(hf_data)
    license_val = card.get("license") or next(
        (t.replace("license:", "") for t in hf_data.get("tags", []) if str(t).startswith("license:")), None
    )

    draft = {
        "name": model_name,
        "full_name": search_id if hf_data else None,
        "organization": search_id.split("/")[0] if hf_data and "/" in search_id else None,
        "type": model_type,
        "params_b": params_b,
        "context_length": None,    # hard to extract reliably, leave for manual confirm
        "license": license_val,
        "languages": json.dumps(card.get("language", []), ensure_ascii=False) if card.get("language") else "[]",
        "modality_in": "[]",
        "modality_out": "[]",
        "benchmarks": json.dumps(benchmarks, ensure_ascii=False),
        "hf_url": f"https://huggingface.co/{search_id}" if hf_data else None,
        "hf_downloads": hf_data.get("downloads"),
        "hf_likes": hf_data.get("likes"),
        "release_date": None,  # Repository creation is not the model's release date.
        "notes": None,
        "_evidence": {"hf_metadata": hf_data, "model_card": readme},
        # Confidence metadata — shown in the review UI
        "confidence": json.dumps({
            "type": "high" if model_type != "Unknown" else "none",
            "params_b": "high" if params_b else "low",
            "license": "high" if license_val else "low",
            "benchmarks": "low" if benchmarks else "none",
        }, ensure_ascii=False),
    }

    logger.info(
        "Enrich done for %s: type=%s params=%.1fB license=%s benchmarks=%d",
        model_name, model_type, params_b or 0, license_val or "?", len(benchmarks),
    )
    return draft
