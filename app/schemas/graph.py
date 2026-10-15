"""企业知识图谱 API 数据契约。"""

from typing import Any

from pydantic import BaseModel, Field


class GraphEntityInput(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    type: str = Field(min_length=1, max_length=80)
    tenant_id: str | None = Field(default=None, max_length=120)
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphRelationInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=120)
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphUpsertRequest(BaseModel):
    nodes: list[GraphEntityInput] = Field(min_length=1, max_length=5000)
    relations: list[GraphRelationInput] = Field(default_factory=list, max_length=10000)
    # 由网关注入；不信任客户端提交的身份字段。
    tenant_id: str | None = Field(default=None, max_length=120)


class GraphPathRequest(BaseModel):
    start_id: str = Field(min_length=1, max_length=200)
    target_type: str | None = Field(default=None, max_length=80)
    tenant_id: str | None = Field(default=None, max_length=120)
    limit: int = Field(default=20, ge=1, le=50)


class GraphChunkInput(BaseModel):
    """RAGFlow Chunk 的最小输入；其余字段保留在 metadata 中。"""

    content: str = Field(min_length=1, max_length=20000)
    chunk_id: str | None = Field(default=None, max_length=200)
    document_id: str | None = Field(default=None, max_length=200)
    document_name: str | None = Field(default=None, max_length=500)
    dataset_id: str | None = Field(default=None, max_length=200)
    page: int | None = Field(default=None, ge=1, le=100000)
    version: str | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphExtractionRequest(BaseModel):
    """图谱抽取请求，可直接传入 Chunk 或从 RAGFlow 文档读取。"""

    dataset_id: str | None = Field(default=None, max_length=200)
    document_id: str | None = Field(default=None, max_length=200)
    chunks: list[GraphChunkInput] = Field(default_factory=list, max_length=100)
    max_chunks: int = Field(default=20, ge=1, le=100)
    tenant_id: str | None = Field(default=None, max_length=120)
