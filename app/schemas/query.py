from pydantic import BaseModel, Field, field_validator, model_validator

from app.multimodal import (
    MAX_ATTACHMENTS,
    MAX_TOTAL_ATTACHMENT_SOURCE_CHARS,
)


class QueryAttachment(BaseModel):
    """A bounded image attachment for one workbench question."""

    name: str = Field(default="图片", max_length=160)
    mime_type: str = Field(min_length=1, max_length=80)
    data_url: str | None = Field(default=None, max_length=8_000_000)
    url: str | None = Field(default=None, max_length=4096)
    size_bytes: int | None = Field(default=None, ge=0, le=5_000_000)

    @field_validator("name", "mime_type")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value).strip()

    @model_validator(mode="after")
    def validate_source(self) -> "QueryAttachment":
        self.mime_type = self.mime_type.lower()
        if not self.mime_type.startswith("image/"):
            raise ValueError("工作台暂只支持图片附件")
        if bool(self.data_url) == bool(self.url):
            raise ValueError("图片附件必须且只能提供 data_url 或 url")
        if self.data_url and not self.data_url.lower().startswith("data:image/"):
            raise ValueError("图片 data_url 格式无效")
        if self.url and not self.url.lower().startswith(("http://", "https://")):
            raise ValueError("图片 URL 只允许使用 http 或 https")
        return self


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10000)
    conversation_id: str | None = None
    # 可选的应用中心上下文，用于将一次真实 Agent 运行回写到具体应用。
    app_id: str | None = Field(default=None, max_length=160)
    # 可选的工作流运行上下文。未传时保持旧版 DeepAgent 入口行为。
    workflow_id: str | None = Field(default=None, max_length=120)
    workflow_version: int | None = Field(default=None, ge=1)
    workflow_params: dict = Field(default_factory=dict)
    # None 表示未指定，使用应用/工作流默认范围；空列表表示用户明确
    # 取消所有数据源，不能再被服务端恢复成默认范围。
    source_types: list[str] | None = None
    filters: dict = Field(default_factory=dict)
    attachments: list[QueryAttachment] = Field(
        default_factory=list,
        max_length=MAX_ATTACHMENTS,
    )

    @model_validator(mode="after")
    def validate_attachment_total(self) -> "QueryRequest":
        total = sum(
            len(item.data_url or item.url or "")
            for item in self.attachments
        )
        if total > MAX_TOTAL_ATTACHMENT_SOURCE_CHARS:
            raise ValueError("本次图片总大小过大，请减少图片数量或压缩图片")
        return self
