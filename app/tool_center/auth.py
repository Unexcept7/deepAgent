from __future__ import annotations

import hmac
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request, Response

from app.application_store import ApplicationError, get_application_store
from app.config import get_settings
from app.tool_center.store import ToolCenterError, get_tool_center_store


COOKIE = "deepagent_tool_session"


@dataclass(frozen=True)
class Principal:
    user_id: str
    roles: tuple[str, ...]
    workspaces: tuple[str, ...] = ("default",)
    auth_ref: str | None = None
    permissions: tuple[str, ...] | None = None

    def has(self, *roles: str) -> bool:
        return bool(set(self.roles) & set(roles))

    def public(self) -> dict:
        return {"user_id": self.user_id, "roles": list(self.roles), "workspaces": list(self.workspaces)}

    def can(self, permission: str) -> bool:
        if self.has("admin"):
            return True
        if self.permissions is not None:
            return permission in self.permissions or "*" in self.permissions
        baseline = {"tool:invoke", "knowledge:read", "device:read", "alarm:read", "work_order:draft", "work_order:create"}
        if self.has("approver"):
            baseline.add("device:control")
        return self.has("operator", "approver", "editor") and permission in baseline


def check_origin(request: Request):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() != request.url.netloc.lower():
        raise ToolCenterError("拒绝跨站请求", 403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise ToolCenterError("拒绝跨站请求", 403)


def configured_principal(token: str) -> Principal | None:
    if not token:
        return None
    try:
        configs = json.loads(get_settings().tool_center_auth_tokens or "[]")
        for item in configs:
            if hmac.compare_digest(str(item.get("token") or ""), token):
                roles = tuple(item.get("roles") or [])
                if not roles or set(roles) - {"admin", "editor", "operator", "approver", "viewer"}:
                    continue
                return Principal(str(item.get("user_id") or "api-user"), roles, tuple(item.get("workspaces") or ["default"]),
                    hashlib.sha256(token.encode()).hexdigest(), tuple(item["permissions"]) if "permissions" in item else None)
    except (ValueError, TypeError, AttributeError):
        raise ToolCenterError("服务器工具中心身份配置无效", 500)
    return None


def refresh_principal(principal: Principal) -> Principal:
    if not principal.auth_ref:
        # Trusted in-process integrations may inject principals; HTTP callers
        # never control this branch, roles or identity fields.
        return principal
    if principal.auth_ref == "local":
        if not get_settings().tool_center_local_admin:
            raise ToolCenterError("本地管理员身份已撤销", 401)
        return Principal("local-admin", ("admin",), ("default",), "local")
    try:
        configs = json.loads(get_settings().tool_center_auth_tokens or "[]")
        for item in configs:
            token = str(item.get("token") or "")
            if hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), principal.auth_ref):
                current = configured_principal(token)
                if current:
                    return current
    except (ValueError, TypeError, AttributeError):
        pass
    raise ToolCenterError("申请人身份令牌已撤销，请重新登录/申请", 401)


def optional_principal(request: Request | None) -> Principal | None:
    if request is None:
        return None
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        principal = configured_principal(authorization[7:])
        if principal is None:
            raise ToolCenterError("令牌无效或已撤销", 401)
        return principal
    session = get_tool_center_store().session(request.cookies.get(COOKIE))
    if not session:
        return None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        if not hmac.compare_digest(request.headers.get("x-tool-center-csrf", ""), session.get("csrf_token", "")):
            raise ToolCenterError("缺少或无效的 CSRF 令牌，请刷新页面", 403)
    return refresh_principal(Principal(session["user_id"], tuple(session["roles"]), tuple(session["workspaces"]), session.get("auth_ref")))


def require_principal(request: Request) -> Principal:
    principal = optional_principal(request)
    if principal is None:
        raise ToolCenterError("请先登录工具中心", 401)
    return principal


def workspace_for(request: Request, principal: Principal) -> str:
    workspace = request.headers.get("x-workspace-id") or request.query_params.get("workspace_id") or principal.workspaces[0]
    if workspace not in principal.workspaces:
        raise ToolCenterError("没有此工作区的权限", 403)
    return workspace


def authorize_application(principal: Principal, application: dict, workspace: str, action="run"):
    permissions = application.get("permissions") or {}
    if permissions.get("workspace_id", "default") != workspace or workspace not in principal.workspaces:
        raise ToolCenterError("应用不属于当前工作区", 403)
    if principal.has("admin"):
        return
    aliases = {"editor": "员工", "operator": "员工", "approver": "员工", "viewer": "查看者"}
    roles = set(principal.roles) | {aliases.get(role, role) for role in principal.roles}
    required = permissions.get(action + "_roles")
    if required is not None and not roles & set(required):
        raise ToolCenterError("用户角色不在该 Agent 的" + action + "授权范围", 403)


def require_manager(request: Request) -> Principal:
    principal = require_principal(request)
    workspace_for(request, principal)
    if not principal.has("admin", "editor"):
        raise ToolCenterError("只有管理员/编辑者可以修改定义", 403)
    if request.path_params.get("app_id") and request.url.path.startswith("/api/apps"):
        try:
            application = get_application_store().get(request.path_params["app_id"])
        except ApplicationError as exc:
            raise ToolCenterError(str(exc), 404) from exc
        authorize_application(principal, application, workspace_for(request, principal), "edit")
    return principal


def require_admin(request: Request) -> Principal:
    principal = require_principal(request)
    workspace_for(request, principal)
    if not principal.has("admin"):
        raise ToolCenterError("只有管理员可以管理凭据、发布和审批策略", 403)
    return principal


def issue_session(response: Response, request: Request, principal: Principal) -> dict:
    token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
    hours = max(1, min(get_settings().tool_center_session_hours, 24))
    data = {**principal.public(), "csrf_token": csrf, "auth_ref": principal.auth_ref}
    get_tool_center_store().create_session(token, data, time.time() + hours * 3600)
    response.set_cookie(COOKIE, token, max_age=hours * 3600, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/")
    return {"principal": principal.public(), "csrf_token": csrf, "expires_in_seconds": hours * 3600}


def local_principal(request: Request) -> Principal:
    check_origin(request)
    if not get_settings().tool_center_local_admin:
        raise ToolCenterError("本地管理员模式已关闭，请使用企业身份令牌登录", 403)
    # Do not trust forwarded headers or arbitrary Host values (DNS rebinding).
    if (not request.client or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or any(request.headers.get(key) for key in ("x-forwarded-for", "forwarded", "x-real-ip"))
            or request.headers.get("x-tool-center-login") != "1"):
        raise ToolCenterError("本地管理员入口只允许直接访问回环地址", 403)
    return Principal("local-admin", ("admin",), ("default",), "local")
