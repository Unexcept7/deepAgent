"""DeepAgent 可调用的统一搜索工具。"""

import json
import time

from langchain_core.tools import tool

from app.search.models import SearchRequest
from app.search.models import RetrievalPlan
from app.search.planner import plan_search_query
from app.search.service import get_unified_search_service
from app.tool_center.context import execution_context
from app.observability import current_trace_recorder


@tool
def unified_search(
    query: str,
    source_types: list[str] | None = None,
    top_k: int = 12,
    filters: dict | None = None,
    query_plan: dict | None = None,
) -> str:
    """跨企业文档、标准 QA、知识图谱和业务 API 的统一只读搜索。

    当问题涉及手册、故障关系、实时状态或维修记录时优先使用该工具。
    工具只读，不会创建工单或修改设备。
    """
    started_at = time.perf_counter()
    trace = current_trace_recorder()
    result: dict[str, object]
    plan_result = None
    try:
        requested_source_types = (
            [item for item in source_types if item in {"document", "qa", "graph", "sql", "api", "mcp"}]
            if source_types is not None
            else None
        )
        query_plan_value = RetrievalPlan.model_validate(query_plan) if query_plan else None
        context = execution_context.get()
        service = get_unified_search_service()
        available = {
            item.get("source_type")
            for item in service.source_status()
            if item.get("available")
        }
        graph_requested = requested_source_types is None or "graph" in requested_source_types
        sql_requested = requested_source_types is None or "sql" in requested_source_types
        if context is not None and context.source_types is not None:
            graph_requested = "graph" in context.source_types
            sql_requested = "sql" in context.source_types
        if query_plan_value is None and (
            (graph_requested and "graph" in available)
            or (sql_requested and "sql" in available)
        ):
            plan_result = plan_search_query(query)
            query_plan_value = plan_result.plan
        request = SearchRequest(
            query=query,
            source_types=requested_source_types,
            top_k=max(1, min(int(top_k), 50)),
            filters=filters or {},
            query_plan=query_plan_value,
        )
        if context:
            request.user_id = context.principal.user_id
            request.tenant_id = context.workspace_id
            request.app_id = context.app_id
        response = service.search(request)
        result = response.model_dump(mode="json")
        if plan_result is not None:
            result["query_plan"] = plan_result.plan.model_dump(mode="json")
            result["query_plan_diagnostics"] = plan_result.diagnostics
            result["warnings"] = [
                *result.get("warnings", []),
                *plan_result.warnings,
            ]
    except Exception as exc:
        result = {
            "query": query,
            "hits": [],
            "total": 0,
            "errors": [str(exc)],
        }
    if trace is not None:
        trace.record_data_source(
            source_type="unified_search",
            source_name="统一搜索编排器",
            duration_ms=round((time.perf_counter() - started_at) * 1000),
            status="error" if result.get("errors") else "success",
            count=len(result.get("hits") or []),
            error="；".join(str(item) for item in result.get("errors") or []) or None,
            tool_name="unified_search",
            attributes={
                "requested_source_types": source_types if source_types is not None else [],
                "source_scope_explicit": source_types is not None,
                "top_k": max(1, min(int(top_k), 50)),
                "source_counts": result.get("source_counts") or {},
                "query_plan": result.get("query_plan") or query_plan or {},
            },
        )
    return json.dumps(result, ensure_ascii=False, default=str)
