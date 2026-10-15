"""Server-owned execution tracing for enterprise Agent runs.

The trace is deliberately a span record, rather than a copy of the model's
internal reasoning.  It records which component ran, when it ran, how it
ended, and the provider-reported token usage when the provider returns it.
This gives the UI a useful waterfall without exposing chain-of-thought or
credentials.
"""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler


_current_trace: ContextVar["TraceRecorder | None"] = ContextVar(
    "deepagent_trace_recorder",
    default=None,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_preview(value: Any, limit: int = 240) -> str | None:
    """Return a bounded non-secret preview for diagnostics."""
    if value is None:
        return None
    text = str(value).replace("\x00", " ").strip()
    if not text:
        return None
    return text[:limit] + ("…" if len(text) > limit else "")


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _usage_from_mapping(source: Any) -> dict[str, int]:
    if not isinstance(source, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens", "prompt_token_count"),
        "output_tokens": ("output_tokens", "completion_tokens", "completion_token_count"),
        "total_tokens": ("total_tokens", "total_token_count"),
    }
    result: dict[str, int] = {}
    for target, keys in aliases.items():
        for key in keys:
            value = _int_or_none(source.get(key))
            if value is not None:
                result[target] = value
                break
    if "total_tokens" not in result and {
        "input_tokens",
        "output_tokens",
    }.issubset(result):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def extract_token_usage(response: Any) -> dict[str, int]:
    """Normalize LangChain messages and LLMResult/OpenAI metadata."""
    sources: list[Any] = []
    if isinstance(response, dict):
        sources.append(response)
    for attribute in ("usage_metadata", "response_metadata", "llm_output"):
        value = getattr(response, attribute, None)
        if isinstance(value, dict):
            sources.append(value)
            for nested_key in ("token_usage", "usage", "usage_metadata"):
                nested = value.get(nested_key)
                if isinstance(nested, dict):
                    sources.append(nested)

    # LLMResult contains generations whose ChatGeneration message commonly
    # carries the same usage metadata.  Keep this fallback compatible with
    # both the old and current LangChain shapes.
    generations = getattr(response, "generations", None)
    if isinstance(generations, list):
        for group in generations:
            if not isinstance(group, (list, tuple)):
                continue
            for generation in group:
                message = getattr(generation, "message", None)
                if message is not None:
                    for attribute in ("usage_metadata", "response_metadata"):
                        value = getattr(message, attribute, None)
                        if isinstance(value, dict):
                            sources.append(value)
                            for nested_key in ("token_usage", "usage"):
                                nested = value.get(nested_key)
                                if isinstance(nested, dict):
                                    sources.append(nested)

    result: dict[str, int] = {}
    for source in sources:
        values = _usage_from_mapping(source)
        for key, value in values.items():
            # Provider metadata is authoritative.  The first value wins so a
            # generic response_metadata field cannot overwrite usage_metadata.
            result.setdefault(key, value)
    if "total_tokens" not in result and {
        "input_tokens",
        "output_tokens",
    }.issubset(result):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def model_identity(value: Any) -> tuple[str | None, str | None]:
    """Extract a safe model/provider label from a model or callback payload."""
    if isinstance(value, dict):
        kwargs = value.get("kwargs") if isinstance(value.get("kwargs"), dict) else {}
        model = (
            value.get("model_name")
            or value.get("model")
            or kwargs.get("model_name")
            or kwargs.get("model")
        )
        identifier = value.get("id") or []
        if isinstance(identifier, (list, tuple)):
            provider = str(identifier[0]) if identifier else None
        else:
            provider = str(identifier) if identifier else None
        return safe_preview(model, 100), safe_preview(provider, 100)
    model = (
        getattr(value, "model_name", None)
        or getattr(value, "model", None)
        or getattr(value, "model_id", None)
    )
    return safe_preview(model, 100), safe_preview(value.__class__.__name__, 100)


def summarize_trace_spans(
    spans: list[dict[str, Any]],
    *,
    trace_id: str | None = None,
    total_duration_ms: int | None = None,
) -> dict[str, Any]:
    """Build the client summary from the same spans shown in the UI."""

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in spans or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        span_id = str(item.get("span_id") or "")
        if span_id and span_id in seen_ids:
            continue
        if span_id:
            seen_ids.add(span_id)
        item["attributes"] = dict(item.get("attributes") or {})
        normalized.append(item)

    llm_spans = [item for item in normalized if item.get("type") == "llm"]
    token_spans = [
        item
        for item in llm_spans
        if item.get("input_tokens") is not None
        or item.get("output_tokens") is not None
        or item.get("total_tokens") is not None
    ]
    input_tokens = sum(
        max(0, int(item.get("input_tokens") or 0))
        for item in token_spans
        if item.get("input_tokens") is not None
    )
    output_tokens = sum(
        max(0, int(item.get("output_tokens") or 0))
        for item in token_spans
        if item.get("output_tokens") is not None
    )
    total_tokens = 0
    has_total_tokens = False
    for item in token_spans:
        if item.get("input_tokens") is not None and item.get("output_tokens") is not None:
            total_tokens += max(0, int(item.get("input_tokens") or 0))
            total_tokens += max(0, int(item.get("output_tokens") or 0))
            has_total_tokens = True
        elif item.get("total_tokens") is not None:
            total_tokens += max(0, int(item.get("total_tokens") or 0))
            has_total_tokens = True

    complete_provider_spans = [
        item
        for item in llm_spans
        if item.get("input_tokens") is not None
        and item.get("output_tokens") is not None
        and item.get("token_source") == "provider"
    ]
    if not token_spans:
        token_source = "unavailable"
    elif len(complete_provider_spans) == len(llm_spans):
        token_source = "provider"
    else:
        token_source = "partial"

    return {
        "trace_id": trace_id,
        "span_count": len(normalized),
        "spans": normalized,
        "total_duration_ms": total_duration_ms,
        "model_calls": sum(
            max(0, int(item.get("model_calls") or 0))
            for item in llm_spans
        ),
        "tool_span_count": sum(
            item.get("type") in {"tool", "retriever", "api"}
            for item in normalized
        ),
        "input_tokens": input_tokens if token_spans else None,
        "output_tokens": output_tokens if token_spans else None,
        "total_tokens": total_tokens if has_total_tokens else None,
        "token_source": token_source,
        "token_usage": {
            "input_tokens": input_tokens if token_spans else None,
            "output_tokens": output_tokens if token_spans else None,
            "total_tokens": total_tokens if has_total_tokens else None,
            "source": token_source,
        },
    }


class TraceRecorder:
    """Thread-safe span recorder shared by one Agent invocation."""

    def __init__(
        self,
        trace_id: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.trace_id = str(trace_id or uuid4())
        self.on_event = on_event
        self._spans: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._private: dict[str, dict[str, Any]] = {}
        self._workflow_spans: dict[str, str] = {}
        self._tool_spans: dict[str, str] = {}
        self._lock = threading.RLock()

    def set_callback(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self.on_event = callback

    def set_trace_id(self, trace_id: str | None) -> None:
        if trace_id:
            self.trace_id = str(trace_id)
            with self._lock:
                for span in self._spans:
                    span["trace_id"] = self.trace_id

    def _publish(self, span: dict[str, Any]) -> None:
        callback = self.on_event
        if callback is None:
            return
        try:
            callback({"type": "trace_span", "span": dict(span)})
        except Exception:
            # Tracing must never change the result of the Agent run.
            return

    def record(
        self,
        *,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        span_type: str = "stage",
        name: str,
        component: str | None = None,
        status: str = "success",
        started_perf: float | None = None,
        duration_ms: int | None = None,
        model: str | None = None,
        provider: str | None = None,
        model_calls: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        token_source: str | None = None,
        tool_name: str | None = None,
        connection_id: str | None = None,
        workflow_id: str | None = None,
        workflow_version: int | None = None,
        node_id: str | None = None,
        input_preview: str | None = None,
        output_preview: str | None = None,
        error: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        current_perf = time.perf_counter()
        ended_at: str | None = None
        with self._lock:
            sid = str(span_id or uuid4())
            item = self._by_id.get(sid)
            if item is None:
                started_perf = current_perf if started_perf is None else started_perf
                item = {
                    "span_id": sid,
                    "parent_span_id": parent_span_id,
                    "trace_id": self.trace_id,
                    "type": span_type,
                    "name": name,
                    "component": component,
                    "status": status,
                    "started_at": now_iso(),
                    "ended_at": None,
                    "duration_ms": None,
                    "model": model,
                    "provider": provider,
                    "model_calls": model_calls or 0,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "token_source": token_source or "unavailable",
                    "tool_name": tool_name,
                    "connection_id": connection_id,
                    "workflow_id": workflow_id,
                    "workflow_version": workflow_version,
                    "node_id": node_id,
                    "input_preview": safe_preview(input_preview),
                    "output_preview": safe_preview(output_preview),
                    "error": safe_preview(error, 500),
                    "attributes": dict(attributes or {}),
                }
                self._spans.append(item)
                self._by_id[sid] = item
                self._private[sid] = {"started_perf": started_perf}
            else:
                if parent_span_id and not item.get("parent_span_id"):
                    item["parent_span_id"] = parent_span_id
                if component:
                    item["component"] = component
                if model:
                    item["model"] = model
                if provider:
                    item["provider"] = provider
                if span_type and not item.get("type"):
                    item["type"] = span_type
                if name:
                    item["name"] = name
                if started_perf is not None:
                    self._private.setdefault(sid, {})["started_perf"] = started_perf

            updates = {
                "status": status,
                "model": model,
                "provider": provider,
                "model_calls": model_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "token_source": token_source,
                "tool_name": tool_name,
                "connection_id": connection_id,
                "workflow_id": workflow_id,
                "workflow_version": workflow_version,
                "node_id": node_id,
                "input_preview": safe_preview(input_preview),
                "output_preview": safe_preview(output_preview),
                "error": safe_preview(error, 500),
            }
            for key, value in updates.items():
                if value is not None:
                    item[key] = value
            if attributes:
                item.setdefault("attributes", {}).update(attributes)

            if item.get("input_tokens") is not None and item.get("output_tokens") is not None:
                item["total_tokens"] = (
                    item["input_tokens"] + item["output_tokens"]
                )
            elif total_tokens is not None:
                item["total_tokens"] = max(0, int(total_tokens))

            if status != "running":
                if duration_ms is None:
                    started_value = self._private.get(sid, {}).get("started_perf", current_perf)
                    duration_ms = round(max(0.0, current_perf - started_value) * 1000)
                item["duration_ms"] = max(0, int(duration_ms))
                ended_at = now_iso()
                item["ended_at"] = ended_at
            elif duration_ms is not None:
                item["duration_ms"] = max(0, int(duration_ms))
            snapshot = dict(item)
            snapshot["attributes"] = dict(item.get("attributes") or {})
        self._publish(snapshot)
        return sid

    def record_workflow_node(self, event: dict[str, Any]) -> None:
        node_id = str(event.get("node_id") or "")
        if not node_id:
            return
        key = ":".join(
            str(event.get(item) or "")
            for item in ("workflow_id", "workflow_run_id", "node_id")
        )
        status = str(event.get("status") or "success")
        span_id = self._workflow_spans.get(key)
        if span_id is None:
            span_id = f"workflow:{event.get('workflow_run_id') or self.trace_id}:{node_id}"
            self._workflow_spans[key] = span_id
        self.record(
            span_id=span_id,
            span_type="stage",
            name=f"阶段：{event.get('node_name') or node_id}",
            component="workflow",
            status="running" if status == "running" else status,
            duration_ms=event.get("duration_ms"),
            workflow_id=str(event.get("workflow_id") or "") or None,
            workflow_version=event.get("workflow_version"),
            node_id=node_id,
            attributes={"node_type": event.get("node_type"), "detail": event.get("detail")},
        )

    def record_llm_stage(
        self,
        name: str,
        *,
        duration_ms: int,
        model: str | None = None,
        provider: str | None = None,
        model_calls: int = 1,
        usage: dict[str, int] | None = None,
        status: str = "success",
        error: str | None = None,
        input_chars: int | None = None,
        output_chars: int | None = None,
    ) -> None:
        usage = usage or {}
        self.record(
            span_type="llm",
            name=f"大模型：{name}",
            component="qwen",
            status=status,
            duration_ms=duration_ms,
            model=model,
            provider=provider or "DashScope/OpenAI-compatible",
            model_calls=max(1, int(model_calls or 1)),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            token_source="provider" if usage else "unavailable",
            error=error,
            attributes={
                key: value
                for key, value in {"input_chars": input_chars, "output_chars": output_chars}.items()
                if value is not None
            },
        )

    def record_tool_event(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("call_id") or "")
        if not call_id:
            return
        span_id = self._tool_spans.get(call_id)
        if span_id is None:
            span_id = f"tool:{call_id}"
            self._tool_spans[call_id] = span_id
        status = str(event.get("status") or "running")
        if status in {"awaiting_confirmation", "awaiting_approval"}:
            span_status = "waiting_confirmation"
        else:
            span_status = status
        self.record(
            span_id=span_id,
            span_type="tool",
            name=f"工具：{event.get('tool_name') or event.get('tool_id') or call_id}",
            component="tool_center",
            status=span_status,
            duration_ms=event.get("duration_ms"),
            tool_name=str(event.get("tool_name") or event.get("tool_id") or "") or None,
            connection_id=str(event.get("connection_id") or "") or None,
            workflow_id=str(event.get("workflow_id") or "") or None,
            workflow_version=event.get("workflow_version"),
            error=event.get("error"),
            attributes={
                key: event.get(key)
                for key in ("risk", "debug", "message", "decision_by")
                if event.get(key) is not None
            },
        )

    def record_data_source(
        self,
        *,
        source_type: str,
        source_name: str,
        duration_ms: int,
        status: str,
        count: int = 0,
        error: str | None = None,
        connection_id: str | None = None,
        tool_name: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        kind = "retriever" if source_type in {"document", "qa", "graph"} else "api"
        self.record(
            span_type=kind,
            name=f"{source_name}",
            component=source_type,
            status=status,
            duration_ms=duration_ms,
            tool_name=tool_name,
            connection_id=connection_id,
            error=error,
            attributes={"source_type": source_type, "result_count": count, **(attributes or {})},
        )

    def export(self, *, total_duration_ms: int | None = None) -> dict[str, Any]:
        with self._lock:
            spans = [dict(item, attributes=dict(item.get("attributes") or {})) for item in self._spans]
        return summarize_trace_spans(
            spans,
            trace_id=self.trace_id,
            total_duration_ms=total_duration_ms,
        )


def current_trace_recorder() -> TraceRecorder | None:
    return _current_trace.get()


def trace_scope(function):
    """Install one recorder for a synchronous Agent invocation."""

    @wraps(function)
    def scoped(*args, **kwargs):
        existing = _current_trace.get()
        if existing is not None:
            return function(*args, **kwargs)
        recorder = TraceRecorder()
        token = _current_trace.set(recorder)
        try:
            return function(*args, **kwargs)
        finally:
            _current_trace.reset(token)

    return scoped


class TraceCallbackHandler(BaseCallbackHandler):
    """Capture provider model spans from DeepAgent/LangGraph callbacks."""

    raise_error = False

    def __init__(self) -> None:
        super().__init__()
        self._runs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _key(run_id: UUID | str) -> str:
        return str(run_id)

    @staticmethod
    def _message_stats(messages: Any) -> dict[str, int]:
        count = 0
        chars = 0
        for group in messages if isinstance(messages, list) else []:
            items = group if isinstance(group, (list, tuple)) else [group]
            for item in items:
                count += 1
                content = getattr(item, "content", item)
                chars += len(str(content or ""))
        return {"message_count": count, "input_chars": chars}

    def _start(
        self,
        serialized: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None,
        stats: dict[str, int],
    ) -> None:
        recorder = current_trace_recorder()
        if recorder is None:
            return
        key = self._key(run_id)
        if key in self._runs:
            return
        model, provider = model_identity(serialized)
        parent = self._runs.get(self._key(parent_run_id), {}).get("span_id") if parent_run_id else None
        span_id = recorder.record(
            span_type="llm",
            name=f"大模型：{model or 'Qwen'}",
            component="qwen",
            provider=provider or "DashScope/OpenAI-compatible",
            model=model,
            status="running",
            parent_span_id=parent,
            attributes=stats,
        )
        self._runs[key] = {
            "span_id": span_id,
            "model": model,
            "provider": provider,
            "name": f"大模型：{model or 'Qwen'}",
        }

    def _finish(
        self,
        run_id: UUID,
        *,
        status: str,
        response: Any = None,
        error: str | None = None,
    ) -> None:
        recorder = current_trace_recorder()
        state = self._runs.pop(self._key(run_id), None)
        if recorder is None or state is None:
            return
        usage = extract_token_usage(response)
        recorder.record(
            span_id=state["span_id"],
            name=state.get("name") or "大模型：Qwen",
            status=status,
            model_calls=1,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            token_source="provider" if usage else "unavailable",
            error=error,
        )

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self._start(
            serialized,
            run_id=run_id,
            parent_run_id=parent_run_id,
            stats=self._message_stats(messages),
        )

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self._start(
            serialized,
            run_id=run_id,
            parent_run_id=parent_run_id,
            stats={"prompt_count": len(prompts or []), "input_chars": sum(len(str(item or "")) for item in prompts or [])},
        )

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        self._finish(run_id, status="success", response=response)

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._finish(run_id, status="error", error=str(error))
