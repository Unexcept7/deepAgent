from __future__ import annotations

import json
from typing import Literal
from urllib.parse import quote, unquote
import re

import requests
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.schemas.tool_center import (
    ConnectionInput, CredentialInput, DecisionRequest, EnabledRequest,
    ImportRequest, InvokeRequest, LifecycleRequest, LoginRequest, ToolInput,
)
from app.tool_center.auth import (
    COOKIE, Principal, check_origin, configured_principal, issue_session, local_principal,
    optional_principal, require_admin, require_manager, require_principal, workspace_for,
)
from app.tool_center.context import ExecutionContext
from app.tool_center.imports import preview_bundle, preview_mcp, preview_openapi, templates
from app.tool_center.service import ToolCenterService
from app.tool_center.store import ToolCenterError, get_tool_center_store


router = APIRouter(prefix="/api/tool-center", tags=["工具与数据"])
Kind = Literal["tools", "connections"]


def context_for(request: Request, principal: Principal) -> ExecutionContext:
    return ExecutionContext(principal, workspace_for(request, principal))


@router.get("/session")
def session(request: Request):
    store = get_tool_center_store()
    principal = optional_principal(request)
    session_data = store.session(request.cookies.get(COOKIE)) if principal else {}
    return {
        "authenticated": principal is not None, "principal": principal.public() if principal else None,
        "csrf_token": (session_data or {}).get("csrf_token"),
        "local_login_available": bool(store.settings.tool_center_local_admin and request.client
            and request.client.host in {"127.0.0.1", "::1"} and request.url.hostname in {"localhost", "127.0.0.1", "::1"}
            and not any(request.headers.get(key) for key in ("x-forwarded-for", "forwarded", "x-real-ip"))),
    }


@router.post("/session/local")
def local_session(request: Request, response: Response):
    return issue_session(response, request, local_principal(request))


@router.post("/session/login")
def login(payload: LoginRequest, request: Request, response: Response):
    check_origin(request)
    store = get_tool_center_store()
    # Throttle guesses without ever saving submitted tokens.
    from datetime import UTC, datetime
    events = [event for event in store.audits("default", 300)
              if event["action"] == "login_failed" and event["target"] == (request.client.host if request.client else "unknown")
              and (datetime.now(UTC) - datetime.fromisoformat(event["created_at"])).total_seconds() < 60]
    if len(events) >= 10:
        raise ToolCenterError("登录尝试过多，请稍后再试", 429)
    principal = configured_principal(payload.token)
    if not principal:
        store.audit("default", "anonymous", "login_failed", request.client.host if request.client else "unknown")
        raise ToolCenterError("企业身份令牌无效", 401)
    return issue_session(response, request, principal)


@router.post("/session/logout")
def logout(request: Request, response: Response, principal=Depends(require_principal)):
    get_tool_center_store().delete_session(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"status": "logged_out"}


@router.get("/overview")
def overview(request: Request, principal=Depends(require_principal)):
    context = context_for(request, principal)
    store, service = get_tool_center_store(), ToolCenterService()
    tools = store.list(context.workspace_id, "tools")
    connections = service.connection_status(context.workspace_id)
    calls = service.visible_calls(context)
    warnings = []
    if store.key_source == "local_file":
        warnings.append("开发模式：加密主密钥保存在本地文件。正式部署请设置 TOOL_CENTER_ENCRYPTION_KEYS 并单独备份/限制访问。")
    if store.settings.tool_center_local_admin:
        warnings.append("开发模式：直接回环访问允许本地管理员登录。正式部署请关闭 TOOL_CENTER_LOCAL_ADMIN 并配置真实身份令牌。")
    return {"workspace_id": context.workspace_id, "principal": principal.public(),
            "tool_count": len(tools), "connection_count": len(connections),
            "pending_count": sum(call["status"].startswith("awaiting") for call in calls),
            "call_count": len(calls), "warnings": warnings,
            "allowed_hosts": store.settings.tool_center_allowed_hosts.split(",")}


@router.get("/tools")
def tools(request: Request, principal=Depends(require_principal)):
    workspace = workspace_for(request, principal)
    service = ToolCenterService()
    connections = {item["connection_id"]: item for item in service.connection_status(workspace)}
    items = []
    for item in service.store.list(workspace, "tools"):
        connection = connections.get(item.get("connection_id"))
        status = "disabled" if not item["enabled"] else "draft" if not item["published_version"] else "local" if item["kind"] == "builtin" else (connection or {}).get("health", {}).get("status", "unknown")
        items.append({**item, "connection_name": (connection or {}).get("name"), "execution_status": status})
    return {"tools": items}


@router.post("/tools")
def save_tool(payload: ToolInput, request: Request, principal=Depends(require_manager)):
    workspace = workspace_for(request, principal)
    definition = payload.model_dump()
    ToolCenterService().validate_tool(workspace, definition)
    return {"tool": get_tool_center_store().save(workspace, "tools", definition, principal.user_id)}


@router.get("/connections")
def connections(request: Request, principal=Depends(require_principal)):
    return {"connections": ToolCenterService().connection_status(workspace_for(request, principal))}


@router.post("/connections")
def save_connection(payload: ConnectionInput, request: Request, principal=Depends(require_manager)):
    workspace = workspace_for(request, principal)
    definition = payload.model_dump()
    ToolCenterService().validate_connection(workspace, definition)
    return {"connection": get_tool_center_store().save(workspace, "connections", definition, principal.user_id)}


@router.get("/resources/{kind}/{rid}")
def detail(kind: Kind, rid: str, request: Request, principal=Depends(require_principal)):
    return {"resource": get_tool_center_store().get(workspace_for(request, principal), kind, rid)}


@router.post("/resources/{kind}/{rid}/publish")
def publish(kind: Kind, rid: str, payload: LifecycleRequest, request: Request, principal=Depends(require_admin)):
    workspace = workspace_for(request, principal)
    store, service = get_tool_center_store(), ToolCenterService()
    definition = store.get(workspace, kind, rid)
    if kind == "tools":
        service.validate_tool(workspace, definition, publishing=True)
    else:
        service.validate_connection(workspace, definition)
    return {"resource": store.publish(workspace, kind, rid, principal.user_id, payload.version, payload.note)}


@router.post("/resources/{kind}/{rid}/rollback")
def rollback(kind: Kind, rid: str, payload: LifecycleRequest, request: Request, principal=Depends(require_admin)):
    if not payload.version:
        raise ToolCenterError("必须选择回滚版本")
    workspace = workspace_for(request, principal)
    store = get_tool_center_store()
    # Validate the target snapshot before committing a newly published version.
    with store.transaction() as db:
        row = db.execute("SELECT definition FROM versions WHERE workspace=? AND kind=? AND id=? AND version=?",
                         (workspace, kind, rid, payload.version)).fetchone()
    if not row:
        raise ToolCenterError("历史版本不存在", 404)
    definition = json.loads(row["definition"])
    service = ToolCenterService(store)
    service.validate_tool(workspace, definition, publishing=True) if kind == "tools" else service.validate_connection(workspace, definition)
    return {"resource": store.rollback(workspace, kind, rid, payload.version, principal.user_id, payload.note)}


@router.post("/resources/{kind}/{rid}/enabled")
def enabled(kind: Kind, rid: str, payload: EnabledRequest, request: Request, principal=Depends(require_manager)):
    return {"resource": get_tool_center_store().set_enabled(workspace_for(request, principal), kind, rid, payload.enabled, principal.user_id)}


@router.get("/resources/{kind}/{rid}/compare")
def compare(kind: Kind, rid: str, from_version: int, to_version: int, request: Request, principal=Depends(require_principal)):
    return get_tool_center_store().compare(workspace_for(request, principal), kind, rid, from_version, to_version)


@router.post("/connections/{cid}/test")
def test_connection(cid: str, request: Request, principal=Depends(require_manager)):
    return {"health": ToolCenterService().health(workspace_for(request, principal), cid)}


@router.get("/credentials")
def credentials(request: Request, principal=Depends(require_principal)):
    return {"credentials": get_tool_center_store().credentials(workspace_for(request, principal))}


@router.post("/credentials")
def save_credential(payload: CredentialInput, request: Request, principal=Depends(require_admin)):
    return {"credential": get_tool_center_store().save_credential(workspace_for(request, principal), payload.model_dump(), principal.user_id)}


@router.delete("/credentials/{cid}")
def delete_credential(cid: str, request: Request, principal=Depends(require_admin)):
    get_tool_center_store().delete_credential(workspace_for(request, principal), cid, principal.user_id)
    return {"status": "deleted"}


@router.post("/credentials/reencrypt")
def reencrypt(request: Request, principal=Depends(require_admin)):
    return get_tool_center_store().rotate_encryption(workspace_for(request, principal), principal.user_id)


@router.post("/tools/{tool_id}/invoke")
def invoke(tool_id: str, payload: InvokeRequest, request: Request, principal=Depends(require_principal)):
    context = context_for(request, principal)
    context.app_id, context.workflow_id, context.trace_id = payload.app_id, payload.workflow_id, payload.trace_id
    if payload.app_id:
        from app.application_store import ApplicationError, get_application_store
        try:
            context.environment = get_application_store().for_run(payload.app_id).get("runtime", {}).get("environment", "production")
        except ApplicationError as exc:
            raise ToolCenterError(str(exc), 409) from exc
    return {"call": ToolCenterService().invoke(context, tool_id, payload)}


@router.get("/calls")
def calls(request: Request, status: str | None = None, limit: int = 100, principal=Depends(require_principal)):
    return {"calls": ToolCenterService().visible_calls(context_for(request, principal), status=status, limit=limit)}


@router.get("/calls/{cid}")
def call_detail(cid: str, request: Request, principal=Depends(require_principal)):
    workspace = workspace_for(request, principal)
    call, _ = get_tool_center_store().call(workspace, cid)
    if call["actor_id"] != principal.user_id and not principal.has("admin", "editor", "approver"):
        raise ToolCenterError("不能读取其他用户的调用记录", 403)
    return {"call": ToolCenterService()._expire(workspace, call)}


@router.post("/calls/{cid}/decision")
def decision(cid: str, payload: DecisionRequest, request: Request, principal=Depends(require_principal)):
    return {"call": ToolCenterService().decide(context_for(request, principal), cid, payload.decision, payload.note)}


@router.get("/audit")
def audit(request: Request, limit: int = 100, principal=Depends(require_manager)):
    return {"events": get_tool_center_store().audits(workspace_for(request, principal), limit)}


@router.get("/templates")
def tool_templates(request: Request, principal=Depends(require_principal)):
    workspace_for(request, principal)
    return {"templates": templates(get_tool_center_store().settings)}


@router.get("/export")
def export(request: Request, principal=Depends(require_manager)):
    workspace = workspace_for(request, principal)
    document = {"schema_version": 1}
    store = get_tool_center_store()
    for kind, model in (("tools", ToolInput), ("connections", ConnectionInput)):
        document[kind] = [{key: value for key, value in item.items() if key in model.model_fields and key != "base_version"}
                          for item in store.list(workspace, kind)]
    store.audit(workspace, principal.user_id, "export", "definitions")
    return document


@router.post("/imports/preview")
def import_preview(payload: ImportRequest, request: Request, principal=Depends(require_manager)):
    workspace = workspace_for(request, principal)
    if len(json.dumps(payload.document)) > 1000000:
        raise ToolCenterError("导入文档超过 1 MB")
    try:
        if payload.format == "bundle":
            return preview_bundle(payload.document)
        if not payload.connection_id:
            raise ToolCenterError("请选择连接")
        service = ToolCenterService()
        connection = service.store.runtime(workspace, "connections", payload.connection_id, debug=True)
        return preview_openapi(payload.document, connection) if payload.format == "openapi" else preview_mcp(service, workspace, connection)
    except ValidationError as exc:
        raise ToolCenterError("导入定义未通过安全/Schema 校验", 422) from exc


@router.post("/imports")
def import_commit(payload: ImportRequest, request: Request, principal=Depends(require_manager)):
    if payload.format != "bundle":
        raise ToolCenterError("请先预览，再以 bundle 提交审核后的定义")
    workspace = workspace_for(request, principal)
    try:
        bundle = preview_bundle(payload.document)
    except ValidationError as exc:
        raise ToolCenterError("导入定义未通过安全/Schema 校验", 422) from exc
    service = ToolCenterService()
    for connection in bundle["connections"]:
        service.validate_connection(workspace, connection)
    combined = {item["connection_id"]: item for item in bundle["connections"]}
    for tool in bundle["tools"]:
        if tool["kind"] == "builtin":
            service.validate_tool(workspace, tool)
            continue
        if tool.get("connection_id") in combined:
            connection = combined[tool["connection_id"]]
            if connection["environment"] != tool["environment"]:
                raise ToolCenterError("工具与连接环境不一致")
            expected = "mcp" if tool["kind"] == "mcp" else "business_api"
            if connection["type"] != expected:
                raise ToolCenterError("工具与连接类型不一致")
        else:
            service.validate_tool(workspace, tool)
    return service.store.import_bundle(workspace, bundle["connections"], bundle["tools"], principal.user_id)


@router.get("/connections/{cid}/documents/{dataset_id}/{document_id}")
def document(cid: str, dataset_id: str, document_id: str, request: Request, principal=Depends(require_principal)):
    workspace = workspace_for(request, principal)
    service = ToolCenterService()
    connection = service.store.runtime(workspace, "connections", cid)
    if connection["type"] != "ragflow":
        raise ToolCenterError("此连接不是 RAGFlow")
    dataset_ids = connection["config"].get("dataset_ids") or []
    if dataset_id not in dataset_ids:
        raise ToolCenterError("该知识库未绑定到此连接", 403)
    service.check_endpoint(connection["endpoint"])
    upstream_session = requests.Session()
    upstream_session.trust_env = False
    try:
        upstream = upstream_session.get(
            connection["endpoint"].rstrip("/") + "/api/v1/datasets/" + quote(dataset_id, safe="") + "/documents/" + quote(document_id, safe=""),
            headers=service.headers(workspace, connection), stream=True, allow_redirects=False, timeout=60)
        if 300 <= upstream.status_code < 400:
            raise ToolCenterError("拒绝上游文档重定向", 502)
        upstream.raise_for_status()
    except Exception:
        upstream_session.close()
        raise
    def content():
        try:
            yield from upstream.iter_content(chunk_size=65536)
        finally:
            upstream.close()
            upstream_session.close()

    # RAGFlow returns the original document stream.  Preserve the real media
    # type so PDF citations open in the browser's viewer instead of being
    # downloaded as an opaque ``application/octet-stream`` file.
    media_type = (upstream.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    response_headers = {"X-Content-Type-Options": "nosniff"}
    disposition = upstream.headers.get("Content-Disposition", "")
    if media_type == "application/pdf":
        filename = ""
        filename_star = re.search(r"filename\\*=(?:UTF-8'')?([^;]+)", disposition, flags=re.IGNORECASE)
        if filename_star:
            filename = unquote(filename_star.group(1).strip().strip('"'))
        else:
            filename_match = re.search(r"filename=([^;]+)", disposition, flags=re.IGNORECASE)
            if filename_match:
                filename = filename_match.group(1).strip().strip('"')
        # Some RAGFlow deployments expose UTF-8 filenames through a header
        # decoded as latin-1 by the HTTP client; repair that common case.
        if filename:
            try:
                filename = filename.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        filename = filename or f"{document_id}.pdf"
        response_headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(filename)}"
    elif disposition:
        response_headers["Content-Disposition"] = disposition
    return StreamingResponse(content(), media_type=media_type, headers=response_headers)
