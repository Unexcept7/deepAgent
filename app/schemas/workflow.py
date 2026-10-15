"""工作流定义、版本和调试请求的数据契约。

工作流是后端可执行配置，不是前端画布的私有状态。节点和边采用稳定的
JSON 契约，前端可以编辑，运行时也可以据此生成可审计的节点事件。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


WorkflowStatus = Literal["draft", "published", "disabled"]


class WorkflowNode(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    type: str = Field(min_length=1, max_length=80)
    x: float = 0
    y: float = 0
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)
    condition: str | None = None
    branch: str | None = None
    human_confirmation: bool = False

    @field_validator("id", "type", "name")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("节点字段不能为空")
        return value


class WorkflowEdge(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    source: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=120)
    label: str | None = Field(default=None, max_length=120)
    condition: str | None = None
    enabled: bool = True

    @field_validator("source", "target")
    @classmethod
    def trim_endpoint(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("边必须包含 source 和 target")
        return value


class WorkflowDefinitionInput(BaseModel):
    workflow_id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)
    nodes: list[WorkflowNode] = Field(default_factory=list, max_length=100)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=300)
    base_version: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("工作流名称不能为空")
        return value

    @model_validator(mode="after")
    def validate_graph(self):
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("节点 id 必须唯一")
        known_ids = set(node_ids)
        missing = [
            f"{edge.source}->{edge.target}"
            for edge in self.edges
            if edge.source not in known_ids or edge.target not in known_ids
        ]
        if missing:
            raise ValueError(f"边引用了不存在的节点：{'、'.join(missing[:5])}")
        return self


class WorkflowPublishRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=500)


class WorkflowRollbackRequest(BaseModel):
    version: int = Field(ge=1)
    publish: bool = True
    note: str = Field(default="", max_length=500)


class WorkflowTemplateRequest(BaseModel):
    template_id: str = Field(min_length=1, max_length=120)
    workflow_id: str | None = Field(default=None, max_length=120)
    name: str | None = Field(default=None, max_length=160)


class WorkflowNodeDebugRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=120)
    input: dict[str, Any] = Field(default_factory=dict)

