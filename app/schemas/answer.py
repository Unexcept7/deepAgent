"""Structured contracts for fact-bound answer composition.

The model may organize a response, but it does not own factual values.  Facts
are created from the already judged requirement results and referenced by
stable ``fact_id`` values in the answer plan.
"""

from typing import Literal

from pydantic import BaseModel, Field


AnswerStatus = Literal[
    "supported",
    "partial",
    "ambiguous",
    "conflict",
    "not_found",
]


ClaimStatus = Literal["supported", "unsupported", "conflict"]
AnswerPlanSegmentType = Literal["text", "fact"]


class AnswerFact(BaseModel):
    """A source-bound fact accepted by the requirement evidence judge."""

    fact_id: str = Field(min_length=1, max_length=64)
    requirement_id: str = Field(default="", max_length=64)
    entity: str = Field(default="", max_length=160)
    attribute: str = Field(default="", max_length=200)
    value: str = Field(default="", max_length=2400)
    evidence_ids: list[str] = Field(default_factory=list)
    status: ClaimStatus = "unsupported"
    reason: str = Field(default="", max_length=800)
    source_names: list[str] = Field(default_factory=list)


class AnswerPlanSegment(BaseModel):
    """One connective-text or source-bound fact segment in the answer."""

    type: AnswerPlanSegmentType
    text: str = Field(default="", max_length=1200)
    fact_id: str = Field(default="", max_length=64)
    prefix: str = Field(default="", max_length=500)
    suffix: str = Field(default="", max_length=500)


class AnswerPlan(BaseModel):
    """The only model-controlled structure used to render factual content."""

    segments: list[AnswerPlanSegment] = Field(default_factory=list)
    used_fact_ids: list[str] = Field(default_factory=list)
    invalid_fact_ids: list[str] = Field(default_factory=list)
    dropped_segments: int = Field(default=0, ge=0)


class AnswerClaim(BaseModel):
    """One factual statement in the answer and its source binding."""

    claim_id: str = Field(min_length=1, max_length=48)
    fact_id: str | None = Field(default=None, max_length=64)
    text: str = Field(min_length=1, max_length=1200)
    entity: str = Field(default="", max_length=160)
    attribute: str = Field(default="", max_length=160)
    value: str = Field(default="", max_length=1200)
    evidence_ids: list[str] = Field(default_factory=list)
    status: ClaimStatus = "supported"
    reason: str = Field(default="", max_length=600)
    confidence: float | None = Field(default=None, ge=0, le=1)


class AnswerDraft(BaseModel):
    """Rendered answer plus the fact ledger and model-selected plan."""

    status: AnswerStatus = "supported"
    answer: str = Field(min_length=1, max_length=8000)
    answer_plan: AnswerPlan | None = None
    fact_ledger: list[AnswerFact] = Field(default_factory=list)
    claims: list[AnswerClaim] = Field(default_factory=list)
    used_evidence_ids: list[str] = Field(default_factory=list)
    invalid_evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: str | None = Field(default=None, max_length=1000)
    follow_up: str | None = Field(default=None, max_length=500)
    repaired: bool = False
