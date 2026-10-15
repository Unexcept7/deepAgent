from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings


class ToolCenterError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def now() -> str:
    return datetime.now(UTC).isoformat()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "••••••" if any(word in key.lower() for word in ("password", "secret", "token", "api_key", "authorization", "cookie"))
            else redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact(child) for child in value]
    return value


def object_schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def initial_resources(settings) -> list[tuple[str, dict]]:
    resources: list[tuple[str, dict]] = []
    connections = [
        ("env-ragflow", "RAGFlow 文档知识库", "ragflow", settings.ragflow_base_url, "env-ragflow-key", "bearer",
         {"dataset_ids": settings.configured_dataset_ids()}, "/api/v1/datasets"),
        ("env-neo4j", "Neo4j 企业知识图谱", "neo4j", settings.neo4j_uri, "env-neo4j-key", "basic", {}, "/"),
        ("env-business", "企业业务 API", "business_api", settings.business_api_base_url, "env-business-key", "bearer", {}, "/health"),
        ("env-sql", "结构化只读搜索 API", "sql", settings.sql_search_url, "env-business-key", "bearer",
         {"enforce_allowlist": True, "allow_natural_query": False, "max_rows": 100, "datasets": {}}, "/health"),
        ("env-mcp", "MCP 搜索代理", "mcp", settings.mcp_search_url, "env-mcp-key", "bearer", {"transport": "search_proxy"}, "/health"),
        ("env-qa", "标准 QA 问答文件", "qa", "", None, "none", {"path": settings.qa_data_path or ""}, "/"),
    ]
    for cid, name, ctype, endpoint, credential, auth, config, health in connections:
        resources.append(("connections", {
            "connection_id": cid, "name": name, "description": "从现有服务器配置初始化；不会将 .env 密钥发送给浏览器。",
            "type": ctype, "endpoint": endpoint or "", "owner": "system", "environment": "production",
            "credential_id": credential, "auth_mode": auth, "auth_header": "X-API-Key",
            "enabled": True, "timeout_seconds": 8, "health_path": health, "config": config,
        }))
    device = {"type": "string", "minLength": 1, "maxLength": 120}
    problem = {"type": "string", "minLength": 1, "maxLength": 2000}
    priority = {"type": "string", "enum": ["low", "medium", "high", "urgent"], "default": "medium"}
    defs = [
        ("get_device_status", "设备实时状态", "查询设备当前状态和关键参数", "http", "read_only", "GET", "/devices/{device_id}/status",
         object_schema({"device_id": device}, ["device_id"]), None, "device:read"),
        ("get_alarm_history", "设备报警历史", "查询设备最近 1–365 天的报警", "http", "read_only", "GET", "/devices/{device_id}/alarms",
         object_schema({"device_id": device, "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30}}, ["device_id"]), None, "alarm:read"),
        ("draft_work_order", "生成维修工单草稿", "仅生成本地草稿，不写入企业业务系统", "builtin", "draft", "GET", "/",
         object_schema({"device_id": device, "problem": problem, "priority": priority, "suggested_action": {"type": "string", "maxLength": 2000}}, ["device_id", "problem"]),
         "draft_work_order", "work_order:draft"),
        ("submit_work_order", "提交正式维修工单", "创建正式工单；服务端等待本人明确确认后才调用业务 API", "http", "confirm", "POST", "/work-orders",
         object_schema({"device_id": device, "problem": problem, "priority": priority}, ["device_id", "problem"]), None, "work_order:create"),
        ("set_device_parameter", "修改设备参数", "高风险设备控制；需要不同人员审批，默认停用", "http", "device_control", "PUT", "/devices/{device_id}/parameters",
         object_schema({"device_id": device, "parameter": {"type": "string", "minLength": 1}, "value": {}}, ["device_id", "parameter", "value"]), None, "device:control"),
    ]
    for tid, name, desc, kind, risk, method, path, schema, handler, permission in defs:
        resources.append(("tools", {
            "tool_id": tid, "name": name, "description": desc, "kind": kind, "handler": handler,
            "connection_id": None if kind == "builtin" else "env-business", "owner": "system",
            "environment": "production", "enabled": tid != "set_device_parameter", "risk": risk,
            "required_permission": permission, "allowed_roles": ["admin", "operator", "approver"],
            "input_schema": schema, "output_schema": {"type": "object"}, "method": method, "path": path,
            "timeout_seconds": 8, "rate_limit_per_minute": 30, "config": {},
        }))
    return resources


class ToolCenterStore:
    """SQLite transactions own versions, action claims and idempotency.

    Network calls never run inside database transactions. An action is claimed
    once before I/O and never automatically retried after an uncertain write.
    """

    def __init__(self, path: str | Path | None = None, *, settings=None, encryption_key: str | bytes | None = None):
        self.settings = settings or get_settings()
        configured = path or self.settings.tool_center_db_path
        self.path = Path(configured) if configured else Path(__file__).parents[1] / "data" / "tool_center.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keys = encryption_key or self.settings.tool_center_encryption_keys
        self.key_source = "environment" if keys else "local_file"
        if not keys:
            key_path = self.path.with_suffix(".key")
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                keys = key_path.read_bytes()
            else:
                keys = Fernet.generate_key()
                with os.fdopen(fd, "wb") as handle:
                    handle.write(keys)
        if isinstance(keys, bytes):
            keys = keys.decode()
        try:
            self.cipher = MultiFernet([Fernet(key.strip().encode()) for key in str(keys).split(",") if key.strip()])
        except (ValueError, TypeError) as exc:
            raise ToolCenterError("凭据加密密钥无效；请检查 TOOL_CENTER_ENCRYPTION_KEYS", 500) from exc
        self._initialize()

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def encrypt(self, value: Any) -> str:
        return self.cipher.encrypt(encode(value).encode()).decode()

    def decrypt(self, value: str) -> Any:
        try:
            return json.loads(self.cipher.decrypt(value.encode()))
        except (InvalidToken, ValueError) as exc:
            raise ToolCenterError("无法解密凭据/动作，请恢复原加密密钥；不会生成替代密钥", 500) from exc

    def _initialize(self):
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS resources (
                workspace TEXT, kind TEXT, id TEXT, version INTEGER, published INTEGER,
                enabled INTEGER, definition TEXT, updated_at TEXT, updated_by TEXT,
                PRIMARY KEY(workspace,kind,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS versions (
                workspace TEXT, kind TEXT, id TEXT, version INTEGER, definition TEXT,
                created_at TEXT, created_by TEXT, note TEXT, PRIMARY KEY(workspace,kind,id,version))""")
            db.execute("""CREATE TABLE IF NOT EXISTS credentials (
                workspace TEXT, id TEXT, name TEXT, environment TEXT, encrypted TEXT,
                env_reference TEXT, version INTEGER, updated_at TEXT, updated_by TEXT,
                PRIMARY KEY(workspace,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                digest TEXT PRIMARY KEY, principal TEXT, expires REAL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, workspace TEXT, actor TEXT, idem_key TEXT, fingerprint TEXT,
                tool_id TEXT, started REAL, status TEXT, data TEXT, encrypted_payload TEXT,
                UNIQUE(workspace,actor,idem_key))""")
            db.execute("CREATE INDEX IF NOT EXISTS call_rate ON calls(workspace,actor,tool_id,started)")
            db.execute("""CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT, actor TEXT,
                action TEXT, target TEXT, detail TEXT, created_at TEXT)""")
            marker = db.execute("SELECT 1 FROM resources WHERE workspace='default' LIMIT 1").fetchone()
            if not marker:
                for kind, definition in initial_resources(self.settings):
                    rid = definition["tool_id" if kind == "tools" else "connection_id"]
                    db.execute("INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?)",
                               ("default", kind, rid, 1, 1, int(definition["enabled"]), encode(definition), now(), "system"))
                    db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)",
                               ("default", kind, rid, 1, encode(definition), now(), "system", "迁移现有配置"))
                for cid, name, fields in [
                    ("env-ragflow-key", "服务器 RAGFlow 凭据", {"token": "ragflow_api_key"}),
                    ("env-neo4j-key", "服务器 Neo4j 凭据", {"username": "neo4j_username", "password": "neo4j_password"}),
                    ("env-business-key", "服务器业务 API 凭据", {"token": "business_api_token"}),
                    ("env-sql-key", "服务器 SQL 搜索网关凭据", {"token": "sql_search_token"}),
                    ("env-mcp-key", "服务器 MCP 凭据", {"token": "mcp_search_token"}),
                ]:
                    db.execute("INSERT INTO credentials VALUES(?,?,?,?,?,?,?,?,?)",
                               ("default", cid, name, "production", None, encode(fields), 1, now(), "system"))

    @staticmethod
    def _resource(db, workspace, kind, rid):
        row = db.execute("SELECT * FROM resources WHERE workspace=? AND kind=? AND id=?", (workspace, kind, rid)).fetchone()
        if not row:
            raise ToolCenterError("定义不存在", 404)
        return row

    def list(self, workspace, kind):
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM resources WHERE workspace=? AND kind=? ORDER BY id", (workspace, kind)).fetchall()
            return [self._public(row) for row in rows]

    @staticmethod
    def _public(row):
        definition = json.loads(row["definition"])
        result = {
            **definition, "version": row["version"], "published_version": row["published"],
            "enabled": bool(row["enabled"]), "status": "disabled" if not row["enabled"]
            else "published" if row["version"] == row["published"] else "draft",
            "updated_at": row["updated_at"], "updated_by": row["updated_by"],
        }
        if row["kind"] == "tools":
            result["read_only"] = definition["risk"] == "read_only"
        return result

    def get(self, workspace, kind, rid):
        with self.transaction() as db:
            row = self._resource(db, workspace, kind, rid)
            result = self._public(row)
            result["versions"] = [dict(item) for item in db.execute(
                "SELECT version,created_at,created_by,note FROM versions WHERE workspace=? AND kind=? AND id=? ORDER BY version DESC",
                (workspace, kind, rid))]
            return result

    def runtime(self, workspace, kind, rid, *, debug=False):
        with self.transaction() as db:
            row = self._resource(db, workspace, kind, rid)
            if not row["enabled"]:
                raise ToolCenterError("定义已停用", 409)
            version = row["version"] if debug else row["published"]
            if not version:
                raise ToolCenterError("定义未发布，只有管理员/编辑者可以调试", 409)
            selected = db.execute("SELECT definition FROM versions WHERE workspace=? AND kind=? AND id=? AND version=?",
                                  (workspace, kind, rid, version)).fetchone()
            definition = json.loads(selected["definition"])
            if not definition.get("enabled", True):
                raise ToolCenterError("所选版本已停用", 409)
            return {**definition, "version": version}

    def save(self, workspace, kind, definition, actor, *, note="保存草稿"):
        rid = definition["tool_id" if kind == "tools" else "connection_id"]
        definition = dict(definition)
        base = definition.pop("base_version", None)
        with self.transaction() as db:
            current = db.execute("SELECT * FROM resources WHERE workspace=? AND kind=? AND id=?", (workspace, kind, rid)).fetchone()
            if current and (base is None or int(base) != current["version"]):
                raise ToolCenterError("定义版本已变化，请刷新后再保存（必须携带 base_version）", 409)
            version = current["version"] + 1 if current else 1
            enabled = current["enabled"] if current else int(definition.get("enabled", True))
            db.execute("""INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace,kind,id) DO UPDATE SET version=excluded.version,
                definition=excluded.definition,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                (workspace, kind, rid, version, current["published"] if current else None, enabled, encode(definition), now(), actor))
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)",
                       (workspace, kind, rid, version, encode(definition), now(), actor, note))
            self._audit(db, workspace, actor, "save", f"{kind}/{rid}", {"version": version})
        return self.get(workspace, kind, rid)

    def publish(self, workspace, kind, rid, actor, version=None, note=""):
        with self.transaction() as db:
            row = self._resource(db, workspace, kind, rid)
            version = version or row["version"]
            if version != row["version"]:
                raise ToolCenterError("只能发布当前草稿；历史版本请使用回滚", 409)
            if not row["enabled"]:
                raise ToolCenterError("请先启用再发布", 409)
            if not json.loads(row["definition"]).get("enabled", True):
                raise ToolCenterError("当前草稿标记为停用，请先启用再发布", 409)
            db.execute("UPDATE resources SET published=?,updated_by=?,updated_at=? WHERE workspace=? AND kind=? AND id=?",
                       (version, actor, now(), workspace, kind, rid))
            self._audit(db, workspace, actor, "publish", f"{kind}/{rid}", {"version": version, "note": note})
        return self.get(workspace, kind, rid)

    def import_bundle(self, workspace, connections, tools, actor):
        resources = [("connections", item) for item in connections] + [("tools", item) for item in tools]
        with self.transaction() as db:
            for kind, definition in resources:
                definition = dict(definition)
                rid = definition["tool_id" if kind == "tools" else "connection_id"]
                base = definition.pop("base_version", None)
                current = db.execute("SELECT * FROM resources WHERE workspace=? AND kind=? AND id=?", (workspace, kind, rid)).fetchone()
                if current and base != current["version"]:
                    raise ToolCenterError("导入 ID 已存在，需显式提供正确的 base_version：" + rid, 409)
                version = current["version"] + 1 if current else 1
                db.execute("""INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(workspace,kind,id) DO UPDATE SET version=excluded.version,
                    definition=excluded.definition,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                    (workspace, kind, rid, version, current["published"] if current else None,
                     current["enabled"] if current else int(definition["enabled"]), encode(definition), now(), actor))
                db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)",
                           (workspace, kind, rid, version, encode(definition), now(), actor, "导入草稿"))
            self._audit(db, workspace, actor, "import_bundle", "resources", {"count": len(resources)})
        return {"imported_connections": len(connections), "imported_tools": len(tools), "status": "draft"}

    def set_enabled(self, workspace, kind, rid, enabled, actor):
        with self.transaction() as db:
            current = self._resource(db, workspace, kind, rid)
            definition = json.loads(current["definition"])
            if bool(definition.get("enabled", True)) != bool(enabled):
                definition["enabled"] = bool(enabled)
                version = current["version"] + 1
                db.execute("UPDATE resources SET version=?,definition=? WHERE workspace=? AND kind=? AND id=?",
                           (version, encode(definition), workspace, kind, rid))
                db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)",
                           (workspace, kind, rid, version, encode(definition), now(), actor, "启停状态变更"))
            db.execute("UPDATE resources SET enabled=?,updated_at=?,updated_by=? WHERE workspace=? AND kind=? AND id=?",
                       (int(enabled), now(), actor, workspace, kind, rid))
            self._audit(db, workspace, actor, "enable" if enabled else "disable", f"{kind}/{rid}", {})
        return self.get(workspace, kind, rid)

    def rollback(self, workspace, kind, rid, version, actor, note=""):
        with self.transaction() as db:
            current = self._resource(db, workspace, kind, rid)
            if not current["enabled"]:
                raise ToolCenterError("请先启用再回滚发布", 409)
            row = db.execute("SELECT definition FROM versions WHERE workspace=? AND kind=? AND id=? AND version=?",
                             (workspace, kind, rid, version)).fetchone()
            if not row:
                raise ToolCenterError("历史版本不存在", 404)
            definition = json.loads(row["definition"])
            if not definition.get("enabled", True):
                raise ToolCenterError("不能回滚发布已停用的历史版本；请保存为启用的草稿再发布", 409)
            new_version = current["version"] + 1
            db.execute("UPDATE resources SET version=?,published=?,definition=?,updated_at=?,updated_by=? WHERE workspace=? AND kind=? AND id=?",
                       (new_version, new_version, encode(definition), now(), actor, workspace, kind, rid))
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)",
                       (workspace, kind, rid, new_version, encode(definition), now(), actor, note or f"回滚至 v{version}"))
            self._audit(db, workspace, actor, "rollback", f"{kind}/{rid}", {"from_version": version, "published_version": new_version})
        return self.get(workspace, kind, rid)

    def compare(self, workspace, kind, rid, left, right):
        with self.transaction() as db:
            self._resource(db, workspace, kind, rid)
            records = []
            for version in (left, right):
                row = db.execute("SELECT definition FROM versions WHERE workspace=? AND kind=? AND id=? AND version=?",
                                 (workspace, kind, rid, version)).fetchone()
                if not row:
                    raise ToolCenterError("历史版本不存在", 404)
                records.append(json.loads(row[0]))
        a, b = records
        return {"from_version": left, "to_version": right, "changes": [
            {"field": key, "before": a.get(key), "after": b.get(key)}
            for key in sorted(set(a) | set(b)) if a.get(key) != b.get(key)]}

    @staticmethod
    def _audit(db, workspace, actor, action, target, detail):
        db.execute("INSERT INTO audit(workspace,actor,action,target,detail,created_at) VALUES(?,?,?,?,?,?)",
                   (workspace, actor, action, target, encode(redact(detail)), now()))

    def audit(self, workspace, actor, action, target, detail=None):
        with self.transaction() as db:
            self._audit(db, workspace, actor, action, target, detail or {})

    def audits(self, workspace, limit=100):
        with self.transaction() as db:
            return [{**dict(row), "detail": json.loads(row["detail"])} for row in db.execute(
                "SELECT * FROM audit WHERE workspace=? ORDER BY id DESC LIMIT ?", (workspace, min(limit, 500)))]

    def credentials(self, workspace):
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM credentials WHERE workspace=? ORDER BY id", (workspace,)).fetchall()
        result = []
        for row in rows:
            ref = json.loads(row["env_reference"] or "{}")
            configured = bool(row["encrypted"]) or any(getattr(self.settings, field, None) for key, field in ref.items() if key != "username")
            result.append({
                "credential_id": row["id"], "name": row["name"], "environment": row["environment"],
                "version": row["version"], "updated_at": row["updated_at"], "updated_by": row["updated_by"],
                "source": "environment" if ref else "encrypted", "configured": configured, "masked": "••••••" if configured else "未配置",
            })
        return result

    def credential_secret(self, workspace, cid):
        if not cid:
            return {}
        with self.transaction() as db:
            row = db.execute("SELECT * FROM credentials WHERE workspace=? AND id=?", (workspace, cid)).fetchone()
        if not row:
            raise ToolCenterError("凭据不存在或不属于当前工作区", 404)
        if row["encrypted"]:
            return self.decrypt(row["encrypted"])
        return {key: getattr(self.settings, field, "") or "" for key, field in json.loads(row["env_reference"] or "{}").items()}

    def save_credential(self, workspace, payload, actor):
        with self.transaction() as db:
            old = db.execute("SELECT version FROM credentials WHERE workspace=? AND id=?", (workspace, payload["credential_id"])).fetchone()
            version = old["version"] + 1 if old else 1
            db.execute("""INSERT INTO credentials VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace,id) DO UPDATE SET name=excluded.name,environment=excluded.environment,
                encrypted=excluded.encrypted,env_reference=NULL,version=excluded.version,
                updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                (workspace, payload["credential_id"], payload["name"], payload["environment"],
                 self.encrypt(payload["secret"]), None, version, now(), actor))
            self._audit(db, workspace, actor, "credential_rotate" if old else "credential_create", payload["credential_id"], {"version": version})
        return next(item for item in self.credentials(workspace) if item["credential_id"] == payload["credential_id"])

    def delete_credential(self, workspace, cid, actor):
        with self.transaction() as db:
            # Historical published snapshots can still use an old reference.
            definitions = db.execute("SELECT definition FROM versions WHERE workspace=? AND kind='connections'", (workspace,))
            if any(json.loads(row[0]).get("credential_id") == cid for row in definitions):
                raise ToolCenterError("连接的当前或历史版本仍引用该凭据，请先让相关版本改用其他凭据后再删除", 409)
            cursor = db.execute("DELETE FROM credentials WHERE workspace=? AND id=?", (workspace, cid))
            if not cursor.rowcount:
                raise ToolCenterError("凭据不存在", 404)
            self._audit(db, workspace, actor, "credential_delete", cid, {})

    def rotate_encryption(self, workspace, actor):
        count = 0
        with self.transaction() as db:
            for table, column in (("credentials", "encrypted"), ("calls", "encrypted_payload")):
                for row in db.execute(f"SELECT rowid,{column} FROM {table} WHERE workspace=? AND {column} IS NOT NULL", (workspace,)).fetchall():
                    value = self.cipher.rotate(row[column].encode()).decode()
                    db.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (value, row["rowid"]))
                    count += 1
            self._audit(db, workspace, actor, "reencrypt", "credentials+actions", {"count": count})
        return {"rotated_count": count, "key_source": self.key_source}

    def create_session(self, token, principal, expires):
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), encode(principal), expires))

    def session(self, token):
        if not token:
            return None
        with self.transaction() as db:
            row = db.execute("SELECT principal FROM sessions WHERE digest=? AND expires>?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return json.loads(row["principal"]) if row else None

    def delete_session(self, token):
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def reserve_call(self, workspace, actor, key, request_fingerprint, tool, payload, data, status):
        with self.transaction() as db:
            old = db.execute("SELECT * FROM calls WHERE workspace=? AND actor=? AND idem_key=?", (workspace, actor, key)).fetchone()
            if old:
                if old["fingerprint"] != request_fingerprint:
                    raise ToolCenterError("同一幂等键不能用于不同工具、应用或参数", 409)
                return json.loads(old["data"]), False
            count = db.execute("SELECT COUNT(*) FROM calls WHERE workspace=? AND actor=? AND tool_id=? AND started>?",
                               (workspace, actor, tool["tool_id"], time.time() - 60)).fetchone()[0]
            if count >= tool["rate_limit_per_minute"]:
                raise ToolCenterError("工具调用超过每分钟限流，请稍后再试", 429)
            data = {
                **data, "call_id": str(uuid4()), "workspace_id": workspace, "actor_id": actor,
                "tool_id": tool["tool_id"], "tool_version": tool["version"], "tool_name": tool["name"],
                "status": status, "risk": tool["risk"], "created_at": now(), "updated_at": now(),
                "parameters": redact(payload["parameters"]), "idempotency_key": key,
            }
            db.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (data["call_id"], workspace, actor, key, request_fingerprint, tool["tool_id"],
                        time.time(), status, encode(data), self.encrypt(payload)))
            self._audit(db, workspace, actor, "invoke_request", data["call_id"], {"tool_id": tool["tool_id"], "status": status})
        return data, True

    def call(self, workspace, cid):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM calls WHERE workspace=? AND id=?", (workspace, cid)).fetchone()
        if not row:
            raise ToolCenterError("调用记录不存在或不属于当前工作区", 404)
        return json.loads(row["data"]), self.decrypt(row["encrypted_payload"])

    def calls(self, workspace, *, actor=None, status=None, limit=100):
        with self.transaction() as db:
            rows = db.execute("SELECT data FROM calls WHERE workspace=? AND (? IS NULL OR actor=?) AND (? IS NULL OR status=?) ORDER BY started DESC LIMIT ?",
                              (workspace, actor, actor, status, status, min(limit, 500))).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_call(self, workspace, cid, updates, *, expected=None, actor="system"):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM calls WHERE workspace=? AND id=?", (workspace, cid)).fetchone()
            if not row:
                raise ToolCenterError("调用记录不存在", 404)
            data = json.loads(row["data"])
            if expected is not None and row["status"] not in expected:
                return data, False
            data.update(redact(updates))
            data["updated_at"] = now()
            db.execute("UPDATE calls SET status=?,data=? WHERE id=?", (data["status"], encode(data), cid))
            self._audit(db, workspace, actor, "call_state", cid, {"status": data["status"], "note": data.get("decision_note")})
        return data, True


@lru_cache(maxsize=1)
def get_tool_center_store() -> ToolCenterStore:
    return ToolCenterStore()
