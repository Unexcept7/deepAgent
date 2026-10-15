from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Role = Literal["admin", "editor", "operator", "approver", "viewer"]
Environment = Literal["test", "production"]
Risk = Literal["read_only", "draft", "confirm", "approval", "device_control"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


def validate_schema(schema: dict, *, object_required: bool = False) -> dict:
    if len(str(schema)) > 40000:
        raise ValueError("Schema 过大")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("JSON Schema 格式无效") from exc
    if object_required and schema.get("type") != "object":
        raise ValueError("输入 Schema 必须是 object")
    def walk(value, depth=0):
        if depth > 20:
            raise ValueError("Schema 嵌套超过 20 层")
        if isinstance(value, dict):
            if any(key in value for key in ("$ref", "$dynamicRef")):
                raise ValueError("请展开 Schema 引用；不允许运行时读取外部引用")
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
    walk(schema)
    return schema


def reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in {"password", "token", "api_key", "secret", "authorization", "cookie", "access_token"}:
                raise ValueError("秘密请保存到凭据库，定义只能使用 credential_id")
            reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            reject_secrets(child)


class ConnectionInput(Contract):
    connection_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,99}$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1200)
    type: Literal["ragflow", "neo4j", "business_api", "sql", "mcp", "qa"]
    endpoint: str = Field(default="", max_length=2000)
    owner: str = Field(default="", max_length=160)
    environment: Environment = "test"
    credential_id: str | None = Field(default=None, max_length=120)
    auth_mode: Literal["none", "bearer", "api_key", "basic"] = "none"
    auth_header: str = Field(default="X-API-Key", pattern=r"^[A-Za-z0-9-]{1,80}$")
    enabled: bool = True
    timeout_seconds: int = Field(default=8, ge=1, le=60)
    health_path: str = Field(default="/health", max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    base_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def safe_definition(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("名称不能为空")
        if self.type != "qa" and self.endpoint:
            parsed = urlsplit(self.endpoint)
            protocols = {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"} if self.type == "neo4j" else {"http", "https"}
            if parsed.scheme not in protocols or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("连接地址必须使用支持的协议，不能包含账号、密码、查询参数或片段")
        if not self.health_path.startswith("/") or ".." in self.health_path or self.health_path.startswith("//") or any(c in self.health_path for c in "?#"):
            raise ValueError("健康检查路径无效")
        reject_secrets(self.config)
        return self


class ToolInput(Contract):
    tool_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1600)
    kind: Literal["builtin", "http", "mcp"] = "http"
    handler: str | None = Field(default=None, max_length=100)
    connection_id: str | None = Field(default=None, max_length=100)
    owner: str = Field(default="", max_length=160)
    environment: Environment = "test"
    enabled: bool = True
    risk: Risk = "read_only"
    required_permission: str = Field(default="tool:invoke", max_length=120)
    allowed_roles: list[Role] = Field(default_factory=lambda: ["admin", "operator", "approver"])
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    output_schema: dict[str, Any] = Field(default_factory=dict)
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(default="/", max_length=1000)
    timeout_seconds: int = Field(default=8, ge=1, le=60)
    rate_limit_per_minute: int = Field(default=30, ge=1, le=1000)
    config: dict[str, Any] = Field(default_factory=dict)
    base_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def safe_tool(self):
        self.name, self.description = self.name.strip(), self.description.strip()
        if not self.name or not self.description:
            raise ValueError("工具名称和说明不能为空")
        validate_schema(self.input_schema, object_required=True)
        validate_schema(self.output_schema)
        if any(item in (self.input_schema.get("properties") or {}) for item in ("confirmed", "approved", "user_id", "workspace_id", "credential_id")):
            raise ValueError("工具参数不能声明授权、身份或凭据字段")
        if self.kind == "http":
            if not self.connection_id:
                raise ValueError("HTTP 工具必须选择数据连接")
            if not self.path.startswith("/") or self.path.startswith("//") or ".." in self.path or any(c in self.path for c in "?#\\"):
                raise ValueError("工具路径必须是安全的相对路径")
            fields = re.findall(r"\{([^{}]+)\}", self.path)
            if any(field not in self.input_schema.get("properties", {}) for field in fields):
                raise ValueError("路径变量必须在输入 Schema 中声明")
            if self.method not in {"GET", "HEAD"} and self.risk in {"read_only", "draft"}:
                raise ValueError("HTTP 写方法至少需要人工确认，不能标成只读/本地草稿")
        if self.kind == "mcp" and not self.connection_id:
            raise ValueError("MCP 工具必须选择数据连接")
        reject_secrets(self.config)
        return self


class CredentialInput(Contract):
    credential_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,119}$")
    name: str = Field(min_length=1, max_length=160)
    environment: Environment = "test"
    secret: dict[str, str] = Field(min_length=1, max_length=10)

    @field_validator("secret")
    @classmethod
    def secret_fields(cls, values):
        if set(values) - {"token", "api_key", "username", "password"}:
            raise ValueError("凭据只支持 token、api_key、username、password")
        if not any(values.values()) or any("••" in value or "****" in value for value in values.values()):
            raise ValueError("请输入真实凭据，不能使用脱敏占位符")
        if any(len(value)>16384 or '\r' in value or '\n' in value for value in values.values()):
            raise ValueError("凭据过长或含有不允许的换行符")
        return values


class LifecycleRequest(Contract):
    version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=500)


class EnabledRequest(Contract):
    enabled: bool


class KnowledgeBindingRequest(Contract):
    """RAGFlow dataset IDs maintained from the knowledge-base page."""

    dataset_ids: list[str] = Field(default_factory=list, max_length=100)
    base_version: int | None = Field(default=None, ge=1)

    @field_validator("dataset_ids")
    @classmethod
    def normalize_dataset_ids(cls, values):
        normalized: list[str] = []
        for value in values:
            item = str(value or "").strip()
            if not item:
                continue
            if len(item) > 200 or any(char.isspace() for char in item) or any(char in item for char in "/\\?#"):
                raise ValueError("知识库 ID 只能包含非空白字符，且不能包含路径或查询符号")
            if item not in normalized:
                normalized.append(item)
        return normalized


class InvokeRequest(Contract):
    parameters: dict[str, Any] = Field(default_factory=dict)
    app_id: str | None = Field(default=None, max_length=160)
    workflow_id: str | None = Field(default=None, max_length=120)
    trace_id: str | None = Field(default=None, max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=160)
    debug: bool = False


class DecisionRequest(Contract):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=500)


class LoginRequest(Contract):
    token: str = Field(min_length=16, max_length=4000)


class ImportRequest(Contract):
    format: Literal["bundle", "openapi", "mcp"] = "bundle"
    document: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None
