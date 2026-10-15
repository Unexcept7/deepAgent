"""Deterministic routing-policy evaluation and semantic-route vocabulary.

The policy center remains a safe, explainable fallback and configuration
boundary.  The runtime semantic router may classify the current request, but
it cannot bypass the published route catalog, source permissions or evidence
requirements represented here.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.agents.evidence import extract_aspects, extract_identifiers, extract_technical_subjects
from app.dictionary_store import dictionary_fault_code_match
from app.routing_policy_store import RoutingPolicyStore, get_routing_policy_store


FAULT_CODE_PATTERN = re.compile(
    r"(?:故障码|错误码|报警码|故障|错误|报警)\s*[:：]?\s*[A-Z]{1,4}[-_ ]?\d{2,5}"
    r"|(?<![A-Za-z0-9])E[-_ ]?\d{2,5}(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def detected_entity_types(query: str) -> list[str]:
    text = str(query or "")
    detected: list[str] = []

    def add(value: str) -> None:
        if value not in detected:
            detected.append(value)

    if extract_identifiers(text):
        add("device_model")
    try:
        configured_fault_code = dictionary_fault_code_match(text)
    except Exception:
        configured_fault_code = None
    if (configured_fault_code is None and FAULT_CODE_PATTERN.search(text)) or configured_fault_code is True:
        add("fault_code")
    if extract_technical_subjects(text):
        add("technical_subject")
    if extract_aspects(text):
        add("aspect")
    if any(term in text for term in ("设备", "模块", "传感器", "网关", "北辰")):
        add("device")
    return detected


def _normalize(value: str) -> str:
    return str(value or "").strip().casefold()


def _match_policy(policy: dict[str, Any], query: str, entity_types: Iterable[str]) -> tuple[bool, list[str]]:
    match = policy.get("match") or {}
    normalized = _normalize(query)
    keywords = [str(item).strip() for item in match.get("keywords") or [] if str(item).strip()]
    entities = {str(item).strip() for item in entity_types if str(item).strip()}
    requested_entities = {str(item).strip() for item in match.get("entity_types") or [] if str(item).strip()}
    mode = str(match.get("mode") or "any").lower()
    reasons: list[str] = []

    keyword_hits = [item for item in keywords if _normalize(item) in normalized]
    if keywords:
        keyword_ok = len(keyword_hits) == len(keywords) if mode == "all" else bool(keyword_hits)
        if not keyword_ok:
            return False, []
        reasons.append("关键词：" + "、".join(keyword_hits))
    if requested_entities:
        entity_hits = sorted(requested_entities & entities)
        entity_ok = len(entity_hits) == len(requested_entities) if mode == "all" else bool(entity_hits)
        if not entity_ok:
            return False, []
        reasons.append("实体：" + "、".join(entity_hits))
    if not keywords and not requested_entities:
        return False, []
    return True, reasons


def preview_routing_policies(
    query: str,
    *,
    workflow_id: str | None = None,
    store: RoutingPolicyStore | None = None,
) -> dict[str, Any]:
    policy_store = store or get_routing_policy_store()
    entities = detected_entity_types(query)
    candidates: list[dict[str, Any]] = []
    for policy in policy_store.published():
        matched, reasons = _match_policy(policy, query, entities)
        if not matched:
            continue
        candidates.append({
            "policy_id": policy.get("policy_id") or policy.get("id"),
            "name": policy.get("name"),
            "category": policy.get("category"),
            "priority": int(policy.get("priority") or 0),
            "version": policy.get("version"),
            "reasons": reasons,
            "action": policy.get("action") or {},
        })
    candidates.sort(key=lambda item: (-int(item.get("priority") or 0), str(item.get("policy_id") or "")))
    selected = candidates[0] if candidates else None
    return {
        "query": query,
        "workflow_id": workflow_id,
        "detected_entity_types": entities,
        "matched_count": len(candidates),
        "candidates": candidates,
        "selected": selected,
        "route": (selected or {}).get("action", {}).get("route") if selected else "unknown",
    }


def evaluate_routing_policy(
    query: str,
    *,
    workflow_id: str | None = None,
    store: RoutingPolicyStore | None = None,
) -> dict[str, Any]:
    preview = preview_routing_policies(query, workflow_id=workflow_id, store=store)
    selected = preview.get("selected") or {}
    return {
        "policy_id": selected.get("policy_id"),
        "policy_name": selected.get("name"),
        "category": selected.get("category"),
        "version": selected.get("version"),
        "route": selected.get("action", {}).get("route") or "unknown",
        "reasons": selected.get("reasons") or [],
        "actions": selected.get("action") or {},
        "detected_entity_types": preview.get("detected_entity_types") or [],
        "matched_count": preview.get("matched_count", 0),
    }


def routing_policy_catalog(
    *,
    store: RoutingPolicyStore | None = None,
) -> list[dict[str, Any]]:
    """Expose published policy semantics as LLM hints, not executable authority.

    The semantic router needs to know the vocabulary that administrators use
    in the policy center.  Actions are deliberately omitted here: source
    selection, permissions and confirmation remain backend decisions.
    """
    policy_store = store or get_routing_policy_store()
    catalog: list[dict[str, Any]] = []
    for policy in policy_store.published():
        match = policy.get("match") or {}
        catalog.append({
            "policy_id": policy.get("policy_id") or policy.get("id"),
            "name": policy.get("name"),
            "category": policy.get("category"),
            "priority": int(policy.get("priority") or 0),
            "route": (policy.get("action") or {}).get("route") or "unknown",
            "keywords": list(match.get("keywords") or []),
            "entity_types": list(match.get("entity_types") or []),
            "examples": list(match.get("examples") or []),
            "description": policy.get("description") or "",
        })
    return sorted(
        catalog,
        key=lambda item: (-int(item.get("priority") or 0), str(item.get("policy_id") or "")),
    )
