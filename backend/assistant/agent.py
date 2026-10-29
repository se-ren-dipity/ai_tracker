"""One bounded, read-only conversation turn; validated answers are published once."""
import asyncio
import json
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path
from openai import AsyncOpenAI
from pydantic import ValidationError

from fastapi import HTTPException
from backend.collector.enricher import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from backend.collector.source_health import research_health, cooling_down, block_research, ServiceBlocked
from . import session
from .schemas import Answer
from .tools import TOOLS, ToolSession

logger = logging.getLogger(__name__)
PROMPT = Path(__file__).with_name("system_prompt.txt").read_text(encoding="utf-8")
SCHEMA = json.dumps(Answer.model_json_schema(), ensure_ascii=False, separators=(",", ":"))


def get_client():
    # This client belongs to one turn; never close the collector's cached client.
    return AsyncOpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=DEEPSEEK_BASE_URL, timeout=55, max_retries=0)


def validate_answer(raw, evidence, needs_evidence=False):
    answer = Answer.model_validate_json(raw)
    cited = []
    for block in answer.blocks:
        if re.search(r"https?://|<\s*(script|iframe|img)\b", block.text, re.I):
            raise ValueError("正文不要包含 URL 或 HTML，链接由服务器提供")
        for identifier in block.citation_ids:
            source = evidence.sources.get(identifier)
            if source is None or identifier not in evidence.exposed or source["kind"] == "link" or not source["content"]:
                raise ValueError(f"来源 {identifier} 没有实际提供正文或不在已展示结果中，请删除该引用及无依据的结论")
            if identifier not in cited:
                cited.append(identifier)
    if needs_evidence and not evidence.trace:
        raise ValueError("事实性问题须先检索，不能仅用记忆回答")
    if answer.kind in ("news", "recommendation") and evidence.sources and not cited:
        raise ValueError("新闻和模型结论需要引用已有证据")
    cards = []
    for card in answer.cards:
        resolved = evidence.cards.get((card.kind, card.id))
        if resolved is None or not any(s["url"] == resolved["url"] and s["id"] in evidence.exposed for s in evidence.sources.values()):
            raise ValueError(f"卡片 {card.kind}/{card.id} 不属于实际展示过的候选，请删除")
        if resolved not in cards:
            cards.append(resolved)
    result = answer.model_dump()
    result["cards"] = cards
    result["sources"] = [{k: v for k, v in evidence.sources[identifier].items() if k != "content"}
                         | {"excerpt": evidence.sources[identifier]["content"][:500]} for identifier in cited]
    return result


def fallback(evidence, reason):
    return {"kind": "explanation", "blocks": [{"text": "这次没能完成可靠的整理。已找到的本站资料放在下面，可以先打开查看。"
            if evidence.cards else "这次暂时没能完成回答，请稍后再试。", "citation_ids": []}],
            "cards": list(evidence.cards.values())[:5], "sources": [], "limitations": [reason], "follow_up_question": None}


def compact_output(value, string_limit=800, list_limit=4):
    if isinstance(value, dict):
        return {key: compact_output(item, string_limit, list_limit) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_output(item, string_limit, list_limit) for item in value[:list_limit]]
    if isinstance(value, str) and len(value) > string_limit:
        return value[:string_limit] + "…（摘录截断）"
    return value


def tool_content(output):
    for width, count in ((800, 4), (400, 3), (200, 2)):
        shortened = compact_output(output, width, count)
        if shortened != output:
            shortened["truncated"] = "已节选；total 仍是全部命中数，未展示的内容不能视为已核对。可减少 limit 或读取具体 ID。"
        serialized = json.dumps(shortened, ensure_ascii=False, default=str)
        if len(serialized.encode()) < 6500:
            return serialized
    return json.dumps({"error": "材料过多，请缩小查询或读取单条。"}, ensure_ascii=False)


def bounded_messages(messages, tools):
    # A conservative estimate, not a tokenizer. Reserve 12K tokens before each call;
    # actual provider usage reconciles the reservation. Never split a tool message.
    size = len(json.dumps(messages, ensure_ascii=False).encode()) + len(json.dumps(tools, ensure_ascii=False).encode())
    if size > 24000:
        for message in messages:
            if message["role"] == "tool":
                value = compact_output(json.loads(message["content"]), 180, 2)
                value["truncated"] = "较早工具结果已节选，不能声称穷尽全部资料。"
                message["content"] = json.dumps(value, ensure_ascii=False, default=str)
                size = len(json.dumps(messages, ensure_ascii=False).encode()) + len(json.dumps(tools, ensure_ascii=False).encode())
                if size <= 24000:
                    break
        if size > 24000:
            raise ValueError("本轮材料已达到上下文预算，请缩小问题范围")
    return messages


async def answer_turn(identifier, evidence, client):
    question, history, config = await asyncio.to_thread(session.context, identifier)
    needs_evidence = bool(re.search(r"发布|新闻|开源|参数|长文|模型|产品|OpenAI|ChatGPT|工程进展", question, re.I))
    today = session.day_key()
    recent = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
    system = PROMPT + f"\n可信运行上下文：北京时间日期 {today}；最近7个自然日精确为 {recent} 至 {today}（含两端），不得多算一天。最多4次模型调用、8次工具、2次搜索和2次读网页，最后一轮只整理。回答控制在600个中文字以内；闲聊通常1–2句。\nAnswer JSON schema：" + SCHEMA
    messages = [{"role": "system", "content": system}]
    # Keep whole pairs and leave room for tools. Older history is context, never new evidence.
    for pair in range(0, len(history), 2):
        if sum(len(m["content"]) for m in history[pair:]) <= 2200:
            messages.extend(history[pair:])
            break
    messages.append({"role": "user", "content": question})
    last_error, repairs = "回答未通过引用与结构校验", 0
    for index in range(4):
        use_tools = index < 3 and len(evidence.trace) < 8
        tool_list = TOOLS if use_tools else []
        # Reserve the final call for synthesis; do not pay for another research round.
        if not use_tools:
            messages.append({"role": "user", "content": "工具预算结束。仅用已取得资料，按 Answer schema 返回完整 JSON，资料不足写明限制。"})
        await asyncio.to_thread(session.phase, identifier, "thinking" if use_tools else "composing")
        try:
            bounded_messages(messages, tool_list)
        except ValueError:
            # If research filled the context, spend the remaining room on the answer.
            use_tools, tool_list = False, []
            bounded_messages(messages, [])
            messages.append({"role":"user", "content":"材料预算已满。停止工具调用，仅依据已取得材料给出简短 JSON 答案，明确未核对的范围。"})
        health = await asyncio.to_thread(research_health)
        if cooling_down(health):
            raise ServiceBlocked(health)
        await asyncio.to_thread(session.reserve_call, identifier, 12000, 2000)
        kwargs = {"model": os.getenv("ASSISTANT_MODEL") or DEEPSEEK_MODEL, "messages": messages,
                  "max_tokens": 2000, "temperature": 0.2, "response_format": {"type": "json_object"}}
        if use_tools:
            kwargs["tools"] = tool_list
        else:
            kwargs["response_format"] = {"type": "json_object"}
        response = await client.chat.completions.create(**kwargs)
        usage = response.usage
        await asyncio.to_thread(session.settle_call, identifier, 12000, 2000,
            usage.prompt_tokens if usage else None, usage.completion_tokens if usage else None, response.model)
        message = response.choices[0].message
        if message.tool_calls and use_tools:
            messages.append(message.model_dump(exclude_none=True, include={"role", "content", "tool_calls"}))
            for call in message.tool_calls:
                output = await evidence.execute(call.function.name, call.function.arguments)
                serialized = tool_content(output)
                evidence.exposed.update(re.findall(r'"(?:citation_id|id)"\s*:\s*"(E\d+)"', serialized))
                messages.append({"role": "tool", "tool_call_id": call.id, "content": serialized})
            continue
        try:
            raw = message.content or "{}"
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            result = validate_answer(raw, evidence, needs_evidence)
            await asyncio.to_thread(session.phase, identifier, "composing")
            return result
        except ValueError as exc:
            repairs += 1
            if repairs > 1:
                raise ValueError(last_error) from exc
            # Do not feed invalid generated text back as evidence.
            reason = json.dumps([{"field": e["loc"], "type": e["type"]} for e in exc.errors()], ensure_ascii=False) if isinstance(exc, ValidationError) else str(exc)
            messages.append({"role": "user", "content": "上次校验失败：" + reason[:500] +
                "。仅用实际取得的资料，按 Answer schema 返回 JSON。可引用 ID：" + ",".join(sorted(evidence.exposed))})
    raise ValueError(last_error)


async def run(identifier):
    evidence = ToolSession(identifier)
    client = None
    try:
        _, _, config = await asyncio.to_thread(session.context, identifier)
        evidence.web_enabled = config.web_enabled
        client = get_client()
        async with asyncio.timeout(90):
            result = await answer_turn(identifier, evidence, client)
        await asyncio.to_thread(session.finish, identifier, "done", result, None, evidence.trace)
    except asyncio.CancelledError:
        await asyncio.to_thread(session.finish, identifier, "cancelled", None, "已停止生成。", evidence.trace)
        raise
    except InterruptedError:
        pass
    except Exception as exc:
        if not isinstance(exc, HTTPException) and getattr(exc, "status_code", None) in (401, 402, 403, 429):
            await asyncio.to_thread(block_research, exc)
        if isinstance(exc, TimeoutError):
            reason = "处理超过 90 秒，已停止。"
        elif isinstance(exc, ServiceBlocked):
            reason = "模型服务正在等待恢复，暂时无法回答。"
        elif isinstance(exc, HTTPException):
            reason = str(exc.detail)
        elif isinstance(exc, ValueError):
            reason = str(exc)[:150]
        else:
            reason = "模型服务暂不可用，请稍后再试。"
        logger.warning("Assistant request %s failed: %s", identifier, type(exc).__name__)
        await asyncio.to_thread(session.finish, identifier, "failed", fallback(evidence, reason), reason, evidence.trace)
    finally:
        if client is not None:
            await client.close()
