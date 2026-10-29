"""Bounded recovery decisions shared by collectors and paid research tools."""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx


class ServiceBlocked(RuntimeError):
    def __init__(self, record):
        self.record = record
        self.status_code = record.get("http_status")
        super().__init__(record.get("diagnosis", "来源等待恢复"))


def diagnosis(error, now=None):
    now = now or datetime.now(timezone.utc)
    response = getattr(error, "response", None)
    code = response.status_code if response is not None else getattr(error, "status_code", None)
    kind, message, delay = "network", "连接失败；有限重试后保留缓存，稍后重新连接。", 0
    if code in (401, 402):
        kind, message, delay = "authentication", "服务认证或额度不可用；核对端点、凭据及服务权限，暂停重复请求。", 3600
    elif code == 403:
        kind, message, delay = "access_denied", "服务拒绝访问；需要区分权限限制与拦截页，不自动绕过。", 3600
    elif code == 429:
        kind, message, delay = "rate_limit", "服务限流；按服务端要求延后请求。", 300
        retry_after = response.headers.get("Retry-After", "") if response is not None else ""
        try:
            delay = max(1, int(retry_after))
        except ValueError:
            try:
                delay = max(1, int((parsedate_to_datetime(retry_after) - now).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    elif code == 404:
        kind, message = "not_found", "入口不存在；核对官方入口或地址迁移，历史内容保留。"
    elif code and code >= 500:
        kind, message = "upstream", "上游服务错误；有限重试后使用已验证缓存。"
    elif isinstance(error, ValueError):
        kind, message = "parse", "内容未通过解析校验；保留原缓存，检查来源格式或拦截页面。"
    return {"failure_kind": kind, "diagnosis": message, "http_status": code,
            "next_retry_at": (now + timedelta(seconds=delay)).isoformat() if delay else None}


def cooling_down(record):
    value = record.get("next_retry_at")
    return bool(value and datetime.fromisoformat(value) > datetime.now(timezone.utc))


RESEARCH_SERVICE = "service:deepseek-research"


def credential_version():
    return hashlib.sha256(os.getenv("DEEPSEEK_API_KEY", "").encode()).hexdigest()


def research_health():
    from backend.collector.source_fetch import read_record
    record = read_record(RESEARCH_SERVICE)
    if record.get("credential_version") != credential_version():
        return {}
    return {key: value for key, value in record.items() if key != "credential_version"}


def block_research(error):
    from backend.collector.source_fetch import save_record
    record = {"name": "DeepSeek 模型补查", "status": "blocked", **diagnosis(error),
              "credential_version": credential_version(), "checked_at": datetime.now(timezone.utc).isoformat()}
    save_record(RESEARCH_SERVICE, record)
    return record


def clear_research_block():
    from backend.collector.source_fetch import save_record
    save_record(RESEARCH_SERVICE, {"name": "DeepSeek 模型补查", "status": "ok", "credential_version": credential_version()})
