import copy
import hashlib
import json
import logging
import re
from typing import Any

import requests
from langchain_core.tools import tool

from app.agents.evidence import (
    assess_evidence,
    find_similar_identifiers,
    normalize_evidence_mode,
)
from app.config import get_settings
from app.observability import current_trace_recorder
from app.tool_center.context import execution_context


logger = logging.getLogger(__name__)
_RERANK_DOCUMENT_INDEX_RE = re.compile(
    r"(?:document\s+index|index)\s*[:=]\s*(\d+)",
    re.IGNORECASE,
)


def _sha256_text(value: Any) -> str:
    """Return a stable, non-reversible identifier for diagnostic logs."""

    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()


def _log_preview(value: Any, limit: int = 160) -> str:
    """Keep diagnostic previews bounded and on one log line."""

    return " ".join(str(value or "").split())[:limit]


def _safe_identifier(value: Any) -> str | None:
    """Show enough of an identifier to correlate logs without dumping secrets."""

    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= 8:
        return text
    return f"{text[:3]}...{text[-4:]}"


def _rerank_document_index(error: BaseException) -> int | None:
    match = _RERANK_DOCUMENT_INDEX_RE.search(str(error))
    return int(match.group(1)) if match else None


class RAGFlowClient:
    """Minimal client for RAGFlow dataset discovery and chunk retrieval."""

    def __init__(self, settings=None, requester=None) -> None:
        settings = settings or get_settings()
        self.requester = requester or requests
        self.base_url = settings.ragflow_base_url.rstrip("/")
        self.api_key = settings.ragflow_api_key or ""
        self.configured_ids = settings.configured_dataset_ids()
        self.auto_discover = settings.ragflow_auto_discover_datasets
        self.evidence_mode = normalize_evidence_mode(
            getattr(settings, "evidence_mode", "generic")
        )
        self.evidence_min_similarity = getattr(settings, "evidence_min_similarity", 0.2)
        self.evidence_generic_min_similarity = getattr(
            settings,
            "evidence_generic_min_similarity",
            0.3,
        )
        self.rerank_id = str(getattr(settings, "ragflow_rerank_id", "") or "").strip()
        # Keep lightweight test doubles and older integrations unchanged when
        # they do not expose the new budget setting.  Production Settings
        # defaults to 4 and can be overridden through RAGFLOW_TOP_K.
        self.max_top_k = max(1, int(getattr(settings, "ragflow_top_k", 20)))
        # This is the RAGFlow-side recall/rerank pool, not the final page size.
        # Keep it configurable so the qwen reranker remains enabled without
        # silently sending an unnecessarily large candidate window.
        self.retrieval_top_k = max(
            1,
            min(1024, int(getattr(settings, "ragflow_retrieval_top_k", 24))),
        )
        self.chunk_max_chars = max(
            500,
            int(getattr(settings, "ragflow_chunk_max_chars", 2200)),
        )
        self.catalog_timeout_seconds = max(
            1,
            int(getattr(settings, "ragflow_catalog_timeout_seconds", 5)),
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def list_datasets(self) -> list[dict[str, str]]:
        self._ensure_configured()
        datasets: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        page = 1
        page_size = 100  # RAGFlow 限制 page_size 最大为 100。

        while True:
            response = self.requester.get(
                f"{self.base_url}/api/v1/datasets",
                headers=self.headers,
                params={"page": page, "page_size": page_size},
                timeout=self.catalog_timeout_seconds,
            )
            if response.status_code == 401:
                raise RuntimeError("RAGFlow API Key 无效或已过期，请在 RAGFlow 中重新生成 API Key")
            response.raise_for_status()
            body = response.json()
            self._ensure_success(body)
            data = body.get("data") or []
            if isinstance(data, dict):
                data = data.get("datasets") or data.get("items") or []
            if not isinstance(data, list):
                break

            new_count = 0
            for item in data:
                if not isinstance(item, dict):
                    continue
                dataset_id = str(item.get("id", ""))
                if not dataset_id or dataset_id in seen_ids:
                    continue
                seen_ids.add(dataset_id)
                new_count += 1
                datasets.append({
                    "id": dataset_id,
                    "name": str(item.get("name", "未命名知识库")),
                })

            if len(data) < page_size or new_count == 0:
                break
            page += 1

        return datasets

    def list_configured_datasets(self) -> list[dict[str, str]]:
        """Return only datasets explicitly bound to this application."""
        if not self.configured_ids:
            return []

        dataset_map = {
            item["id"]: item
            for item in self.list_datasets()
        }
        return [
            dataset_map[dataset_id]
            for dataset_id in self.configured_ids
            if dataset_id in dataset_map
        ]

    def search(self, query: str, top_k: int = 8) -> dict[str, Any]:
        self._ensure_configured()
        top_k = max(1, min(int(top_k), self.max_top_k))
        dataset_ids = list(self.configured_ids)
        # 配置了绑定 ID 后严格限制检索范围；自动发现只作为无绑定 ID 时的兜底。
        if not dataset_ids and self.auto_discover:
            datasets = self.list_datasets()
            dataset_ids = [item["id"] for item in datasets]
        dataset_ids = list(dict.fromkeys(dataset_ids))
        if not dataset_ids:
            raise RuntimeError("RAGFlow 中没有可检索的知识库")

        retrieval_top_k = max(top_k, self.retrieval_top_k)
        payload = {
            "question": query,
            "dataset_ids": dataset_ids,
            "page": 1,
            # page_size 是最终返回给 DeepAgent 的 Top-N；top_k 是 RAGFlow
            # 召回并送入重排模型的内部候选池。两者必须分开配置。
            "page_size": top_k,
            "top_k": retrieval_top_k,
            # 不在 RAGFlow API 层用相似度做硬过滤。相似度只负责候选排序，
            # 由本地证据层根据内容、实体、主题和属性底线决定是否保留。
            "similarity_threshold": 0.0,
            "vector_similarity_weight": 0.3,
            "keyword": True,
            "highlight": False,
        }
        if self.rerank_id:
            payload["rerank_id"] = self.rerank_id

        request_log = self._retrieval_log_context(query, payload, dataset_ids)
        logger.info(
            "RAGFlow retrieval request: %s",
            json.dumps(request_log, ensure_ascii=False, sort_keys=True),
        )
        response = None
        body: dict[str, Any] | None = None
        try:
            response = self.requester.post(
                f"{self.base_url}/api/v1/retrieval",
                headers=self.headers,
                json=payload,
                timeout=60,
            )
            if response.status_code == 401:
                raise RuntimeError("RAGFlow API Key 无效或已过期，请在 RAGFlow 中重新生成 API Key")
            try:
                body = response.json()
            except Exception:
                body = None
            response.raise_for_status()
            if not isinstance(body, dict):
                raise RuntimeError("RAGFlow 返回格式错误：响应不是 JSON 对象")
            self._ensure_success(body)
        except Exception as exc:
            failure_log = {
                **request_log,
                "error_type": type(exc).__name__,
                "error": _log_preview(exc, 1200),
                "http_status": getattr(response, "status_code", None),
                "response_code": body.get("code") if isinstance(body, dict) else None,
                "response_message": _log_preview(body.get("message"), 600)
                if isinstance(body, dict) and body.get("message")
                else None,
            }
            logger.warning(
                "RAGFlow retrieval failed: %s",
                json.dumps(failure_log, ensure_ascii=False, sort_keys=True),
            )
            error_for_index = " ".join(
                part
                for part in (
                    str(exc),
                    str(body.get("message") or "") if isinstance(body, dict) else "",
                )
                if part
            )
            rerank_index = _rerank_document_index(RuntimeError(error_for_index))
            if rerank_index is not None and self.rerank_id:
                self._probe_rerank_candidate(
                    query=query,
                    dataset_ids=dataset_ids,
                    payload=payload,
                    target_index=rerank_index,
                )
            raise

        data = body.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        chunks = data.get("chunks") or []
        if not isinstance(chunks, list):
            chunks = []
        try:
            reported_total = int(data.get("total"))
        except (TypeError, ValueError):
            reported_total = len(chunks)
        logger.info(
            "RAGFlow retrieval response: %s",
            json.dumps(
                {
                    **request_log,
                    "http_status": getattr(response, "status_code", None),
                    "response_code": body.get("code"),
                    "total": reported_total,
                    "returned_chunks": len(chunks),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        candidates = []
        for item in chunks:
            if not isinstance(item, dict):
                continue
            try:
                similarity = round(float(item.get("similarity") or 0), 4)
            except (TypeError, ValueError):
                similarity = 0.0
            candidates.append({
                "content": item.get("content", ""),
                "chunk_id": str(item.get("chunk_id") or item.get("id") or "") or None,
                "document_name": (
                    item.get("document_keyword")
                    or item.get("document_name")
                    or item.get("docnm_kwd")
                    or item.get("name")
                    or item.get("document_id", "未知文档")
                ),
                "document_id": item.get("document_id"),
                "dataset_id": item.get("dataset_id"),
                "page": self._extract_page(item.get("positions"), item),
                "positions": item.get("positions"),
                "score": similarity,
            })
        assessment = assess_evidence(
            query,
            candidates,
            min_similarity=self.evidence_min_similarity,
            generic_min_similarity=self.evidence_generic_min_similarity,
            mode=self.evidence_mode,
        )
        similar_entities = find_similar_identifiers(query, candidates)
        # Keep evidence assessment based on the complete retrieved text, but
        # send only bounded chunk content to Qwen and the client.  This avoids
        # repeated long ToolMessages dominating the input-token bill.
        compact_chunks = []
        for chunk in assessment["chunks"]:
            compact = dict(chunk)
            compact["content"] = str(compact.get("content") or "")[: self.chunk_max_chars]
            compact_chunks.append(compact)
        assessment["chunks"] = compact_chunks
        return {
            "query": query,
            "dataset_ids": dataset_ids,
            "rerank_id": self.rerank_id or None,
            "evidence_status": assessment["status"],
            "evidence_reason": assessment["reason"],
            "candidate_count": assessment["candidate_count"],
            "verified_count": assessment["verified_count"],
            "provided_count": assessment.get("provided_count", len(assessment["chunks"])),
            "rejected_count": assessment["rejected_count"],
            "requested_entities": assessment["requested_entities"],
            "covered_entities": assessment["covered_entities"],
            "missing_entities": assessment["missing_entities"],
            "similar_entities": similar_entities,
            "requested_subjects": assessment["requested_subjects"],
            "covered_subjects": assessment["covered_subjects"],
            "missing_subjects": assessment["missing_subjects"],
            "requested_aspects": assessment["requested_aspects"],
            "covered_aspects": assessment["covered_aspects"],
            "missing_aspects": assessment["missing_aspects"],
            "missing_entity_aspects": assessment["missing_entity_aspects"],
            "missing_requirements": assessment["missing_requirements"],
            "coverage_limited": assessment["coverage_limited"],
            "conflicts": assessment["conflicts"],
            "rejection_reasons": assessment["rejection_reasons"],
            "validation_mode": assessment.get("validation_mode", "industrial_strict"),
            "non_blocking": bool(assessment.get("non_blocking", False)),
            "warnings": assessment.get("warnings", []),
            "chunks": assessment["chunks"],
            "total": reported_total if reported_total >= len(candidates) else len(candidates),
        }

    @staticmethod
    def _retrieval_log_context(
        query: str,
        payload: dict[str, Any],
        dataset_ids: list[str],
    ) -> dict[str, Any]:
        """Build bounded diagnostics without logging API keys or chunk bodies."""

        rerank_id = payload.get("rerank_id")
        return {
            "query_sha256": _sha256_text(query),
            "query_chars": len(str(query or "")),
            "query_preview": _log_preview(query),
            "dataset_count": len(dataset_ids),
            "dataset_ids_sha256": _sha256_text("\n".join(dataset_ids)),
            "page_size": payload.get("page_size"),
            "retrieval_top_k": payload.get("top_k"),
            "similarity_threshold": payload.get("similarity_threshold"),
            "vector_similarity_weight": payload.get("vector_similarity_weight"),
            "keyword": payload.get("keyword"),
            "rerank_id": _safe_identifier(rerank_id),
            "rerank_id_sha256": _sha256_text(rerank_id) if rerank_id else None,
        }

    def _probe_rerank_candidate(
        self,
        *,
        query: str,
        dataset_ids: list[str],
        payload: dict[str, Any],
        target_index: int,
    ) -> None:
        """Probe the failing candidate index without changing the real result.

        RAGFlow builds the internal ``input.documents`` list before it calls the
        configured reranker, so the client cannot inspect that exact request.
        A one-off no-rerank retrieval gives us the closest observable candidate
        metadata.  It is intentionally bounded and logs lengths/hashes only,
        never the chunk body.
        """

        probe_size = min(
            100,
            max(
                target_index + 1,
                int(payload.get("top_k") or 1),
                int(payload.get("page_size") or 1),
            ),
        )
        probe_payload = dict(payload)
        probe_payload.pop("rerank_id", None)
        probe_payload["page_size"] = probe_size
        probe_payload["top_k"] = probe_size
        probe_log = {
            **self._retrieval_log_context(query, probe_payload, dataset_ids),
            "diagnostic": "rerank_candidate_probe_without_rerank",
            "target_index": target_index,
            "probe_size": probe_size,
        }
        logger.warning(
            "RAGFlow rerank candidate probe start: %s",
            json.dumps(probe_log, ensure_ascii=False, sort_keys=True),
        )
        try:
            response = self.requester.post(
                f"{self.base_url}/api/v1/retrieval",
                headers=self.headers,
                json=probe_payload,
                timeout=60,
            )
            if response.status_code == 401:
                raise RuntimeError("RAGFlow API Key 无效或已过期")
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError("响应不是 JSON 对象")
            self._ensure_success(body)
            data = body.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            chunks = data.get("chunks") or []
            if not isinstance(chunks, list):
                chunks = []
            candidate = chunks[target_index] if target_index < len(chunks) else None
            candidate_log = {
                **probe_log,
                "http_status": response.status_code,
                "returned_chunks": len(chunks),
                "candidate_present": candidate is not None,
                "candidate": self._candidate_log_metadata(target_index, candidate),
            }
            logger.warning(
                "RAGFlow rerank candidate probe result: %s",
                json.dumps(candidate_log, ensure_ascii=False, sort_keys=True),
            )
        except Exception as exc:
            logger.warning(
                "RAGFlow rerank candidate probe failed: %s",
                json.dumps(
                    {
                        **probe_log,
                        "error_type": type(exc).__name__,
                        "error": _log_preview(exc, 800),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

    @classmethod
    def _candidate_log_metadata(
        cls,
        index: int,
        candidate: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            return {"index": index, "candidate_present": False}
        content = str(candidate.get("content") or "")
        return {
            "index": index,
            "candidate_present": True,
            "content_chars": len(content),
            "content_utf8_bytes": len(content.encode("utf-8", errors="replace")),
            "content_sha256": _sha256_text(content),
            "document_name": (
                candidate.get("document_keyword")
                or candidate.get("document_name")
                or candidate.get("docnm_kwd")
                or candidate.get("name")
                or candidate.get("document_id")
            ),
            "document_id": candidate.get("document_id"),
            "dataset_id": candidate.get("dataset_id"),
            "chunk_id": candidate.get("chunk_id") or candidate.get("id"),
            "page": cls._extract_page(candidate.get("positions"), candidate),
        }

    def list_documents(self, dataset_id: str, page_size: int = 100) -> list[dict[str, Any]]:
        """读取指定 RAGFlow 知识库的文档目录，供图谱导入页面选择来源。"""
        self._ensure_configured()
        dataset_id = str(dataset_id or "").strip()
        if not dataset_id:
            raise ValueError("dataset_id 不能为空")
        documents: list[dict[str, Any]] = []
        page = 1
        page_size = max(1, min(int(page_size), 100))
        while True:
            response = self.requester.get(
                f"{self.base_url}/api/v1/datasets/{dataset_id}/documents",
                headers=self.headers,
                params={"page": page, "page_size": page_size},
                timeout=self.catalog_timeout_seconds,
            )
            if response.status_code == 401:
                raise RuntimeError("RAGFlow API Key 无效或已过期")
            response.raise_for_status()
            body = response.json()
            self._ensure_success(body)
            data = body.get("data") or []
            if isinstance(data, dict):
                # RAGFlow's current API returns documents under ``data.docs``.
                # Older deployments used ``documents`` or ``items``; keep all
                # variants so the catalog does not appear empty after an
                # upgrade.
                data = data.get("docs") or data.get("documents") or data.get("items") or []
            if not isinstance(data, list):
                break
            for item in data:
                if not isinstance(item, dict):
                    continue
                doc_id = str(item.get("id") or item.get("document_id") or "").strip()
                if not doc_id:
                    continue
                documents.append({
                    "id": doc_id,
                    "name": str(item.get("name") or item.get("document_name") or item.get("docnm_kwd") or doc_id),
                    # ``run`` is the parsing state (e.g. DONE/FAIL), while
                    # RAGFlow's numeric ``status`` is only the file flag.
                    # Prefer the human-readable parsing state in the UI.
                    "status": str(item.get("run") or item.get("status") or ""),
                    "chunk_count": item.get("chunk_count") or item.get("chunk_num"),
                    "updated_at": item.get("updated_at") or item.get("update_time"),
                })
            if len(data) < page_size:
                break
            page += 1
        return documents

    def list_document_chunks(self, dataset_id: str, document_id: str, page_size: int = 100) -> list[dict[str, Any]]:
        """读取 RAGFlow 文档 Chunk，保留页码和原始元数据。"""
        self._ensure_configured()
        dataset_id = str(dataset_id or "").strip()
        document_id = str(document_id or "").strip()
        if not dataset_id or not document_id:
            raise ValueError("dataset_id 和 document_id 不能为空")
        chunks: list[dict[str, Any]] = []
        page = 1
        page_size = max(1, min(int(page_size), 100))
        while True:
            response = self.requester.get(
                f"{self.base_url}/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks",
                headers=self.headers,
                params={"page": page, "page_size": page_size},
                timeout=60,
            )
            if response.status_code == 401:
                raise RuntimeError("RAGFlow API Key 无效或已过期")
            response.raise_for_status()
            body = response.json()
            self._ensure_success(body)
            data = body.get("data") or []
            if isinstance(data, dict):
                data = data.get("chunks") or data.get("items") or []
            if not isinstance(data, list):
                break
            for item in data:
                if not isinstance(item, dict) or not str(item.get("content") or "").strip():
                    continue
                chunks.append({
                    "content": str(item.get("content") or ""),
                    "chunk_id": str(item.get("id") or item.get("chunk_id") or "") or None,
                    "document_id": document_id,
                    "document_name": str(item.get("document_name") or item.get("docnm_kwd") or document_id),
                    "dataset_id": dataset_id,
                    "page": self._extract_page(item.get("positions"), item),
                    "version": str(item.get("version") or "") or None,
                    "metadata": item,
                })
            if len(data) < page_size:
                break
            page += 1
        return chunks

    def open_document(self, dataset_id: str, document_id: str) -> requests.Response:
        """Open a RAGFlow source document stream without exposing the API key."""
        self._ensure_configured()
        response = self.requester.get(
            f"{self.base_url}/api/v1/datasets/{dataset_id}/documents/{document_id}",
            headers=self.headers,
            stream=True,
            timeout=60,
        )
        if response.status_code == 401:
            response.close()
            raise RuntimeError("RAGFlow API Key 无效或已过期")
        response.raise_for_status()
        return response

    def _ensure_configured(self) -> None:
        if not self.base_url or not self.api_key:
            raise RuntimeError("请配置 RAGFLOW_BASE_URL 和 RAGFLOW_API_KEY")

    @staticmethod
    def _ensure_success(body: Any) -> None:
        if not isinstance(body, dict):
            raise RuntimeError("RAGFlow 返回格式错误：响应不是 JSON 对象")
        if body.get("code", 0) != 0:
            raise RuntimeError(body.get("message", "RAGFlow 返回错误"))

    @staticmethod
    def _coerce_page(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 1:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            page = int(value.strip())
            return page if page >= 1 else None
        return None

    @classmethod
    def _extract_page(cls, positions: Any, metadata: dict[str, Any] | None = None) -> int | None:
        """Normalize the page supplied by different RAGFlow deployments.

        The client never guesses a page from chunk order. It only accepts an
        explicit page field or the documented first coordinate in positions,
        so an unknown page remains ``None`` instead of becoming a misleading
        citation.
        """
        metadata = metadata if isinstance(metadata, dict) else {}
        for key in ("page", "page_num", "page_number"):
            page = cls._coerce_page(metadata.get(key))
            if page is not None:
                return page
        nested = metadata.get("metadata")
        if isinstance(nested, dict):
            for key in ("page", "page_num", "page_number"):
                page = cls._coerce_page(nested.get(key))
                if page is not None:
                    return page
        if isinstance(positions, dict):
            for key in ("page", "page_num", "page_number"):
                page = cls._coerce_page(positions.get(key))
                if page is not None:
                    return page
        if isinstance(positions, list) and positions:
            first = positions[0]
            if isinstance(first, dict):
                return cls._extract_page(first)
            value = first[0] if isinstance(first, (list, tuple)) and first else first
            return cls._coerce_page(value)
        return None


def _runtime_ragflow_clients() -> list[tuple[str, RAGFlowClient]]:
    """Build RAGFlow clients scoped to the current published application.

    Direct ``search_knowledge_base`` calls are kept for compatibility with
    older workflows, but an explicit application binding must also constrain
    this path.  The unified-search path applies the same scope in
    ``managed_connectors``.
    """

    context = execution_context.get()
    if context is None or context.knowledge_binding_mode != "explicit":
        return [("default", RAGFlowClient())]

    from app.tool_center.service import ToolCenterService

    service = ToolCenterService()
    clients: list[tuple[str, RAGFlowClient]] = []
    for definition in service.store.list(context.workspace_id, "connections"):
        if definition.get("type") != "ragflow":
            continue
        connection_id = str(definition.get("connection_id") or "")
        if not definition.get("enabled") or not definition.get("published_version"):
            continue
        if context.connection_ids is not None and connection_id not in context.connection_ids:
            continue
        if str(definition.get("environment") or "production") != context.environment:
            continue
        allowed_ids = (context.knowledge_dataset_ids or {}).get(connection_id)
        if not allowed_ids:
            continue
        try:
            connection = service.store.runtime(context.workspace_id, "connections", connection_id)
        except Exception:
            continue
        connection = copy.deepcopy(connection)
        connection["connection_id"] = connection_id
        connection["environment"] = definition.get("environment", context.environment)
        connection["config"] = {
            **(connection.get("config") or {}),
            "dataset_ids": list(allowed_ids),
            "auto_discover": False,
        }
        clients.append((connection_id, service.ragflow_client(context.workspace_id, connection)))
    if not clients:
        raise RuntimeError("当前应用没有可用的已发布 RAGFlow 知识库绑定")
    return clients


def _merge_scoped_results(results: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """Merge results from multiple explicitly bound RAGFlow connections."""

    if len(results) == 1:
        return results[0]
    merged = dict(results[0])
    merged["query"] = query
    merged["dataset_ids"] = list(dict.fromkeys(
        dataset_id
        for item in results
        for dataset_id in item.get("dataset_ids") or []
    ))
    list_fields = {
        "chunks", "similar_entities", "covered_entities", "missing_entities",
        "requested_subjects", "covered_subjects", "missing_subjects",
        "requested_aspects", "covered_aspects", "missing_aspects",
        "missing_entity_aspects", "missing_requirements", "conflicts",
        "warnings", "rejection_reasons",
    }
    for field in list_fields:
        values = []
        for item in results:
            value = item.get(field)
            if isinstance(value, dict):
                values.extend(value.items())
            elif isinstance(value, list):
                values.extend(value)
        if field == "rejection_reasons":
            counts: dict[str, int] = {}
            for key, value in values:
                counts[str(key)] = counts.get(str(key), 0) + int(value or 0)
            merged[field] = counts
        else:
            merged[field] = values
    for field in ("candidate_count", "verified_count", "provided_count", "rejected_count", "total"):
        merged[field] = sum(int(item.get(field) or 0) for item in results)
    merged["evidence_reason"] = "；".join(
        str(item.get("evidence_reason"))
        for item in results
        if item.get("evidence_reason")
    )
    merged["non_blocking"] = all(bool(item.get("non_blocking")) for item in results)
    return merged


@tool
def search_knowledge_base(query: str, top_k: int = 8) -> str:
    """检索绑定的 RAGFlow 知识库并返回候选片段及证据状态。"""
    started_at = __import__("time").perf_counter()
    trace = current_trace_recorder()
    if not str(query or "").strip():
        result = {
            "guarded": True,
            "error": "检索 query 不能为空",
            "query": "",
            "chunks": [],
            "total": 0,
        }
    else:
        try:
            scoped = _runtime_ragflow_clients()
            result = _merge_scoped_results(
                [client.search(query=query, top_k=max(1, min(top_k, 20))) for _, client in scoped],
                query,
            )
        except Exception as exc:
            result = {"error": str(exc), "query": query, "chunks": [], "total": 0}
    if trace is not None:
        trace.record_data_source(
            source_type="document",
            source_name="RAGFlow 知识库",
            duration_ms=round((__import__("time").perf_counter() - started_at) * 1000),
            status="error" if result.get("error") else "success",
            count=len(result.get("chunks") or []),
            error=result.get("error"),
            tool_name="search_knowledge_base",
            attributes={
                "query": str(result.get("query") or query)[:240],
                "top_k": max(1, min(int(top_k), 20)),
                "dataset_ids": result.get("dataset_ids") or [],
            },
        )
    return json.dumps(result, ensure_ascii=False)


@tool
def list_knowledge_bases() -> str:
    """列出 RAGFlow 中当前可用的全部知识库及其 ID。"""
    started_at = __import__("time").perf_counter()
    trace = current_trace_recorder()
    try:
        context = execution_context.get()
        clients = _runtime_ragflow_clients()
        if context is not None and context.knowledge_binding_mode == "explicit":
            datasets = [
                dataset
                for _, client in clients
                for dataset in client.list_configured_datasets()
            ]
        else:
            datasets = clients[0][1].list_datasets()
        result = {"datasets": datasets, "total": len(datasets)}
    except Exception as exc:
        result = {"error": str(exc), "datasets": [], "total": 0}
    if trace is not None:
        trace.record_data_source(
            source_type="document",
            source_name="RAGFlow 知识库目录",
            duration_ms=round((__import__("time").perf_counter() - started_at) * 1000),
            status="error" if result.get("error") else "success",
            count=len(result.get("datasets") or []),
            error=result.get("error"),
            tool_name="list_knowledge_bases",
            attributes={"operation": "list_datasets"},
        )
    return json.dumps(result, ensure_ascii=False)


RAGFLOW_TOOLS = [search_knowledge_base, list_knowledge_bases]
