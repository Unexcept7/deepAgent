"""Model construction with backward-compatible profile routing."""

from __future__ import annotations

from functools import lru_cache

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.model_registry import (
    ModelRegistryError,
    get_model_registry,
)

load_dotenv()


def create_legacy_chat_model(
    *,
    model: str,
    temperature: float = 0,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """Build the environment-backed OpenAI-compatible client in one place."""
    settings = get_settings()
    api_key = settings.dashscope_api_key
    base_url = settings.dashscope_base_url
    if not api_key or not base_url:
        raise RuntimeError("DASHSCOPE_API_KEY and DASHSCOPE_BASE_URL are required")
    model_kwargs = {
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "temperature": temperature,
        "extra_body": {"enable_thinking": False},
        "timeout": 60,
        "max_retries": settings.qwen_max_retries,
        "http_client": httpx.Client(trust_env=settings.qwen_trust_env, timeout=60),
        "http_async_client": httpx.AsyncClient(
            trust_env=settings.qwen_trust_env,
            timeout=60,
        ),
    }
    if max_tokens is not None:
        model_kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**model_kwargs)


def _legacy_qwen_model() -> ChatOpenAI:
    """Keep the original environment-only path as the final compatibility fallback."""

    return create_legacy_chat_model(model=get_settings().qwen_model)


def _profile_model(
    profile_id: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    profile = get_model_registry().resolve(profile_id)
    settings = get_settings()
    # Resolve secrets only at construction time; they are never returned by
    # the model-center API.  Local providers may not require an API key.
    from app.model_registry import _profile_api_key, _profile_base_url

    api_key = _profile_api_key(profile) or "not-needed"
    base_url = _profile_base_url(profile)
    provider = str(profile.get("provider") or "")
    timeout_seconds = int(profile.get("timeout_seconds", 60))
    model_kwargs = {
        "model": str(profile.get("model")),
        "api_key": api_key,
        "base_url": base_url,
        "temperature": float(profile.get("temperature", 0) if temperature is None else temperature),
        "max_tokens": int(profile.get("max_tokens", 8192) if max_tokens is None else max_tokens),
        "timeout": timeout_seconds,
        "max_retries": int(profile.get("max_retries", settings.qwen_max_retries)),
        "http_client": httpx.Client(trust_env=settings.qwen_trust_env, timeout=timeout_seconds),
        "http_async_client": httpx.AsyncClient(trust_env=settings.qwen_trust_env, timeout=timeout_seconds),
    }
    # DashScope-specific thinking control must not be sent blindly to other
    # OpenAI-compatible providers.
    if profile.get("enable_thinking") is not None and (
        "dashscope" in provider.lower() or "qwen" in str(profile.get("model") or "").lower()
    ):
        model_kwargs["extra_body"] = {"enable_thinking": bool(profile["enable_thinking"])}
    model = ChatOpenAI(**model_kwargs)
    # ChatOpenAI is a Pydantic model in some LangChain versions and rejects
    # arbitrary fields through normal setattr.  These are observability-only
    # attributes, so bypass validation while keeping the client compatible
    # across supported versions.
    try:
        object.__setattr__(model, "deepagent_profile_id", profile_id)
        object.__setattr__(model, "deepagent_provider", provider)
    except Exception:
        pass
    return model


@lru_cache(maxsize=128)
def _create_profile_model(
    profile_id: str,
    temperature: float | None,
    max_tokens: int | None,
) -> ChatOpenAI:
    return _profile_model(profile_id, temperature=temperature, max_tokens=max_tokens)


def create_model_for_profile(
    profile_id: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """Build a cached model client from a registry profile."""

    return _create_profile_model(
        str(profile_id),
        None if temperature is None else float(temperature),
        None if max_tokens is None else int(max_tokens),
    )


def create_model_for_stage(
    stage: str,
    *,
    query: str = "",
    attachments: list[dict] | None = None,
    complexity: dict | None = None,
    profile_id: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """Create the model selected for a pipeline stage.

    An explicit profile ID is used by the running request so all sibling
    calls agree on the same routing decision.
    """

    settings = get_settings()
    selected = profile_id
    if not selected and bool(getattr(settings, "model_routing_enabled", False)):
        try:
            from app.model_registry import get_model_registry

            selected = get_model_registry().select(
                stage,
                query=query,
                attachments=attachments,
                complexity=complexity,
            ).get("profile_id")
        except ModelRegistryError:
            selected = None
    try:
        if selected:
            return create_model_for_profile(
                str(selected),
                temperature=temperature,
                max_tokens=max_tokens,
            )
    except Exception:
        # A partially configured optional profile must not take down the
        # existing single-model deployment.
        pass
    return create_qwen_model()


@lru_cache(maxsize=1)
def create_qwen_model() -> ChatOpenAI:
    """Create the current balanced Qwen model for legacy callers."""

    try:
        return create_model_for_profile("balanced")
    except (ModelRegistryError, RuntimeError):
        return _legacy_qwen_model()


def clear_model_caches() -> None:
    """Apply model-center changes without requiring a process restart."""

    _create_profile_model.cache_clear()
    create_qwen_model.cache_clear()
