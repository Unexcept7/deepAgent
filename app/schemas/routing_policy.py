"""路由策略中心的数据契约。

路由策略描述“什么问题进入哪个业务路由”，以及该路由使用哪些数据源和
安全约束。策略通过版本化存储发布，运行时只读取已发布版本。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


PolicyMatchMode = Literal["any", "all"]


class RoutingPolicyMatch(BaseModel):
    keywords: list[str] = Field(default_factory=list, max_length=80)
    entity_types: list[str] = Field(default_factory=list, max_length=20)
    examples: list[str] = Field(default_factory=list, max_length=20)
    mode: PolicyMatchMode = "any"

    @field_validator("keywords", "entity_types", "examples")
    @classmethod
    def clean_items(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            item = str(value).strip()
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned


class RoutingPolicyAction(BaseModel):
    route: Literal["knowledge_qa", "business_query", "business_action", "casual", "dataset_list"] = "knowledge_qa"
    source_types: list[str] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=12, ge=1, le=50)
    similarity_threshold: float = Field(default=0.3, ge=0, le=1)
    require_evidence: bool = True
    human_confirmation: bool = False

    @field_validator("source_types")
    @classmethod
    def clean_sources(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            item = str(value).strip().lower()
            if item not in {"document", "qa", "graph", "sql", "api", "mcp"}:
                raise ValueError("不支持的数据源类型")
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned


class RoutingPolicyDefinitionInput(BaseModel):
    policy_id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    category: str = Field(default="知识问答", min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    enabled: bool = True
    priority: int = Field(default=50, ge=0, le=1000)
    match: RoutingPolicyMatch = Field(default_factory=RoutingPolicyMatch)
    action: RoutingPolicyAction = Field(default_factory=RoutingPolicyAction)
    base_version: int | None = Field(default=None, ge=1)

    @field_validator("name", "category")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("路由策略名称和类别不能为空")
        return value


class RoutingPolicyPublishRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=500)


class RoutingPolicyRollbackRequest(BaseModel):
    version: int = Field(ge=1)
    publish: bool = True
    note: str = Field(default="", max_length=500)


class RoutingPolicyTestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    workflow_id: str | None = Field(default=None, max_length=120)
