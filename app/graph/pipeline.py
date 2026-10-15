"""RAGFlow Chunk -> Qwen 实体关系抽取流水线。"""

from __future__ import annotations

import hashlib
from typing import Any

from app.agents.claims import _invoke_json
from app.agents.model import create_model_for_stage


EXTRACTION_SYSTEM_PROMPT = """你是企业制造业知识图谱抽取器。
只根据用户提供的文档 Chunk 抽取明确出现的实体和关系，禁止补充常识或猜测。
必须只输出严格 JSON 对象，不要 Markdown 或说明文字，格式如下：
{
  "entities": [{"name":"实体名称","type":"Device|Component|Alarm|Fault|SOP|Case|WorkOrder|Document|Other"}],
  "relations": [{"source":"源实体名称","type":"关系名称","target":"目标实体名称","evidence_ids":["E-1"],"confidence":0.0}]
}
关系方向必须遵守文档原意；无法确认方向或没有证据的关系不要输出。
"""


def _stable_id(entity_type: str, name: str) -> str:
    digest = hashlib.sha256(f"{entity_type}|{name}".encode("utf-8")).hexdigest()[:20]
    return f"{entity_type.lower()}:{digest}"


def _evidence_id(chunk: dict[str, Any], index: int) -> str:
    value = str(chunk.get("chunk_id") or "").strip()
    return value or f"E-{index + 1}"


def _chunk_context(chunks: list[dict[str, Any]]) -> tuple[str, dict[str, dict[str, Any]]]:
    evidence: dict[str, dict[str, Any]] = {}
    parts: list[str] = []
    for index, chunk in enumerate(chunks):
        evidence_id = _evidence_id(chunk, index)
        evidence[evidence_id] = chunk
        parts.append(
            f"[{evidence_id}] 文档={chunk.get('document_name') or '未知'} "
            f"页码={chunk.get('page') if chunk.get('page') is not None else '未知'}\n"
            f"{str(chunk.get('content') or '')[:12000]}"
        )
    return "\n\n---\n\n".join(parts), evidence


def _chunk_evidence_record(evidence_id: str, chunk: dict[str, Any]) -> dict[str, Any]:
    """Persist the original RAGFlow provenance needed by a graph edge."""

    dataset_id = str(chunk.get("dataset_id") or "").strip() or None
    document_id = str(chunk.get("document_id") or "").strip() or None
    return {
        "evidence_id": evidence_id,
        "document_id": document_id,
        "dataset_id": dataset_id,
        "document_name": chunk.get("document_name"),
        "page": chunk.get("page"),
        "chunk_id": chunk.get("chunk_id"),
        "version": chunk.get("version"),
        "excerpt": str(chunk.get("content") or "").strip()[:2000],
        "url": (
            f"/source-documents/{dataset_id}/{document_id}"
            if dataset_id and document_id
            else None
        ),
    }


def extract_graph_from_chunks(chunks: list[dict[str, Any]], tenant_id: str | None = None) -> dict[str, Any]:
    """调用 Qwen 生成可审核的 Neo4j 导入草稿，不执行写入。"""
    if not chunks:
        raise ValueError("没有可抽取的 Chunk")
    context, evidence = _chunk_context(chunks)
    payload = _invoke_json(
        create_model_for_stage(
            "graph_extraction",
            query=context[:4000],
        ),
        EXTRACTION_SYSTEM_PROMPT,
        f"待处理文档 Chunk：\n{context}",
    )
    raw_entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
    raw_relations = payload.get("relations") if isinstance(payload.get("relations"), list) else []
    entities: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for item in raw_entities:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        entity_type = str(item.get("type") or "Other").strip() or "Other"
        if not name:
            continue
        key = name.casefold()
        node = by_name.get(key)
        if node is None:
            node = {
                "id": _stable_id(entity_type, name),
                "name": name,
                "type": entity_type,
                "tenant_id": tenant_id,
                "properties": {"source": "ragflow_qwen_extraction"},
            }
            by_name[key] = node
            entities.append(node)

    relations: list[dict[str, Any]] = []
    skipped_relations = 0
    seen_relations: set[tuple[str, str, str]] = set()
    for item in raw_relations:
        if not isinstance(item, dict):
            continue
        source_name = str(item.get("source") or item.get("from") or "").strip()
        target_name = str(item.get("target") or item.get("to") or "").strip()
        relation_type = str(item.get("type") or item.get("relation") or "").strip()
        source = by_name.get(source_name.casefold())
        target = by_name.get(target_name.casefold())
        if not source or not target or not relation_type:
            continue
        key = (source["id"], relation_type, target["id"])
        if key in seen_relations:
            continue
        seen_relations.add(key)
        evidence_ids = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
        evidence_entries = [
            (str(value), evidence[str(value)])
            for value in evidence_ids
            if str(value) in evidence
        ]
        source_chunks = [chunk for _, chunk in evidence_entries]
        if not source_chunks:
            # Do not attach an edge to an arbitrary first Chunk.  A graph
            # relation without an explicit source is not auditable and can
            # make an unrelated page look like its evidence.
            skipped_relations += 1
            continue
        first = source_chunks[0]
        evidence_records = [
            _chunk_evidence_record(evidence_id, chunk)
            for evidence_id, chunk in evidence_entries
        ]
        evidence_props = {
            "source_document": first.get("document_name"),
            "document_id": first.get("document_id"),
            "dataset_id": first.get("dataset_id"),
            "page": first.get("page"),
            "chunk_id": first.get("chunk_id"),
            "version": first.get("version"),
            "confidence": float(item.get("confidence") or 0.0),
            "evidence_ids": [evidence_id for evidence_id, _ in evidence_entries],
            "evidence_records": evidence_records,
            "review_status": "pending",
        }
        relations.append({
            "source_id": source["id"],
            "target_id": target["id"],
            "type": relation_type,
            "properties": evidence_props,
        })
    return {
        "entities": entities,
        "relations": relations,
        "source_chunks": len(chunks),
        "skipped_relations_without_evidence": skipped_relations,
        "model_calls": int(payload.get("_model_calls") or 1),
        "model_usage": payload.get("_model_usage") or {},
    }
