"""Visitor ownership, admission and budgets; each transaction ends before network IO."""
import hashlib
import hmac
import json
import os
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import text

from backend.database import SessionLocal
from backend.models.assistant import AssistantSession, AssistantRequest, AssistantUsage, AssistantConfig
from .schemas import Settings

COOKIE = "tracker_visitor"
PROMPT_VERSION = "0.3"
UTC8 = timezone(timedelta(hours=8))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def day_key():
    return datetime.now(UTC8).date().isoformat()


@contextmanager
def transaction():
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        yield db
        db.commit()


def settings(db):
    row = db.get(AssistantConfig, 1)
    return Settings.model_validate_json(row.value) if row else Settings()


def usage(db, key):
    row = db.get(AssistantUsage, key)
    if row is None:
        row = AssistantUsage(key=key, count=0, calls=0, input_tokens=0, output_tokens=0, web_calls=0)
        db.add(row)
        db.flush()
    return row


def require_session(db, token):
    row = db.get(AssistantSession, digest(token)) if token else None
    if row is None or row.expires_at <= datetime.utcnow():
        raise HTTPException(401, "会话已过期，请重新打开囤囤。")
    return row


def address_key(request):
    # request.client is resolved by the ASGI server's trusted-proxy policy, not arbitrary headers.
    address = request.client.host if request.client else "unknown"
    return digest(os.getenv("ADMIN_PASSWORD_HASH", "") + ":assistant:" + address)


def check_origin(request):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "请从本站打开囤囤。")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不接受跨站请求。")


def check_write(request):
    check_origin(request)
    token = request.cookies.get(COOKIE, "")
    if not token or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), digest(token + ":assistant-csrf")):
        raise HTTPException(403, "会话校验失败，请重新打开囤囤。")
    return token


def summary(db, visitor):
    config = settings(db)
    own = db.get(AssistantUsage, f"session:{visitor.id}:{day_key()}")
    site = db.get(AssistantUsage, f"site:{day_key()}")
    return {"enabled": config.enabled, "configured": bool(os.getenv("DEEPSEEK_API_KEY")),
            "remaining": max(0, config.session_limit - (own.count if own else 0)),
            "site_remaining": max(0, config.site_limit - (site.count if site else 0)),
            "daily_limit": config.session_limit, "version": visitor.version}


def public_request(row):
    return {"id": row.id, "request_id": row.client_id, "version": row.version, "message": row.message,
            "status": row.status, "phase": row.phase, "answer": json.loads(row.result) if row.result else None,
            "error": row.error}


def open_session(request):
    check_origin(request)
    token = request.cookies.get(COOKIE, "")
    with transaction() as db:
        visitor = db.get(AssistantSession, digest(token)) if token else None
        if visitor is None or visitor.expires_at <= datetime.utcnow():
            minute = datetime.utcnow().strftime("%Y%m%d%H%M")
            rate = usage(db, f"bootstrap:{address_key(request)}:{minute}")
            if rate.count >= 15:
                raise HTTPException(429, "打开次数过多，请稍后再试。", headers={"Retry-After": "60"})
            rate.count += 1
            token = secrets.token_urlsafe(32)
            visitor = AssistantSession(id=digest(token), version=1, expires_at=datetime.utcnow() + timedelta(hours=24))
            db.add(visitor)
            db.flush()
        rows = db.query(AssistantRequest).filter_by(session_id=visitor.id, version=visitor.version).order_by(
            AssistantRequest.created_at.desc()).limit(12).all()
        payload = summary(db, visitor) | {"csrf": digest(token + ":assistant-csrf"),
            "history": [public_request(row) for row in reversed(rows)], "expires_at": visitor.expires_at.isoformat() + "Z"}
    return token, payload


def count_attempt(token, address):
    # Separate transaction: rejected requests still consume the short-term rate allowance.
    with transaction() as db:
        visitor = require_session(db, token)
        config = settings(db)
        minute = datetime.utcnow().strftime("%Y%m%d%H%M")
        own = usage(db, f"rate:{visitor.id}:{minute}")
        ip = usage(db, f"iprate:{address}:{minute}")
        blocked = own.count >= config.minute_limit or ip.count >= max(10, config.minute_limit * 5)
        own.count += 1
        ip.count += 1
    if blocked:
        raise HTTPException(429, "问得有点快，请稍后再试。", headers={"Retry-After": "60"})


def admit(token, address, body):
    # Retries return the original request without running or charging it again.
    with SessionLocal() as db:
        visitor = require_session(db, token)
        old = db.query(AssistantRequest).filter_by(session_id=visitor.id, client_id=str(body.request_id)).first()
        if old:
            if old.version != visitor.version or old.message != body.message.strip():
                raise HTTPException(409, "请求编号已使用，请重新发送。")
            return public_request(old), False
    count_attempt(token, address)
    with transaction() as db:
        visitor = require_session(db, token)
        old = db.query(AssistantRequest).filter_by(session_id=visitor.id, client_id=str(body.request_id)).first()
        if old:
            if old.version != visitor.version or old.message != body.message.strip():
                raise HTTPException(409, "请求编号已使用，请重新发送。")
            return public_request(old), False
        if body.version != visitor.version:
            raise HTTPException(409, "对话已更新，请重新打开囤囤。")
        config = settings(db)
        if not config.enabled:
            raise HTTPException(503, "囤囤暂时休息中，请稍后再来。")
        if not os.getenv("DEEPSEEK_API_KEY"):
            raise HTTPException(503, "助手尚未配置模型服务，新闻和模型库仍可浏览。")
        if not body.message.strip():
            raise HTTPException(422, "请输入问题。")
        running = db.query(AssistantRequest).filter_by(status="running")
        if running.filter_by(session_id=visitor.id).first():
            raise HTTPException(409, "上一条问题仍在处理，可以先停止它。")
        if running.count() >= config.max_concurrent:
            raise HTTPException(429, "大家都在问囤囤，请稍后再试。", headers={"Retry-After": "10"})
        day = day_key()
        budgets = [(f"session:{visitor.id}:{day}", config.session_limit),
                   (f"ip:{address}:{day}", config.ip_limit), (f"site:{day}", config.site_limit)]
        for key, limit in budgets:
            counter = usage(db, key)
            if counter.count >= limit:
                raise HTTPException(429, "今日使用额度已用完，明天再来吧。")
            counter.count += 1
        row = AssistantRequest(id=str(uuid.uuid4()), session_id=visitor.id, client_id=str(body.request_id),
            version=visitor.version, address=address, day=day, message=body.message.strip(), status="running",
            phase="thinking", prompt_version=PROMPT_VERSION, calls=0, input_tokens=0, output_tokens=0, web_calls=0)
        db.add(row)
        db.flush()
        return public_request(row), True


def owned_request(token, identifier):
    with SessionLocal() as db:
        visitor = require_session(db, token)
        row = db.get(AssistantRequest, identifier)
        if row is None or row.session_id != visitor.id or row.version != visitor.version:
            raise HTTPException(404, "未找到这条对话。")
        return public_request(row)


def context(identifier):
    with SessionLocal() as db:
        row = db.get(AssistantRequest, identifier)
        history = db.query(AssistantRequest).filter_by(session_id=row.session_id, version=row.version, status="done").filter(
            AssistantRequest.created_at < row.created_at).order_by(AssistantRequest.created_at.desc()).limit(6).all()
        messages = []
        for previous in reversed(history):
            answer = json.loads(previous.result)
            messages += [{"role": "user", "content": previous.message},
                         {"role": "assistant", "content": "\n".join(block["text"] for block in answer["blocks"])[:1800]}]
        return row.message, messages, settings(db)


def active(db, identifier):
    row = db.get(AssistantRequest, identifier)
    if row is None or row.status != "running":
        raise InterruptedError("请求已停止")
    visitor = db.get(AssistantSession, row.session_id)
    if visitor is None or visitor.version != row.version or visitor.expires_at <= datetime.utcnow():
        raise InterruptedError("会话已失效")
    return row


def phase(identifier, value):
    with transaction() as db:
        active(db, identifier).phase = value


def reserve_call(identifier, input_tokens, output_tokens):
    with transaction() as db:
        row = active(db, identifier)
        config = settings(db)
        total = usage(db, f"site:{row.day}")
        if (not config.enabled or row.calls >= 4 or total.calls >= config.daily_calls
                or total.input_tokens + input_tokens > config.input_budget
                or total.output_tokens + output_tokens > config.output_budget):
            raise HTTPException(429, "本轮或今日模型调用预算已用完。")
        row.calls += 1
        total.calls += 1
        row.input_tokens += input_tokens
        row.output_tokens += output_tokens
        total.input_tokens += input_tokens
        total.output_tokens += output_tokens


def settle_call(identifier, reserved_in, reserved_out, actual_in, actual_out, model):
    with transaction() as db:
        row = db.get(AssistantRequest, identifier)
        if row is None:
            return  # Cleared/expired requests retain conservative global reservations.
        total = usage(db, f"site:{row.day}")
        for field, reserved, actual in [("input_tokens", reserved_in, actual_in), ("output_tokens", reserved_out, actual_out)]:
            if actual is not None and actual >= 0:
                delta = actual - reserved
                setattr(row, field, max(0, getattr(row, field) + delta))
                setattr(total, field, max(0, getattr(total, field) + delta))
        row.model = str(model)[:100]


def reserve_web(identifier):
    with transaction() as db:
        row = active(db, identifier)
        config = settings(db)
        total = usage(db, f"site:{row.day}")
        if not config.enabled or not config.web_enabled or row.web_calls >= 4 or total.web_calls >= config.site_limit * 4:
            raise HTTPException(429, "本轮联网预算已用完或联网功能已关闭。")
        row.web_calls += 1
        total.web_calls += 1


def finish(identifier, status, result=None, error=None, trace=()):
    with transaction() as db:
        row = db.get(AssistantRequest, identifier)
        if row is None or row.status != "running":
            return
        row.status, row.phase = status, status
        row.result = json.dumps(result, ensure_ascii=False) if result else None
        row.error, row.trace = error, json.dumps(list(trace), ensure_ascii=False)
        row.finished_at = datetime.utcnow()
        refund_unpaid(db, row)


def refund_unpaid(db, row):
    if not row.calls and not row.web_calls:
        for key in [f"session:{row.session_id}:{row.day}", f"ip:{row.address}:{row.day}", f"site:{row.day}"]:
            counter = usage(db, key)
            counter.count = max(0, counter.count - 1)


def clear_conversation(token):
    with transaction() as db:
        visitor = require_session(db, token)
        identifiers = []
        for row in db.query(AssistantRequest).filter_by(session_id=visitor.id):
            if row.status == "running":
                identifiers.append(row.id)
                row.status = row.phase = "cancelled"
                row.finished_at = datetime.utcnow()
                refund_unpaid(db, row)
            row.message, row.result, row.trace, row.error = "", None, "[]", None
        visitor.version += 1
        payload = summary(db, visitor)
    return identifiers, payload


def recover_and_cleanup(recover=False):
    with transaction() as db:
        now = datetime.utcnow()
        running = db.query(AssistantRequest).filter_by(status="running")
        if not recover:
            running = running.filter(AssistantRequest.created_at < now - timedelta(seconds=100))
        for row in running:
            row.status, row.phase, row.error = "interrupted", "interrupted", "处理已中断，请重新发送问题。"
            row.finished_at = now
            refund_unpaid(db, row)
        db.query(AssistantRequest).filter(AssistantRequest.created_at < now - timedelta(hours=24)).delete()
        db.query(AssistantSession).filter(AssistantSession.expires_at < now).delete()
        db.query(AssistantUsage).filter(AssistantUsage.created_at < now - timedelta(days=8)).delete()


def admin_overview():
    with SessionLocal() as db:
        total = db.get(AssistantUsage, f"site:{day_key()}")
        return {"settings": settings(db).model_dump(), "usage": {
            key: getattr(total, key) if total else 0 for key in ("count", "calls", "input_tokens", "output_tokens", "web_calls")},
            "running": db.query(AssistantRequest).filter_by(status="running").count(), "day": day_key()}
