from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.answer import AnswerDraft


class Citation(BaseModel):
    evidence_id: str | None = None
    chunk_id: str | int | None = None
    dataset_id: str | None = None
    document_id: str | None = None
    document_name: str
    page: int | None = None
    score: float | None = None
    url: str | None = None
    excerpt: str | None = None
    source_type: str = "document"
    source_name: str | None = None
    version: str | None = None
    updated_at: str | None = None


class GraphEvidenceSource(BaseModel):
    """Original document provenance attached to a derived graph edge."""

    evidence_id: str | None = None
    document_id: str | None = None
    dataset_id: str | None = None
    document_name: str | None = None
    page: int | str | None = None
    chunk_id: str | int | None = None
    version: str | None = None
    excerpt: str | None = None
    url: str | None = None
    score: float | None = None


class GraphRelationEvidence(BaseModel):
    """A graph fact and the source material that supports that relationship."""

    relation_id: str
    source_entity: str
    source_id: str | None = None
    relation: str
    target_entity: str
    target_id: str | None = None
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    evidence: list[GraphEvidenceSource] = Field(default_factory=list)
    evidence_status: Literal["bound", "partial", "unverified"] = "unverified"
    graph_query: str | None = None


class RetrievalHit(BaseModel):
    """A chunk returned by RAGFlow during the current retrieval."""

    evidence_id: str | None = None
    document_name: str
    page: int | None = None
    score: float | None = None
    content: str
    url: str | None = None
    source_type: str = "document"
    source_name: str | None = None
    version: str | None = None
    authority: float | None = None
    freshness: float | None = None
    metadata: dict = Field(default_factory=dict)


class SuggestedQuestion(BaseModel):
    text: str
    query: str


class Claim(BaseModel):
    """A factual statement and the verdict produced for it this turn."""

    fact_id: str | None = None
    entity: str
    attribute: str
    value: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["supported", "unsupported", "conflict"]
    reason: str


class Requirement(BaseModel):
    id: str
    subject: str
    request: str
    output_type: str
    retrieval_query: str
    query_span: str = ""


class RequirementSource(BaseModel):
    evidence_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    document_name: str
    document_id: str | None = None
    dataset_id: str | None = None
    page: int | None = None
    score: float | None = None
    url: str | None = None
    chunk_id: str | int | None = None
    excerpt: str | None = None


class RequirementResult(BaseModel):
    requirement_id: str
    subject: str
    request: str
    output_type: str
    value: str | None = None
    status: Literal["supported", "unsupported", "conflict"]
    reason_code: str
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    sources: list[RequirementSource] = Field(default_factory=list)
    source_binding_status: Literal["complete", "missing", "not_available", "legacy"] = "not_available"


class RequirementCoverage(BaseModel):
    complete: bool = False
    audited: bool = False
    plan_complete: bool = False
    all_requirements_accounted_for: bool = False
    planned_requirement_count: int = 0
    result_count: int = 0
    missing_requirement_ids: list[str] = Field(default_factory=list)
    missing_requests: list[str] = Field(default_factory=list)
    supported_count: int = 0
    unsupported_count: int = 0
    conflict_count: int = 0
    source_binding_complete: bool = True
    source_binding_failures: list[str] = Field(default_factory=list)


class TraceStep(BaseModel):
    name: str
    detail: str
    status: str = "success"


class TraceSpan(BaseModel):
    """One observable execution unit in the Agent waterfall."""

    span_id: str
    parent_span_id: str | None = None
    trace_id: str | None = None
    type: str = "stage"
    name: str
    component: str | None = None
    status: str = "success"
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    model: str | None = None
    provider: str | None = None
    model_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    token_source: str = "unavailable"
    tool_name: str | None = None
    connection_id: str | None = None
    workflow_id: str | None = None
    workflow_version: int | None = None
    node_id: str | None = None
    input_preview: str | None = None
    output_preview: str | None = None
    error: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class ExecutionTrace(BaseModel):
    route: str
    routing_policy: dict = Field(default_factory=dict)
    trace_id: str | None = None
    workflow_id: str | None = None
    workflow_version: int | None = None
    workflow_run_id: str | None = None
    used_ragflow: bool = False
    model_calls: int = 0
    ragflow_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    total_duration_ms: int | None = None
    token_source: str = "unavailable"
    span_count: int = 0
    tool_span_count: int = 0
    token_usage: dict[str, Any] = Field(default_factory=dict)
    spans: list[TraceSpan] = Field(default_factory=list)
    steps: list[TraceStep] = Field(default_factory=list)


class EvidenceSummary(BaseModel):
    has_error: bool = False
    errors: list[str] = Field(default_factory=list)
    status: str = "not_required"
    requires_evidence: bool = False
    reason: str | None = None
    candidate_count: int = 0
    verified_count: int = 0
    provided_count: int = 0
    rejected_count: int = 0
    validation_mode: str | None = None
    non_blocking: bool = False
    warnings: list[str] = Field(default_factory=list)
    requested_entities: list[str] = Field(default_factory=list)
    covered_entities: list[str] = Field(default_factory=list)
    missing_entities: list[str] = Field(default_factory=list)
    similar_entities: list[dict] = Field(default_factory=list)
    requested_subjects: list[str] = Field(default_factory=list)
    covered_subjects: list[str] = Field(default_factory=list)
    missing_subjects: list[str] = Field(default_factory=list)
    requested_aspects: list[str] = Field(default_factory=list)
    covered_aspects: list[str] = Field(default_factory=list)
    missing_aspects: list[str] = Field(default_factory=list)
    missing_entity_aspects: list[dict[str, str]] = Field(default_factory=list)
    missing_requirements: list[dict] = Field(default_factory=list)
    coverage_limited: bool = False
    conflicts: list[dict] = Field(default_factory=list)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    violations: list[str] = Field(default_factory=list)
    used_evidence_ids: list[str] = Field(default_factory=list)


class AgentResponse(BaseModel):
    conversation_id: str
    answer: str
    answer_draft: AnswerDraft | None = None
    tool_calls: list[dict] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    retrieval_hits: list[RetrievalHit] = Field(default_factory=list)
    graph_relations: list[GraphRelationEvidence] = Field(default_factory=list)
    evidence: EvidenceSummary | None = None
    claims: list[Claim] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    results: list[RequirementResult] = Field(default_factory=list)
    coverage: RequirementCoverage | None = None
    suggestions: list[SuggestedQuestion] = Field(default_factory=list)
    trace: ExecutionTrace | None = None
