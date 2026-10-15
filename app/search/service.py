"""多数据源统一搜索编排服务。"""

from __future__ import annotations

import time
from concurrent.futures import as_completed
from functools import lru_cache
from threading import RLock

from app.config import get_settings
from app.search.connectors import SearchConnector, default_connectors
from app.search.models import SearchHit, SearchRequest, SearchResponse
from app.observability import current_trace_recorder
from app.tool_center.context import ContextThreadPoolExecutor as ThreadPoolExecutor, execution_context


def _rrf_merge(
    groups: dict[str, list[SearchHit]],
    *,
    top_k: int,
    rank_constant: int = 60,
) -> list[SearchHit]:
    """按来源独立排序后用 RRF 融合，避免直接比较异构分数。

    BM25、向量相似度和图谱分数不在同一量纲。企业搜索系统通常先在
    每个来源内部排序，再使用排名融合；原始 ``ranking_score`` 只作为
    同一 RRF 分数下的稳定次序。
    """
    source_weights = {
        "qa": 1.0,
        "api": 1.0,
        "document": 1.0,
        "graph": 1.0,
        "sql": 1.0,
        "mcp": 0.95,
    }
    fused: dict[tuple[str, str, str], tuple[float, SearchHit]] = {}
    for source_type, source_hits in groups.items():
        ranked = sorted(
            source_hits,
            key=lambda item: item.ranking_score,
            reverse=True,
        )
        for rank, hit in enumerate(ranked, start=1):
            key = _hit_identity(hit)
            score = source_weights.get(source_type, 1.0) / (rank_constant + rank)
            previous = fused.get(key)
            if previous is None:
                fused[key] = (score, hit)
            else:
                fused[key] = (previous[0] + score, previous[1])

    ordered = sorted(
        fused.values(),
        key=lambda item: (item[0], item[1].ranking_score),
        reverse=True,
    )
    result: list[SearchHit] = []
    for fusion_score, hit in ordered[:top_k]:
        result.append(
            hit.model_copy(
                update={
                    "metadata": {
                        **hit.metadata,
                        "fusion": "rrf",
                        "fusion_score": round(fusion_score, 8),
                    }
                }
            )
        )
    return result


def _hit_identity(hit: SearchHit) -> tuple[str, str, str, str, str, str]:
    """Return a stable chunk identity for deduplication.

    Titles are not document identities: two pages in one PDF often share the
    same title, while the first 300 characters can be identical in repeated
    headers.  Prefer connector-provided IDs and keep the complete normalized
    content as the final guard so separate evidence chunks are not collapsed.
    """
    return (
        str(hit.source_type or ""),
        str(hit.dataset_id or ""),
        str(hit.document_id or ""),
        str(hit.evidence_id or ""),
        str(hit.page or ""),
        " ".join(str(hit.content or "").split()),
    )


class UnifiedSearchService:
    """并行调用连接器并融合结果的只读搜索服务。"""

    def __init__(self, connectors: list[SearchConnector] | None = None) -> None:
        self.connectors = connectors if connectors is not None else default_connectors()
        settings = get_settings()
        self._failure_threshold = max(
            1,
            int(getattr(settings, "search_circuit_failure_threshold", 2)),
        )
        self._cooldown_seconds = max(
            1,
            int(getattr(settings, "search_circuit_cooldown_seconds", 15)),
        )
        self._health_lock = RLock()
        self._health: dict[str, dict[str, object]] = {}

    def _health_record(self, source_type: str) -> dict[str, object]:
        with self._health_lock:
            return self._health.setdefault(source_type, {
                "failure_count": 0,
                "last_error": None,
                "last_failure_at": None,
                "last_success_at": None,
                "circuit_open_until": None,
            })

    def _circuit_open(self, source_type: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._health_lock:
            record = self._health_record(source_type)
            opened_until = record.get("circuit_open_until")
            if not isinstance(opened_until, (int, float)):
                return False
            if opened_until <= now:
                record["circuit_open_until"] = None
                return False
            return True

    def _record_success(self, source_type: str) -> None:
        with self._health_lock:
            record = self._health_record(source_type)
            record.update({
                "failure_count": 0,
                "last_error": None,
                "last_success_at": time.time(),
                "circuit_open_until": None,
            })

    def _record_failure(self, source_type: str, error: str) -> None:
        with self._health_lock:
            record = self._health_record(source_type)
            failures = int(record.get("failure_count") or 0) + 1
            record.update({
                "failure_count": failures,
                "last_error": error,
                "last_failure_at": time.time(),
            })
            if failures >= self._failure_threshold:
                record["circuit_open_until"] = time.time() + self._cooldown_seconds

    def source_status(self) -> list[dict[str, object]]:
        now = time.time()
        result = []
        for connector in self.connectors:
            configured = bool(connector.available)
            record = dict(self._health_record(connector.source_type))
            open_until = record.get("circuit_open_until")
            circuit_open = self._circuit_open(connector.source_type, now)
            state = "unconfigured"
            if configured:
                state = "circuit_open" if circuit_open else (
                    "degraded" if record.get("failure_count") else "configured"
                )
            result.append({
                "source_type": connector.source_type,
                "name": connector.name,
                "available": configured and not circuit_open,
                "configured": configured,
                "state": state,
                "failure_count": int(record.get("failure_count") or 0),
                "last_error": record.get("last_error"),
                "last_failure_at": record.get("last_failure_at"),
                "last_success_at": record.get("last_success_at"),
                "circuit_open_until": open_until if circuit_open else None,
            })
        return result

    def search(self, request: SearchRequest) -> SearchResponse:
        started = time.perf_counter()
        settings = get_settings()
        configured_types = {
            item.strip().lower()
            for item in str(settings.search_sources or "document,qa,graph,sql,api").split(",")
            if item.strip()
        }
        context = execution_context.get()
        if context is not None:
            configured_types = set(context.source_types) if context.source_types is not None else {connector.source_type for connector in self.connectors}
        requested = (
            set(request.source_types)
            if request.source_types is not None
            else configured_types
        )
        query_plan_attributes = (
            request.query_plan.model_dump(mode="json")
            if request.query_plan
            else {}
        )
        selected = [
            connector for connector in self.connectors
            if connector.source_type in requested
            and connector.source_type in configured_types
            and connector.available
            and not self._circuit_open(connector.source_type)
        ]
        warnings: list[str] = []
        errors: list[str] = []
        trace: list[dict[str, object]] = []
        for connector in self.connectors:
            if connector.source_type in requested and not connector.available:
                warnings.append(f"{connector.name} 未配置，已跳过")
            elif connector.source_type in requested and self._circuit_open(connector.source_type):
                warnings.append(
                    f"{connector.name} 最近连续失败，暂时熔断 {self._cooldown_seconds} 秒，已跳过"
                )
        if not selected:
            return SearchResponse(
                query=request.query,
                sources_requested=sorted(requested),
                warnings=[*warnings, "没有可用的已配置搜索源"],
                duration_ms=round((time.perf_counter() - started) * 1000),
            )

        hits_by_source: dict[str, list[SearchHit]] = {}
        worker_count = max(1, min(len(selected), int(settings.search_max_parallel)))
        source_started_at: dict[int, float] = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {}
            source_started_at = {}
            for connector in selected:
                source_started_at[id(connector)] = time.perf_counter()
                futures[executor.submit(connector.search, request)] = connector
            for future in as_completed(futures):
                connector = futures[future]
                source_started = source_started_at.get(id(connector), time.perf_counter())
                connection = getattr(connector, "connection", {}) or {}
                connection_id = connection.get("connection_id") if isinstance(connection, dict) else None
                try:
                    source_hits = future.result()
                    hits_by_source.setdefault(connector.source_type, []).extend(source_hits)
                    self._record_success(connector.source_type)
                    duration_ms = round((time.perf_counter() - source_started) * 1000)
                    trace.append({
                        "source_type": connector.source_type,
                        "source_name": connector.name,
                        "status": "success",
                        "count": len(source_hits),
                        "duration_ms": duration_ms,
                    })
                    recorder = current_trace_recorder()
                    if recorder is not None:
                        recorder.record_data_source(
                            source_type=connector.source_type,
                            source_name=connector.name,
                            duration_ms=duration_ms,
                            status="success",
                            count=len(source_hits),
                            connection_id=connection_id,
                            attributes={
                                "query": request.query[:240],
                                "top_k": request.top_k,
                                "query_plan": query_plan_attributes,
                            },
                        )
                except Exception as exc:  # 单个系统故障不能拖垮其他来源
                    message = f"{connector.name} 检索失败：{exc}"
                    errors.append(message)
                    self._record_failure(connector.source_type, str(exc))
                    duration_ms = round((time.perf_counter() - source_started) * 1000)
                    trace.append({
                        "source_type": connector.source_type,
                        "source_name": connector.name,
                        "status": "error",
                        "error": str(exc),
                        "duration_ms": duration_ms,
                    })
                    recorder = current_trace_recorder()
                    if recorder is not None:
                        recorder.record_data_source(
                            source_type=connector.source_type,
                            source_name=connector.name,
                            duration_ms=duration_ms,
                            status="error",
                            error=str(exc),
                            connection_id=connection_id,
                            attributes={
                                "query": request.query[:240],
                                "top_k": request.top_k,
                                "query_plan": query_plan_attributes,
                            },
                        )

        deduplicated_groups: dict[str, list[SearchHit]] = {}
        for source_type, source_hits in hits_by_source.items():
            deduplicated: dict[tuple[str, str, str, str, str, str], SearchHit] = {}
            for hit in source_hits:
                key = _hit_identity(hit)
                existing = deduplicated.get(key)
                if existing is None or hit.ranking_score > existing.ranking_score:
                    deduplicated[key] = hit
            deduplicated_groups[source_type] = list(deduplicated.values())
        ordered = _rrf_merge(
            deduplicated_groups,
            top_k=request.top_k,
        )
        source_counts: dict[str, int] = {}
        for hit in ordered:
            source_counts[hit.source_type] = source_counts.get(hit.source_type, 0) + 1
        return SearchResponse(
            query=request.query,
            hits=ordered,
            sources_requested=sorted(requested),
            sources_used=sorted(source_counts),
            source_counts=source_counts,
            warnings=warnings,
            errors=errors,
            total=len(ordered),
            duration_ms=round((time.perf_counter() - started) * 1000),
            trace=trace,
        )


@lru_cache(maxsize=1)
def _default_search_service() -> UnifiedSearchService:
    return UnifiedSearchService()


def get_unified_search_service() -> UnifiedSearchService:
    context = execution_context.get()
    if context is not None:
        from app.tool_center.service import managed_connectors
        return UnifiedSearchService(managed_connectors(context))
    return _default_search_service()


get_unified_search_service.cache_clear = _default_search_service.cache_clear
