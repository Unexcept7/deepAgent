"""统一搜索连接器。

连接器是只读检索适配器，不执行写入。所有写入动作仍应通过 MCP/API
工具并走风险策略。未配置的连接器会返回 ``available=False``，不会让
整个应用启动失败，这对企业分阶段接入尤其重要。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

import requests

from app.agents.evidence import extract_identifiers
from app.config import get_settings
from app.graph.neo4j_store import GraphStoreError, Neo4jGraphStore
from app.graph.provenance import (
    graph_relation_id,
    normalize_graph_relation_evidence,
)
from app.graph.relevance import expand_graph_attribute_terms, rank_graph_records
from app.search.models import SearchHit, SearchRequest
from app.search.structured_query import (
    normalize_sql_hits,
    sql_request_payload,
)
from app.tools.ragflow import RAGFlowClient


_QA_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)


def _qa_terms(value: Any) -> set[str]:
    """Tokenize mixed Chinese/technical text without a heavyweight segmenter."""

    terms: set[str] = set()
    for token in _QA_TOKEN_RE.findall(str(value or "").casefold()):
        if token.isascii():
            terms.add(token)
        elif len(token) > 1:
            terms.update(token[index:index + 2] for index in range(len(token) - 1))
    return terms


class SearchConnector(ABC):
    source_type: str
    name: str

    @property
    @abstractmethod
    def available(self) -> bool:
        """连接器是否已配置并可调用。"""

    @abstractmethod
    def search(self, request: SearchRequest) -> list[SearchHit]:
        """执行只读检索。"""


class RAGFlowSearchConnector(SearchConnector):
    source_type = "document"
    name = "RAGFlow 文档知识库"

    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = str(settings.ragflow_base_url or "").strip()
        self._api_key = str(settings.ragflow_api_key or "").strip()

    @property
    def available(self) -> bool:
        return bool(self._base_url and self._api_key)

    def search(self, request: SearchRequest) -> list[SearchHit]:
        hits: list[SearchHit] = []
        plan = request.query_plan
        raw_queries = (
            plan.document_queries
            if plan and plan.document_queries
            else [request.query]
        )
        queries = list(dict.fromkeys(
            str(query).strip()
            for query in raw_queries
            if str(query).strip()
        )) or [request.query]
        client = RAGFlowClient()
        for query in queries:
            payload = client.search(query, top_k=request.top_k)
            for item in payload.get("chunks") or []:
                if not isinstance(item, dict):
                    continue
                dataset_id = str(item.get("dataset_id") or "") or None
                document_id = str(item.get("document_id") or "") or None
                evidence_id = str(item.get("evidence_id") or "")
                if not evidence_id:
                    evidence_id = f"document:{dataset_id or 'unknown'}:{document_id or item.get('document_name', 'unknown')}:{item.get('page') or 'na'}"
                hits.append(SearchHit(
                    source_type="document",
                    source_name=self.name,
                    title=str(item.get("document_name") or "未知文档"),
                    content=str(item.get("content") or ""),
                    score=float(item.get("score") or 0),
                    authority=0.8,
                    freshness=0.8,
                    entity_match=0.7 if query else 0,
                    document_id=document_id,
                    dataset_id=dataset_id,
                    page=item.get("page"),
                    evidence_id=evidence_id,
                    url=(
                        f"/source-documents/{dataset_id}/{document_id}"
                        if dataset_id and document_id else None
                    ),
                    metadata={
                        "validation_mode": payload.get("validation_mode"),
                        "retrieval_query": query,
                    },
                ))
        return hits


class QAFileConnector(SearchConnector):
    """可选的本地 QA JSON 连接器，便于没有独立 QA 服务时落地标准问答。

    文件格式为 ``[{"question": "...", "answer": "...", "version": "R1"}]``。
    它不是生产级全文检索实现；企业接入数据库或搜索引擎时只需替换该连接器。
    """

    source_type = "qa"
    name = "标准 QA 问答库"

    def __init__(self) -> None:
        self.path = str(get_settings().qa_data_path or "").strip()

    @property
    def available(self) -> bool:
        from pathlib import Path

        return bool(self.path and Path(self.path).is_file())

    def search(self, request: SearchRequest) -> list[SearchHit]:
        from pathlib import Path

        try:
            records = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"QA 数据读取失败：{exc}") from exc
        query_terms = _qa_terms(request.query)
        hits: list[SearchHit] = []
        for index, item in enumerate(records if isinstance(records, list) else []):
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "")
            answer = str(item.get("answer") or "")
            haystack = _qa_terms(f"{question} {answer}")
            overlap = sum(1 for term in query_terms if term and term in haystack)
            if not overlap:
                continue
            score = min(1.0, overlap / max(1, len(query_terms)))
            hits.append(SearchHit(
                source_type="qa",
                source_name=self.name,
                title=question or f"QA-{index + 1}",
                content=answer,
                score=score,
                authority=float(item.get("authority", 0.95)),
                freshness=float(item.get("freshness", 0.9)),
                entity_match=score,
                version=str(item.get("version") or "") or None,
                evidence_id=f"qa:{index + 1}",
                metadata={"question": question, "scope": item.get("scope")},
            ))
        return hits


class Neo4jSearchConnector(SearchConnector):
    """可选 Neo4j 图谱连接器。

    通过参数化 Cypher 查询实体和关系，禁止模型直接提交任意 Cypher。
    未安装 neo4j 驱动或未配置密码时保持不可用。
    """

    source_type = "graph"
    name = "Neo4j 企业知识图谱"

    def __init__(self) -> None:
        settings = get_settings()
        self.store = Neo4jGraphStore(settings)

    @property
    def available(self) -> bool:
        return self.store.configured

    def search(self, request: SearchRequest) -> list[SearchHit]:
        plan = request.query_plan
        if plan is not None:
            # An explicit plan with no graph query means that the semantic
            # planner did not identify a safe graph entity. Never turn the
            # whole natural-language question into an exact entity lookup.
            queries = list(plan.graph_queries or plan.entity_ids)
        else:
            # Keep direct connector callers safe as well: only product-like
            # identifiers are valid fallbacks for exact neighbor search.
            queries = extract_identifiers(request.query)
        attribute_keys = plan.attribute_keys if plan else []
        attribute_terms = expand_graph_attribute_terms(
            attribute_keys
        )
        relation_types = plan.relation_types if plan else []
        candidate_limit = min(
            50,
            max(request.top_k, request.top_k * 4)
            if attribute_terms or relation_types
            else request.top_k,
        )
        hits: list[SearchHit] = []
        for graph_query in queries:
            try:
                records = self.store.search_entity_neighbors(
                    graph_query,
                    limit=candidate_limit,
                    filters=request.filters,
                    attribute_terms=attribute_terms,
                    relation_types=relation_types,
                )
            except GraphStoreError:
                raise
            ranked_records = rank_graph_records(
                records,
                attribute_terms=attribute_keys,
                relation_types=relation_types,
                limit=request.top_k,
            )
            for item, relevance in ranked_records:
                left = item["left"]
                right = item["right"]
                relation = item["relation"]
                relation_type = str(relation.get("type") or "关联")
                content = (
                    f"{left.get('name') or left.get('id') or '未知实体'} "
                    f"-[{relation_type}]-> "
                    f"{right.get('name') or right.get('id') or '未知实体'}"
                )
                relation_id = graph_relation_id(left, right, relation)
                graph_evidence = normalize_graph_relation_evidence(
                    left,
                    right,
                    relation,
                    relation_id=relation_id,
                )
                primary_evidence = (graph_evidence.get("evidence") or [{}])[0]
                hits.append(SearchHit(
                    source_type="graph",
                    source_name=self.name,
                    title=str(left.get("name") or left.get("id") or "图谱关系"),
                    content=content,
                    score=float(relevance.get("score") or 0.85),
                    authority=0.9,
                    freshness=0.8,
                    entity_match=1.0,
                    document_id=str(primary_evidence.get("document_id") or "") or None,
                    dataset_id=str(primary_evidence.get("dataset_id") or "") or None,
                    version=str(primary_evidence.get("version") or "") or None,
                    page=int(primary_evidence["page"]) if str(primary_evidence.get("page") or "").isdigit() else None,
                    evidence_id=relation_id,
                    url=str(primary_evidence.get("url") or "") or None,
                    metadata={
                        "graph_query": graph_query,
                        "left": left,
                        "right": right,
                        "relation": relation,
                        "graph_relation": graph_evidence,
                        "graph_relevance": relevance,
                    },
                ))
        return hits


class BusinessSearchConnector(SearchConnector):
    """统一业务搜索 API 适配器。

    企业可在网关后提供一个只读 ``POST /search`` 接口，返回标准化或常见
    ``items`` 数组。这样 MES、ERP、CMMS 可以先由企业 API 聚合，后续再拆
    成独立连接器而不影响 Agent。
    """

    source_type = "api"
    name = "企业业务统一搜索 API"

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = str(settings.business_api_base_url or "").strip().rstrip("/")
        self.token = str(settings.business_api_token or "").strip()
        self.timeout = max(1, int(settings.business_api_timeout_seconds))

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    def search(self, request: SearchRequest) -> list[SearchHit]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = requests.post(
            f"{self.base_url}/search",
            headers=headers,
            json={"query": request.query, "filters": request.filters, "top_k": request.top_k},
            timeout=min(self.timeout, max(1, request.timeout_ms // 1000)),
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else payload
        hits: list[SearchHit] = []
        for index, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            hits.append(SearchHit(
                source_type="api",
                source_name=str(item.get("source_name") or self.name),
                title=str(item.get("title") or item.get("name") or "业务记录"),
                content=str(item.get("content") or item.get("summary") or json.dumps(item, ensure_ascii=False)),
                score=float(item.get("score") or 0.8),
                authority=float(item.get("authority") or 0.9),
                freshness=float(item.get("freshness") or 0.9),
                entity_match=float(item.get("entity_match") or 0.8),
                document_id=str(item.get("id") or "") or None,
                version=str(item.get("version") or "") or None,
                updated_at=item.get("updated_at"),
                evidence_id=str(item.get("evidence_id") or f"api:{index + 1}"),
                url=str(item.get("url") or "") or None,
                metadata=item,
            ))
        return hits


class SQLSearchConnector(SearchConnector):
    """受控 SQL 搜索适配器。

    这里不接受模型生成的 SQL，而是调用企业侧已经做过字段白名单、
    参数化和只读限制的 ``SQL_SEARCH_URL`` 服务。该边界能避免 Agent
    直接接触生产数据库账号。
    """

    source_type = "sql"
    name = "企业结构化数据搜索"

    def __init__(self) -> None:
        settings = get_settings()
        self.url = str(settings.sql_search_url or "").strip()
        self.token = str(
            getattr(settings, "sql_search_token", None)
            or settings.business_api_token
            or ""
        ).strip()
        self.timeout = max(
            1,
            int(getattr(settings, "sql_search_timeout_seconds", 8)),
        )
        self.max_rows = max(1, min(200, int(getattr(settings, "sql_search_max_rows", 100))))

    @property
    def available(self) -> bool:
        return bool(self.url)

    def search(self, request: SearchRequest) -> list[SearchHit]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        plans = list(request.query_plan.structured_queries) if request.query_plan else []
        payload = sql_request_payload(request, plans, max_rows=self.max_rows)
        response = requests.post(
            self.url,
            headers=headers,
            json=payload,
            timeout=min(self.timeout, max(1, request.timeout_ms // 1000)),
        )
        response.raise_for_status()
        return normalize_sql_hits(
            response.json(),
            source_name=self.name,
            evidence_prefix="sql",
        )


class MCPReadSearchConnector(SearchConnector):
    """可选 MCP 搜索代理。

    MCP 本身主要用于工具调用；只有企业提供只读搜索代理时才启用此
    连接器。响应仍被限制为统一的 SearchHit，不允许返回可执行命令。
    """

    source_type = "mcp"
    name = "MCP 只读搜索代理"

    def __init__(self) -> None:
        settings = get_settings()
        self.url = str(settings.mcp_search_url or "").strip()
        self.token = str(settings.mcp_search_token or "").strip()

    @property
    def available(self) -> bool:
        return bool(self.url)

    def search(self, request: SearchRequest) -> list[SearchHit]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = requests.post(
            self.url,
            headers=headers,
            json={"query": request.query, "top_k": request.top_k, "filters": request.filters},
            timeout=max(1, request.timeout_ms // 1000),
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return [
            SearchHit(
                source_type="mcp",
                source_name=str(item.get("source_name") or self.name),
                title=str(item.get("title") or "MCP 记录"),
                content=str(item.get("content") or ""),
                score=float(item.get("score") or 0.75),
                authority=float(item.get("authority") or 0.8),
                freshness=float(item.get("freshness") or 0.8),
                entity_match=float(item.get("entity_match") or 0.7),
                evidence_id=str(item.get("evidence_id") or f"mcp:{index + 1}"),
                metadata={"read_only": True, **item},
            )
            for index, item in enumerate(items)
            if isinstance(item, dict)
        ]


def default_connectors() -> list[SearchConnector]:
    """返回内置连接器；可通过 ``SEARCH_SOURCES`` 选择实际启用类型。"""
    return [
        RAGFlowSearchConnector(),
        QAFileConnector(),
        Neo4jSearchConnector(),
        BusinessSearchConnector(),
        SQLSearchConnector(),
        MCPReadSearchConnector(),
    ]
