"""Read-only model registry tools: python -m backend.mcp_server."""
import argparse
import hmac
import os
from typing import Annotated, Any

from dotenv import load_dotenv
load_dotenv()

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from backend.database import SessionLocal, init_db
from backend import registry


def create_server(session_factory=SessionLocal):
    server = MCPServer("AI Tracker", instructions="先 search_models 查找候选，再 get_model 核对机构、版本与证据；相似名称不代表同一模型。所有工具只读。")
    readonly = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

    @server.tool(annotations=readonly, structured_output=True)
    def search_news(terms: list[str] | None = None, date_from: str | None = None, date_to: str | None = None,
                    limit: Annotated[int, Field(ge=1, le=20)] = 8, offset: Annotated[int, Field(ge=0, le=1000)] = 0) -> dict[str, Any]:
        """按来源发布日期检索已整理新闻，最近默认北京时间 7 个自然日。"""
        from backend.assistant.tools import news_search
        from backend.assistant.schemas import NewsQuery
        with session_factory() as db:
            return news_search(db, NewsQuery(terms=terms or [], date_from=date_from, date_to=date_to, limit=limit, offset=offset))

    @server.tool(annotations=readonly, structured_output=True)
    def find_models(params_min: float | None = None, params_max: float | None = None, context_min: int | None = None,
                    type: str | None = None, weight_access: str | None = None,
                    limit: Annotated[int, Field(ge=1, le=20)] = 8, offset: Annotated[int, Field(ge=0, le=1000)] = 0) -> dict[str, Any]:
        """条件筛选已收录模型，将符合条件与字段未知的候选分开。参数量采用总参数。"""
        from backend.assistant.tools import find_models as query_models
        from backend.assistant.schemas import ModelFilter
        with session_factory() as db:
            return query_models(db, ModelFilter(params_min=params_min, params_max=params_max, context_min=context_min,
                type=type, weight_access=weight_access, limit=limit, offset=offset))

    @server.tool(annotations=readonly, structured_output=True)
    def search_models(query: Annotated[str, Field(min_length=1, max_length=200)], organization: str | None = None,
                      limit: Annotated[int, Field(ge=1, le=50)] = 10, offset: Annotated[int, Field(ge=0)] = 0) -> dict[str, Any]:
        """检索模型名称及已确认别名，返回候选、机构、具体版本和匹配依据。"""
        with session_factory() as db:
            return registry.search_models(db, query, organization, limit, offset, include_pending=True)

    @server.tool(annotations=readonly, structured_output=True)
    def get_model(model_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """读取模型当前档案、别名、后续事件和仍待确认的字段冲突。"""
        with session_factory() as db:
            try:
                return registry.get_model(db, model_id)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc

    @server.tool(annotations=readonly, structured_output=True)
    def get_evidence(model_id: Annotated[int, Field(ge=1)], field: str | None = None,
                     limit: Annotated[int, Field(ge=1, le=100)] = 30) -> list[dict[str, Any]]:
        """读取字段证据及原文引用；不要把 proposed 更正建议当作已确认事实。"""
        with session_factory() as db:
            return registry.get_evidence(db, model_id, field, limit)

    @server.tool(annotations=readonly, structured_output=True)
    def get_source_health() -> dict[str, Any]:
        """查看来源错误分类、诊断建议、缓存时间及下一次恢复时间，不返回凭据。"""
        from backend.collector.source_settings import source_overview
        from backend.collector.source_health import research_health
        with session_factory() as db:
            return {"sources": source_overview(db), "research_service": research_health()}

    return server


class BearerAuth(BaseHTTPMiddleware):
    def __init__(self, app, token):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + self.token).encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)


def create_http_app(server, token):
    if not token or len(token) < 32:
        raise ValueError("HTTP MCP 需要设置至少 32 个字符的 MCP_READ_TOKEN")
    app = server.streamable_http_app()
    app.add_middleware(BearerAuth, token=token)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    init_db()
    server = create_server()
    if args.transport == "stdio":
        server.run()
    else:
        import uvicorn
        uvicorn.run(create_http_app(server, os.getenv("MCP_READ_TOKEN", "")), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
