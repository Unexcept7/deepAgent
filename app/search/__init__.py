"""多数据源统一搜索子系统。

搜索层独立于 Agent，负责把文档、QA、图谱、SQL 和业务 API 的结果
转换为统一的证据结构。Agent 只需要选择 ``unified_search``，不需要
了解每个后端的返回格式。
"""

from app.search.models import SearchHit, SearchRequest, SearchResponse
from app.search.service import UnifiedSearchService, get_unified_search_service

__all__ = [
    "SearchHit",
    "SearchRequest",
    "SearchResponse",
    "UnifiedSearchService",
    "get_unified_search_service",
]
