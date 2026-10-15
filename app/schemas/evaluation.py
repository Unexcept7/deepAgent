"""多智能体评测接口的数据契约。"""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, Field


class EvaluationCase(BaseModel):
    """一条可重复执行的 Agent 问题和可选期望。"""

    case_id: str = Field(default_factory=lambda: f"case-{uuid4().hex[:10]}", max_length=120)
    # 兼容项目现有 JSONL 检索评测集中的 id/gold_documents 字段。
    id: str | None = Field(default=None, max_length=120)
    query: str = Field(min_length=1, max_length=10000)
    expected_answer_contains: list[str] = Field(default_factory=list, max_length=20)
    expected_intents: list[str] = Field(default_factory=list, max_length=12)
    expected_entities: list[str] = Field(default_factory=list, max_length=20)
    expected_source_types: list[str] = Field(default_factory=list, max_length=12)
    expected_documents: list[str] = Field(default_factory=list, max_length=20)
    gold_documents: list[str | dict] = Field(default_factory=list, max_length=20)
    required_concepts: list[str] = Field(default_factory=list, max_length=30)
    expected_outcome: str | None = Field(default=None, max_length=80)
    expected_evidence_status: str | None = Field(default=None, max_length=80)
    group_id: str | None = Field(default=None, max_length=120)
    split: str | None = Field(default=None, max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=20)
    notes: str | None = Field(default=None, max_length=500)


class EvaluationRunRequest(BaseModel):
    """请求多个已发布 Agent 在同一组问题上进行对比。"""

    name: str = Field(default="未命名评测", min_length=1, max_length=160)
    agent_ids: list[str] = Field(min_length=1, max_length=8)
    cases: list[EvaluationCase] = Field(min_length=1, max_length=100)
    max_parallel: int = Field(default=2, ge=1, le=8)
