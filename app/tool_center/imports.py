from __future__ import annotations

import copy
import re

from app.schemas.tool_center import ConnectionInput, ToolInput
from app.tool_center.store import ToolCenterError, initial_resources


def expand_schema(value, document, stack=()):
    if isinstance(value, dict):
        if "$ref" in value:
            ref = value["$ref"]
            if not ref.startswith("#/") or ref in stack:
                raise ToolCenterError("只支持非递归的本地 OpenAPI 引用")
            target = document
            try:
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError) as exc:
                raise ToolCenterError("OpenAPI 引用不存在") from exc
            return expand_schema({**target, **{k: v for k, v in value.items() if k != "$ref"}}, document, (*stack, ref))
        return {key: expand_schema(child, document, stack) for key, child in value.items()}
    if isinstance(value, list):
        return [expand_schema(child, document, stack) for child in value]
    return value


def preview_openapi(document, connection):
    if not str(document.get("openapi", "")).startswith("3."):
        raise ToolCenterError("只支持 OpenAPI 3.x JSON 文档")
    result = []
    for path, path_item in document.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.upper() not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            if not isinstance(operation, dict):
                continue
            tid = operation.get("operationId") or re.sub(r"[^A-Za-z0-9_]+", "_", method + "_" + path).strip("_")
            properties, required, query_fields, body_fields = {}, [], [], []
            for parameter in path_item.get("parameters", []) + operation.get("parameters", []):
                parameter = expand_schema(parameter, document)
                if parameter.get("in") not in {"path", "query"}:
                    raise ToolCenterError("当前导入不支持 header/cookie 参数，请改用连接凭据或手动定义")
                name = parameter["name"]
                properties[name] = expand_schema(parameter.get("schema", {"type": "string"}), document)
                if parameter.get("required") or parameter.get("in") == "path":
                    required.append(name)
                if parameter.get("in") == "query":
                    query_fields.append(name)
            body = expand_schema(operation.get("requestBody", {}), document)
            if body:
                schema = body.get("content", {}).get("application/json", {}).get("schema")
                if not schema or schema.get("type", "object") != "object":
                    raise ToolCenterError("当前导入仅支持 object 类型的 application/json 请求体")
                for name, spec in schema.get("properties", {}).items():
                    if name in properties:
                        raise ToolCenterError("OpenAPI 参数与请求体字段重名，请手动定义")
                    properties[name] = spec
                    body_fields.append(name)
                required.extend(schema.get("required", []))
            definition = ToolInput(
                tool_id=tid, name=operation.get("summary") or tid, description=operation.get("description") or operation.get("summary") or f"{method.upper()} {path}",
                kind="http", connection_id=connection["connection_id"], environment=connection["environment"],
                risk="read_only" if method.upper() in {"GET", "HEAD"} else "confirm",
                method=method.upper(), path=path,
                input_schema={"type": "object", "properties": properties, "required": list(dict.fromkeys(required)), "additionalProperties": False},
                config={"query_parameters": query_fields, "body_parameters": body_fields, "include_path_in_body": False},
            ).model_dump()
            result.append(definition)
            if len(result) > 100:
                raise ToolCenterError("一次最多导入 100 个工具")
    return {"connections": [], "tools": result, "warnings": ["工具保存为草稿，不自动发布或授权给 Agent；所有 HTTP 写方法默认需要人工确认。"]}


def preview_bundle(document):
    if set(document) - {"schema_version", "tools", "connections", "warnings"}:
        raise ToolCenterError("导入包包含不支持的字段；不接受凭据、会话或调用记录")
    connections = [ConnectionInput.model_validate(item).model_dump() for item in document.get("connections", [])]
    tools = [ToolInput.model_validate(item).model_dump() for item in document.get("tools", [])]
    if len(connections) > 50 or len(tools) > 100:
        raise ToolCenterError("导入数量超过限制")
    if len({item["tool_id"] for item in tools}) != len(tools) or len({item["connection_id"] for item in connections}) != len(connections):
        raise ToolCenterError("导入包中存在重复 ID")
    return {"connections": connections, "tools": tools, "warnings": ["凭据不会导入；请先创建对应凭据。导入只保存草稿，不会自动发布或授权。"]}


def preview_mcp(service, workspace, connection):
    tools, cursor = [], None
    for _ in range(10):
        payload, _ = service.mcp_rpc(workspace, connection, "tools/list", {"cursor": cursor} if cursor else {})
        for item in payload.get("tools", []):
            remote = item.get("name", "")
            tid = re.sub(r"[^A-Za-z0-9_-]+", "_", remote)[:64]
            if not tid or not tid[0].isalpha():
                tid = "mcp_" + tid[:60]
            tools.append(ToolInput(
                tool_id=tid, name=item.get("title") or remote, description=item.get("description") or remote,
                kind="mcp", connection_id=connection["connection_id"], environment=connection["environment"],
                risk="confirm", input_schema=item.get("inputSchema") or {"type": "object", "properties": {}},
                output_schema=item.get("outputSchema") or {}, config={"remote_name": remote},
            ).model_dump())
            if len(tools) > 100:
                raise ToolCenterError("MCP 服务超过 100 个工具，请分批手动登记")
        cursor = payload.get("nextCursor")
        if not cursor:
            break
    if cursor:
        raise ToolCenterError("MCP 工具分页超过限制")
    if len({item["tool_id"] for item in tools}) != len(tools):
        raise ToolCenterError("MCP 工具名规范化后发生 ID 冲突，请手动命名")
    return {"tools": tools, "connections": [], "warnings": ["不信任远端 readOnlyHint；所有发现的 MCP 工具默认需要人工确认，请审核后再调整风险并发布。"]}


def templates(settings):
    return [{"template_id": item["tool_id"], "name": item["name"], "definition": copy.deepcopy(item)}
            for kind, item in initial_resources(settings) if kind == "tools"]
