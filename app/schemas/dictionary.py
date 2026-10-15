"""Schemas for admin-maintained preprocessing dictionaries."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


DictionaryType = Literal[
    "technical_subject",
    "aspect",
    "stop_exact",
    "stop_prefix",
    "fault_code",
    "fault_context",
    "device_alias",
    "model_alias",
]


def _clean_list(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in result:
            result.append(item)
    return result


class DictionaryEntry(BaseModel):
    entry_id: str | None = Field(default=None, max_length=120)
    dictionary_type: DictionaryType
    canonical_value: str = Field(min_length=1, max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    query_terms: list[str] = Field(default_factory=list, max_length=100)
    evidence_terms: list[str] = Field(default_factory=list, max_length=100)
    min_digits: int = Field(default=2, ge=1, le=12)
    max_digits: int = Field(default=5, ge=1, le=12)
    resolved_id: str | None = Field(default=None, max_length=160)
    description: str = Field(default="", max_length=800)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    priority: int = Field(default=100, ge=0, le=1000)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("canonical_value", "description")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("aliases", "query_terms", "evidence_terms")
    @classmethod
    def clean_terms(cls, values: list[str]) -> list[str]:
        return _clean_list(values)

    @model_validator(mode="after")
    def validate_digit_range(self) -> "DictionaryEntry":
        if self.min_digits > self.max_digits:
            raise ValueError("min_digits 不能大于 max_digits")
        return self


class DictionaryDefinitionInput(BaseModel):
    dictionary_id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    enabled: bool = True
    entries: list[DictionaryEntry] = Field(default_factory=list, max_length=1000)
    base_version: int | None = Field(default=None, ge=1)

    @field_validator("name", "description")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value).strip()


class DictionaryPublishRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=500)


class DictionaryRollbackRequest(BaseModel):
    version: int = Field(ge=1)
    publish: bool = True
    note: str = Field(default="", max_length=500)


class DictionaryTestRequest(BaseModel):
    dictionary_id: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=4000)
    version: int | None = Field(default=None, ge=1)
