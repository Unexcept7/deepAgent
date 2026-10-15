"""JSON-backed workflow definitions and version management.

The store deliberately keeps the persistence contract independent from the
visual editor.  It can later be replaced by a database repository without
changing the API or the DeepAgent runtime adapter.
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
from app.schemas.workflow import WorkflowDefinitionInput


class WorkflowError(ValueError):
    """A client-correctable workflow definition or version error."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "-", value.strip().lower())
    return value.strip("-") or f"workflow-{uuid4().hex[:8]}"


def _node(
    node_id: str,
    name: str,
    node_type: str,
    x: int,
    y: int = 110,
    *,
    params: dict[str, Any] | None = None,
    enabled: bool = True,
    condition: str | None = None,
    branch: str | None = None,
    human_confirmation: bool = False,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": name,
        "type": node_type,
        "x": x,
        "y": y,
        "enabled": enabled,
        "params": params or {},
        "condition": condition,
        "branch": branch,
        "human_confirmation": human_confirmation,
    }


def _edge(source: str, target: str, label: str | None = None, condition: str | None = None) -> dict[str, Any]:
    return {
        "id": f"edge-{source}-{target}",
        "source": source,
        "target": target,
        "label": label,
        "condition": condition,
        "enabled": True,
    }


def _snapshot(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(workflow.get(key))
        for key in (
            "workflow_id",
            "id",
            "name",
            "description",
            "enabled",
            "params",
            "nodes",
            "edges",
            "version",
        )
    }


DEFAULT_PARAMS = {
    "source_types": ["document", "qa", "graph", "sql", "api"],
    "top_k": 12,
    "similarity_threshold": 0.3,
    "evidence_mode": "generic",
    "timeout_seconds": 8,
    "max_parallel": 4,
    "human_confirmation": {
        "enabled": True,
        "points": ["human-confirm", "work-order-confirm"],
        "actions": ["write", "publish", "submit"],
    },
}


def _knowledge_workflow() -> dict[str, Any]:
    nodes = [
        _node("route", "意图和实体识别", "router", 40),
        _node("plan", "用户需求规划", "requirement_plan", 250, params={"max_items": 12}),
        _node("search", "统一搜索", "unified_search", 470, params={"top_k": 12}),
        _node("evidence", "证据校验", "evidence_gate", 690, params={"threshold": 0.3}),
        _node("judge", "证据裁判与覆盖审计", "claim_judge", 910),
        _node("answer", "引用回答", "answer", 1130),
        _node(
            "human-confirm",
            "人工确认点",
            "human_confirmation",
            910,
            280,
            enabled=False,
            human_confirmation=True,
            params={"actions": ["写入", "发布"]},
        ),
    ]
    edges = [
        _edge("route", "plan"),
        _edge("plan", "search"),
        _edge("search", "evidence"),
        _edge("evidence", "judge"),
        _edge("judge", "answer"),
        _edge("judge", "human-confirm", "需要写入时", "requires_confirmation == true"),
        _edge("human-confirm", "answer", "确认后"),
    ]
    return _workflow(
        "knowledge-research",
        "多源知识检索",
        "强制检索并对每个需求项执行证据校验、裁判和覆盖审计。",
        nodes,
        edges,
        params=copy.deepcopy(DEFAULT_PARAMS),
    )


def _evidence_review_workflow() -> dict[str, Any]:
    """Dedicated workflow for the evidence-reviewer application."""
    nodes = [
        _node("route", "识别待校验问题", "router", 40),
        _node("plan", "拆分需求项", "requirement_plan", 250, params={"max_items": 12}),
        _node("search", "召回候选证据", "unified_search", 470, params={"top_k": 12}),
        _node("evidence", "证据门控", "evidence_gate", 690, params={"threshold": 0.3}),
        _node("judge", "需求结果裁判", "claim_judge", 910),
        _node("answer", "安全引用回答", "answer", 1130),
    ]
    edges = [
        _edge("route", "plan"),
        _edge("plan", "search"),
        _edge("search", "evidence"),
        _edge("evidence", "judge"),
        _edge("judge", "answer"),
    ]
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update({
        "source_types": ["document", "qa", "graph"],
        "evidence_mode": "industrial_strict",
        "human_confirmation": {"enabled": False, "points": [], "actions": []},
    })
    return _workflow(
        "evidence-review",
        "证据校验流程",
        "专门用于候选证据、事实声明、需求结果和覆盖范围的严格校验。",
        nodes,
        edges,
        params=params,
    )


def _diagnosis_workflow() -> dict[str, Any]:
    nodes = [
        _node("route", "识别设备/故障码", "router", 40),
        _node("status", "查询实时状态", "api", 280, params={"source": "api", "top_k": 5}),
        _node("parallel-search", "并行检索手册与案例", "parallel", 500, params={"branches": ["manual", "case"]}),
        _node("manual", "设备手册", "unified_search", 720, 60, params={"source_types": ["document"], "top_k": 8}),
        _node("case", "历史案例", "unified_search", 720, 190, params={"source_types": ["qa", "graph"], "top_k": 8}),
        _node("diagnose", "生成诊断建议", "answer", 970, 110),
        _node("human-confirm", "高风险动作确认", "human_confirmation", 1190, 260, enabled=False, human_confirmation=True),
    ]
    edges = [
        _edge("route", "status"),
        _edge("status", "parallel-search"),
        _edge("parallel-search", "manual", "手册"),
        _edge("parallel-search", "case", "案例"),
        _edge("manual", "diagnose"),
        _edge("case", "diagnose"),
        _edge("diagnose", "human-confirm", "需要执行动作", "requires_confirmation == true"),
    ]
    params = copy.deepcopy(DEFAULT_PARAMS)
    params["source_types"] = ["api", "document", "qa", "graph"]
    return _workflow("equipment-diagnosis", "设备故障诊断", "实时状态、手册和历史案例并行汇总后生成诊断建议。", nodes, edges, params=params)


def _work_order_workflow() -> dict[str, Any]:
    nodes = [
        _node("aggregate", "汇总诊断证据", "evidence_gate", 50),
        _node("draft", "生成工单草稿", "answer", 310, params={"output": "work_order_draft"}),
        _node("review", "人工确认", "human_confirmation", 570, params={"actions": ["创建工单"]}, human_confirmation=True),
        _node("submit", "受控提交", "tool", 830, enabled=False, params={"tool": "create_work_order"}),
        _node("notify", "返回提交结果", "answer", 1090),
    ]
    edges = [_edge("aggregate", "draft"), _edge("draft", "review"), _edge("review", "submit", "确认后"), _edge("submit", "notify")]
    params = copy.deepcopy(DEFAULT_PARAMS)
    params["source_types"] = ["document", "qa", "graph", "api"]
    params["human_confirmation"]["enabled"] = True
    return _workflow("work-order-draft", "维修工单草稿", "先生成可审阅草稿，人工确认后才允许进入受控提交节点。", nodes, edges, params=params)


def _workflow(workflow_id: str, name: str, description: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *, params: dict[str, Any]) -> dict[str, Any]:
    timestamp = _now()
    workflow = {
        "workflow_id": workflow_id,
        "id": workflow_id,
        "name": name,
        "description": description,
        "enabled": True,
        "status": "published",
        "version": 1,
        "published_version": 1,
        "params": params,
        "nodes": nodes,
        "edges": edges,
        "created_at": timestamp,
        "updated_at": timestamp,
        "updated_by": "system",
        "versions": [],
        "run_history": [],
    }
    workflow["versions"] = [{
        "version": 1,
        "status": "published",
        "created_at": timestamp,
        "published_at": timestamp,
        "note": "内置模板",
        "snapshot": _snapshot(workflow),
    }]
    return workflow


DEFAULT_WORKFLOWS = [
    _knowledge_workflow(),
    _evidence_review_workflow(),
    _diagnosis_workflow(),
    _work_order_workflow(),
]

DEFAULT_TEMPLATES = [
    {
        "template_id": "knowledge-research",
        "name": "多源知识检索",
        "description": "检索、证据门控、逐项裁判和引用回答。",
        "category": "知识问答",
        "workflow": _knowledge_workflow(),
    },
    {
        "template_id": "equipment-diagnosis",
        "name": "设备故障诊断",
        "description": "设备状态与手册/案例并行检索的诊断流程。",
        "category": "设备运维",
        "workflow": _diagnosis_workflow(),
    },
    {
        "template_id": "evidence-review",
        "name": "证据校验流程",
        "description": "候选证据、事实声明、需求结果和覆盖审计。",
        "category": "质量治理",
        "workflow": _evidence_review_workflow(),
    },
    {
        "template_id": "work-order-draft",
        "name": "维修工单草稿",
        "description": "证据汇总、草稿生成、人工确认和受控提交。",
        "category": "工单",
        "workflow": _work_order_workflow(),
    },
]


class WorkflowStore:
    def __init__(self, path: str | Path | None = None) -> None:
        configured_path = path or getattr(get_settings(), "workflow_store_path", None)
        self.path = Path(configured_path) if configured_path else Path(__file__).parent / "data" / "workflows.json"
        self._lock = RLock()

    def _empty_document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "updated_at": _now(),
            "workflows": copy.deepcopy(DEFAULT_WORKFLOWS),
            "templates": copy.deepcopy(DEFAULT_TEMPLATES),
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            document = self._empty_document()
            self._write(document)
            return document
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"工作流定义文件无法读取：{exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("workflows"), list):
            raise WorkflowError("工作流定义文件格式无效")
        document.setdefault("schema_version", 1)
        document.setdefault("templates", copy.deepcopy(DEFAULT_TEMPLATES))
        existing_workflow_ids = {
            str(item.get("workflow_id") or item.get("id"))
            for item in document["workflows"]
        }
        existing_template_ids = {
            str(item.get("template_id"))
            for item in document.get("templates") or []
        }
        changed = False
        for workflow in DEFAULT_WORKFLOWS:
            workflow_id = str(workflow.get("workflow_id") or workflow.get("id"))
            if workflow_id not in existing_workflow_ids:
                document["workflows"].append(copy.deepcopy(workflow))
                existing_workflow_ids.add(workflow_id)
                changed = True
        for template in DEFAULT_TEMPLATES:
            template_id = str(template.get("template_id"))
            if template_id not in existing_template_ids:
                document["templates"].append(copy.deepcopy(template))
                existing_template_ids.add(template_id)
                changed = True
        if changed:
            self._write(document)
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document["updated_at"] = _now()
        fd, temp_name = tempfile.mkstemp(prefix="workflows-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _find(document: dict[str, Any], workflow_id: str) -> dict[str, Any]:
        for workflow in document.get("workflows", []):
            if str(workflow.get("workflow_id") or workflow.get("id")) == workflow_id:
                return workflow
        raise WorkflowError(f"工作流不存在：{workflow_id}")

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            document = self._read()
            result = []
            for workflow in document["workflows"]:
                item = copy.deepcopy(workflow)
                item.pop("versions", None)
                item.pop("run_history", None)
                result.append(item)
            return result

    def get(self, workflow_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._find(self._read(), workflow_id))

    def for_run(self, workflow_id: str, version: int | None = None) -> dict[str, Any]:
        workflow = self.get(workflow_id)
        if not workflow.get("enabled", True) or workflow.get("status") == "disabled":
            raise WorkflowError(f"工作流已停用：{workflow_id}")

        # 运行时默认只读取发布版本，不能因为调用方没有传 version 就
        # 意外执行当前草稿。应用绑定的历史发布版本仍然可以稳定运行。
        selected_version = version
        if selected_version is None:
            selected_version = workflow.get("published_version")
            if selected_version is None:
                raise WorkflowError(f"工作流尚未发布，不能运行：{workflow_id}")
        selected_version = int(selected_version)
        current_version = int(workflow.get("version") or 0)
        published_version = int(workflow.get("published_version") or 0)
        if selected_version == current_version:
            if workflow.get("status") != "published" or published_version != current_version:
                raise WorkflowError(
                    f"工作流版本 {selected_version} 仍是草稿，不能运行：{workflow_id}"
                )
            return workflow
        for item in workflow.get("versions", []):
            if int(item.get("version") or 0) != selected_version:
                continue
            if item.get("status") != "published":
                raise WorkflowError(
                    f"工作流版本 {selected_version} 未发布，不能运行：{workflow_id}"
                )
            snapshot = copy.deepcopy(item.get("snapshot") or {})
            if not snapshot:
                raise WorkflowError(f"工作流版本 {selected_version} 快照为空：{workflow_id}")
            snapshot["status"] = "published"
            snapshot["published_version"] = workflow.get("published_version")
            return snapshot
        raise WorkflowError(f"工作流 {workflow_id} 不存在版本 {selected_version}")

    def upsert(self, payload: WorkflowDefinitionInput, workflow_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            target_id = workflow_id or payload.workflow_id or _slug(payload.name)
            try:
                current = self._find(document, target_id)
            except WorkflowError:
                current = None
            if current is not None:
                if payload.base_version is not None and int(current.get("version") or 0) != payload.base_version:
                    raise WorkflowError(
                        f"工作流版本已变化：当前为 {current.get('version')}，提交基于 {payload.base_version}"
                    )
                version = int(current.get("version") or 0) + 1
                created_at = current.get("created_at") or _now()
                published_version = current.get("published_version")
                run_history = current.get("run_history", [])
                versions = current.get("versions", [])
            else:
                version = 1
                created_at = _now()
                published_version = None
                run_history = []
                versions = []
            updated = {
                "workflow_id": target_id,
                "id": target_id,
                "name": payload.name,
                "description": payload.description,
                "enabled": payload.enabled,
                "status": "draft",
                "version": version,
                "published_version": published_version,
                "params": payload.params,
                "nodes": [item.model_dump(mode="json") for item in payload.nodes],
                "edges": [item.model_dump(mode="json") for item in payload.edges],
                "created_at": created_at,
                "updated_at": _now(),
                "updated_by": "api",
                "versions": versions,
                "run_history": run_history,
            }
            if current is None:
                document["workflows"].append(updated)
            else:
                index = document["workflows"].index(current)
                document["workflows"][index] = updated
            self._write(document)
            return copy.deepcopy(updated)

    def publish(self, workflow_id: str, version: int | None = None, note: str = "") -> dict[str, Any]:
        with self._lock:
            document = self._read()
            workflow = self._find(document, workflow_id)
            if version is not None and int(workflow.get("version") or 0) != version:
                raise WorkflowError(f"当前编辑版本为 {workflow.get('version')}，不能发布版本 {version}")
            current_version = int(workflow.get("version") or 0)
            existing = next((item for item in workflow.get("versions", []) if int(item.get("version") or 0) == current_version), None)
            if existing is None:
                workflow.setdefault("versions", []).append({
                    "version": current_version,
                    "status": "published",
                    "created_at": workflow.get("updated_at") or _now(),
                    "published_at": _now(),
                    "note": note,
                    "snapshot": _snapshot(workflow),
                })
            else:
                existing.update({"status": "published", "published_at": _now(), "note": note})
            workflow["status"] = "published"
            workflow["published_version"] = current_version
            workflow["updated_at"] = _now()
            self._write(document)
            return copy.deepcopy(workflow)

    def rollback(self, workflow_id: str, version: int, *, publish: bool = True, note: str = "") -> dict[str, Any]:
        with self._lock:
            document = self._read()
            workflow = self._find(document, workflow_id)
            target = next((item for item in workflow.get("versions", []) if int(item.get("version") or 0) == version), None)
            if target is None:
                raise WorkflowError(f"工作流 {workflow_id} 不存在可回滚版本 {version}")
            snapshot = copy.deepcopy(target.get("snapshot") or {})
            next_version = int(workflow.get("version") or 0) + 1
            for key in ("name", "description", "enabled", "params", "nodes", "edges"):
                workflow[key] = copy.deepcopy(snapshot.get(key))
            workflow["version"] = next_version
            workflow["status"] = "published" if publish else "draft"
            workflow["published_version"] = next_version if publish else workflow.get("published_version")
            workflow["updated_at"] = _now()
            if publish:
                workflow.setdefault("versions", []).append({
                    "version": next_version,
                    "status": "published",
                    "created_at": workflow["updated_at"],
                    "published_at": workflow["updated_at"],
                    "note": note or f"回滚到版本 {version}",
                    "rolled_back_from": version,
                    "snapshot": _snapshot(workflow),
                })
            self._write(document)
            return copy.deepcopy(workflow)

    def compare(self, workflow_id: str, from_version: int, to_version: int) -> dict[str, Any]:
        workflow = self.get(workflow_id)

        def snapshot_for(version: int) -> dict[str, Any]:
            if version == int(workflow.get("version") or 0):
                return _snapshot(workflow)
            for item in workflow.get("versions", []):
                if int(item.get("version") or 0) == version:
                    return copy.deepcopy(item.get("snapshot") or {})
            raise WorkflowError(f"不存在版本 {version}")

        before = snapshot_for(from_version)
        after = snapshot_for(to_version)
        changed_fields = [
            key for key in ("name", "description", "enabled", "params")
            if before.get(key) != after.get(key)
        ]
        before_nodes = {str(item.get("id")): item for item in before.get("nodes", [])}
        after_nodes = {str(item.get("id")): item for item in after.get("nodes", [])}
        node_changes = {
            "added": [after_nodes[key] for key in sorted(set(after_nodes) - set(before_nodes))],
            "removed": [before_nodes[key] for key in sorted(set(before_nodes) - set(after_nodes))],
            "changed": [
                {"id": key, "before": before_nodes[key], "after": after_nodes[key]}
                for key in sorted(set(before_nodes) & set(after_nodes))
                if before_nodes[key] != after_nodes[key]
            ],
        }

        def edge_key(item: dict[str, Any]) -> str:
            return str(item.get("id") or f"{item.get('source')}->{item.get('target')}:{item.get('label') or ''}")

        before_edges = {edge_key(item): item for item in before.get("edges", [])}
        after_edges = {edge_key(item): item for item in after.get("edges", [])}
        edge_changes = {
            "added": [after_edges[key] for key in sorted(set(after_edges) - set(before_edges))],
            "removed": [before_edges[key] for key in sorted(set(before_edges) - set(after_edges))],
            "changed": [
                {"id": key, "before": before_edges[key], "after": after_edges[key]}
                for key in sorted(set(before_edges) & set(after_edges))
                if before_edges[key] != after_edges[key]
            ],
        }
        return {
            "workflow_id": workflow_id,
            "from_version": from_version,
            "to_version": to_version,
            "changed": bool(changed_fields or any(node_changes.values()) or any(edge_changes.values())),
            "changed_fields": changed_fields,
            "nodes": node_changes,
            "edges": edge_changes,
        }

    def templates(self) -> list[dict[str, Any]]:
        with self._lock:
            document = self._read()
            return copy.deepcopy(document.get("templates") or DEFAULT_TEMPLATES)

    def create_from_template(self, template_id: str, workflow_id: str | None = None, name: str | None = None) -> dict[str, Any]:
        template = next((item for item in self.templates() if item.get("template_id") == template_id), None)
        if template is None:
            raise WorkflowError(f"工作流模板不存在：{template_id}")
        base = copy.deepcopy(template.get("workflow") or {})
        target_id = workflow_id or f"{template_id}-{uuid4().hex[:6]}"
        payload = WorkflowDefinitionInput(
            workflow_id=target_id,
            name=name or f"{base.get('name', template_id)}（副本）",
            description=base.get("description", ""),
            enabled=True,
            params=base.get("params") or {},
            nodes=base.get("nodes") or [],
            edges=base.get("edges") or [],
        )
        return self.upsert(payload, target_id)

    def record_run(self, workflow_id: str, version: int, run_id: str, status: str, *, duration_ms: int | None = None, error: str | None = None) -> None:
        with self._lock:
            document = self._read()
            workflow = self._find(document, workflow_id)
            history = workflow.setdefault("run_history", [])
            existing = next((item for item in history if item.get("run_id") == run_id), None)
            payload = {"run_id": run_id, "version": version, "status": status, "updated_at": _now()}
            if duration_ms is not None:
                payload["duration_ms"] = duration_ms
            if error:
                payload["error"] = error
            if existing is None:
                history.append({"started_at": _now(), **payload})
            else:
                existing.update(payload)
            del history[:-50]
            self._write(document)

    def runs(self, workflow_id: str) -> list[dict[str, Any]]:
        return self.get(workflow_id).get("run_history", [])


@lru_cache(maxsize=1)
def get_workflow_store() -> WorkflowStore:
    return WorkflowStore()
