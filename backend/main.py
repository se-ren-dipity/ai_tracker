"""FastAPI application entry point — personal AI information tracker."""
import logging
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)

from backend.database import init_db
from backend.routers import articles, models as models_router, pending as pending_router
from backend.routers import insights

app = FastAPI(title="AI Tracker", version="0.3.0")
model_worker = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

    if os.getenv("DEEPSEEK_API_KEY"):
        from backend.collector.scheduler import start_scheduler
        start_scheduler()
        global model_worker
        root = Path(__file__).resolve().parent.parent
        with (root / "data" / "model-worker.log").open("a", encoding="utf-8") as log:
            model_worker = subprocess.Popen([sys.executable, "-X", "utf8", "-B", "-m", "backend.collector.model_worker"],
                cwd=root, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    else:
        logging.getLogger(__name__).warning(
            "DEEPSEEK_API_KEY not set — scheduler disabled."
        )


@app.on_event("shutdown")
async def shutdown():
    if model_worker and model_worker.poll() is None:
        model_worker.terminate()
        # The next worker resumes persisted checkpoints after the lease expires.


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
