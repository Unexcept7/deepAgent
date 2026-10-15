from contextvars import ContextVar, copy_context
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from dataclasses import dataclass
from typing import Any, Callable

from app.tool_center.auth import Principal


@dataclass
class ExecutionContext:
    principal: Principal
    workspace_id: str
    app_id: str | None = None
    workflow_id: str | None = None
    trace_id: str | None = None
    environment: str = "production"
    connection_ids: tuple[str, ...] | None = None
    source_types: tuple[str, ...] | None = None
    # ``inherit`` keeps the legacy connection-level RAGFlow allow-list.
    # ``explicit`` is resolved from the published application's concrete
    # knowledge bindings and is enforced by managed connectors.
    knowledge_binding_mode: str = "inherit"
    knowledge_dataset_ids: dict[str, tuple[str, ...]] | None = None
    knowledge_binding_revisions: dict[str, int] | None = None
    # Per-application restrictions for non-document sources. Values are
    # narrowed again by the connection's published policy before execution.
    source_policies: dict[str, dict[str, Any]] | None = None
    on_event: Callable | None = None
    routing_confirmation: bool = False


execution_context: ContextVar[ExecutionContext | None] = ContextVar("tool_center_execution", default=None)


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):
        return super().submit(copy_context().run, fn, *args, **kwargs)


def runtime_scope(function):
    @wraps(function)
    def scoped(*args, **kwargs):
        token = execution_context.set(kwargs.get("tool_context"))
        try:
            return function(*args, **kwargs)
        finally:
            execution_context.reset(token)
    return scoped
