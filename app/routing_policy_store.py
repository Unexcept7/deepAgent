"""JSON-backed, versioned routing policy definitions."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.schemas.routing_policy import RoutingPolicyDefinitionInput


class RoutingPolicyError(ValueError):
    """A client-correctable routing policy definition or version error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", value.strip().lower()).strip("-")
    return result or f"routing-policy-{uuid4().hex[:8]}"


def _snapshot(policy: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "policy_id", "id", "name", "category", "description", "enabled", "priority",
        "match", "action", "version", "published_version", "status", "updated_at", "updated_by",
    )
    return {key: copy.deepcopy(policy.get(key)) for key in keys if key in policy}


DEFAULT_POLICY_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "policy_id": "fault-code-knowledge",
        "name": "故障码知识问答",
        "category": "故障码",
        "description": "识别设备故障码、报警码和错误码，强制检索企业资料并进行证据校验。",
        "priority": 200,
        "match": {
            "keywords": ["故障码", "错误码", "报警码", "故障", "报警"],
            "entity_types": [],
            "examples": ["北辰47 的 E203 是什么？", "E203 怎么处理？"],
            "mode": "any",
        },
        "action": {
            "route": "knowledge_qa",
            "source_types": ["document", "qa", "graph"],
            "top_k": 12,
            "similarity_threshold": 0.3,
            "require_evidence": True,
            "human_confirmation": False,
        },
    },
    {
        "policy_id": "device-status-query",
        "name": "设备实时状态查询",
        "category": "业务查询",
        "description": "查询设备当前状态、报警记录和实时运行数据，不自动执行写操作。",
        "priority": 180,
        "match": {
            "keywords": ["实时状态", "当前状态", "当前报警", "报警记录", "运行状态"],
            "entity_types": [],
            "examples": ["查询北辰47当前状态", "设备现在还在报警吗？"],
            "mode": "any",
        },
        "action": {
            "route": "business_query",
            "source_types": ["api", "sql"],
            "top_k": 8,
            "similarity_threshold": 0.3,
            "require_evidence": False,
            "human_confirmation": False,
        },
    },
    {
        "policy_id": "work-order-action",
        "name": "维修工单动作",
        "category": "业务动作",
        "description": "创建、提交或修改维修工单；允许生成草稿，但正式提交必须人工确认。",
        "priority": 300,
        "match": {
            "keywords": ["创建工单", "提交工单", "维修工单", "工单草稿", "修改设备参数"],
            "entity_types": [],
            "examples": ["给北辰47创建维修工单"],
            "mode": "any",
        },
        "action": {
            "route": "business_action",
            "source_types": ["api"],
            "top_k": 5,
            "similarity_threshold": 0.3,
            "require_evidence": False,
            "human_confirmation": True,
        },
    },
    {
        "policy_id": "generic-knowledge-question",
        "name": "通用知识问答",
        "category": "知识问答",
        "description": "处理设备参数、协议、功能、规格和说明书等事实型知识问题。",
        "priority": 100,
        "match": {
            "keywords": ["是什么", "多少", "支持", "参数", "规格", "功能", "协议", "接口", "说明书", "手册"],
            "entity_types": [],
            "examples": ["M1101 有多少路数字量输入？", "这个模块支持什么通信协议？"],
            "mode": "any",
        },
        "action": {
            "route": "knowledge_qa",
            "source_types": ["document", "qa", "graph"],
            "top_k": 12,
            "similarity_threshold": 0.3,
            "require_evidence": True,
            "human_confirmation": False,
        },
    },
    {
        "policy_id": "casual-conversation",
        "name": "普通对话",
        "category": "普通对话",
        "description": "处理问候、能力介绍等不需要企业资料的对话。",
        "priority": 50,
        "match": {
            "keywords": ["你好", "您好", "你是谁", "你能做什么", "介绍一下你的能力"],
            "entity_types": [],
            "examples": ["你好", "你能做什么？"],
            "mode": "any",
        },
        "action": {
            "route": "casual",
            "source_types": [],
            "top_k": 1,
            "similarity_threshold": 0.0,
            "require_evidence": False,
            "human_confirmation": False,
        },
    },
)


class RoutingPolicyStore:
    def __init__(self, path: str | Path | None = None) -> None:
        configured_path = path or getattr(get_settings(), "routing_policy_store_path", None)
        self.path = Path(configured_path) if configured_path else Path(__file__).parent / "data" / "routing_policies.json"
        self._lock = RLock()

    def _empty_document(self) -> dict[str, Any]:
        policies: list[dict[str, Any]] = []
        for definition in DEFAULT_POLICY_DEFINITIONS:
            policy = {
                **copy.deepcopy(definition),
                "id": definition["policy_id"],
                "enabled": True,
                "status": "published",
                "version": 1,
                "published_version": 1,
                "created_at": _now(),
                "updated_at": _now(),
                "updated_by": "system",
            }
            policy["versions"] = [{
                "version": 1,
                "status": "published",
                "created_at": policy["created_at"],
                "published_at": policy["updated_at"],
                "note": "系统默认策略",
                "snapshot": _snapshot(policy),
            }]
            policies.append(policy)
        return {"schema_version": 1, "updated_at": _now(), "policies": policies}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            document = self._empty_document()
            self._write(document)
            return document
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RoutingPolicyError(f"路由策略文件无法读取：{exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("policies"), list):
            raise RoutingPolicyError("路由策略文件格式无效")
        document.setdefault("schema_version", 1)
        changed = False
        existing = {str(item.get("policy_id") or item.get("id")) for item in document["policies"]}
        for definition in DEFAULT_POLICY_DEFINITIONS:
            if definition["policy_id"] not in existing:
                policy = {
                    **copy.deepcopy(definition),
                    "id": definition["policy_id"],
                    "enabled": True,
                    "status": "published",
                    "version": 1,
                    "published_version": 1,
                    "created_at": _now(),
                    "updated_at": _now(),
                    "updated_by": "system",
                    "versions": [],
                }
                policy["versions"] = [{
                    "version": 1,
                    "status": "published",
                    "created_at": policy["created_at"],
                    "published_at": policy["updated_at"],
                    "note": "新增系统默认策略",
                    "snapshot": _snapshot(policy),
                }]
                document["policies"].append(policy)
                existing.add(definition["policy_id"])
                changed = True
        if changed:
            self._write(document)
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document["updated_at"] = _now()
        fd, temp_name = tempfile.mkstemp(prefix="routing-policies-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _find(document: dict[str, Any], policy_id: str) -> dict[str, Any]:
        for policy in document.get("policies", []):
            if str(policy.get("policy_id") or policy.get("id")) == policy_id:
                return policy
        raise RoutingPolicyError(f"路由策略不存在：{policy_id}")

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            document = self._read()
            result = []
            for policy in document["policies"]:
                item = copy.deepcopy(policy)
                item.pop("versions", None)
                result.append(item)
            return sorted(result, key=lambda item: (-int(item.get("priority") or 0), str(item.get("name") or "")))

    def get(self, policy_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._find(self._read(), policy_id))

    def published(self) -> list[dict[str, Any]]:
        with self._lock:
            document = self._read()
            result: list[dict[str, Any]] = []
            for current in document["policies"]:
                version = int(current.get("published_version") or 0)
                if version == int(current.get("version") or 0) and current.get("status") == "published":
                    if current.get("enabled", True):
                        result.append(copy.deepcopy(current))
                    continue
                historical = next(
                    (item.get("snapshot") for item in current.get("versions", []) if int(item.get("version") or 0) == version),
                    None,
                )
                if historical and historical.get("enabled", True):
                    result.append(copy.deepcopy(historical))
            return result

    def upsert(self, payload: RoutingPolicyDefinitionInput, policy_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            target_id = policy_id or payload.policy_id or _slug(payload.name)
            try:
                current = self._find(document, target_id)
            except RoutingPolicyError:
                current = None
            if current is not None:
                if payload.base_version is None or int(current.get("version") or 0) != payload.base_version:
                    raise RoutingPolicyError(
                        f"路由策略版本已变化：当前为 {current.get('version')}，提交基于 {payload.base_version}"
                    )
                version = int(current.get("version") or 0) + 1
                created_at = current.get("created_at") or _now()
                published_version = current.get("published_version")
                versions = current.get("versions", [])
            else:
                version = 1
                created_at = _now()
                published_version = None
                versions = []
            updated = {
                "policy_id": target_id,
                "id": target_id,
                "name": payload.name,
                "category": payload.category,
                "description": payload.description,
                "enabled": payload.enabled,
                "status": "draft",
                "priority": payload.priority,
                "match": payload.match.model_dump(mode="json"),
                "action": payload.action.model_dump(mode="json"),
                "version": version,
                "published_version": published_version,
                "created_at": created_at,
                "updated_at": _now(),
                "updated_by": "api",
                "versions": versions,
            }
            if current is None:
                document["policies"].append(updated)
            else:
                document["policies"][document["policies"].index(current)] = updated
            self._write(document)
            return copy.deepcopy(updated)

    def publish(self, policy_id: str, version: int | None = None, note: str = "") -> dict[str, Any]:
        with self._lock:
            document = self._read()
            policy = self._find(document, policy_id)
            if version is not None and int(policy.get("version") or 0) != version:
                raise RoutingPolicyError(f"当前编辑版本为 {policy.get('version')}，不能发布版本 {version}")
            current_version = int(policy.get("version") or 0)
            existing = next((item for item in policy.get("versions", []) if int(item.get("version") or 0) == current_version), None)
            if existing is None:
                policy.setdefault("versions", []).append({
                    "version": current_version,
                    "status": "published",
                    "created_at": policy.get("updated_at") or _now(),
                    "published_at": _now(),
                    "note": note,
                    "snapshot": _snapshot(policy),
                })
            else:
                existing.update({"status": "published", "published_at": _now(), "note": note})
            policy["status"] = "published"
            policy["published_version"] = current_version
            policy["updated_at"] = _now()
            self._write(document)
            return copy.deepcopy(policy)

    def rollback(self, policy_id: str, version: int, *, publish: bool = True, note: str = "") -> dict[str, Any]:
        with self._lock:
            document = self._read()
            policy = self._find(document, policy_id)
            target = next((item for item in policy.get("versions", []) if int(item.get("version") or 0) == version), None)
            if target is None:
                raise RoutingPolicyError(f"路由策略 {policy_id} 不存在可回滚版本 {version}")
            snapshot = copy.deepcopy(target.get("snapshot") or {})
            next_version = int(policy.get("version") or 0) + 1
            for key in ("name", "category", "description", "enabled", "priority", "match", "action"):
                policy[key] = copy.deepcopy(snapshot.get(key))
            policy["version"] = next_version
            policy["status"] = "published" if publish else "draft"
            policy["published_version"] = next_version if publish else policy.get("published_version")
            policy["updated_at"] = _now()
            if publish:
                policy.setdefault("versions", []).append({
                    "version": next_version,
                    "status": "published",
                    "created_at": policy["updated_at"],
                    "published_at": policy["updated_at"],
                    "note": note or f"回滚到版本 {version}",
                    "rolled_back_from": version,
                    "snapshot": _snapshot(policy),
                })
            self._write(document)
            return copy.deepcopy(policy)

    def compare(self, policy_id: str, from_version: int, to_version: int) -> dict[str, Any]:
        policy = self.get(policy_id)

        def snapshot_for(version: int) -> dict[str, Any]:
            if version == int(policy.get("version") or 0):
                return _snapshot(policy)
            for item in policy.get("versions", []):
                if int(item.get("version") or 0) == version:
                    return copy.deepcopy(item.get("snapshot") or {})
            raise RoutingPolicyError(f"不存在版本 {version}")

        before = snapshot_for(from_version)
        after = snapshot_for(to_version)
        fields = ("name", "category", "description", "enabled", "priority", "match", "action")
        changed_fields = [key for key in fields if before.get(key) != after.get(key)]
        return {
            "policy_id": policy_id,
            "from_version": from_version,
            "to_version": to_version,
            "changed": bool(changed_fields),
            "changed_fields": changed_fields,
            "before": before,
            "after": after,
        }


@lru_cache(maxsize=1)
def get_routing_policy_store() -> RoutingPolicyStore:
    return RoutingPolicyStore()
