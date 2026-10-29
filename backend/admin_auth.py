"""Single administrator, revocable server-side sessions and CSRF protection."""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from backend.database import SessionLocal, get_db
from backend.models.operations import AdminEvent, AdminSession, LoginAttempt

router = APIRouter(prefix="/api/admin", tags=["admin"])
COOKIE = "tracker_admin"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt:{salt}:{value}"


def valid_password(password):
    try:
        method, salt, expected = os.getenv("ADMIN_PASSWORD_HASH", "").split(":")
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return method == "scrypt" and hmac.compare_digest(actual, expected)
    except ValueError:
        return False


def session_for(db, request):
    token = request.cookies.get(COOKIE, "")
    credential = os.getenv("ADMIN_PASSWORD_HASH", "")
    row = db.get(AdminSession, digest(token)) if token and credential else None
    if row and row.expires_at > datetime.utcnow() and hmac.compare_digest(row.credential_hash, digest(credential)):
        return row
    return None


async def admin_guard(request, call_next):
    path = request.url.path
    mutation = request.method not in ("GET", "HEAD", "OPTIONS")
    public_auth = path in ("/api/admin/login", "/api/admin/session")
    guest_write = ((request.method, path) in {("POST", "/api/assistant/messages"), ("DELETE", "/api/assistant/conversation")}
                   or (request.method == "POST" and re.fullmatch(r"/api/assistant/requests/[0-9a-fA-F-]{36}/cancel", path)))
    protected = path.startswith(("/api/admin/", "/api/pending")) or (path.startswith("/api/") and mutation and not guest_write)
    if protected and not public_auth:
        with SessionLocal() as db:
            if not session_for(db, request):
                return JSONResponse({"detail": "请先登录管理员"}, status_code=401)
        if mutation and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), digest(request.cookies.get(COOKIE, "") + ":csrf")):
            return JSONResponse({"detail": "会话校验失败，请刷新后重试"}, status_code=403)
    response = await call_next(request)
    if path.startswith(("/api/admin/", "/api/pending", "/api/assistant/")):
        response.headers["Cache-Control"] = "no-store"
    if protected and mutation and not public_auth and not path.startswith("/api/admin/sources/") and response.status_code < 400:
        with SessionLocal() as db:
            # Paths only: never store passwords, source URLs or API keys.
            db.add(AdminEvent(action=f"{request.method} {path}"))
            db.commit()
    return response


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@router.post("/login")
def login(body: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    if not os.getenv("ADMIN_PASSWORD_HASH"):
        raise HTTPException(503, "尚未设置管理员密码，请在服务器运行 python -m backend.admin_auth")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(403, "不接受跨站登录")
    now = datetime.utcnow()
    address = request.client.host if request.client else "unknown"
    db.query(LoginAttempt).filter(LoginAttempt.attempted_at < now - timedelta(minutes=15)).delete()
    failures = db.query(LoginAttempt).filter_by(address=address).count()
    if failures >= 5:
        db.commit()
        raise HTTPException(429, "登录失败次数过多，请 15 分钟后重试")
    # Count before password checking, including concurrent requests.
    db.add(LoginAttempt(address=address))
    db.commit()
    if not valid_password(body.password):
        raise HTTPException(401, "密码错误")
    db.query(LoginAttempt).filter_by(address=address).delete()
    db.query(AdminSession).filter(AdminSession.expires_at <= now).delete()
    old = request.cookies.get(COOKIE)
    if old:
        db.query(AdminSession).filter_by(token_hash=digest(old)).delete()
    token = secrets.token_urlsafe(32)
    db.add(AdminSession(token_hash=digest(token), credential_hash=digest(os.environ["ADMIN_PASSWORD_HASH"]),
                        expires_at=now + timedelta(hours=12)))
    db.add(AdminEvent(action="管理员登录"))
    db.commit()
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=43200,
                        secure=os.getenv("ADMIN_COOKIE_SECURE", "true").lower() == "true", path="/")
    return {"authenticated": True, "csrf": digest(token + ":csrf")}


@router.get("/session")
def session(request: Request, db: Session = Depends(get_db)):
    logged_in = bool(session_for(db, request))
    return {"configured": bool(os.getenv("ADMIN_PASSWORD_HASH")), "authenticated": logged_in,
            "csrf": digest(request.cookies[COOKIE] + ":csrf") if logged_in else None}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    db.query(AdminSession).filter_by(token_hash=digest(request.cookies.get(COOKIE, ""))).delete()
    db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"authenticated": False}


if __name__ == "__main__":
    import argparse
    from getpass import getpass
    from pathlib import Path
    from dotenv import set_key
    parser = argparse.ArgumentParser(description="Configure the single administrator password")
    parser.add_argument("--print-hash", action="store_true", help="Print a hash to copy into the server .env")
    args = parser.parse_args()
    password = getpass("Administrator password (12+ characters): ")
    if len(password) < 12 or password != getpass("Confirm password: "):
        raise SystemExit("Password must be at least 12 characters and match.")
    hashed = password_hash(password)
    if args.print_hash:
        print("ADMIN_PASSWORD_HASH=" + hashed)
    else:
        set_key(Path(".env"), "ADMIN_PASSWORD_HASH", hashed)
        print("Saved password hash to .env. Restart the web service to apply.")
