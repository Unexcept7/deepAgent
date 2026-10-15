"""统一搜索的数据契约。

不同系统的结果字段差异很大：RAGFlow 返回 chunk，图数据库返回路径，
业务 API 返回记录。先转换为这些稳定模型，前端、评测和 Agent 就不需要
为每个连接器编写一套分支逻辑。
"""

from datetime import datetime
from typing import Any, Literal
import re

from pydantic import BaseModel, Field, field_validator


SourceType = Literal["document", "qa", "graph", "sql", "api", "mcp"]


StructuredValue = str | int | float | bool | None | list[str | int | float | bool]


class StructuredQueryFilter(BaseModel):
    """A logical filter; it is never treated as a SQL fragment."""

    field: str = Field(min_length=1, max_length=120)
    operator: Literal[
        "eq",
        "neq",
        "in",
        "not_in",
        "contains",
        "starts_with",
        "gte",
        "gt",
        "lte",
        "lt",
        "between",
    ] = "eq"
    value: StructuredValue

    @field_validator("field")
    @classmethod
    def safe_field(cls, value: str) -> str:
        value = str(value).strip()
        if not value or not re.fullmatch(r"[\w.-]{1,120}", value, flags=re.UNICODE):
            raise ValueError("结构化查询字段名无效")
        return value


class StructuredQueryAggregation(BaseModel):
    function: Literal["count", "sum", "avg", "min", "max"]
    field: str = Field(default="*", min_length=1, max_length=120)
    alias: str | None = Field(default=None, max_length=120)

    @field_validator("field", "alias")
    @classmethod
    def safe_aggregation_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        if not value or not re.fullmatch(r"[\w.*-]{1,120}", value, flags=re.UNICODE):
            raise ValueError("结构化查询聚合字段名无效")
        return value


class StructuredQueryOrder(BaseModel):
    field: str = Field(min_length=1, max_length=120)
    direction: Literal["asc", "desc"] = "desc"

    @field_validator("field")
    @classmethod
    def safe_order_field(cls, value: str) -> str:
        value = str(value).strip()
        if not value or not re.fullmatch(r"[\w.*-]{1,120}", value, flags=re.UNICODE):
            raise ValueError("结构化查询排序字段名无效")
        return value


class StructuredQueryPlan(BaseModel):
    """Logical structured-data query passed to an allow-listed gateway.

    The model may propose this plan, but it cannot submit SQL, table joins,
    URLs, or credentials. The gateway maps logical datasets and fields to
    parameterized SQL after applying its own allow-list and row/column policy.
    """

    dataset: str = Field(min_length=1, max_length=160)
    select: list[str] = Field(default_factory=list, max_length=30)
    filters: list[StructuredQueryFilter] = Field(default_factory=list, max_length=30)
    aggregations: list[StructuredQueryAggregation] = Field(default_factory=list, max_length=10)
    group_by: list[str] = Field(default_factory=list, max_length=12)
    order_by: list[StructuredQueryOrder] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=100000)

    @field_validator("dataset")
    @classmethod
    def safe_dataset(cls, value: str) -> str:
        value = str(value).strip()
        if not value or not re.fullmatch(r"[\w.-]{1,160}", value, flags=re.UNICODE):
            raise ValueError("结构化查询数据集名称无效")
        return value

    @field_validator("select", "group_by")
    @classmethod
    def safe_field_list(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            item = str(value).strip()
            if not item or not re.fullmatch(r"[\w.*-]{1,120}", item, flags=re.UNICODE):
                raise ValueError("结构化查询字段名无效")
            if item not in result:
                result.append(item)
        return result


class RetrievalPlan(BaseModel):
    """由语义路由提出、由后端执行的只读检索计划。

    ``graph_queries`` 只用于实体/关系定位，``attribute_keys`` 和
    ``relation_types`` 只用于当前问题的关系筛选与排序，``document_queries``
    用于 RAGFlow 等文档连接器，``structured_queries`` 只允许表达逻辑数据集、
    字段、过滤和聚合。模型可以提出候选，但不能提交 Cypher、SQL、URL 或
    工具权限；后端会在进入连接器前再次收敛和校验这些字段。
    """

    graph_queries: list[str] = Field(default_factory=list, max_length=8)
    document_queries: list[str] = Field(default_factory=list, max_length=8)
    structured_queries: list[StructuredQueryPlan] = Field(default_factory=list, max_length=8)
    entity_ids: list[str] = Field(default_factory=list, max_length=8)
    attribute_keys: list[str] = Field(default_factory=list, max_length=20)
    relation_types: list[str] = Field(default_factory=list, max_length=12)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10000)
    # None=使用配置默认范围；[]=明确不使用任何来源。
    source_types: list[SourceType] | None = None
    top_k: int = Field(default=12, ge=1, le=50)
    timeout_ms: int = Field(default=8000, ge=100, le=60000)
    filters: dict[str, Any] = Field(default_factory=dict)
    query_plan: RetrievalPlan | None = None
    # 由网关注入，连接器不得相信客户端自行传入的用户身份。
    tenant_id: str | None = None
    user_id: str | None = None
    app_id: str | None = Field(default=None, max_length=160)


class SearchHit(BaseModel):
    """跨数据源统一的可引用结果。"""

    source_type: SourceType
    source_name: str
    title: str
    content: str
    score: float = 0.0
    authority: float = 0.5
    freshness: float = 0.5
    entity_match: float = 0.0
    document_id: str | None = None
    dataset_id: str | None = None
    page: int | None = None
    version: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    updated_at: datetime | str | None = None
    evidence_id: str
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def ranking_score(self) -> float:
        """返回可解释的融合排序分数。"""
        return round(
            0.50 * self.score
            + 0.20 * self.authority
            + 0.15 * self.freshness
            + 0.10 * self.entity_match
            + 0.05 * _source_priority(self.source_type),
            6,
        )


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit] = Field(default_factory=list)
    sources_requested: list[str] = Field(default_factory=list)
    sources_used: list[str] = Field(default_factory=list)
    source_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    total: int = 0
    duration_ms: int = 0
    trace: list[dict[str, Any]] = Field(default_factory=list)


def _source_priority(source_type: str) -> float:
    # 标准 QA 和业务实时 API 的结果通常比未经审核的普通文本更可靠。
    return {
        "qa": 1.0,
        "api": 0.95,
        "sql": 0.9,
        "document": 0.8,
        "graph": 0.85,
        "mcp": 0.7,
    }.get(source_type, 0.5)
