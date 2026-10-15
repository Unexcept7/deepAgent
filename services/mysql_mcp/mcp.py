from __future__ import annotations

import json
from typing import Any


SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
)


TOOLS = [
    {
        "name": "equipment_search",
        "title": "设备与告警搜索",
        "description": "在 MySQL 设备表和告警表中执行受限关键词搜索，不接受自然语言 SQL。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "filters": {"type": "object", "additionalProperties": False, "properties": {
                    "device_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["online", "offline", "warning"]},
                }},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"items": {"type": "array"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "get_device_status",
        "title": "读取设备状态",
        "description": "按设备编号读取设备的型号、AI/DI/DO 路数、协议和当前状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"device_id": {"type": "string", "minLength": 1}},
            "required": ["device_id"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"device": {"type": ["object", "null"]}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "get_alarm_history",
        "title": "读取设备告警历史",
        "description": "按设备编号读取最近一段时间内的告警记录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "minLength": 1},
                "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"items": {"type": "array"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _text_content(value: dict[str, Any]) -> list[dict[str, str]]:
    return [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}]


class MCPService:
    def __init__(self, repository: Any):
        self.repository = repository

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _error(request.get("id") if isinstance(request, dict) else None, -32600, "无效的 JSON-RPC 请求")

        method = request.get("method")
        request_id = request.get("id")
        if not isinstance(method, str):
            return _error(request_id, -32600, "缺少 method") if request_id is not None else None
        if method == "notifications/initialized":
            return None
        if request_id is None:
            return None

        try:
            params = request.get("params") or {}
            if method == "initialize":
                requested = params.get("protocolVersion")
                protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[-1]
                return _response(
                    request_id,
                    {
                        "protocolVersion": protocol_version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "mysql-mcp", "version": "1.0.0"},
                    },
                )
            if method == "tools/list":
                return _response(request_id, {"tools": TOOLS})
            if method == "tools/call":
                return _response(request_id, {"content": [], **self._call_tool(params)})
            return _error(request_id, -32601, f"不支持的方法: {method}")
        except (KeyError, TypeError, ValueError) as exc:
            return _error(request_id, -32602, str(exc))
        except Exception as exc:  # pragma: no cover - depends on the live database
            return _error(request_id, -32000, f"数据库查询失败: {exc}")

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise ValueError("tools/call 缺少工具名")
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是对象")

        if name == "equipment_search":
            query = str(arguments.get("query") or "").strip()
            if not query:
                raise ValueError("query 不能为空")
            data = {"query": query, "items": self.repository.search(
                query, int(arguments.get("top_k", 10)), arguments.get("filters") or {}
            )}
        elif name == "get_device_status":
            device_id = str(arguments.get("device_id") or "").strip()
            if not device_id:
                raise ValueError("device_id 不能为空")
            data = {"device": self.repository.get_device(device_id)}
        elif name == "get_alarm_history":
            device_id = str(arguments.get("device_id") or "").strip()
            if not device_id:
                raise ValueError("device_id 不能为空")
            data = {"device_id": device_id, "items": self.repository.list_alarms(
                device_id, int(arguments.get("days", 30)), int(arguments.get("limit", 50))
            )}
        else:
            raise ValueError(f"未知工具: {name}")

        return {
            "isError": False,
            "content": _text_content(data),
            "structuredContent": data,
        }
