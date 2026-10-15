"""Query-driven relevance filtering for graph relations.

The graph is useful for locating an object and a small set of related facts,
but an entity often has many generic ``包含``/``组成`` edges.  Those edges are
not evidence that the current question is about them.  This module keeps the
filter deterministic and explainable: query attributes are matched against
the two endpoints, the relation label, and the relation's source excerpt.
The original RAGFlow chunk remains the answer evidence.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.graph.provenance import decode_graph_property


_NORMALIZE_RE = re.compile(r"[\s_\-_/\\,:，。；;、（）()【】\[\]<>《》]+")
_GENERIC_RELATION_TYPES = {
    "包含",
    "组成",
    "包括",
    "拥有",
    "涉及",
    "关联",
    "contains",
    "has",
    "includes",
}

# Model-produced attribute keys are deliberately kept as aliases rather than
# hard-coded Cypher or a second model call.  This lets an English key from the
# semantic router still locate Chinese graph labels.
_ATTRIBUTE_ALIASES: dict[str, tuple[str, ...]] = {
    "communication_interface": ("通信接口", "通讯接口"),
    "communication_parameters": ("通信参数", "通讯参数", "波特率"),
    "input_type": ("输入类型",),
    "output_type": ("输出类型",),
    "input_range": ("输入量程", "输入范围"),
    "output_range": ("输出量程", "输出范围"),
    "wiring": ("接线", "接线方式", "端子"),
    "terminal": ("端子", "接线端子"),
    "接线": ("接线方式", "端子", "接线端子"),
    "接法": ("接线", "接线方式", "端子"),
    "接入": ("接线", "接线方式", "端子"),
    "二线制": ("接线", "端子"),
    "三线制": ("接线", "端子"),
    "四线制": ("接线", "端子"),
}


def _normalized(value: Any) -> str:
    return _NORMALIZE_RE.sub("", str(value or "").casefold())


def _append_unique(result: list[str], seen: set[str], value: Any, *, limit: int) -> None:
    text = str(value or "").strip()
    if not text:
        return
    key = _normalized(text)
    if not key or key in seen:
        return
    seen.add(key)
    result.append(text[:80])
    if len(result) >= limit:
        return


def _unique_terms(values: Iterable[Any] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        key = _normalized(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        result.append(text[:80])
    return result


def expand_graph_attribute_terms(values: Iterable[Any] | None, *, limit: int = 40) -> list[str]:
    """Return safe graph terms plus known bilingual aliases.

    The returned values are still plain parameters.  They are never inserted
    into Cypher text.
    """

    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        _append_unique(result, seen, value, limit=limit)
        for alias in _ATTRIBUTE_ALIASES.get(str(value or "").strip().casefold(), ()):
            _append_unique(result, seen, alias, limit=limit)
        if len(result) >= limit:
            break
    return result[:limit]


def _decoded_text(value: Any, *, depth: int = 0) -> str:
    """Extract searchable text from native or JSON-encoded Neo4j values."""

    if depth > 2 or value is None:
        return ""
    value = decode_graph_property(value)
    if isinstance(value, dict):
        return " ".join(
            _decoded_text(item, depth=depth + 1)
            for item in value.values()
        )[:6000]
    if isinstance(value, (list, tuple, set)):
        return " ".join(
            _decoded_text(item, depth=depth + 1)
            for item in value
        )[:6000]
    return str(value)


def _node_text(node: dict[str, Any]) -> str:
    preferred = (
        node.get("name"),
        node.get("id"),
        node.get("code"),
        node.get("entity_type"),
        node.get("model"),
    )
    return " ".join(str(item or "") for item in preferred)


def _relation_evidence_text(relation: dict[str, Any]) -> str:
    values = [
        relation.get("excerpt"),
        relation.get("source_excerpt"),
        relation.get("evidence_records"),
        relation.get("evidence"),
        relation.get("attribute"),
        relation.get("attribute_key"),
        relation.get("value"),
        relation.get("target"),
        relation.get("target_value"),
        relation.get("unit"),
    ]
    return " ".join(_decoded_text(item) for item in values if item not in (None, ""))[:12000]


def score_graph_relation(
    left: dict[str, Any],
    right: dict[str, Any],
    relation: dict[str, Any],
    *,
    attribute_terms: Iterable[Any] | None = None,
    relation_types: Iterable[Any] | None = None,
) -> dict[str, Any] | None:
    """Score one relation for the current question.

    ``None`` means the edge has no query-relevant match and should not be
    displayed for an attribute-focused graph lookup.  With no terms the
    function keeps the legacy behavior and returns a neutral score.
    """

    raw_terms = _unique_terms(attribute_terms)
    raw_term_keys = {_normalized(item) for item in raw_terms}
    terms = expand_graph_attribute_terms(raw_terms)
    normalized_terms = [
        (_normalized(item), str(item), _normalized(item) not in raw_term_keys)
        for item in terms
    ]
    requested_relation_types = [
        _normalized(item)
        for item in relation_types or []
        if _normalized(item)
    ]

    relation_type = str(relation.get("type") or relation.get("relation") or "关联")
    normalized_relation_type = _normalized(relation_type)
    relation_type_matches_requested = bool(requested_relation_types) and any(
        item in normalized_relation_type for item in requested_relation_types
    )
    # Relation labels are ranking hints.  When the question also carries
    # concrete values such as Pt100/三线制, a useful edge may legitimately be
    # labelled ``支持`` or ``包含`` instead of ``接线``.  Only a relation-only
    # lookup is gated by relation_types.
    if requested_relation_types and not relation_type_matches_requested and not normalized_terms:
        return None

    endpoint_text = _normalized(f"{_node_text(left)} {_node_text(right)}")
    relation_text = _normalized(relation_type)
    evidence_text = _normalized(_relation_evidence_text(relation))

    matched_terms: list[str] = []
    endpoint_matches: list[str] = []
    relation_matches: list[str] = []
    evidence_matches: list[str] = []
    primary_matches = 0
    alias_matches = 0
    primary_endpoint = False
    alias_endpoint = False
    primary_relation = False
    alias_relation = False
    primary_evidence = False
    alias_evidence = False
    for normalized_term, original_term, is_alias in normalized_terms:
        if not normalized_term:
            continue
        if normalized_term in endpoint_text:
            endpoint_matches.append(original_term)
            if is_alias:
                alias_endpoint = True
            else:
                primary_endpoint = True
        if normalized_term in relation_text:
            relation_matches.append(original_term)
            if is_alias:
                alias_relation = True
            else:
                primary_relation = True
        if normalized_term in evidence_text:
            evidence_matches.append(original_term)
            if is_alias:
                alias_evidence = True
            else:
                primary_evidence = True
        if normalized_term in endpoint_text or normalized_term in relation_text or normalized_term in evidence_text:
            matched_terms.append(original_term)
            if is_alias:
                alias_matches += 1
            else:
                primary_matches += 1

    if normalized_terms and not matched_terms:
        return None

    # A chunk-level excerpt can contain the whole page and mention many
    # attributes.  It is therefore supporting context, not an edge match by
    # itself.  At least one endpoint or the relation label must match the
    # requested attribute; the original document connector remains responsible
    # for returning the excerpt as answer evidence.
    if normalized_terms and not endpoint_matches and not relation_matches:
        return None

    generic_relation = normalized_relation_type in _GENERIC_RELATION_TYPES
    # A generic composition edge is retained only when the requested value is
    # found on an endpoint.  Chunk-level evidence is intentionally not enough:
    # one extracted page can mention many attributes and would otherwise make
    # every generic edge appear relevant.  This is the key
    # difference between "M1304 has many parts" and "M1304's Pt100 wiring".
    if normalized_terms and generic_relation and not endpoint_matches:
        return None

    coverage = min(
        1.0,
        (primary_matches + 0.35 * alias_matches) / max(1, len(raw_terms)),
    )
    endpoint_quality = 1.0 if primary_endpoint else (0.55 if alias_endpoint else 0.0)
    relation_quality = 1.0 if primary_relation else (0.55 if alias_relation else 0.0)
    evidence_quality = 1.0 if primary_evidence else (0.55 if alias_evidence else 0.0)
    score = (
        0.50 * coverage
        + 0.25 * endpoint_quality
        + 0.15 * relation_quality
        + 0.10 * evidence_quality
    )
    if not normalized_terms and requested_relation_types:
        score = 0.75
    elif not normalized_terms:
        score = 0.0

    return {
        "matched": True,
        "score": round(min(1.0, score), 6),
        "matched_terms": list(dict.fromkeys(matched_terms)),
        "endpoint_matches": list(dict.fromkeys(endpoint_matches)),
        "relation_matches": list(dict.fromkeys(relation_matches)),
        "requested_relation_match": relation_type_matches_requested,
        "evidence_matches": list(dict.fromkeys(evidence_matches)),
        "direct_match": bool(endpoint_matches or relation_matches),
        "evidence_match": bool(evidence_matches),
        "relation_type": relation_type,
        "support_role": "direct" if endpoint_matches or relation_matches else "source_evidence",
    }


def rank_graph_records(
    records: Iterable[dict[str, Any]],
    *,
    attribute_terms: Iterable[Any] | None = None,
    relation_types: Iterable[Any] | None = None,
    limit: int = 50,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Filter and rank graph records, returning the explanation per record."""

    ranked: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        left = record.get("left") or {}
        right = record.get("right") or {}
        relation = record.get("relation") or {}
        if not all(isinstance(item, dict) for item in (left, right, relation)):
            continue
        relevance = score_graph_relation(
            left,
            right,
            relation,
            attribute_terms=attribute_terms,
            relation_types=relation_types,
        )
        if relevance is None:
            continue
        ranked.append((index, record, relevance))

    ranked.sort(
        key=lambda item: (
            float(item[2].get("score") or 0),
            bool(item[2].get("direct_match")),
            bool(item[2].get("evidence_match")),
            -item[0],
        ),
        reverse=True,
    )
    return [(record, relevance) for _, record, relevance in ranked[: max(1, int(limit))]]
