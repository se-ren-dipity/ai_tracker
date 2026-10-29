"""Read-only visitor tools. Sources and detail links are assigned by the server."""
import asyncio
import json
from datetime import date, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import and_, func, or_

from backend import registry
from backend.database import SessionLocal
from backend.models.article import Article
from backend.models.insight import Insight, SourceItem
from backend.models.model_db import AIModel
from backend.models.model_task import ModelObservation
from backend.collector.enricher import DEEPSEEK_BASE_URL
from backend.collector.model_research import public_url
from backend.collector.source_health import research_health, cooling_down, block_research, ServiceBlocked
from . import session
from .schemas import NewsQuery, NewsID, ModelQuery, ModelFilter, ModelID, EvidenceQuery, WebQuery, SourceID


def news_search(db, args: NewsQuery):
    end = args.date_to or date.fromisoformat(session.day_key())
    start = args.date_from or end - timedelta(days=6)
    source_day = func.date(SourceItem.published, "+8 hours")
    published = db.query(SourceItem.insight_id).filter(SourceItem.published.is_not(None)).subquery()
    in_range = db.query(SourceItem.insight_id).filter(source_day >= start.isoformat(), source_day <= end.isoformat()).subquery()
    query = db.query(Insight).filter(or_(Insight.id.in_(db.query(in_range.c.insight_id)), and_(
        Insight.id.not_in(db.query(published.c.insight_id).filter(published.c.insight_id.is_not(None))),
        func.date(Insight.published, "+8 hours") >= start.isoformat(),
        func.date(Insight.published, "+8 hours") <= end.isoformat())))
    if args.terms:
        terms = list(args.terms)
        if any(t.casefold() in {"openai", "chatgpt"} for t in terms):
            terms += ["OpenAI", "ChatGPT"]
        if any(t in {"开放权重", "开源模型", "开源"} for t in terms):
            terms += ["开放权重", "开源", "open weights"]
        if any(t in {"语音模型", "语音", "音频"} for t in terms):
            terms += ["语音", "音频", "speech", "audio"]
        columns = [Insight.title, Insight.summary, Insight.facts, Insight.body_paragraphs, Insight.keywords, Insight.model_names]
        query = query.filter(or_(*(column.icontains(term, autoescape=True) for column in columns for term in set(terms))))
    if args.category:
        query = query.filter_by(category=args.category)
    total = query.count()
    rows = query.order_by(Insight.published.desc(), Insight.id.desc()).offset(args.offset).limit(args.limit).all()
    latest = db.query(func.max(SourceItem.published)).filter(SourceItem.insight_id.is_not(None)).scalar()
    return {"date_from": start.isoformat(), "date_to": end.isoformat(), "timezone": "Asia/Shanghai",
            "total": total, "has_more": args.offset + len(rows) < total,
            "latest_source_published": latest.isoformat() + "Z" if latest else None,
            "scope": "本站已整理事件；日期为来源报道日，不能等同事件发生日。命中不代表发布主体已核实。",
            "items": [{"id": row.id, "title": row.title, "summary": (row.summary or "")[:650],
                       "category": row.category, "event_kind": row.event_kind,
                       "published": row.published.isoformat() + "Z" if row.published else None} for row in rows]}


MODEL_FIELDS = ("id", "name", "organization", "type", "params_b", "context_length", "license",
                "weight_access", "release_date", "release_precision", "field_states", "last_checked_at")


def model_public(row):
    data = row.to_dict() if isinstance(row, AIModel) else row
    return {key: data.get(key) for key in MODEL_FIELDS}


def find_models(db, args: ModelFilter):
    matched, unknown = [], []
    query = db.query(AIModel).filter(AIModel.existence_status == "confirmed")
    if args.type:
        query = query.filter(func.lower(AIModel.type) == args.type.lower())
    for row in query.order_by(AIModel.release_date.desc(), AIModel.id.desc()):
        missing, mismatch = [], False
        for field, lower, upper in [("params_b", args.params_min, args.params_max), ("context_length", args.context_min, None)]:
            value = getattr(row, field)
            state = json.loads(row.field_states or "{}").get(field)
            if isinstance(state, dict):
                state = state.get("state")
            if lower is None and upper is None:
                continue
            if value is None or state == "conflict":
                missing.append(field)
            elif (lower is not None and value < lower) or (upper is not None and value > upper):
                mismatch = True
        if args.weight_access:
            if row.weight_access not in ("open", "closed"):
                missing.append("weight_access")
            elif row.weight_access != args.weight_access:
                mismatch = True
        if mismatch:
            continue
        item = model_public(row) | {"unverified_fields": missing}
        (unknown if missing else matched).append(item)
    return {"filters": args.model_dump(), "total": len(matched), "unknown_total": len(unknown),
            "items": matched[args.offset:args.offset + args.limit], "unknown": unknown[args.offset:args.offset + args.limit],
            "has_more": max(len(matched), len(unknown)) > args.offset + args.limit,
            "scope": "总参数量；上下文容量不等于长文理解能力。unknown 候选未满足全部条件，库内字段仍需核对原始依据。"}


TOOL_MODELS = {
    "search_news": (NewsQuery, "检索本站已整理事件，按来源日期筛选并去重。terms 为 OR 同义词；最近默认 7 个自然日，分页有 total。"),
    "get_news": (NewsID, "读取已收录新闻的正文、事实和原始来源摘录，核实具体主体、动作和日期。"),
    "search_models": (ModelQuery, "按名称及已确认别名找模型；近似名称只是候选，需核对具体版本与机构。"),
    "find_models": (ModelFilter, "按总参数量、上下文容量、类型 LLM/VLM 等和权重开放状态筛选。分别返回明确匹配和字段未知的候选。"),
    "get_model": (ModelID, "读取具体模型当前档案及资料链接。库内记录不等于实时官方确认。"),
    "get_model_evidence": (EvidenceQuery, "读取已接受的字段证据；不返回待审核更正。fields 为空时取所有字段。"),
    "search_official_web": (WebQuery, "按需搜索官方资料，最多 2 次；正文和搜索摘要明确区分，不提供用户无关隐私。"),
    "read_source": (SourceID, "读取本轮已取得的官方来源 ID 正文，最多 2 次；不接受任意 URL。"),
}
TOOLS = [{"type": "function", "function": {"name": name, "description": description,
          "parameters": model.model_json_schema()}} for name, (model, description) in TOOL_MODELS.items()]
OFFICIAL_DOMAINS = ("openai.com", "anthropic.com", "deepseek.com", "qwen.ai", "qwenlm.github.io", "huggingface.co",
                    "github.com", "arxiv.org", "blog.google", "ai.google.dev", "ai.meta.com", "mistral.ai", "microsoft.com")


def official_url(url):
    if not public_url(url):
        return False
    parsed = urlsplit(url)
    host = parsed.hostname.lower()
    return parsed.port in (None, 443, 80) and any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS)


class ToolSession:
    def __init__(self, identifier, web_enabled=True):
        self.identifier, self.web_enabled = identifier, web_enabled
        self.sources, self.cards, self.trace, self.exposed = {}, {}, [], set()
        self.searches = self.fetches = 0

    def source(self, title, url, content, kind="archive", **metadata):
        if not (url.startswith("#") or public_url(url)):
            return None
        for existing in self.sources.values():
            if existing["url"] == url and existing["content"] == str(content)[:3200]:
                return existing
        identifier = f"E{len(self.sources) + 1}"
        result = {"id": identifier, "title": title[:200], "url": url, "content": str(content)[:3200], "kind": kind, **metadata}
        self.sources[identifier] = result
        return result

    def add_card(self, kind, item):
        card = {"kind": kind, "id": item["id"], "title": item.get("title") or item["name"],
                "url": f"#{'insight' if kind == 'news' else 'model'}/{item['id']}"}
        self.cards[(kind, item["id"])] = card
        source = self.source(card["title"], card["url"], json.dumps(item, ensure_ascii=False), "archive")
        return item | {"citation_id": source["id"]}

    def local(self, name, args):
        with SessionLocal() as db:
            if name == "search_news":
                output = news_search(db, args)
                output["items"] = [self.add_card("news", item) for item in output["items"]]
                return output
            if name == "get_news":
                row = db.get(Insight, args.insight_id)
                if row is None:
                    raise ValueError("新闻不存在")
                item = {k: v for k, v in row.to_dict().items() if k not in ("generated_at", "keywords")}
                item["body_paragraphs"] = item["body_paragraphs"][:8]
                item["facts"] = item["facts"][:8]
                item = self.add_card("news", item)
                sources = []
                for source, article in db.query(SourceItem, Article).join(Article, Article.id == SourceItem.article_id).filter(
                        SourceItem.insight_id == row.id).limit(4):
                    evidence = self.source(article.source or source.title, article.url, source.body_text,
                        "report", published=source.published.isoformat() + "Z" if source.published else None)
                    if evidence:
                        sources.append(evidence)
                return {"item": item, "sources": sources}
            if name == "search_models":
                result = registry.search_models(db, args.query, args.organization, limit=args.limit, include_pending=False)
                items = [row for row in result["items"] if row.get("existence_status") == "confirmed"]
                return {"total": result["total"], "items": [self.add_card("model", model_public(row) | {
                    "exact_name": row["exact_name"], "organization_match": row["organization_match"]}) for row in items]}
            if name == "find_models":
                output = find_models(db, args)
                for group in ("items", "unknown"):
                    output[group] = [self.add_card("model", item) for item in output[group]]
                return output
            row = db.get(AIModel, args.model_id)
            if row is None or row.existence_status != "confirmed":
                raise ValueError("未找到已收录模型")
            if name == "get_model":
                item = model_public(row) | {"languages": json.loads(row.languages or "[]"), "notes": (row.notes or "")[:1200],
                                           "benchmarks": json.loads(row.benchmarks or "{}")}
                links = [self.source(row.name, url, "尚未读取该页面正文", "link") for url in
                         (row.hf_url, row.github_url, row.paper_url, row.source_url) if url and public_url(url)]
                return {"item": self.add_card("model", item), "sources": links}
            query = db.query(ModelObservation).filter_by(model_id=row.id, status="accepted")
            if args.fields:
                query = query.filter(ModelObservation.field.in_(args.fields))
            sources = []
            for obs in query.order_by(ModelObservation.id.desc()).limit(12):
                if obs.url and obs.quote:
                    sources.append(self.source(row.name + " · " + obs.field, obs.url, obs.quote, "evidence",
                        field=obs.field, value=json.loads(obs.value or "null"), observed_at=obs.observed_at.isoformat() + "Z"))
            return {"model_id": row.id, "sources": [item for item in sources if item],
                    "scope": "已接受的历史证据；可能存在不同时点的值，须结合当前档案。"}

    async def web(self, name, args):
        if not self.web_enabled:
            raise ValueError("管理员已关闭联网补充，可使用本站资料")
        health = await asyncio.to_thread(research_health)
        if cooling_down(health):
            raise ServiceBlocked(health)
        if name == "search_official_web":
            if self.searches >= 2:
                raise ValueError("本轮搜索已达 2 次")
            self.searches += 1
            endpoint, body = "search", {"query": args.query, "count": 5, "need_content": True}
        else:
            source = self.sources.get(args.source_id)
            if not source or not official_url(source["url"]):
                raise ValueError("只能读取本轮取得的官方站点或模型/代码/论文托管来源")
            if self.fetches >= 2:
                raise ValueError("本轮正文读取已达 2 次")
            self.fetches += 1
            endpoint, body = "fetch", {"url": source["url"]}
        await asyncio.to_thread(session.reserve_web, self.identifier)
        import os
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(f"{DEEPSEEK_BASE_URL}/v1/tools/{endpoint}",
                headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]}, json=body)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if response.status_code in (401, 402, 403, 429):
                    await asyncio.to_thread(block_research, exc)
                raise
            data = response.json()
        if endpoint == "fetch":
            return {"source": self.source(data.get("title") or source["title"], source["url"],
                                          data.get("content") or "", "web_body")}
        return {"sources": [self.source(r.get("title") or r["url"], r["url"],
                    r.get("content") or r.get("summary") or r.get("snippet") or "",
                    "web_body" if r.get("content") else "search_snippet")
                for r in data.get("results", []) if official_url(r.get("url", ""))][:4],
                "scope": "仅保留官方域名及代码/模型/论文托管站点；托管站点仍需核实发布账号，摘要不能当正文。"}

    async def execute(self, name, arguments):
        if len(self.trace) >= 8:
            return {"error": "本轮工具预算已用完，请整理已有材料"}
        entry = {"tool": name, "status": "error"}
        self.trace.append(entry)
        try:
            if name not in TOOL_MODELS:
                raise ValueError("工具不在允许范围内")
            args = TOOL_MODELS[name][0].model_validate_json(arguments)
            await asyncio.to_thread(session.phase, self.identifier, "searching")
            result = await self.web(name, args) if name in ("search_official_web", "read_source") else await asyncio.to_thread(self.local, name, args)
            entry["status"] = "ok"
            return result
        except (ValueError, httpx.HTTPError, ServiceBlocked) as exc:
            entry["error"] = type(exc).__name__
            return {"error": str(exc)[:200] if isinstance(exc, ValueError) else "来源暂不可用，本次未取得资料；使用已有证据并说明缺口。"}
