from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import requests
from jsonschema import Draft202012Validator
from langchain_core.tools import StructuredTool

from app.application_store import ApplicationError, get_application_store
from app.config import get_settings
from app.graph.provenance import graph_relation_id, normalize_graph_relation_evidence
from app.graph.relevance import expand_graph_attribute_terms, rank_graph_records
from app.schemas.tool_center import ConnectionInput, InvokeRequest
from app.search.models import SearchHit, SearchRequest
from app.search.structured_query import (
    StructuredQueryPolicyError,
    merge_sql_scope,
    normalize_sql_hits,
    sql_request_payload,
    validate_structured_queries,
)
from app.tool_center.auth import Principal, refresh_principal, authorize_application
from app.tool_center.context import ExecutionContext
from app.tool_center.store import ToolCenterError, fingerprint, get_tool_center_store, now, redact
from app.workflow_store import WorkflowError, get_workflow_store


ALIASES = {"device_status": "get_device_status", "create_work_order": "submit_work_order"}
INTERNAL_TOOLS = {"unified_search", "evidence_review", "coverage_audit"}
WRITES = {"confirm", "approval", "device_control"}
logger = logging.getLogger(__name__)


def canonical_tool(value: str) -> str:
    return ALIASES.get(value, value)


def bound_tools(application: dict) -> dict:
    return {canonical_tool(item["tool_id"]): item for item in application.get("tools", [])
            if item.get("enabled", True)}


def validate_parameters(schema: dict, parameters: dict) -> dict:
    if len(json.dumps(parameters, default=str)) > 100000:
        raise ToolCenterError("工具参数过大")
    parameters = dict(parameters)
    for key, prop in schema.get("properties", {}).items():
        if key not in parameters and "default" in prop:
            parameters[key] = prop["default"]
    errors = sorted(Draft202012Validator(schema).iter_errors(parameters), key=lambda item: str(item.path))
    if errors:
        # Do not echo sensitive values included in jsonschema's error.message.
        error = errors[0]
        path = ".".join(str(part) for part in error.path) or "parameters"
        raise ToolCenterError(f"参数校验失败：{path} 不满足 {error.validator} 约束", 422)
    return parameters


class ToolCenterService:
    def __init__(self, store=None):
        self.store = store or get_tool_center_store()

    def check_endpoint(self, endpoint: str):
        parsed = urlsplit(endpoint)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ToolCenterError("连接地址未配置或无效", 409)
        settings = self.store.settings
        allowed = {host.strip().lower() for host in settings.tool_center_allowed_hosts.split(",") if host.strip()}
        # Existing administrator-provided environment endpoints remain usable.
        for field in ("ragflow_base_url", "neo4j_uri", "business_api_base_url", "sql_search_url", "mcp_search_url"):
            host = urlsplit(getattr(settings, field, None) or "").hostname
            if host:
                allowed.add(host.lower())
        if parsed.hostname.lower() not in allowed:
            raise ToolCenterError("目标主机未获服务器授权，请加入 TOOL_CENTER_ALLOWED_HOSTS", 403)

    def validate_connection(self, workspace, definition):
        if definition["type"] != "qa" and definition.get("endpoint"):
            self.check_endpoint(definition["endpoint"])
        cid = definition.get("credential_id")
        if cid:
            credential = next((item for item in self.store.credentials(workspace) if item["credential_id"] == cid), None)
            if not credential:
                raise ToolCenterError("凭据不存在或不属于当前工作区", 404)
            if credential["environment"] != definition["environment"]:
                raise ToolCenterError("数据连接和凭据必须属于同一环境", 409)
        if definition["type"] == "qa" and definition.get("config", {}).get("path"):
            self.qa_path(definition["config"]["path"])

    def validate_tool(self, workspace, definition, *, publishing=False):
        if definition["kind"] == "builtin" and definition.get("handler") != "draft_work_order":
            raise ToolCenterError("只允许已实现的内置处理器 draft_work_order")
        if definition["kind"] == "builtin" and definition.get("connection_id"):
            raise ToolCenterError("本地工单草稿不能绑定外部连接")
        if definition.get("connection_id"):
            connection = self.store.runtime(workspace, "connections", definition["connection_id"]) if publishing else self.store.get(workspace, "connections", definition["connection_id"])
            if connection["environment"] != definition["environment"]:
                raise ToolCenterError("工具和数据连接必须属于同一环境", 409)
            if definition["kind"] == "http" and connection["type"] != "business_api":
                raise ToolCenterError("HTTP 业务工具必须使用 business_api 连接")
            if definition["kind"] == "mcp" and connection["type"] != "mcp":
                raise ToolCenterError("MCP 工具必须使用 MCP 连接")
        if definition["kind"] == "builtin" and definition["risk"] != "draft":
            raise ToolCenterError("本地工单草稿的风险必须为 draft")

    def headers(self, workspace, connection):
        secret = self.store.credential_secret(workspace, connection.get("credential_id"))
        headers = {"Content-Type": "application/json"}
        mode = connection.get("auth_mode", "none")
        if mode in {"bearer", "api_key"}:
            token = secret.get("token") or secret.get("api_key")
            if not token:
                raise ToolCenterError("数据连接凭据未配置", 409)
            if mode == "bearer":
                headers["Authorization"] = f"Bearer {token}"
            else:
                header = connection.get("auth_header", "X-API-Key")
                if header.lower() in {"host", "content-length", "cookie", "connection"}:
                    raise ToolCenterError("不允许此凭据头名称")
                headers[header] = token
        elif mode == "basic":
            from requests.auth import _basic_auth_str
            if not secret.get("password"):
                raise ToolCenterError("数据连接密码未配置", 409)
            headers["Authorization"] = _basic_auth_str(secret.get("username", ""), secret["password"])
        return headers

    def request(self, method, url, *, timeout, headers=None, params=None, payload=None, notification=False, rpc_id=None):
        self.check_endpoint(url)
        started = time.monotonic()
        with requests.Session() as session:
            session.trust_env = False
            with session.request(method, url, headers=headers, params=params, json=payload,
                                 timeout=(min(5, timeout), timeout), allow_redirects=False, stream=True) as response:
                if 300 <= response.status_code < 400:
                    raise ToolCenterError("上游返回重定向，已拒绝向其他地址发送凭据", 502)
                response.raise_for_status()
                response_headers = dict(response.headers)
                if notification or method == "HEAD" or response.status_code == 204:
                    return {}, response_headers
                limit, chunks = 2 * 1024 * 1024, []
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    size = 0
                    event_data = []
                    for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                        if time.monotonic() - started > timeout:
                            raise requests.Timeout("MCP 响应超时")
                        size += len(line or "")
                        if size > limit:
                            raise ToolCenterError("上游响应超过 2 MB", 502)
                        if line.startswith("data:"):
                            event_data.append(line[5:].lstrip())
                        elif not line and event_data:
                            body = json.loads("\n".join(event_data))
                            event_data = []
                            if body.get("id") == rpc_id:
                                return body, response_headers
                    raise ToolCenterError("MCP 流未返回匹配的响应", 502)
                total = 0
                for chunk in response.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > limit:
                        raise ToolCenterError("上游响应超过 2 MB", 502)
                    if time.monotonic() - started > timeout:
                        raise requests.Timeout("响应超时")
                    chunks.append(chunk)
                return json.loads(b"".join(chunks) or b"{}"), response_headers

    def scrub(self, workspace, value):
        value = redact(value)
        # Prevent a downstream API echoing the injected credential in an error/result.
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        for item in self.store.credentials(workspace):
            try:
                secrets = self.store.credential_secret(workspace, item["credential_id"])
            except ToolCenterError:
                continue
            for key, secret in secrets.items():
                if key != "username" and secret and len(secret) >= 4:
                    encoded = encoded.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "••••••")
        return json.loads(encoded)

    def qa_path(self, value):
        root = Path(__file__).parents[2].resolve()
        path = Path(value).resolve()
        if not path.is_relative_to(root):
            raise ToolCenterError("QA 文件必须位于项目目录内", 403)
        if path.suffix.lower() != ".json":
            raise ToolCenterError("QA 文件必须是 JSON")
        return path

    def _authorization(self, context: ExecutionContext, tool: dict, request: InvokeRequest):
        actor = refresh_principal(context.principal)
        if context.workspace_id not in actor.workspaces:
            raise ToolCenterError("申请人的工作区权限已撤销", 403)
        if not request.debug and not actor.has("admin") and not set(actor.roles) & set(tool["allowed_roles"]):
            raise ToolCenterError("没有该工具的调用权限", 403)
        if not request.debug and not actor.can(tool["required_permission"]):
            raise ToolCenterError("用户没有工具所要求的权限标识：" + tool["required_permission"], 403)
        if request.debug:
            if not actor.has("admin", "editor"):
                raise ToolCenterError("只有管理员/编辑者可以调试工具", 403)
            if tool["environment"] == "production" and tool["risk"] in WRITES:
                raise ToolCenterError("不允许调试生产写操作，请克隆到测试环境", 403)
            return {}, None, False
        app_id = context.app_id or request.app_id
        if not app_id:
            raise ToolCenterError("正式调用必须指定已发布的 Agent 应用；无应用调试请使用 debug", 403)
        try:
            application = get_application_store().for_run(app_id)
        except ApplicationError as exc:
            raise ToolCenterError(str(exc), 409) from exc
        app_workspace = (application.get("permissions") or {}).get("workspace_id", "default")
        if app_workspace != context.workspace_id:
            raise ToolCenterError("应用不属于当前工作区", 403)
        authorize_application(actor, application, context.workspace_id, "run")
        refs = bound_tools(application)
        if tool["tool_id"] not in refs:
            raise ToolCenterError("该 Agent 未被授权使用此工具", 403)
        if tool["environment"] != context.environment:
            raise ToolCenterError("正式 Agent 不可跨环境调用工具", 403)
        needs_confirmation = bool(refs[tool["tool_id"]].get("requires_confirmation"))
        workflow_id = context.workflow_id or request.workflow_id or application.get("workflow_id")
        if workflow_id:
            bound = [item["workflow_id"] for item in application.get("workflow_bindings", []) if item.get("enabled", True)]
            if workflow_id not in bound and workflow_id != application.get("workflow_id"):
                raise ToolCenterError("工作流不属于此应用", 403)
            try:
                workflow = get_workflow_store().for_run(workflow_id, application.get("workflow_version"))
            except WorkflowError as exc:
                raise ToolCenterError(str(exc), 409) from exc
            for node in workflow.get("nodes", []):
                ref = node.get("params", {}).get("tool_id") or node.get("params", {}).get("tool")
                if ref and canonical_tool(ref) == tool["tool_id"]:
                    if not node.get("enabled", True):
                        raise ToolCenterError("工作流中该工具节点已停用", 409)
                    needs_confirmation |= bool(node.get("human_confirmation"))
            return application, workflow, needs_confirmation
        return application, None, needs_confirmation

    def invoke(self, context: ExecutionContext, tool_id: str, request: InvokeRequest):
        workspace = context.workspace_id
        if workspace not in context.principal.workspaces:
            raise ToolCenterError("没有此工作区的权限", 403)
        tool = self.store.runtime(workspace, "tools", canonical_tool(tool_id), debug=request.debug)
        application, workflow, needs_confirmation = self._authorization(context, tool, request)
        parameters = validate_parameters(tool["input_schema"], request.parameters)
        connection = None
        credential_version = None
        if tool.get("connection_id"):
            # Debugging a tool never implicitly changes the connection version.
            connection = self.store.runtime(workspace, "connections", tool["connection_id"])
            self.validate_connection(workspace, connection)
            if connection["type"] != "neo4j":
                self.headers(workspace, connection)
            if connection.get("credential_id"):
                credential_version = next(item["version"] for item in self.store.credentials(workspace) if item["credential_id"] == connection["credential_id"])
        approval = tool["risk"] in {"approval", "device_control"}
        confirm = tool["risk"] == "confirm" or needs_confirmation or context.routing_confirmation
        status = "awaiting_approval" if approval else "awaiting_confirmation" if confirm else "running"
        expires = time.time() + max(1, self.store.settings.tool_center_confirmation_minutes) * 60
        payload = {"tool": tool, "connection": connection, "parameters": parameters,
                   "requester_id": context.principal.user_id, "requester_roles": list(context.principal.roles),
                   "requester_auth_ref": context.principal.auth_ref,
                   "requester_permissions": context.principal.permissions,
                   "application_version": application.get("published_version"), "credential_version": credential_version,
                   "workflow_version": workflow.get("version") if workflow else None}
        data = {"app_id": context.app_id or request.app_id, "workflow_id": workflow.get("workflow_id") if workflow else request.workflow_id,
                "workflow_version": workflow.get("version") if workflow else None,
                "trace_id": context.trace_id or request.trace_id or str(uuid4()), "debug": request.debug,
                "environment": tool["environment"], "connection_id": tool.get("connection_id"),
                "connection_version": connection.get("version") if connection else None,
                "requires_confirmation": confirm, "requires_approval": approval, "expires_at": expires}
        request_hash = fingerprint({"tool_id": tool["tool_id"], "parameters": parameters, "app_id": data["app_id"],
                                    "workflow_id": data["workflow_id"], "debug": request.debug})
        call, created = self.store.reserve_call(workspace, context.principal.user_id, request.idempotency_key, request_hash, tool, payload, data, status)
        if not created:
            return self._expire(workspace, call)
        if status == "running":
            return self._execute(context, call, payload)
        self._emit(context, call)
        return call

    def _expire(self, workspace, call):
        if call["status"] in {"awaiting_approval", "awaiting_confirmation"} and call["expires_at"] <= time.time():
            return self.store.update_call(workspace, call["call_id"], {"status": "expired"},
                                          expected={"awaiting_approval", "awaiting_confirmation"})[0]
        return call

    def visible_calls(self, context, *, status=None, limit=100):
        actor = None if context.principal.has("admin", "editor", "approver") else context.principal.user_id
        calls = self.store.calls(context.workspace_id, actor=actor, limit=limit)
        calls = [self._expire(context.workspace_id, call) for call in calls]
        return [call for call in calls if status is None or call["status"] == status]

    def decide(self, context: ExecutionContext, cid, decision, note=""):
        workspace, actor = context.workspace_id, refresh_principal(context.principal)
        if workspace not in actor.workspaces:
            raise ToolCenterError("没有此工作区的审批权限", 403)
        call, payload = self.store.call(workspace, cid)
        call = self._expire(workspace, call)
        approval = call["requires_approval"]
        if approval and not (decision == "reject" and actor.user_id == call["actor_id"]):
            if not actor.has("admin", "approver"):
                raise ToolCenterError("该动作需要审批者权限", 403)
            if actor.user_id == call["actor_id"]:
                raise ToolCenterError("高风险动作必须由不同人员审批，申请人不能自行批准", 403)
        elif actor.user_id != call["actor_id"]:
            raise ToolCenterError("普通确认只能由申请人本人完成", 403)
        if call["status"] not in {"awaiting_approval", "awaiting_confirmation"}:
            return call
        expected = {call["status"]}
        if decision == "reject":
            return self.store.update_call(workspace, cid, {"status": "rejected", "decision_by": actor.user_id, "decision_note": note},
                                          expected=expected, actor=actor.user_id)[0]
        # Recheck the applicant's live permissions, definitions and environment.
        requester = refresh_principal(Principal(payload.get("requester_id", call["actor_id"]),
            tuple(payload.get("requester_roles") or []), (workspace,), payload.get("requester_auth_ref"),
            tuple(payload["requester_permissions"]) if payload.get("requester_permissions") is not None else None))
        # Role snapshots are written before the action can be approved.
        if not requester.roles:
            raise ToolCenterError("动作缺少可信申请人身份", 409)
        current_context = ExecutionContext(requester, workspace, call["app_id"], call["workflow_id"],
                                           call["trace_id"], call["environment"], on_event=context.on_event)
        current_tool = self.store.runtime(workspace, "tools", call["tool_id"], debug=call["debug"])
        if current_tool["version"] != call["tool_version"]:
            raise ToolCenterError("工具版本已变化，请重新申请", 409)
        application, workflow, _ = self._authorization(current_context, current_tool, InvokeRequest(
            parameters=payload["parameters"], app_id=call["app_id"], workflow_id=call["workflow_id"],
            idempotency_key=call["idempotency_key"], debug=call["debug"]))
        if application.get("published_version") != payload["application_version"] or (workflow.get("version") if workflow else None) != payload["workflow_version"]:
            raise ToolCenterError("应用/工作流授权版本已变化，请重新申请", 409)
        if call["connection_id"]:
            connection = self.store.runtime(workspace, "connections", call["connection_id"])
            if connection["version"] != call["connection_version"]:
                raise ToolCenterError("数据连接版本已变化，请重新申请", 409)
            if connection.get("credential_id"):
                version = next(item["version"] for item in self.store.credentials(workspace) if item["credential_id"] == connection["credential_id"])
                if version != payload["credential_version"]:
                    raise ToolCenterError("凭据已轮换，请重新申请", 409)
        claimed, changed = self.store.update_call(workspace, cid, {
            "status": "running", "decision_by": actor.user_id, "decision_note": note,
        }, expected=expected, actor=actor.user_id)
        if not changed:
            return claimed
        return self._execute(current_context, claimed, payload)

    def _execute(self, context, call, payload):
        started = time.monotonic()
        self._emit(context, call)
        tool, connection, parameters = payload["tool"], payload["connection"], payload["parameters"]
        dispatched = False
        try:
            if tool["kind"] == "builtin":
                result = {"status": "draft", **parameters, "message": "本地草稿已生成，尚未提交企业业务系统"}
            elif tool["kind"] == "http":
                self.validate_connection(context.workspace_id, connection)
                headers = self.headers(context.workspace_id, connection)
                headers["Idempotency-Key"] = call["idempotency_key"]
                headers["X-Trace-Id"] = call["trace_id"]
                fields = re.findall(r"\{([^{}]+)\}", tool["path"])
                path = tool["path"]
                for field in fields:
                    if field not in parameters or not isinstance(parameters[field], (str, int)):
                        raise ToolCenterError("路径变量缺失或类型无效", 422)
                    path = path.replace("{" + field + "}", quote(str(parameters[field]), safe=""))
                args = {key: value for key, value in parameters.items() if key not in fields or tool["config"].get("include_path_in_body", tool["method"] not in {"GET", "HEAD"})}
                query_fields = tool["config"].get("query_parameters")
                query = args if tool["method"] in {"GET", "HEAD"} else {key: args[key] for key in (query_fields or []) if key in args}
                body_fields = tool["config"].get("body_parameters")
                body = {key: value for key, value in args.items() if key not in (query_fields or [])}
                if body_fields is not None:
                    body = {key: value for key, value in body.items() if key in body_fields}
                timeout = min(tool["timeout_seconds"], connection["timeout_seconds"])
                dispatched = True
                result, _ = self.request(tool["method"], connection["endpoint"].rstrip("/") + "/" + path.lstrip("/"),
                                         timeout=timeout, headers=headers,
                                         params=query,
                                         payload=body if tool["method"] not in {"GET", "HEAD"} else None)
            else:
                dispatched = True
                result = self.mcp_rpc(context.workspace_id, connection, "tools/call",
                                      {"name": tool["config"].get("remote_name", tool["tool_id"]), "arguments": parameters})[0]
                if result.get("isError"):
                    raise ToolCenterError("MCP 工具返回执行错误", 502)
                if result.get("inputRequests"):
                    raise ToolCenterError("MCP 工具需要交互输入，当前客户端不会自动批准或重试", 409)
                if tool["output_schema"] and "structuredContent" in result:
                    result = result["structuredContent"]
            if list(Draft202012Validator(tool["output_schema"]).iter_errors(result)):
                raise ToolCenterError("上游输出不满足配置的 output_schema", 502)
            result = self.scrub(context.workspace_id, result)
            updates = {"status": "success", "result": result}
        except Exception as exc:
            uncertain = dispatched and tool["risk"] in WRITES
            if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code < 500:
                uncertain = False
            updates = {"status": "unknown" if uncertain else "error",
                       "error": self.scrub(context.workspace_id, {"message": str(exc)})["message"],
                       "message": "写操作结果不确定，请通过业务系统核对；系统不会自动重试" if uncertain else "工具调用失败"}
        updates["duration_ms"] = round((time.monotonic() - started) * 1000)
        result, _ = self.store.update_call(context.workspace_id, call["call_id"], updates, expected={"running"}, actor=context.principal.user_id)
        self._emit(context, result)
        return result

    def _emit(self, context, call):
        if not context.on_event:
            return
        context.on_event({"type": "tool_call", **call})
        if call.get("workflow_id"):
            try:
                workflow = get_workflow_store().for_run(call["workflow_id"], call.get("workflow_version"))
                for node in workflow.get("nodes", []):
                    ref = node.get("params", {}).get("tool_id") or node.get("params", {}).get("tool")
                    if ref and canonical_tool(ref) == call["tool_id"]:
                        context.on_event({
                            "type": "workflow_node", "workflow_id": call["workflow_id"], "workflow_version": workflow["version"],
                            "workflow_run_id": call["trace_id"], "node_id": node["id"], "node_name": node["name"],
                            "status": "waiting_confirmation" if call["status"].startswith("awaiting") else call["status"],
                            "detail": f"真实工具调用 {call['call_id']}：{call['status']}",
                        })
            except WorkflowError:
                pass

    def managed_tools(self, context):
        if not context.app_id:
            return []
        try:
            application = get_application_store().for_run(context.app_id)
        except ApplicationError:
            return []
        refs = bound_tools(application)
        result = []
        for definition in self.store.list(context.workspace_id, "tools"):
            tid = definition["tool_id"]
            if tid not in refs or not definition["enabled"] or not definition["published_version"]:
                continue
            try:
                tool = self.store.runtime(context.workspace_id, "tools", tid)
            except ToolCenterError:
                continue
            if tool["environment"] != context.environment or (not context.principal.has("admin") and not set(context.principal.roles) & set(tool["allowed_roles"])):
                continue
            try:
                self._authorization(context, tool, InvokeRequest(app_id=context.app_id, idempotency_key="capability-check"))
            except ToolCenterError:
                continue
            def callback(target):
                def call_tool(**kwargs):
                    request = InvokeRequest(parameters=kwargs, app_id=context.app_id, workflow_id=context.workflow_id,
                        trace_id=context.trace_id, idempotency_key=f"{context.trace_id or uuid4()}:{target}:{fingerprint(kwargs)[:24]}")
                    try:
                        return json.dumps(self.invoke(context, target, request), ensure_ascii=False, default=str)
                    except ToolCenterError as exc:
                        return json.dumps({"status": "denied", "error": str(exc)}, ensure_ascii=False)
                return call_tool
            result.append(StructuredTool(name=tid, description=tool["description"], args_schema=tool["input_schema"], func=callback(tid)))
        return result

    def mcp_rpc(self, workspace, connection, method, params):
        if connection["config"].get("transport", "streamable_http") != "streamable_http":
            raise ToolCenterError("该连接是旧搜索代理，请选择 streamable_http MCP 服务", 409)
        headers = self.headers(workspace, connection)
        headers["Accept"] = "application/json, text/event-stream"
        protocol = connection["config"].get("protocol_version", "2025-11-25")
        if protocol not in {"2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"}:
            raise ToolCenterError("不支持的 MCP 协议版本")
        headers["MCP-Protocol-Version"] = protocol
        def rpc(name, arguments, *, notification=False):
            rid = str(uuid4())
            body = {"jsonrpc": "2.0", "method": name, "params": arguments}
            if not notification:
                body["id"] = rid
            headers["Mcp-Method"] = name
            if protocol == "2026-07-28":
                body["params"] = {**arguments, "_meta": {"io.modelcontextprotocol/protocolVersion": protocol}}
            if "name" in arguments:
                remote = arguments["name"]
                try:
                    remote.encode("ascii")
                    headers["Mcp-Name"] = remote
                except UnicodeEncodeError:
                    raise ToolCenterError("当前客户端只支持 ASCII MCP 工具名")
            else:
                headers.pop("Mcp-Name", None)
            response, metadata = self.request("POST", connection["endpoint"], timeout=connection["timeout_seconds"],
                                               headers=headers, payload=body, rpc_id=rid, notification=notification)
            if response.get("error"):
                raise ToolCenterError("MCP JSON-RPC 调用失败：" + str(response["error"].get("message", "")), 502)
            return response.get("result", {}), metadata
        if protocol != "2026-07-28":
            initialized, metadata = rpc("initialize", {"protocolVersion": protocol, "capabilities": {},
                "clientInfo": {"name": "deepagent-tool-center", "version": "1.0"}})
            negotiated = initialized.get("protocolVersion")
            if negotiated not in {"2025-03-26", "2025-06-18", "2025-11-25"}:
                raise ToolCenterError("MCP 服务协商了不支持的协议版本", 502)
            headers["MCP-Protocol-Version"] = negotiated
            session_id = metadata.get("Mcp-Session-Id") or metadata.get("MCP-Session-Id")
            if session_id:
                headers["Mcp-Session-Id"] = session_id
            rpc("notifications/initialized", {}, notification=True)
        return rpc(method, params)

    def health(self, workspace, cid):
        start = time.monotonic()
        try:
            connection = self.store.runtime(workspace, "connections", cid, debug=True)
            self.validate_connection(workspace, connection)
            if connection["type"] == "neo4j":
                from neo4j import GraphDatabase
                secret = self.store.credential_secret(workspace, connection["credential_id"])
                self.check_endpoint(connection["endpoint"])
                with GraphDatabase.driver(connection["endpoint"], auth=(secret.get("username", "neo4j"), secret.get("password", "")),
                                          connection_timeout=connection["timeout_seconds"]) as driver:
                    driver.verify_connectivity()
                    with driver.session() as session:
                        session.run("RETURN 1 AS ok").single()
            elif connection["type"] == "qa":
                records = json.loads(self.qa_path(connection["config"].get("path", "")).read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    raise ToolCenterError("QA 文件必须是数组")
            elif connection["type"] == "mcp" and connection["config"].get("transport") != "search_proxy":
                self.mcp_rpc(workspace, connection, "tools/list", {})
            else:
                result, _ = self.request("GET", connection["endpoint"].rstrip("/") + "/" + connection["health_path"].lstrip("/"),
                                          timeout=connection["timeout_seconds"], headers=self.headers(workspace, connection))
                if connection["type"] == "ragflow" and result.get("code", 0) != 0:
                    raise ToolCenterError("RAGFlow 返回失败状态", 502)
            result = {"status": "healthy", "available": True}
        except Exception as exc:
            result = {"status": "error", "available": False, "error": self.scrub(workspace, {"message": str(exc)})["message"]}
        result.update({"connection_id": cid, "connection_version": locals().get("connection", {}).get("version"),
                       "checked_at": now(), "duration_ms": round((time.monotonic() - start) * 1000)})
        self.store.audit(workspace, "health-probe", "connection_health", cid, result)
        return result

    def connection_status(self, workspace):
        probes = {event["target"]: event["detail"] for event in reversed(self.store.audits(workspace, 500))
                  if event["action"] == "connection_health"}
        return [{**item, "health": probes.get(item["connection_id"], {"status": "unknown", "available": None})
                 if probes.get(item["connection_id"], {}).get("connection_version") == item["version"] else {"status": "unknown", "available": None},
                 "configured": bool(item.get("endpoint") or item.get("config", {}).get("path"))}
                for item in self.store.list(workspace, "connections")]

    def ragflow_client(self, workspace, connection):
        """Reuse the governed transport for retrieval and catalog calls alike."""
        from types import SimpleNamespace
        from app.tools.ragflow import RAGFlowClient
        service = self
        class Requester:
            def get(self, url, **kwargs):
                return self._call("GET", url, kwargs)
            def post(self, url, **kwargs):
                return self._call("POST", url, kwargs)
            def _call(self, method, url, kwargs):
                data, headers = service.request(method, url,
                    timeout=min(connection["timeout_seconds"], kwargs.get("timeout", 8)),
                    headers=service.headers(workspace, connection), params=kwargs.get("params"), payload=kwargs.get("json"))
                return SimpleNamespace(status_code=200, json=lambda: data, raise_for_status=lambda: None, headers=headers)
        secret = self.store.credential_secret(workspace, connection.get("credential_id"))
        dataset_ids = self._dataset_ids(connection)
        settings = self.store.settings.model_copy(update={
            "ragflow_base_url": connection["endpoint"], "ragflow_api_key": secret.get("token") or secret.get("api_key"),
            "ragflow_dataset_id": None, "ragflow_dataset_ids": ",".join(dataset_ids),
            "ragflow_auto_discover_datasets": bool(connection["config"].get("auto_discover", False)),
        })
        return RAGFlowClient(settings=settings, requester=Requester())

    def resolve_application_knowledge_scope(
        self,
        application: dict,
        workspace: str,
        *,
        require_document_binding: bool = False,
    ) -> dict:
        """Resolve the published RAGFlow datasets allowed by an application.

        Knowledge Center owns dataset ingestion and connection publishing.  An
        application only pins a concrete dataset allow-list in its published
        snapshot.  ``inherit`` deliberately preserves the pre-binding
        behavior for legacy applications; ``explicit`` is the isolated mode.
        """

        mode = str(application.get("knowledge_binding_mode") or "inherit").strip().lower()
        if mode != "explicit":
            return {
                "mode": "inherit",
                "dataset_ids_by_connection": None,
                "published_revisions": {},
                "bindings": [],
            }

        app_environment = str((application.get("runtime") or {}).get("environment") or "production")
        if app_environment not in {"test", "production"}:
            raise ToolCenterError("应用运行环境配置无效", 409)

        source_enabled = any(
            item.get("source_type") == "document" and item.get("enabled", True)
            for item in application.get("data_sources") or []
            if isinstance(item, dict)
        )
        active_bindings = [
            item for item in application.get("knowledge_bindings") or []
            if isinstance(item, dict) and item.get("enabled", True)
        ]
        if require_document_binding and source_enabled and not active_bindings:
            raise ToolCenterError("应用已启用企业文档，但没有绑定任何具体 RAGFlow 知识库", 409)

        connections = {
            str(item.get("connection_id")): item
            for item in self.store.list(workspace, "connections")
            if item.get("connection_id")
        }
        dataset_ids: dict[str, set[str]] = {}
        revisions: dict[str, int] = {}
        seen: set[tuple[str, str]] = set()
        for binding in active_bindings:
            connection_id = str(binding.get("connection_id") or "").strip()
            dataset_id = str(binding.get("dataset_id") or "").strip()
            if not connection_id or not dataset_id:
                raise ToolCenterError("启用的知识库绑定缺少 connection_id 或 dataset_id", 409)
            identity = (connection_id, dataset_id)
            if identity in seen:
                raise ToolCenterError(f"应用重复绑定知识库：{connection_id}/{dataset_id}", 409)
            seen.add(identity)

            definition = connections.get(connection_id)
            if not definition:
                raise ToolCenterError(f"应用绑定的 RAGFlow 连接不存在：{connection_id}", 409)
            if definition.get("type") != "ragflow":
                raise ToolCenterError(f"应用绑定的连接不是 RAGFlow：{connection_id}", 409)
            if str(definition.get("environment") or "production") != app_environment:
                raise ToolCenterError(
                    f"应用环境为 {app_environment}，不能绑定 {connection_id} 的 {definition.get('environment') or 'production'} 连接",
                    409,
                )

            try:
                runtime = self.store.runtime(workspace, "connections", connection_id)
            except ToolCenterError as exc:
                raise ToolCenterError(
                    f"应用绑定的 RAGFlow 连接尚未发布或已停用：{connection_id}；{exc}",
                    409,
                ) from exc
            current_revision = int(runtime.get("version") or 0)
            expected_revision = binding.get("published_revision")
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise ToolCenterError(
                    f"知识库绑定引用的连接发布版本已变化：{connection_id}，应用固定 v{expected_revision}，当前为 v{current_revision}；请重新绑定并发布应用",
                    409,
                )
            published_ids = set(self._dataset_ids(runtime))
            if dataset_id not in published_ids:
                raise ToolCenterError(
                    f"RAGFlow 知识库未包含在连接 {connection_id} 的已发布 allow-list 中：{dataset_id}",
                    409,
                )
            dataset_ids.setdefault(connection_id, set()).add(dataset_id)
            revisions[connection_id] = current_revision

        return {
            "mode": "explicit",
            "dataset_ids_by_connection": {
                key: tuple(sorted(values)) for key, values in dataset_ids.items()
            },
            "published_revisions": revisions,
            "bindings": active_bindings,
        }

    def list_datasets(self, context):
        datasets, warnings = [], []
        for connector in managed_connectors(context):
            connection = connector.connection
            if connection["type"] != "ragflow":
                continue
            try:
                client = self.ragflow_client(context.workspace_id, connection)
                items = client.list_datasets() if client.auto_discover else client.list_configured_datasets()
                datasets.extend({**item, "connection_id": connection["connection_id"], "source_name": connection["name"]} for item in items)
                self.store.audit(context.workspace_id, context.principal.user_id, "dataset_catalog", connection["connection_id"], {"status": "success", "count": len(items)})
            except Exception as exc:
                warning = self.scrub(context.workspace_id, {"message": str(exc)})["message"]
                warnings.append(connection["name"] + "：" + warning)
        return {"datasets": datasets, "total": len(datasets), "warnings": warnings, "configured": bool(datasets) and not warnings}

    @staticmethod
    def _dataset_ids(connection: dict) -> list[str]:
        value = (connection.get("config") or {}).get("dataset_ids") or []
        if isinstance(value, str):
            value = value.replace("，", ",").split(",")
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    def knowledge_dataset_management(self, context):
        """Return editable RAGFlow bindings and the datasets visible to each connection.

        The knowledge page uses this method instead of reading .env directly.  The
        binding is workspace-scoped and still follows the tool-center draft/publish
        lifecycle, so saving an ID cannot silently change a published Agent.
        """
        connections, warnings = [], []
        definitions = [
            item for item in self.store.list(context.workspace_id, "connections")
            if item.get("type") == "ragflow"
        ]
        for definition in definitions:
            draft_ids = self._dataset_ids(definition)
            published_ids: list[str] = []
            if definition.get("published_version"):
                try:
                    runtime = self.store.runtime(
                        context.workspace_id,
                        "connections",
                        definition["connection_id"],
                    )
                    published_ids = self._dataset_ids(runtime)
                except ToolCenterError:
                    # A disabled or otherwise invalid connection is still useful
                    # to show in the editor; its effective IDs are simply empty.
                    published_ids = []

            remote: list[dict] = []
            if definition.get("endpoint") and definition.get("enabled"):
                try:
                    remote = self.ragflow_client(
                        context.workspace_id, definition
                    ).list_datasets()
                except Exception as exc:
                    warning = self.scrub(
                        context.workspace_id, {"message": str(exc)}
                    )["message"]
                    warnings.append(f"{definition['name']}：{warning}")

            remote_ids = {str(item.get("id") or "") for item in remote}
            datasets = [
                {
                    **item,
                    "connected": item.get("id") in draft_ids,
                    "effective": item.get("id") in published_ids,
                    "missing": False,
                }
                for item in remote
                if item.get("id")
            ]
            # Keep manually entered IDs visible even when RAGFlow's catalog call
            # failed or the ID was removed there, so the operator can correct it.
            for dataset_id in draft_ids:
                if dataset_id not in remote_ids:
                    datasets.append({
                        "id": dataset_id,
                        "name": dataset_id,
                        "connected": True,
                        "effective": dataset_id in published_ids,
                        "missing": True,
                    })
            connections.append({
                "connection_id": definition["connection_id"],
                "name": definition.get("name") or definition["connection_id"],
                "description": definition.get("description") or "",
                "endpoint": definition.get("endpoint") or "",
                "enabled": bool(definition.get("enabled")),
                "environment": definition.get("environment") or "production",
                "version": definition.get("version"),
                "published_version": definition.get("published_version"),
                "status": definition.get("status"),
                "dataset_ids": draft_ids,
                "published_dataset_ids": published_ids,
                "pending_publish": draft_ids != published_ids,
                "datasets": datasets,
            })
        return {
            "connections": connections,
            "warnings": warnings,
            "configured": bool(connections),
        }

    def update_ragflow_dataset_ids(
        self,
        workspace: str,
        connection_id: str,
        dataset_ids: list[str],
        actor: str,
        base_version: int | None = None,
    ) -> dict:
        """Save dataset IDs as a versioned RAGFlow connection draft."""
        current = self.store.get(workspace, "connections", connection_id)
        if current.get("type") != "ragflow":
            raise ToolCenterError("指定连接不是 RAGFlow 连接", 409)

        # Only copy fields accepted by ConnectionInput; public store metadata such
        # as published_version/status must not leak into the persisted definition.
        definition = {
            key: current.get(key)
            for key in ConnectionInput.model_fields
            if key in current
        }
        definition["config"] = {
            **(current.get("config") or {}),
            "dataset_ids": list(dict.fromkeys(dataset_ids)),
            # The binding editor represents an explicit allow-list.  This avoids
            # the surprising case where an entered ID is ignored by auto-discovery.
            "auto_discover": False,
        }
        definition["base_version"] = base_version or current["version"]
        validated = ConnectionInput.model_validate(definition).model_dump()
        self.validate_connection(workspace, validated)
        saved = self.store.save(
            workspace,
            "connections",
            validated,
            actor,
            note="维护 RAGFlow 知识库 ID",
        )
        self.store.audit(
            workspace,
            actor,
            "dataset_binding",
            connection_id,
            {"dataset_ids": list(dict.fromkeys(dataset_ids)), "version": saved["version"]},
        )
        return saved

    def search_connection(self, workspace, connection, request: SearchRequest):
        self.validate_connection(workspace, connection)
        ctype = connection["type"]
        source_type = {"ragflow": "document", "neo4j": "graph", "business_api": "api", "qa": "qa", "sql": "sql", "mcp": "mcp"}[ctype]
        secret = self.store.credential_secret(workspace, connection.get("credential_id"))
        if ctype == "ragflow":
            plan = request.query_plan
            queries = (
                plan.document_queries
                if plan and plan.document_queries
                else [request.query]
            )
            client = self.ragflow_client(workspace, connection)
            chunks: list[dict] = []
            query_total = len(queries)
            for query_index, retrieval_query in enumerate(queries, start=1):
                retrieval_query = str(retrieval_query or "").strip()
                logger.info(
                    "Managed RAGFlow search start: connection_id=%s query_index=%d/%d "
                    "query_chars=%d query_preview=%s top_k=%d",
                    connection.get("connection_id"),
                    query_index,
                    query_total,
                    len(retrieval_query),
                    " ".join(retrieval_query.split())[:160],
                    request.top_k,
                )
                try:
                    result = client.search(retrieval_query, top_k=request.top_k)
                except Exception as exc:
                    logger.warning(
                        "Managed RAGFlow search failed: connection_id=%s "
                        "query_index=%d/%d query_chars=%d query_preview=%s error=%s",
                        connection.get("connection_id"),
                        query_index,
                        query_total,
                        len(retrieval_query),
                        " ".join(retrieval_query.split())[:160],
                        str(exc)[:1200],
                    )
                    raise RuntimeError(
                        f"RAGFlow 检索失败（查询 {query_index}/{query_total}）：{exc}"
                    ) from exc
                result_chunks = result.get("chunks", [])
                logger.info(
                    "Managed RAGFlow search success: connection_id=%s "
                    "query_index=%d/%d returned_chunks=%d total=%s",
                    connection.get("connection_id"),
                    query_index,
                    query_total,
                    len(result_chunks),
                    result.get("total"),
                )
                for item in result_chunks:
                    chunks.append({**item, "retrieval_query": retrieval_query})
            return [SearchHit(source_type="document", source_name=connection["name"], title=item.get("document_name") or "文档",
                content=item.get("content") or "", score=float(item.get("score") or 0), authority=0.8, freshness=0.8, entity_match=0.7,
                evidence_id=f"{connection['connection_id']}:{item.get('evidence_id') or index}",
                document_id=item.get("document_id"), dataset_id=item.get("dataset_id"), page=item.get("page"),
                metadata={
                    "connection_id": connection["connection_id"],
                    "retrieval_query": item.get("retrieval_query") or request.query,
                },
                # Managed source links must not be proxied through a different environment credential.
                url=f"/api/tool-center/connections/{connection['connection_id']}/documents/{item['dataset_id']}/{item['document_id']}"
                    if item.get("dataset_id") and item.get("document_id") else None)
                for index, item in enumerate(chunks)]
        if ctype == "neo4j":
            from app.graph.neo4j_store import Neo4jGraphStore
            settings = self.store.settings.model_copy(update={"graph_provider": "neo4j", "neo4j_uri": connection["endpoint"],
                                                               "neo4j_username": secret.get("username", "neo4j"), "neo4j_password": secret.get("password")})
            graph = Neo4jGraphStore(settings)
            filters = dict(request.filters)
            if workspace != "default":
                filters["tenant_id"] = workspace
            try:
                plan = request.query_plan
                queries = (
                    plan.graph_queries
                    if plan and plan.graph_queries
                    else [request.query]
                )
                attribute_keys = plan.attribute_keys if plan else []
                attribute_terms = expand_graph_attribute_terms(attribute_keys)
                relation_types = plan.relation_types if plan else []
                candidate_limit = min(
                    50,
                    max(request.top_k, request.top_k * 4)
                    if attribute_terms or relation_types
                    else request.top_k,
                )
                records: list[dict] = []
                for graph_query in queries:
                    records.extend(
                        graph.search_entity_neighbors(
                            graph_query,
                            limit=candidate_limit,
                            filters=filters,
                            attribute_terms=attribute_terms,
                            relation_types=relation_types,
                        )
                    )
            finally:
                if graph._driver:
                    graph._driver.close()
            ranked_records = rank_graph_records(
                records,
                attribute_terms=attribute_keys,
                relation_types=relation_types,
                limit=request.top_k,
            )
            hits: list[SearchHit] = []
            for item, relevance in ranked_records:
                left = item["left"]
                right = item["right"]
                relation = item["relation"]
                relation_id = f"{connection['connection_id']}:{graph_relation_id(left, right, relation)}"
                graph_evidence = normalize_graph_relation_evidence(
                    left,
                    right,
                    relation,
                    relation_id=relation_id,
                )
                primary = (graph_evidence.get("evidence") or [{}])[0]
                safe_item = self.scrub(workspace, item)
                hits.append(SearchHit(
                    source_type="graph",
                    source_name=connection["name"],
                    title=left.get("name") or left.get("id") or "关系",
                    content=f"{left.get('name', '')} -[{relation.get('type', '关联')}]-> {right.get('name', '')}",
                    score=float(relevance.get("score") or 0.85),
                    authority=0.9,
                    freshness=0.8,
                    entity_match=1,
                    evidence_id=relation_id,
                    document_id=primary.get("document_id"),
                    dataset_id=primary.get("dataset_id"),
                    page=int(primary["page"]) if str(primary.get("page") or "").isdigit() else None,
                    version=primary.get("version"),
                    url=primary.get("url"),
                    metadata={
                        **safe_item,
                        "connection_id": connection["connection_id"],
                        "graph_relation": graph_evidence,
                        "graph_relevance": relevance,
                    },
                ))
            return hits
        if ctype == "qa":
            from app.search.connectors import QAFileConnector
            connector = QAFileConnector()
            connector.path = str(self.qa_path(connection["config"].get("path", "")))
            hits = connector.search(request)
            for hit in hits:
                hit.source_name = connection["name"]
                hit.evidence_id = connection["connection_id"] + ":" + hit.evidence_id
            return hits
        if ctype == "sql":
            # The application may only narrow the published connection policy.
            # Production SQL access requires a logical query plan plus dataset
            # and field allow-lists; the gateway remains responsible for
            # parameterized SQL, tenant filtering and read-only enforcement.
            try:
                config = merge_sql_scope(connection.get("config") or {})
                application_scope = config.pop("application_scope", None)
                config = merge_sql_scope(config, application_scope)
            except StructuredQueryPolicyError as exc:
                raise ToolCenterError(str(exc), 409) from exc
            plans = list(request.query_plan.structured_queries) if request.query_plan else []
            production = str(connection.get("environment") or "production") == "production"
            enforce_allowlist = bool(config.get("enforce_allowlist", production))
            allow_natural_query = bool(config.get("allow_natural_query", not production))
            try:
                plans = validate_structured_queries(
                    plans,
                    config,
                    require_allowlist=enforce_allowlist,
                )
            except StructuredQueryPolicyError as exc:
                raise ToolCenterError(str(exc), 403) from exc
            if not plans and enforce_allowlist and not allow_natural_query:
                raise ToolCenterError(
                    "生产 SQL 连接要求结构化查询计划；请配置数据集/字段白名单或仅在测试环境开启兼容自然语言查询",
                    409,
                )
            try:
                max_rows = max(1, min(200, int(config.get("max_rows") or 100)))
            except (TypeError, ValueError) as exc:
                raise ToolCenterError("SQL 连接 max_rows 配置无效", 409) from exc
            data, _ = self.request(
                "POST",
                connection["endpoint"],
                timeout=min(connection["timeout_seconds"], max(1, request.timeout_ms // 1000)),
                headers=self.headers(workspace, connection),
                payload=sql_request_payload(request, plans, max_rows=max_rows),
            )
            return normalize_sql_hits(
                data,
                source_name=connection["name"],
                evidence_prefix=f"{connection['connection_id']}:sql",
                scrubber=lambda value: self.scrub(workspace, value),
            )
        headers = self.headers(workspace, connection)
        if ctype == "mcp" and connection["config"].get("transport") != "search_proxy":
            remote_name = connection["config"].get("search_tool")
            if not remote_name:
                raise ToolCenterError("MCP 连接未配置经过审核的只读 search_tool", 409)
            # Only an explicitly registered, published read-only MCP capability may power search.
            tools = [item for item in self.store.list(workspace, "tools") if item["kind"] == "mcp"
                     and item.get("connection_id") == connection["connection_id"] and item["published_version"] and item["enabled"]]
            matching = [self.store.runtime(workspace, "tools", item["tool_id"]) for item in tools]
            matching = [item for item in matching if item["risk"] == "read_only" and item["config"].get("remote_name") == remote_name]
            if not matching:
                raise ToolCenterError("MCP 搜索工具尚未注册并发布为只读工具", 403)
            response, _ = self.mcp_rpc(workspace, connection, "tools/call", {"name": remote_name, "arguments": {
                "query": request.query, "top_k": request.top_k, "filters": request.filters}})
            data = response.get("structuredContent") or {}
        else:
            endpoint = connection["endpoint"].rstrip("/") + "/search" if ctype == "business_api" else connection["endpoint"]
            data, _ = self.request("POST", endpoint, timeout=min(connection["timeout_seconds"], max(1, request.timeout_ms // 1000)),
                                    headers=headers, payload={"query": request.query, "top_k": request.top_k,
                                                              "limit": request.top_k, "filters": request.filters})
        items = data.get("items") or data.get("hits") or [] if isinstance(data, dict) else data
        return [SearchHit(source_type=source_type, source_name=connection["name"], title=item.get("title") or item.get("name") or "业务记录",
            content=item.get("content") or item.get("summary") or json.dumps(self.scrub(workspace, item), ensure_ascii=False),
            score=float(item.get("score", 0.8)), authority=0.9, freshness=0.9, entity_match=0.8,
            evidence_id=f"{connection['connection_id']}:{item.get('evidence_id') or index}",
            metadata={"connection_id": connection["connection_id"], **self.scrub(workspace, item)})
            for index, item in enumerate(items) if isinstance(item, dict)]


class ManagedSearchConnector:
    TYPES = {"ragflow": "document", "neo4j": "graph", "business_api": "api", "qa": "qa", "sql": "sql", "mcp": "mcp"}

    def __init__(self, service, workspace, connection):
        self.service, self.workspace, self.connection = service, workspace, connection
        self.source_type = self.TYPES[connection["type"]]
        self.name = connection["name"]

    @property
    def available(self):
        return bool(self.connection.get("endpoint") or self.connection.get("config", {}).get("path"))

    def search(self, request):
        start = time.monotonic()
        try:
            hits = self.service.search_connection(self.workspace, self.connection, request)
            self.service.store.audit(self.workspace, request.user_id or "search", "source_search", self.connection["connection_id"],
                                     {"status": "success", "count": len(hits), "duration_ms": round((time.monotonic() - start) * 1000)})
            return hits
        except Exception as exc:
            message = self.service.scrub(self.workspace, {"error": str(exc)})["error"]
            self.service.store.audit(self.workspace, request.user_id or "search", "source_search", self.connection["connection_id"],
                                     {"status": "error", "error": message})
            raise ToolCenterError(message, 502) from exc


def managed_connectors(context: ExecutionContext):
    service = ToolCenterService()
    definitions = service.store.list(context.workspace_id, "connections")
    selected = []
    for definition in definitions:
        if not definition["enabled"] or not definition["published_version"]:
            continue
        if context.connection_ids is not None and definition["connection_id"] not in context.connection_ids:
            continue
        try:
            connection = service.store.runtime(context.workspace_id, "connections", definition["connection_id"])
        except ToolCenterError:
            continue
        source_type = ManagedSearchConnector.TYPES.get(str(connection.get("type") or ""))
        if source_type == "document" and context.knowledge_binding_mode == "explicit":
            allowed_ids = (context.knowledge_dataset_ids or {}).get(definition["connection_id"])
            # An explicit application scope must never fall back to another
            # RAGFlow connection or to that connection's full allow-list.
            if not allowed_ids:
                continue
            connection = {
                **connection,
                "config": {
                    **(connection.get("config") or {}),
                    "dataset_ids": list(allowed_ids),
                    "auto_discover": False,
                },
            }
        if source_type == "sql":
            application_scope = (context.source_policies or {}).get(definition["connection_id"])
            if isinstance(application_scope, dict):
                connection = {
                    **connection,
                    "config": {
                        **(connection.get("config") or {}),
                        "application_scope": application_scope,
                    },
                }
        if source_type and connection["environment"] == context.environment \
                and (context.source_types is None or source_type in context.source_types):
            selected.append(ManagedSearchConnector(service, context.workspace_id, connection))
    return selected
