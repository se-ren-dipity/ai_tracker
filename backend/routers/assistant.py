"""Guest-only conversation API; separate ownership and CSRF from administrator APIs."""
import asyncio
import json
import os
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from backend.assistant import session, agent
from backend.assistant.schemas import Message
from backend.runtime import process_lock

router = APIRouter(prefix="/api/assistant", tags=["assistant"])
tasks = {}
cleanup_task = None
web_lock = None


@router.get("/session")
def open_session(request: Request, response: Response):
    token, payload = session.open_session(request)
    response.set_cookie(session.COOKIE, token, httponly=True, samesite="strict", max_age=86400,
                        secure=os.getenv("ADMIN_COOKIE_SECURE", "true").lower() == "true", path="/api/assistant")
    response.headers["Cache-Control"] = "no-store"
    return payload


@router.post("/messages")
async def message(body: Message, request: Request):
    token = session.check_write(request)
    result, fresh = await asyncio.to_thread(session.admit, token, session.address_key(request), body)
    identifier = result["id"]
    if fresh:
        task = asyncio.create_task(agent.run(identifier))
        tasks[identifier] = task
        task.add_done_callback(lambda _: tasks.pop(identifier, None))

    async def events():
        previous = None
        while True:
            try:
                current = await asyncio.to_thread(session.owned_request, token, identifier)
            except HTTPException:
                break  # Another tab may have cleared or expired this conversation.
            payload = json.dumps(current, ensure_ascii=False)
            if payload != previous:
                yield f"data: {payload}\n\n"
                previous = payload
            if current["status"] != "running":
                break
            await asyncio.sleep(.5)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/requests/{identifier}")
def get_request(identifier: UUID, request: Request, response: Response):
    session.check_origin(request)
    response.headers["Cache-Control"] = "no-store"
    return session.owned_request(request.cookies.get(session.COOKIE, ""), str(identifier))


@router.post("/requests/{identifier}/cancel")
async def cancel(identifier: UUID, request: Request):
    token = session.check_write(request)
    value = str(identifier)
    await asyncio.to_thread(session.owned_request, token, value)
    await asyncio.to_thread(session.finish, value, "cancelled", None, "已停止生成。")
    if task := tasks.get(value):
        task.cancel()
    return {"status": "cancelled"}


@router.delete("/conversation")
async def clear(request: Request):
    token = session.check_write(request)
    identifiers, payload = await asyncio.to_thread(session.clear_conversation, token)
    for identifier in identifiers:
        if task := tasks.get(identifier):
            task.cancel()
    return payload


async def start():
    global cleanup_task, web_lock
    web_lock = process_lock("assistant-web")
    try:
        web_lock.__enter__()
    except OSError as exc:
        web_lock = None
        raise RuntimeError("问答助手要求同一数据库只运行一个 Web 进程；请关闭重复进程，不要设置多个 uvicorn workers。") from exc
    try:
        await asyncio.to_thread(session.recover_and_cleanup, True)
    except Exception:
        web_lock.__exit__(None, None, None)
        web_lock = None
        raise
    async def cleanup():
        while True:
            await asyncio.sleep(3600)
            await asyncio.to_thread(session.recover_and_cleanup)
    cleanup_task = asyncio.create_task(cleanup())


async def stop():
    global web_lock
    pending = list(tasks.values()) + ([cleanup_task] if cleanup_task else [])
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if web_lock is not None:
        web_lock.__exit__(None, None, None)
        web_lock = None
