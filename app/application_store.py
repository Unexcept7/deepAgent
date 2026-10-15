"""JSON-backed application-center repository.

The repository boundary is intentionally small so the same contract can be
backed by PostgreSQL/SQLite later.  The JSON implementation is useful for the
current single-service deployment and preserves auditable application
snapshots, published versions and run summaries.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.schemas.application import ApplicationDefinitionInput
from app.workflow_store import WorkflowError, WorkflowStore, get_workflow_store


class ApplicationError(ValueError):
    """A client-correctable application definition or lifecycle error."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "-", value.strip().lower())
    return value.strip("-") or f"app-{uuid4().hex[:8]}"


def _snapshot(application: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(application.get(key))
        for key in (
            "app_id",
            "id",
            "name",
            "description",
            "category",
            "type",
            "icon",
            "enabled",
            "tags",
            "workflow_id",
            "workflow_version",
            "workflow_bindings",
            "data_sources",
            "knowledge_binding_mode",
            "knowledge_bindings",
            "tools",
            "runtime",
            "permissions",
            "channels",
            "version",
        )
    }


def _source(source_type: str, name: str, *, enabled: bool = True, **config: Any) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "name": name,
        "enabled": enabled,
        "config": config,
    }


def _tool(tool_id: str, name: str, *, confirmation: bool = False) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "name": name,
        "enabled": True,
        "requires_confirmation": confirmation,
        "config": {},
    }


def _application(
    app_id: str,
    name: str,
    description: str,
    category: str,
    workflow_id: str,
    *,
    sources: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tags: list[str],
    runtime: dict[str, Any],
    permissions: dict[str, Any],
    channels: list[str],
) -> dict[str, Any]:
    timestamp = _now()
    application = {
        "app_id": app_id,
        "id": app_id,
        "name": name,
        "description": description,
        "category": category,
        "type": "DeepAgent",
        "icon": "知" if "知识" in name else "证",
        "enabled": True,
        "status": "published",
        "version": 1,
        "published_version": 1,
        "tags": tags,
        "workflow_id": workflow_id,
        "workflow_version": 1,
        "workflow_bindings": [{
            "workflow_id": workflow_id,
            "version": 1,
            "role": "primary",
            "enabled": True,
        }],
        "data_sources": sources,
        "knowledge_binding_mode": "inherit",
        "knowledge_bindings": [],
        "tools": tools,
        "runtime": runtime,
        "permissions": permissions,
        "channels": channels,
        "created_at": timestamp,
        "updated_at": timestamp,
        "updated_by": "system",
        "versions": [],
        "run_history": [],
    }
    application["versions"] = [{
        "version": 1,
        "status": "published",
        "created_at": timestamp,
        "published_at": timestamp,
        "note": "内置应用",
        "snapshot": _snapshot(application),
    }]
    return application


DEFAULT_APPLICATIONS = [
    _application(
        "enterprise-knowledge-assistant",
        "企业知识助手",
        "自主规划企业知识、图谱、业务数据和实时 API 查询。",
        "知识问答",
        "knowledge-research",
        sources=[
            _source("document", "企业文档"),
            _source("qa", "标准问答"),
            _source("graph", "知识图谱"),
            _source("sql", "业务数据"),
            _source("api", "实时 API"),
        ],
        tools=[
            _tool("unified_search", "统一搜索"),
            _tool("evidence_review", "证据校验"),
            _tool("device_status", "设备实时状态"),
            _tool("create_work_order", "创建维修工单", confirmation=True),
        ],
        tags=["RAG", "多源检索", "证据引用"],
        runtime={
            "top_k": 12,
            "similarity_threshold": 0.3,
            "evidence_mode": "generic",
            "timeout_seconds": 8,
            "human_confirmation": {"enabled": True, "actions": ["write", "publish", "submit"]},
        },
        permissions={
            "owner": "当前登录用户",
            "run_roles": ["员工", "管理员"],
            "edit_roles": ["管理员"],
            "publish_roles": ["管理员"],
        },
        channels=["工作台", "API"],
    ),
    _application(
        "evidence-reviewer",
        "证据校验 Agent",
        "对检索结果、事实声明、版本和引用进行后端校验。",
        "质量治理",
        "evidence-review",
        sources=[
            _source("document", "检索文档"),
            _source("qa", "标准 QA"),
            _source("graph", "知识图谱"),
        ],
        tools=[
            _tool("evidence_review", "证据裁判"),
            _tool("coverage_audit", "覆盖审计"),
        ],
        tags=["Evidence", "覆盖审计", "冲突提示"],
        runtime={
            "top_k": 12,
            "similarity_threshold": 0.3,
            "evidence_mode": "industrial_strict",
            "timeout_seconds": 8,
            "human_confirmation": {"enabled": False, "actions": []},
        },
        permissions={
            "owner": "当前登录用户",
            "run_roles": ["员工", "管理员"],
            "edit_roles": ["管理员"],
            "publish_roles": ["管理员"],
        },
        channels=["工作台", "API"],
    ),
]


class ApplicationStore:
    def __init__(self, path: str | Path | None = None, workflow_store: WorkflowStore | None = None) -> None:
        configured_path = path or getattr(get_settings(), "application_store_path", None)
        self.path = Path(configured_path) if configured_path else Path(__file__).parent / "data" / "applications.json"
        self._lock = RLock()
        self._workflow_store = workflow_store

    def _workflow_repository(self) -> WorkflowStore:
        """Use a colocated repository in isolated deployments/tests when present.

        The production default still uses the configured singleton.  When an
        application store is placed in a temporary or tenant-specific folder,
        a sibling ``workflows.json`` is the matching repository; otherwise a
        strict version check against the global store would validate the wrong
        workflow history.
        """
        if self._workflow_store is not None:
            return self._workflow_store
        sibling = self.path.with_name("workflows.json")
        if sibling.exists():
            self._workflow_store = WorkflowStore(sibling)
            return self._workflow_store
        return get_workflow_store()

    def _empty_document(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "updated_at": _now(),
            "applications": copy.deepcopy(DEFAULT_APPLICATIONS),
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            document = self._empty_document()
            self._write(document)
            return document
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApplicationError(f"应用定义文件无法读取：{exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("applications"), list):
            raise ApplicationError("应用定义文件格式无效")
        document.setdefault("schema_version", 2)
        # Applications created before per-application knowledge bindings were
        # introduced inherit the published RAGFlow connection allow-list.  Do
        # not rewrite the user's JSON file merely by reading it; normalize the
        # shape in memory and let the next normal save/publish persist it.
        for application in document.get("applications", []):
            if not isinstance(application, dict):
                continue
            application.setdefault("knowledge_binding_mode", "inherit")
            application.setdefault("knowledge_bindings", [])
            for version in application.get("versions") or []:
                snapshot = version.get("snapshot") if isinstance(version, dict) else None
                if isinstance(snapshot, dict):
                    snapshot.setdefault("knowledge_binding_mode", "inherit")
                    snapshot.setdefault("knowledge_bindings", [])
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document["updated_at"] = _now()
        fd, temp_name = tempfile.mkstemp(prefix="applications-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _find(document: dict[str, Any], app_id: str) -> dict[str, Any]:
        for application in document.get("applications", []):
            if str(application.get("app_id") or application.get("id")) == app_id:
                return application
        raise ApplicationError(f"应用不存在：{app_id}")

    @staticmethod
    def _public(application: dict[str, Any]) -> dict[str, Any]:
        item = copy.deepcopy(application)
        item.pop("versions", None)
        item.pop("run_history", None)
        history = application.get("run_history") or []
        item["run_count"] = len(history)
        item["last_run"] = copy.deepcopy(history[-1]) if history else None
        return item

    def list(self, *, status: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            applications = [self._public(item) for item in self._read()["applications"]]
            if status:
                applications = [item for item in applications if item.get("status") == status]
            if category:
                applications = [item for item in applications if item.get("category") == category]
            return applications

    def get(self, app_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._find(self._read(), app_id))

    def for_run(self, app_id: str) -> dict[str, Any]:
        """Runtime uses the published snapshot, never an unpublished allowlist."""
        application = self.get(app_id)
        if not application.get("enabled", True) or application.get("status") in {"disabled", "archived"}:
            raise ApplicationError("应用已停用")
        version = application.get("published_version")
        snapshot = next(
            (item.get("snapshot") for item in application.get("versions", [])
             if item.get("version") == version), None,
        )
        if not snapshot:
            raise ApplicationError("应用尚未发布，不能运行")
        return {**copy.deepcopy(snapshot), "status": "published", "published_version": version}

    def upsert(self, payload: ApplicationDefinitionInput, app_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            target_id = app_id or payload.app_id or _slug(payload.name)
            try:
                current = self._find(document, target_id)
            except ApplicationError:
                current = None
            if current is not None:
                if payload.base_version is not None and int(current.get("version") or 0) != payload.base_version:
                    raise ApplicationError(
                        f"应用版本已变化：当前为 {current.get('version')}，提交基于 {payload.base_version}"
                    )
                version = int(current.get("version") or 0) + 1
                created_at = current.get("created_at") or _now()
                published_version = current.get("published_version")
                versions = current.get("versions", [])
                run_history = current.get("run_history", [])
                previous_status = current.get("status")
            else:
                version = 1
                created_at = _now()
                published_version = None
                versions = []
                run_history = []
                previous_status = None
            updated = {
                "app_id": target_id,
                "id": target_id,
                "name": payload.name,
                "description": payload.description,
                "category": payload.category,
                "type": payload.type,
                "icon": payload.icon,
                "enabled": payload.enabled,
                "status": "disabled" if not payload.enabled else "draft",
                "version": version,
                "published_version": published_version,
                "tags": payload.tags,
                "workflow_id": payload.workflow_id,
                "workflow_version": payload.workflow_version,
                "workflow_bindings": [item.model_dump(mode="json") for item in payload.workflow_bindings],
                "data_sources": [item.model_dump(mode="json") for item in payload.data_sources],
                "knowledge_binding_mode": (
                    payload.knowledge_binding_mode
                    if current is None or "knowledge_binding_mode" in payload.model_fields_set
                    else current.get("knowledge_binding_mode", "inherit")
                ),
                "knowledge_bindings": (
                    [item.model_dump(mode="json") for item in payload.knowledge_bindings]
                    if current is None or "knowledge_bindings" in payload.model_fields_set
                    else copy.deepcopy(current.get("knowledge_bindings") or [])
                ),
                "tools": [item.model_dump(mode="json") for item in payload.tools],
                "runtime": payload.runtime,
                "permissions": payload.permissions,
                "channels": payload.channels,
                "created_at": created_at,
                "updated_at": _now(),
                "updated_by": "api",
                "versions": versions,
                "run_history": run_history,
            }
            # Preserve a deliberately disabled state when a caller only edits
            # metadata and does not re-enable the application.
            if current is not None and previous_status == "disabled" and not payload.enabled:
                updated["status"] = "disabled"
            if current is None:
                document["applications"].append(updated)
            else:
                index = document["applications"].index(current)
                document["applications"][index] = updated
            self._write(document)
            return copy.deepcopy(updated)

    def _validate_workflow_binding(self, application: dict[str, Any]) -> None:
        bindings: list[dict[str, Any]] = []
        primary_id = application.get("workflow_id")
        if primary_id:
            bindings.append({
                "workflow_id": primary_id,
                "version": application.get("workflow_version"),
                "role": "primary",
                "enabled": True,
            })
        for binding in application.get("workflow_bindings") or []:
            if not isinstance(binding, dict) or not binding.get("enabled", True):
                continue
            candidate = {
                "workflow_id": binding.get("workflow_id"),
                "version": binding.get("version"),
                "role": binding.get("role", "subflow"),
                "enabled": True,
            }
            if candidate["workflow_id"] and not any(
                item["workflow_id"] == candidate["workflow_id"]
                and item["version"] == candidate["version"]
                for item in bindings
            ):
                bindings.append(candidate)
        for binding in bindings:
            workflow_id = str(binding["workflow_id"])
            try:
                workflow = self._workflow_repository().for_run(
                    workflow_id,
                    binding.get("version"),
                )
            except WorkflowError as exc:
                raise ApplicationError(
                    f"应用绑定的工作流版本不可运行：{workflow_id} v{binding.get('version') or '当前发布版'}；{exc}"
                ) from exc
            if workflow.get("status") != "published":
                raise ApplicationError(
                    f"应用绑定的工作流版本未发布：{workflow_id} v{workflow.get('version')}"
                )

    @staticmethod
    def _validate_knowledge_binding_shape(application: dict[str, Any]) -> None:
        mode = str(application.get("knowledge_binding_mode") or "inherit")
        if mode not in {"inherit", "explicit"}:
            raise ApplicationError(f"知识库绑定模式无效：{mode}")
        seen: set[tuple[str, str]] = set()
        for binding in application.get("knowledge_bindings") or []:
            if not isinstance(binding, dict) or not binding.get("enabled", True):
                continue
            connection_id = str(binding.get("connection_id") or "").strip()
            dataset_id = str(binding.get("dataset_id") or "").strip()
            if not connection_id or not dataset_id:
                raise ApplicationError("启用的知识库绑定必须包含 connection_id 和 dataset_id")
            key = (connection_id, dataset_id)
            if key in seen:
                raise ApplicationError(f"应用重复绑定知识库：{connection_id}/{dataset_id}")
            seen.add(key)

    def publish(self, app_id: str, version: int | None = None, note: str = "") -> dict[str, Any]:
        with self._lock:
            document = self._read()
            application = self._find(document, app_id)
            if version is not None and int(application.get("version") or 0) != version:
                raise ApplicationError(f"当前编辑版本为 {application.get('version')}，不能发布版本 {version}")
            if not application.get("enabled", True):
                raise ApplicationError("停用状态的应用不能发布")
            self._validate_knowledge_binding_shape(application)
            self._validate_workflow_binding(application)
            current_version = int(application.get("version") or 0)
            existing = next(
                (item for item in application.get("versions", []) if int(item.get("version") or 0) == current_version),
                None,
            )
            if existing is None:
                application.setdefault("versions", []).append({
                    "version": current_version,
                    "status": "published",
                    "created_at": application.get("updated_at") or _now(),
                    "published_at": _now(),
                    "note": note,
                    "snapshot": _snapshot(application),
                })
            else:
                existing.update({"status": "published", "published_at": _now(), "note": note})
            application["status"] = "published"
            application["published_version"] = current_version
            application["updated_at"] = _now()
            self._write(document)
            return copy.deepcopy(application)

    def rollback(self, app_id: str, version: int, *, publish: bool = True, note: str = "") -> dict[str, Any]:
        with self._lock:
            document = self._read()
            application = self._find(document, app_id)
            target = next(
                (item for item in application.get("versions", []) if int(item.get("version") or 0) == version),
                None,
            )
            if target is None:
                raise ApplicationError(f"应用 {app_id} 不存在可回滚版本 {version}")
            snapshot = copy.deepcopy(target.get("snapshot") or {})
            next_version = int(application.get("version") or 0) + 1
            for key in (
                "name", "description", "category", "type", "icon", "enabled", "tags",
                "workflow_id", "workflow_version", "workflow_bindings", "data_sources", "tools",
                "knowledge_binding_mode", "knowledge_bindings", "runtime", "permissions", "channels",
            ):
                application[key] = copy.deepcopy(snapshot.get(key))
            application["version"] = next_version
            application["status"] = "published" if publish else ("draft" if application.get("enabled", True) else "disabled")
            application["published_version"] = next_version if publish else application.get("published_version")
            application["updated_at"] = _now()
            if publish:
                self._validate_workflow_binding(application)
                application.setdefault("versions", []).append({
                    "version": next_version,
                    "status": "published",
                    "created_at": application["updated_at"],
                    "published_at": application["updated_at"],
                    "note": note or f"回滚到版本 {version}",
                    "rolled_back_from": version,
                    "snapshot": _snapshot(application),
                })
            self._write(document)
            return copy.deepcopy(application)

    def compare(self, app_id: str, from_version: int, to_version: int) -> dict[str, Any]:
        application = self.get(app_id)

        def snapshot_for(version: int) -> dict[str, Any]:
            if version == int(application.get("version") or 0):
                return _snapshot(application)
            for item in application.get("versions", []):
                if int(item.get("version") or 0) == version:
                    return copy.deepcopy(item.get("snapshot") or {})
            raise ApplicationError(f"不存在版本 {version}")

        before = snapshot_for(from_version)
        after = snapshot_for(to_version)
        fields = (
            "name", "description", "category", "type", "icon", "enabled", "tags",
            "workflow_id", "workflow_version", "workflow_bindings", "data_sources", "tools",
            "knowledge_binding_mode", "knowledge_bindings", "runtime", "permissions", "channels",
        )
        changed_fields = [key for key in fields if before.get(key) != after.get(key)]
        return {
            "app_id": app_id,
            "from_version": from_version,
            "to_version": to_version,
            "changed": bool(changed_fields),
            "changed_fields": changed_fields,
            "before": before,
            "after": after,
        }

    def record_run(
        self,
        app_id: str,
        version: int,
        run_id: str,
        status: str,
        *,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            document = self._read()
            application = self._find(document, app_id)
            history = application.setdefault("run_history", [])
            existing = next((item for item in history if item.get("run_id") == run_id), None)
            payload: dict[str, Any] = {
                "run_id": run_id,
                "version": version,
                "status": status,
                "updated_at": _now(),
            }
            if duration_ms is not None:
                payload["duration_ms"] = duration_ms
            if error:
                payload["error"] = error
            if existing is None:
                history.append({"started_at": _now(), **payload})
            else:
                existing.update(payload)
            del history[:-100]
            self._write(document)

    def runs(self, app_id: str) -> list[dict[str, Any]]:
        return self.get(app_id).get("run_history", [])


@lru_cache(maxsize=1)
def get_application_store() -> ApplicationStore:
    return ApplicationStore()
