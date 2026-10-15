"""Normalize graph relation provenance without replacing document evidence.

Neo4j stores relation properties as flat values.  The graph connector may
therefore receive either native lists/dicts (test doubles and some drivers)
or JSON strings (the production write path).  This module keeps that storage
detail out of the Agent and UI layers and gives every graph edge one stable,
client-facing provenance shape.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


_EVIDENCE_FIELDS = (
    "evidence_id",
    "document_id",
    "dataset_id",
    "document_name",
    "page",
    "chunk_id",
    "version",
    "excerpt",
    "url",
    "score",
)


def decode_graph_property(value: Any) -> Any:
    """Decode a Neo4j property that may have been JSON-serialized."""

    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{\"":
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def _as_list(value: Any) -> list[Any]:
    value = decode_graph_property(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def graph_relation_id(
    left: dict[str, Any],
    right: dict[str, Any],
    relation: dict[str, Any],
) -> str:
    """Return a stable identifier for one directed graph relation."""

    raw = "|".join((
        str(left.get("id") or left.get("name") or ""),
        str(relation.get("type") or relation.get("relation") or ""),
        str(right.get("id") or right.get("name") or ""),
    ))
    return f"graph:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _record_from_relation(
    relation: dict[str, Any],
    evidence_ids: list[str],
) -> dict[str, Any] | None:
    """Build one legacy provenance record from flat relation properties."""

    document_name = (
        relation.get("document_name")
        or relation.get("source_document")
        or relation.get("document")
    )
    record: dict[str, Any] = {
        "evidence_id": evidence_ids[0] if evidence_ids else None,
        "document_id": relation.get("document_id"),
        "dataset_id": relation.get("dataset_id"),
        "document_name": document_name,
        "page": relation.get("page"),
        "chunk_id": relation.get("chunk_id"),
        "version": relation.get("version"),
        "excerpt": relation.get("excerpt") or relation.get("source_excerpt"),
        "url": relation.get("url"),
        "score": relation.get("score"),
    }
    if not any(value not in (None, "", []) for value in record.values()):
        return None
    return record


def normalize_graph_relation_evidence(
    left: dict[str, Any],
    right: dict[str, Any],
    relation: dict[str, Any],
    *,
    relation_id: str | None = None,
) -> dict[str, Any]:
    """Return a graph edge plus its original-document provenance.

    ``evidence_records`` is preferred for newly imported edges.  Older edges
    may only have ``source_document``/``page``/``chunk_id``; those are kept as
    a visible partial binding instead of being silently treated as a raw
    document excerpt.
    """

    relation = relation or {}
    evidence_ids = []
    for value in _as_list(relation.get("evidence_ids")):
        item = _text(value)
        if item and item not in evidence_ids:
            evidence_ids.append(item)

    raw_records = (
        relation.get("evidence_records")
        if relation.get("evidence_records") is not None
        else relation.get("evidence")
    )
    records: list[dict[str, Any]] = []
    for index, raw in enumerate(_as_list(raw_records)):
        if not isinstance(raw, dict):
            continue
        record = dict(raw)
        record["document_name"] = (
            record.get("document_name")
            or record.get("source_document")
            or record.get("document")
        )
        record["excerpt"] = record.get("excerpt") or record.get("source_excerpt") or record.get("content")
        if not record.get("evidence_id") and index < len(evidence_ids):
            record["evidence_id"] = evidence_ids[index]
        if record.get("evidence_id"):
            evidence_id = _text(record["evidence_id"])
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        compact = {
            key: record.get(key)
            for key in _EVIDENCE_FIELDS
            if record.get(key) not in (None, "", [])
        }
        if compact:
            records.append(compact)

    if not records:
        legacy = _record_from_relation(relation, evidence_ids)
        if legacy:
            records.append({
                key: value
                for key, value in legacy.items()
                if value not in (None, "", [])
            })

    for record in records:
        dataset_id = _text(record.get("dataset_id"))
        document_id = _text(record.get("document_id"))
        if not record.get("url") and dataset_id and document_id:
            record["url"] = f"/source-documents/{dataset_id}/{document_id}"

    has_bound_source = any(
        any(record.get(key) not in (None, "", []) for key in (
            "document_id", "dataset_id", "document_name", "page", "chunk_id", "excerpt"
        ))
        for record in records
    )
    if records and has_bound_source and evidence_ids:
        evidence_status = "bound"
    elif records or evidence_ids:
        evidence_status = "partial"
    else:
        evidence_status = "unverified"

    confidence = _number(relation.get("confidence"))
    return {
        "relation_id": relation_id or graph_relation_id(left, right, relation),
        "source_entity": str(left.get("name") or left.get("id") or "未知实体"),
        "source_id": _text(left.get("id")),
        "relation": str(relation.get("type") or relation.get("relation") or "关联"),
        "target_entity": str(right.get("name") or right.get("id") or "未知实体"),
        "target_id": _text(right.get("id")),
        "confidence": confidence,
        "evidence_ids": evidence_ids,
        "evidence": records,
        "evidence_status": evidence_status,
    }

