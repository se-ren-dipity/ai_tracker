"""日报摘要路由"""
from fastapi import APIRouter

router = APIRouter(prefix="/api/digest", tags=["digest"])
# Digest is served from articles router (/api/articles/daily/{date} and /api/articles/today)
