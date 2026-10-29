"""FastAPI application entry point — personal AI information tracker."""
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)

from backend.database import init_db
from backend.routers import articles, models as models_router, pending as pending_router
from backend.routers import insights, admin, assistant
from backend.admin_auth import admin_guard, router as auth_router

app = FastAPI(title="AI Tracker", version="0.3.0")
app.middleware("http")(admin_guard)

@app.middleware("http")
async def revalidate_frontend(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or response.headers.get("content-type", "").startswith("text/html"):
        # Revalidate HTML and scripts together so an updated page cannot reuse stale UI code.
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(articles.router)
app.include_router(models_router.router)
app.include_router(pending_router.router)
app.include_router(insights.router)
app.include_router(auth_router)
app.include_router(admin.router)
app.include_router(assistant.router)


@app.on_event("startup")
async def startup():
    init_db()
    from backend.database import SessionLocal
    from backend.models.model_db import AIModel
    from backend.seed import seed_models
    db = SessionLocal()
    try:
        if db.query(AIModel).count() == 0:
            seed_models(db)
    finally:
        db.close()

    from backend.collector.model_profiles import bootstrap_profiles
    with SessionLocal() as db:
        bootstrap_profiles(db)
    await assistant.start()


@app.on_event("shutdown")
async def shutdown():
    await assistant.stop()

@app.get("/health/ready", include_in_schema=False)
def ready():
    from sqlalchemy import text
    from backend.database import SessionLocal
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok"}


STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def spa_root():
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return {"message": "AI Tracker API v0.3"}


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_catchall(full_path: str):
    if any(full_path.startswith(p) for p in ("api/", "docs", "openapi")):
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return {"message": "Frontend not found."}
