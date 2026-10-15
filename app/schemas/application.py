"""Enterprise Agent application contracts.

The application center is deliberately kept separate from the workflow canvas:
an application owns the user-facing lifecycle, permissions and runtime policy,
while a workflow owns the executable graph.  This makes it possible to bind a
published application to a stable workflow version without making the UI a
source of truth.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


ApplicationStatus = Literal["draft", "published", "disabled", "archived"]


class ApplicationDataSource(BaseModel):
    source_type: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_type", "name")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("数据源字段不能为空")
        return value


class ApplicationKnowledgeBinding(BaseModel):
    """A concrete RAGFlow dataset allowed by one application.

    The binding belongs to the application snapshot, while document parsing,
    chunking and dataset maintenance remain owned by the Knowledge Center.
    ``published_revision`` pins the published RAGFlow connection definition
    used when the application was released; ``None`` keeps a backwards-
    compatible follow-current-published behavior for legacy/test bindings.
    """

    connection_id: str = Field(min_length=1, max_length=120)
    dataset_id: str = Field(min_length=1, max_length=160)
    dataset_name: str = Field(default="", max_length=200)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=1000)
    environment: Literal["test", "production"] = "production"
    published_revision: int | None = Field(default=None, ge=1)
    retrieval_profile: dict[str, Any] = Field(default_factory=dict)
    document_filters: dict[str, Any] = Field(default_factory=dict)
    graph_enabled: bool = False

    @field_validator("connection_id", "dataset_id")
    @classmethod
    def trim_binding_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("知识库绑定字段不能为空")
        return value

    @field_validator("dataset_name")
    @classmethod
    def trim_binding_name(cls, value: str) -> str:
        return value.strip()


class ApplicationTool(BaseModel):
    tool_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    enabled: bool = True
    requires_confirmation: bool = False
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_id", "name")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("工具字段不能为空")
        return value


class ApplicationWorkflowBinding(BaseModel):
    workflow_id: str = Field(min_length=1, max_length=120)
    version: int | None = Field(default=None, ge=1)
    role: Literal["primary", "fallback", "subflow", "tool"] = "primary"
    enabled: bool = True


class ApplicationDefinitionInput(BaseModel):
    app_id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1200)
    category: str = Field(default="企业助手", max_length=80)
    type: str = Field(default="DeepAgent", max_length=80)
    icon: str = Field(default="知", max_length=8)
    enabled: bool = True
    tags: list[str] = Field(default_factory=list, max_length=20)
    workflow_id: str | None = Field(default=None, max_length=120)
    workflow_version: int | None = Field(default=None, ge=1)
    workflow_bindings: list[ApplicationWorkflowBinding] = Field(default_factory=list, max_length=50)
    data_sources: list[ApplicationDataSource] = Field(default_factory=list, max_length=50)
    knowledge_binding_mode: Literal["inherit", "explicit"] = "inherit"
    knowledge_bindings: list[ApplicationKnowledgeBinding] = Field(default_factory=list, max_length=100)
    tools: list[ApplicationTool] = Field(default_factory=list, max_length=100)
    runtime: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    channels: list[str] = Field(default_factory=list, max_length=30)
    base_version: int | None = Field(default=None, ge=1)

    @field_validator("name", "category", "type", "icon")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("应用字段不能为空")
        return value

    @field_validator("tags", "channels")
    @classmethod
    def normalize_text_list(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            item = str(value).strip()
            if item and item not in result:
                result.append(item)
        return result

    @model_validator(mode="after")
    def normalize_primary_workflow(self):
        bindings = list(self.workflow_bindings)
        if self.workflow_id and not any(item.workflow_id == self.workflow_id for item in bindings):
            bindings.insert(
                0,
                ApplicationWorkflowBinding(
                    workflow_id=self.workflow_id,
                    version=self.workflow_version,
                    role="primary",
                ),
            )
        if bindings and not self.workflow_id:
            primary = next((item for item in bindings if item.role == "primary"), bindings[0])
            self.workflow_id = primary.workflow_id
            self.workflow_version = primary.version
        self.workflow_bindings = bindings
        return self


class ApplicationPublishRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=500)


class ApplicationRollbackRequest(BaseModel):
    version: int = Field(ge=1)
    publish: bool = True
    note: str = Field(default="", max_length=500)
