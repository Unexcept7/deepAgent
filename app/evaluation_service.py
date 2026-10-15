"""多智能体评测服务。

评测复用生产 Agent 的已发布应用/工作流和真实连接器，但每条用例都从
空历史开始，不写入用户会话。评测身份采用本地最大权限，符合当前项目
“先不做用户权限限制”的阶段性约定；业务写操作仍不会被评测自动触发。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from app.agents.graph import run_agent
from app.application_store import ApplicationError, get_application_store
from app.config import get_settings
from app.schemas.evaluation import EvaluationCase, EvaluationRunRequest
from app.tool_center.auth import Principal
from app.tool_center.context import ExecutionContext
from app.workflow_store import WorkflowError, get_workflow_store


class EvaluationError(ValueError):
    """评测请求或评测存储错误。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _fold(value: Any) -> str:
    return "".join(_text(value).casefold().split())


def _contains_expected(expected: str, actual_values: list[str]) -> bool:
    target = _fold(expected)
    return bool(target) and any(target == _fold(value) or target in _fold(value) for value in actual_values)


def _case_id(case: EvaluationCase) -> str:
    return _text(case.id) or case.case_id


def _expected_documents(case: EvaluationCase) -> list[str]:
    values = list(case.expected_documents)
    for item in case.gold_documents:
        if isinstance(item, dict):
            values.append(_text(item.get("document_name") or item.get("name") or item.get("document_id")))
        else:
            values.append(_text(item))
    return list(dict.fromkeys(item for item in values if item))


def _published_agent(app_id: str) -> dict[str, Any]:
    try:
        application = get_application_store().for_run(app_id)
    except ApplicationError as exc:
        raise EvaluationError(str(exc)) from exc

    workflow_id = str(application.get("workflow_id") or "").strip() or None
    workflow_version = application.get("workflow_version")
    workflow = None
    if workflow_id:
        try:
            workflow = get_workflow_store().for_run(
                workflow_id,
                int(workflow_version) if workflow_version is not None else None,
            )
        except (WorkflowError, TypeError, ValueError) as exc:
            raise EvaluationError(f"应用绑定的已发布工作流不可运行：{exc}") from exc

    sources = [
        str(item.get("source_type"))
        for item in application.get("data_sources") or []
        if isinstance(item, dict) and item.get("enabled", True) and item.get("source_type")
    ]
    runtime = copy.deepcopy(application.get("runtime") or {})
    permissions = application.get("permissions") or {}
    return {
        "app_id": str(application.get("app_id") or app_id),
        "name": str(application.get("name") or app_id),
        "version": int(application.get("published_version") or application.get("version") or 0),
        "workflow_id": workflow_id,
        "workflow_version": int(workflow.get("version")) if workflow else None,
        "workflow_name": str(workflow.get("name") or "") if workflow else None,
        "source_types": list(dict.fromkeys(sources)) if application.get("data_sources") is not None else None,
        "runtime": runtime,
        "workspace_id": str(permissions.get("workspace_id") or "default"),
    }


def list_evaluation_agents() -> list[dict[str, Any]]:
    """列出可用于评测的已发布 Agent，同时保留不可运行原因。"""
    result: list[dict[str, Any]] = []
    try:
        applications = get_application_store().list()
    except ApplicationError as exc:
        raise EvaluationError(str(exc)) from exc

    for application in applications:
        if application.get("status") != "published" or application.get("enabled") is False:
            continue
        app_id = str(application.get("app_id") or application.get("id") or "").strip()
        if not app_id:
            continue
        item = {
            "app_id": app_id,
            "name": str(application.get("name") or app_id),
            "description": str(application.get("description") or ""),
            "version": application.get("published_version") or application.get("version"),
            "status": "ready",
            "workflow_id": application.get("workflow_id"),
            "workflow_version": application.get("workflow_version"),
            "source_types": [
                str(source.get("source_type"))
                for source in application.get("data_sources") or []
                if isinstance(source, dict) and source.get("enabled", True) and source.get("source_type")
            ],
        }
        try:
            prepared = _published_agent(app_id)
            item.update({
                "status": "ready",
                "workflow_version": prepared.get("workflow_version"),
                "workflow_name": prepared.get("workflow_name"),
                "source_types": prepared.get("source_types"),
            })
        except EvaluationError as exc:
            item.update({"status": "unavailable", "error": str(exc)})
        result.append(item)
    return result


def _evaluation_dataset_root() -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "retrieval_eval"


def _read_dataset_file(dataset_id: str) -> list[dict[str, Any]]:
    if not dataset_id or Path(dataset_id).name != dataset_id or not dataset_id.endswith(".jsonl"):
        raise EvaluationError("评测集标识无效")
    path = (_evaluation_dataset_root() / dataset_id).resolve()
    root = _evaluation_dataset_root().resolve()
    if root not in path.parents or not path.is_file():
        raise EvaluationError(f"评测集不存在：{dataset_id}")
    cases: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvaluationError(f"评测集无法读取：{exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"评测集第 {line_number} 行不是有效 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise EvaluationError(f"评测集第 {line_number} 行必须是对象")
        cases.append(value)
    return cases


def list_evaluation_datasets() -> list[dict[str, Any]]:
    root = _evaluation_dataset_root()
    if not root.is_dir():
        return []
    result = []
    for path in sorted(root.glob("*.jsonl")):
        try:
            cases = _read_dataset_file(path.name)
        except EvaluationError as exc:
            result.append({"dataset_id": path.name, "status": "invalid", "error": str(exc)})
            continue
        splits = sorted({str(item.get("split")) for item in cases if item.get("split")})
        result.append({
            "dataset_id": path.name,
            "name": path.stem,
            "status": "ready",
            "case_count": len(cases),
            "splits": splits,
        })
    return result


def load_evaluation_dataset(dataset_id: str, *, split: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    cases = _read_dataset_file(dataset_id)
    if split:
        cases = [item for item in cases if str(item.get("split") or "") == split]
    return copy.deepcopy(cases[:max(1, min(int(limit), 100))])


def _routing_values(result: dict[str, Any], key: str, fields: tuple[str, ...]) -> list[str]:
    routing = ((result.get("trace") or {}).get("routing_policy") or {})
    values: list[str] = []
    for item in routing.get(key) or []:
        if isinstance(item, dict):
            values.extend(_text(item.get(field)) for field in fields if _text(item.get(field)))
        elif _text(item):
            values.append(_text(item))
    return list(dict.fromkeys(values))


def _actual_source_types(result: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in [*(result.get("citations") or []), *(result.get("retrieval_hits") or [])]:
        if isinstance(item, dict) and _text(item.get("source_type")):
            values.append(_text(item.get("source_type")))
    for span in ((result.get("trace") or {}).get("spans") or []):
        if not isinstance(span, dict):
            continue
        attributes = span.get("attributes") or {}
        if isinstance(attributes, dict) and _text(attributes.get("source_type")):
            values.append(_text(attributes.get("source_type")))
    return list(dict.fromkeys(values))


def _actual_documents_and_text(result: dict[str, Any]) -> tuple[list[str], str]:
    documents: list[str] = []
    evidence_text: list[str] = []
    for item in [*(result.get("citations") or []), *(result.get("retrieval_hits") or [])]:
        if not isinstance(item, dict):
            continue
        document = _text(item.get("document_name") or item.get("title") or item.get("name"))
        if document:
            documents.append(document)
        content = _text(item.get("content") or item.get("excerpt"))
        if content:
            evidence_text.append(content)
    return list(dict.fromkeys(documents)), "\n".join(evidence_text)


def _evaluate_case(case: EvaluationCase, result: dict[str, Any], duration_ms: int) -> dict[str, Any]:
    answer = _text(result.get("answer"))
    actual_intents = _routing_values(result, "semantic_intents", ("intent_id", "intent", "name", "route"))
    actual_entities = _routing_values(result, "semantic_entities", ("original", "normalized", "entity", "value", "entity_id"))
    actual_sources = _actual_source_types(result)
    actual_documents, evidence_text = _actual_documents_and_text(result)
    evidence_status = _text((result.get("evidence") or {}).get("status"))
    checks: list[dict[str, Any]] = []

    for expected in case.expected_answer_contains:
        checks.append({
            "type": "answer_contains",
            "expected": expected,
            "actual": answer,
            "passed": _fold(expected) in _fold(answer),
        })
    for expected in case.expected_intents:
        checks.append({
            "type": "intent",
            "expected": expected,
            "actual": actual_intents,
            "passed": _contains_expected(expected, actual_intents),
        })
    for expected in case.expected_entities:
        checks.append({
            "type": "entity",
            "expected": expected,
            "actual": actual_entities,
            "passed": _contains_expected(expected, actual_entities),
        })
    for expected in case.expected_source_types:
        checks.append({
            "type": "source_type",
            "expected": expected,
            "actual": actual_sources,
            "passed": _contains_expected(expected, actual_sources),
        })
    for expected in _expected_documents(case):
        checks.append({
            "type": "document",
            "expected": expected,
            "actual": actual_documents,
            "passed": _contains_expected(expected, actual_documents),
        })
    for expected in case.required_concepts:
        checks.append({
            "type": "concept",
            "expected": expected,
            "actual": f"{answer}\n{evidence_text}",
            "passed": _fold(expected) in _fold(f"{answer}\n{evidence_text}"),
        })
    if case.expected_outcome:
        outcome = _fold(case.expected_outcome)
        abstained = evidence_status in {"not_found", "unsupported", "blocked"} or any(
            marker in _fold(answer)
            for marker in ("无法确认", "没有可靠证据", "未找到", "不能确认", "没有找到", "资料不足", "前提不成立")
        )
        outcome_passed = abstained if outcome == "abstain" else bool(actual_documents or result.get("retrieval_hits"))
        checks.append({
            "type": "outcome",
            "expected": case.expected_outcome,
            "actual": "abstain" if abstained else "retrieve",
            "passed": outcome_passed,
        })
    if case.expected_evidence_status:
        checks.append({
            "type": "evidence_status",
            "expected": case.expected_evidence_status,
            "actual": evidence_status,
            "passed": _fold(case.expected_evidence_status) == _fold(evidence_status),
        })

    trace = result.get("trace") or {}
    score = None if not checks else round(sum(bool(item["passed"]) for item in checks) / len(checks), 4)
    has_error = bool(result.get("error"))
    return {
        "case_id": _case_id(case),
        "query": case.query,
        "status": "error" if has_error else "success",
        "evaluation_status": "observed" if score is None else "scored",
        "passed": None if score is None else score == 1.0 and not has_error,
        "score": score,
        "checks": checks,
        "answer": str(result.get("answer") or "")[:12000],
        "error": result.get("error"),
        "evidence_status": evidence_status or None,
        "citation_count": len(result.get("citations") or []),
        "retrieval_hit_count": len(result.get("retrieval_hits") or []),
        "route": _text(trace.get("route")) or None,
        "model_calls": int(trace.get("model_calls") or 0),
        "input_tokens": trace.get("input_tokens"),
        "output_tokens": trace.get("output_tokens"),
        "total_tokens": trace.get("total_tokens"),
        "duration_ms": int(duration_ms),
        "intents": actual_intents,
        "entities": actual_entities,
        "source_types": actual_sources,
        "documents": actual_documents,
    }


def _run_one(agent: dict[str, Any], case: EvaluationCase) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        prepared = _published_agent(agent["app_id"])
        principal = Principal(
            "evaluation-runner",
            ("admin", "manager", "operator", "approver", "editor", "viewer"),
            (prepared["workspace_id"],),
            permissions=("*",),
        )
        source_types = prepared.get("source_types")
        context = ExecutionContext(
            principal=principal,
            workspace_id=prepared["workspace_id"],
            app_id=prepared["app_id"],
            workflow_id=prepared.get("workflow_id"),
            environment="production",
            # None deliberately means all published connections in the default
            # max-privilege evaluation scope, filtered only by source_types.
            connection_ids=None,
            source_types=tuple(source_types) if source_types is not None else None,
        )
        result = run_agent(
            [],
            case.query,
            source_types=source_types,
            workflow_id=prepared.get("workflow_id"),
            workflow_version=prepared.get("workflow_version"),
            workflow_params=prepared.get("runtime") or {},
            tool_context=context,
        )
        row = _evaluate_case(
            case,
            result if isinstance(result, dict) else {"answer": str(result)},
            round((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:  # one Agent/case failure cannot hide other comparisons
        row = _evaluate_case(
            case,
            {"error": str(exc), "answer": "", "trace": {}},
            round((time.perf_counter() - started) * 1000),
        )
    row["agent_id"] = agent["app_id"]
    row["agent_name"] = agent.get("name") or agent["app_id"]
    row["agent_version"] = agent.get("version")
    return row


def _aggregate(agent: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [float(row["score"]) for row in rows if row.get("score") is not None]
    durations = [int(row.get("duration_ms") or 0) for row in rows]
    total_tokens = [int(row["total_tokens"]) for row in rows if row.get("total_tokens") is not None]
    evidence_rows = [row for row in rows if row.get("evidence_status")]
    passed = sum(row.get("passed") is True for row in rows)
    return {
        "agent_id": agent["app_id"],
        "agent_name": agent.get("name") or agent["app_id"],
        "version": agent.get("version"),
        "case_count": len(rows),
        "scored_case_count": len(scores),
        "observed_case_count": len(rows) - len(scores),
        "passed_count": passed,
        "pass_rate": round(passed / len(scores), 4) if scores else None,
        "average_score": round(sum(scores) / len(scores), 4) if scores else None,
        "average_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
        "model_calls": sum(int(row.get("model_calls") or 0) for row in rows),
        "total_tokens": sum(total_tokens) if total_tokens else None,
        "evidence_supported_rate": (
            round(sum(_fold(row.get("evidence_status")) in {"supported", "partial", "complete"} for row in evidence_rows) / len(evidence_rows), 4)
            if evidence_rows else None
        ),
    }


class EvaluationStore:
    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or getattr(get_settings(), "evaluation_store_path", None)
        self.path = Path(configured) if configured else Path(__file__).parent / "data" / "evaluations.json"
        self._lock = RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "runs": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvaluationError(f"评测记录无法读取：{exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            raise EvaluationError("评测记录格式无效")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="evaluations-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def save(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            data.setdefault("schema_version", 1)
            data["runs"] = [run, *data.get("runs", [])][:50]
            self._write(data)
        return copy.deepcopy(run)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            summaries = []
            for run in self._read().get("runs", [])[:max(1, min(limit, 50))]:
                summaries.append({
                    "run_id": run.get("run_id"),
                    "name": run.get("name"),
                    "status": run.get("status"),
                    "created_at": run.get("created_at"),
                    "completed_at": run.get("completed_at"),
                    "agent_ids": copy.deepcopy(run.get("agent_ids") or []),
                    "summary": copy.deepcopy(run.get("summary") or {}),
                    "result_count": len(run.get("results") or []),
                })
            return summaries

    def get(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            for run in self._read().get("runs", []):
                if str(run.get("run_id")) == str(run_id):
                    return copy.deepcopy(run)
        raise EvaluationError(f"评测记录不存在：{run_id}")


def run_evaluation(request: EvaluationRunRequest) -> dict[str, Any]:
    available = {item["app_id"]: item for item in list_evaluation_agents()}
    agents: list[dict[str, Any]] = []
    for app_id in request.agent_ids:
        agent = available.get(app_id)
        if agent is None:
            raise EvaluationError(f"Agent 不存在或未发布：{app_id}")
        if agent.get("status") != "ready":
            raise EvaluationError(f"Agent 当前不可评测：{agent.get('name') or app_id}：{agent.get('error') or '配置无效'}")
        agents.append(agent)

    jobs = [(agent, case) for agent in agents for case in request.cases]
    rows: list[dict[str, Any]] = []
    worker_count = max(1, min(request.max_parallel, len(jobs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(_run_one, agent, case): (agent["app_id"], _case_id(case)) for agent, case in jobs}
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda row: (agents.index(next(item for item in agents if item["app_id"] == row["agent_id"])), row["case_id"]))

    aggregates = [_aggregate(agent, [row for row in rows if row["agent_id"] == agent["app_id"]]) for agent in agents]
    ranked = sorted(
        aggregates,
        key=lambda item: (
            item["average_score"] is not None,
            item["average_score"] if item["average_score"] is not None else -1,
            -(item["average_duration_ms"] or 0),
        ),
        reverse=True,
    )
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    score_rows = [row for row in rows if row.get("score") is not None]
    run = {
        "run_id": f"eval-{uuid4().hex[:12]}",
        "name": request.name,
        "status": "success" if any(row.get("status") == "success" for row in rows) else "error",
        "created_at": _now(),
        "completed_at": _now(),
        "max_parallel": request.max_parallel,
        "agent_ids": [agent["app_id"] for agent in agents],
        "agents": agents,
        "cases": [case.model_dump(mode="json") for case in request.cases],
        "summary": {
            "agent_count": len(agents),
            "case_count": len(request.cases),
            "total_runs": len(rows),
            "scored_runs": len(score_rows),
            "passed_runs": sum(row.get("passed") is True for row in score_rows),
            "pass_rate": round(sum(row.get("passed") is True for row in score_rows) / len(score_rows), 4) if score_rows else None,
            "rankings": ranked,
        },
        "results": rows,
    }
    return get_evaluation_store().save(run)


@lru_cache(maxsize=1)
def get_evaluation_store() -> EvaluationStore:
    return EvaluationStore()
