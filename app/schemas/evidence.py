"""Common evidence contract shared by retrieval adapters and answer stages."""

from typing import Any

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    """A source fragment returned by a tool in the current request."""

    evidence_id: str = Field(min_length=1, max_length=160)
    source_type: str = "document"
    source_name: str | None = None
    dataset_id: str | None = None
    document_id: str | None = None
    document_name: str = "未知文档"
    page: int | str | None = None
    chunk_id: str | int | None = None
    content: str = ""
    score: float | None = None
    authority: float | None = None
    freshness: float | None = None
    version: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
