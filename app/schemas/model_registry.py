"""Contracts for the model registry and stage-based model routing."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


ModelCostTier = Literal["low", "medium", "high"]


class ModelProfileDefinitionInput(BaseModel):
    """A non-secret model profile editable from the model center.

    API keys are deliberately referenced by environment-variable name.  The
    model center never accepts or returns a raw secret.
    """

    profile_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    provider: str = Field(default="OpenAI-compatible", max_length=80)
    model: str = Field(min_length=1, max_length=160)
    base_url: str = Field(default="", max_length=500)
    base_url_env: str = Field(default="", max_length=120)
    api_key_env: str = Field(default="", max_length=120)
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    cost_tier: ModelCostTier = "medium"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8192, ge=128, le=32768)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    max_retries: int = Field(default=1, ge=0, le=5)
    enable_thinking: bool | None = None
    enabled: bool = True
    description: str = Field(default="", max_length=1000)

    @field_validator("profile_id", "name", "provider", "model", "base_url", "base_url_env", "api_key_env", "description")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("base_url_env", "api_key_env")
    @classmethod
    def validate_env_name(cls, value: str) -> str:
        value = str(value).strip()
        if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("环境变量名只能包含字母、数字和下划线，且不能以数字开头")
        return value

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            item = str(value).strip().lower()
            if item and item not in result:
                result.append(item)
        return result


class ModelRoutingRuleInput(BaseModel):
    """Map a pipeline stage and difficulty to model profile IDs."""

    stage: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    simple_profile: str = Field(min_length=1, max_length=80)
    normal_profile: str = Field(min_length=1, max_length=80)
    complex_profile: str = Field(min_length=1, max_length=80)
    fallback_profile: str = Field(min_length=1, max_length=80)
    required_capabilities: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True

    @field_validator("stage", "name", "simple_profile", "normal_profile", "complex_profile", "fallback_profile")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValueError("模型路由字段不能为空")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def normalize_required_capabilities(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            item = str(value).strip().lower()
            if item and item not in result:
                result.append(item)
        return result


class ModelRoutingUpdateInput(BaseModel):
    routing: list[ModelRoutingRuleInput] = Field(max_length=30)


class ModelTestRequest(BaseModel):
    profile_id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(default="请用一句话说明你适合处理什么任务。", min_length=1, max_length=1000)
