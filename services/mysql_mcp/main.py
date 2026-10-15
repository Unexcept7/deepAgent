from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from .database import MySQLRepository
from .mcp import MCPService
from .settings import Settings


settings = Settings.from_env()
repository = MySQLRepository(settings)
mcp_service = MCPService(repository)

app = FastAPI(title="MySQL MCP Service", version="1.0.0")


def _authorize(request: Request) -> None:
    if not settings.mcp_auth_token:
        return
    expected = f"Bearer {settings.mcp_auth_token}"
    if request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="未通过 MCP 服务认证")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        repository.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MySQL 不可用: {exc}") from exc
    return {"status": "ok", "database": "ok"}


@app.get("/api/devices")
def list_devices(
    request: Request,
    query: str = "",
    status: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    _authorize(request)
    try:
        items = repository.list_devices(query, status, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items, "total": len(items)}


@app.get("/api/devices/{device_id}")
def get_device(device_id: str, request: Request) -> dict[str, object]:
    _authorize(request)
    item = repository.get_device(device_id)
    if item is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return item


@app.get("/api/devices/{device_id}/alarms")
def get_alarms(
    device_id: str,
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    _authorize(request)
    try:
        items = repository.list_alarms(device_id, days, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"device_id": device_id, "items": items, "total": len(items)}


@app.get("/api/search")
def search(
    request: Request,
    query: str,
    top_k: int = Query(default=10, ge=1, le=50),
) -> dict[str, object]:
    _authorize(request)
    try:
        items = repository.search(query, top_k)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"query": query, "items": items, "total": len(items)}


@app.post("/mcp")
async def mcp(request: Request) -> Response:
    _authorize(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc

    response = mcp_service.handle(payload)
    if response is None:
        return Response(status_code=204)
    return JSONResponse(response)
