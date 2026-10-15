"""Runtime query planning shared by the workbench and direct search APIs.

The semantic router is only allowed to propose entities and retrieval terms.
``build_safe_retrieval_plan`` remains the backend boundary that converts those
proposals into parameterized graph/document queries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.agents.conversation_context import build_semantic_context
from app.agents.semantic_router import run_semantic_router
from app.config import get_settings
from app.model_registry import plan_model_routing
from app.routing_policy_engine import routing_policy_catalog
from app.search.models import RetrievalPlan
from app.search.query_plan import build_safe_retrieval_plan
from app.semantic_prompt_store import get_semantic_prompt_store


logger = logging.getLogger(__name__)


@dataclass
class SearchPlanResult:
    """Safe retrieval plan plus user-visible diagnostics."""

    plan: RetrievalPlan
    status: str
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _semantic_model_profile_id(query: str) -> str | None:
    settings = get_settings()
    if not bool(getattr(settings, "model_routing_enabled", False)):
        return None
    try:
        routing = plan_model_routing(query)
    except Exception as exc:  # pragma: no cover - optional registry fallback
        logger.warning("Semantic model routing unavailable: %s", exc)
        return None
    return str(
        ((routing.get("stages") or {}).get("semantic_router") or {}).get("profile_id")
        or ""
    ).strip() or None


def _entity_diagnostics(prediction: dict[str, Any]) -> list[dict[str, Any]]:
    entities = prediction.get("entities") if isinstance(prediction, dict) else []
    if not isinstance(entities, list):
        return []
    result: list[dict[str, Any]] = []
    for item in entities:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        normalized = str(item.get("normalized_value") or value).strip()
        if not value and not normalized:
            continue
        result.append({
            "entity_type": str(item.get("entity_type") or ""),
            "value": value,
            "normalized_value": normalized,
            "resolved_id": str(item.get("resolved_id") or "") or None,
            "confidence": item.get("confidence", 0),
            "source": str(item.get("source") or ""),
        })
    return result


def plan_search_query(query: str) -> SearchPlanResult:
    """Build the same semantic-to-safe plan used by the workbench.

    This function is deliberately best-effort. A model or Prompt outage must
    not prevent document search; the deterministic builder still extracts
    explicit product identifiers and never sends the full natural-language
    question to Neo4j as an entity value.
    """

    text = str(query or "").strip()
    warnings: list[str] = []
    prediction: dict[str, Any] = {}
    semantic_summary: dict[str, Any] = {"status": "fallback"}
    route_catalog: list[dict[str, Any]] = []
    try:
        route_catalog = routing_policy_catalog()
    except Exception as exc:  # pragma: no cover - policy catalog is a hint
        logger.warning("Routing policy catalog unavailable: %s", exc)

    try:
        semantic_context = build_semantic_context(
            [],
            text,
            route_catalog=route_catalog,
        )
    except Exception as exc:  # pragma: no cover - dictionary hints are optional
        logger.warning("Semantic context unavailable: %s", exc)
        semantic_context = {"prompt_context": {}}
        warnings.append("语义上下文构建失败，已使用确定性实体提取")

    try:
        prompt = get_semantic_prompt_store().active()
    except Exception as exc:  # pragma: no cover - optional prompt store
        prompt = None
        warnings.append(f"语义路由 Prompt 读取失败，已使用确定性实体提取：{exc}")

    if prompt:
        try:
            semantic_result = run_semantic_router(
                text,
                prompt,
                context=(semantic_context.get("prompt_context") or {}),
                route_catalog=route_catalog,
                model_profile_id=_semantic_model_profile_id(text),
            )
            semantic_summary = {
                "status": "success" if not semantic_result.get("error") and semantic_result.get("result") else "fallback",
                "prompt_id": semantic_result.get("prompt_id"),
                "version": semantic_result.get("version"),
                "model": semantic_result.get("model"),
                "model_profile_id": semantic_result.get("model_profile_id"),
                "duration_ms": semantic_result.get("duration_ms"),
                "error": semantic_result.get("error"),
            }
            if semantic_result.get("error"):
                warnings.append(f"语义路由未通过校验，已使用确定性实体提取：{semantic_result['error']}")
            elif isinstance(semantic_result.get("result"), dict):
                prediction = semantic_result["result"]
            else:
                warnings.append("语义路由未返回有效计划，已使用确定性实体提取")
        except Exception as exc:  # pragma: no cover - model connectivity fallback
            logger.warning("Unified search semantic planner failed: %s", exc)
            semantic_summary["error"] = str(exc)
            warnings.append(f"语义路由不可用，已使用确定性实体提取：{exc}")
    else:
        semantic_summary["status"] = "disabled"
        warnings.append("语义路由 Prompt 未启用，已使用确定性实体提取")

    plan = build_safe_retrieval_plan(
        text,
        {
            "entities": prediction.get("entities") or [],
            "intents": prediction.get("intents") or [],
            "retrieval_plan": prediction.get("retrieval_plan") or {},
        },
    )
    diagnostics = {
        "planner": "workbench_semantic_router",
        "status": semantic_summary.get("status") or "fallback",
        "retrieval_query": text,
        "semantic": semantic_summary,
        "entities": _entity_diagnostics(prediction),
        "needs_clarification": bool(prediction.get("needs_clarification")),
        "ambiguity": str(prediction.get("ambiguity") or ""),
        "graph_queries": list(plan.graph_queries),
        "document_queries": list(plan.document_queries),
        "structured_queries": [item.model_dump(mode="json") for item in plan.structured_queries],
        "attribute_keys": list(plan.attribute_keys),
        "relation_types": list(plan.relation_types),
    }
    return SearchPlanResult(
        plan=plan,
        status=str(semantic_summary.get("status") or "fallback"),
        warnings=warnings,
        diagnostics=diagnostics,
    )
