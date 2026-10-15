"""把语义路由结果收敛为安全的只读检索计划。"""

from __future__ import annotations

import re
from typing import Any

from app.agents.evidence import extract_identifiers
from app.search.models import RetrievalPlan, StructuredQueryPlan


_GRAPH_VALUE_PATTERNS = (
    (re.compile(r"(?i)\bpt[\s_-]*1000\b"), "Pt1000"),
    (re.compile(r"(?i)\bpt[\s_-]*100\b"), "Pt100"),
    (re.compile(r"(?i)\bcu[\s_-]*50\b"), "Cu50"),
)
_GRAPH_ATTRIBUTE_TERMS = ("二线制", "三线制", "四线制", "热电阻", "热电偶", "接线", "接法", "端子")


def _clean_values(values: Any, *, limit: int, max_length: int) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        text = text[:max_length]
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _safe_structured_queries(values: Any) -> list[StructuredQueryPlan]:
    """Accept only the logical structured-query contract from the router."""

    if not isinstance(values, list):
        return []
    result: list[StructuredQueryPlan] = []
    for value in values[:8]:
        try:
            result.append(StructuredQueryPlan.model_validate(value))
        except Exception:
            # A malformed structured proposal must not block document/graph
            # retrieval. The SQL connector will use its safe fallback policy.
            continue
    return result


def _infer_graph_attribute_terms(query: str) -> list[str]:
    """Recover exact values that a router may omit from ``attribute_keys``."""

    text = str(query or "")
    result: list[str] = []
    for pattern, canonical in _GRAPH_VALUE_PATTERNS:
        if pattern.search(text):
            result.append(canonical)
    lowered = text.casefold()
    for term in _GRAPH_ATTRIBUTE_TERMS:
        if term.casefold() in lowered:
            result.append(term)
    return result


def _infer_graph_relation_types(query: str) -> list[str]:
    text = str(query or "")
    if any(term in text for term in ("接线", "接法", "接入", "端子", "线制")):
        return ["接线", "接线方式", "端子连接"]
    return []


def build_safe_retrieval_plan(
    query: str,
    prediction: dict[str, Any] | None = None,
) -> RetrievalPlan:
    """从模型结果构建后端可执行的检索计划。

    图谱查询只接受模型已经识别出的实体值或 resolved_id；模型自行写入
    的任意查询表达式不会被直接传入图数据库。文档查询保留意图查询，
    并始终保留原始问题作为兜底，避免模型漏掉用户的属性要求。
    """

    prediction = prediction if isinstance(prediction, dict) else {}
    raw_plan = prediction.get("retrieval_plan")
    raw_plan = raw_plan if isinstance(raw_plan, dict) else {}
    raw_graph_queries = raw_plan.get("graph_queries")
    raw_document_queries = raw_plan.get("document_queries")
    raw_document_queries = raw_document_queries if isinstance(raw_document_queries, list) else []
    structured_queries = _safe_structured_queries(raw_plan.get("structured_queries"))

    entities = prediction.get("entities")
    entities = entities if isinstance(entities, list) else []
    entity_values: list[str] = []
    entity_ids: list[str] = []
    for item in entities:
        if not isinstance(item, dict):
            continue
        for value in (
            item.get("normalized_value"),
            item.get("value"),
        ):
            text = str(value or "").strip()
            if text:
                entity_values.append(text)
                break
        resolved_id = str(item.get("resolved_id") or "").strip()
        if resolved_id:
            entity_ids.append(resolved_id)

    # A graph query is a parameter, never a Cypher fragment. Only values that
    # the model identified as entities or resolved IDs are accepted here.
    # When the semantic router is unavailable, keep a deterministic fallback
    # for product-like identifiers. Passing the full natural-language question
    # to the graph connector would make an exact entity lookup almost always
    # return zero results.
    lexical_entity_values = extract_identifiers(query)
    allowed_graph_values = {
        value.casefold()
        for value in [*entity_values, *entity_ids]
        if value
    }
    graph_queries: list[str] = []
    for value in _clean_values(raw_graph_queries, limit=8, max_length=240):
        if value.casefold() in allowed_graph_values:
            graph_queries.append(value)
    if not graph_queries:
        graph_queries = _clean_values(
            [*entity_values, *entity_ids, *lexical_entity_values],
            limit=8,
            max_length=240,
        )

    intents = prediction.get("intents")
    intents = intents if isinstance(intents, list) else []
    intent_queries = [
        item.get("query")
        for item in intents
        if isinstance(item, dict)
    ]
    document_queries = _clean_values(
        [
            query,
            *raw_document_queries,
            *intent_queries,
        ],
        limit=8,
        max_length=1000,
    )

    attribute_keys = _clean_values(
        [
            *_clean_values(raw_plan.get("attribute_keys"), limit=20, max_length=80),
            *_infer_graph_attribute_terms(query),
        ],
        limit=20,
        max_length=80,
    )
    relation_types = _clean_values(
        [
            *(_clean_values(raw_plan.get("relation_types"), limit=12, max_length=80)),
            *_infer_graph_relation_types(query),
        ],
        limit=12,
        max_length=80,
    )
    return RetrievalPlan(
        graph_queries=graph_queries,
        document_queries=document_queries or [str(query or "").strip()[:1000]],
        structured_queries=structured_queries,
        entity_ids=_clean_values(
            entity_ids,
            limit=8,
            max_length=160,
        ),
        attribute_keys=attribute_keys,
        relation_types=relation_types,
    )
