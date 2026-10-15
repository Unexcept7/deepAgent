"""Schemas for admin-maintained semantic-router prompts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


PromptRoute = Literal[
    "knowledge_qa",
    "business_query",
    "business_action",
    "casual",
    "dataset_list",
]


class SemanticIntentDefinition(BaseModel):
    """One intent that the router may suggest to the backend."""

    intent_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    route: PromptRoute
    description: str = Field(default="", max_length=800)
    examples: list[str] = Field(default_factory=list, max_length=20)
    exclusions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("intent_id", "name", "description")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("examples", "exclusions")
    @classmethod
    def clean_examples(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            item = str(value).strip()
            if item and item not in result:
                result.append(item)
        return result


class SemanticEntityDefinition(BaseModel):
    """One controlled entity type exposed to the semantic router."""

    entity_type: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=800)
    examples: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("entity_type", "name", "description")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("examples")
    @classmethod
    def clean_examples(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            item = str(value).strip()
            if item and item not in result:
                result.append(item)
        return result


class SemanticPromptDefinitionInput(BaseModel):
    """Editable, versioned prompt definition for semantic routing."""

    prompt_id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    enabled: bool = True
    model: str = Field(default="qwen3.8-max", min_length=1, max_length=160)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1200, ge=128, le=8192)
    fallback_mode: Literal["rule_only", "clarify"] = "rule_only"
    system_prompt: str = Field(min_length=20, max_length=20000)
    intents: list[SemanticIntentDefinition] = Field(default_factory=list, max_length=30)
    entities: list[SemanticEntityDefinition] = Field(default_factory=list, max_length=50)
    base_version: int | None = Field(default=None, ge=1)

    @field_validator("name", "description", "model", "system_prompt")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value).strip()


class SemanticPromptPublishRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=500)


class SemanticPromptRollbackRequest(BaseModel):
    version: int = Field(ge=1)
    publish: bool = True
    note: str = Field(default="", max_length=500)


class SemanticPromptTestRequest(BaseModel):
    prompt_id: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=4000)
    version: int | None = Field(default=None, ge=1)
