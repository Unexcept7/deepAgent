from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from threading import Lock, BoundedSemaphore
from concurrent.futures import Future
import time

from app.config import get_settings
from app.tool_center.context import ContextThreadPoolExecutor


_tool_executor = ContextThreadPoolExecutor(max_workers=8)
_tool_slots = BoundedSemaphore(8)


@dataclass
class SearchBudget:
    deadline: float
    max_calls: int
    calls: int = 0
    lock: Lock = field(default_factory=Lock)
    cache: dict = field(default_factory=dict)
    model_calls: int = 0
    planning_calls: int = 0
    max_model_calls: int = 16
    max_planning_calls: int = 4

    def claim(self):
        with self.lock:
            if time.monotonic() >= self.deadline:
                raise TimeoutError("请求检索时间预算已耗尽")
            if self.calls >= self.max_calls:
                raise RuntimeError("请求检索调用上限已达到")
            self.calls += 1


search_budget: ContextVar[SearchBudget | None] = ContextVar("search_budget", default=None)
source_deadline: ContextVar[float | None] = ContextVar("source_deadline", default=None)


def remaining_timeout(seconds: float) -> float:
    budget = search_budget.get()
    remaining = min(seconds, budget.deadline - time.monotonic()) if budget else seconds
    deadline = source_deadline.get()
    if deadline is not None:
        remaining = min(remaining, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("请求检索时间预算已耗尽")
    return remaining


def cached_search(key, function):
    budget = search_budget.get()
    if budget is None:
        return function()
    with budget.lock:
        future = budget.cache.get(key)
        owner = future is None
        if owner:
            future = Future()
            budget.cache[key] = future
    if owner:
        try:
            budget.claim()
            future.set_result(function())
        except Exception as exc:
            future.set_exception(exc)
    return future.result(timeout=remaining_timeout(60))


def claim_model_call(*, planning: bool = False):
    budget = search_budget.get()
    if budget is None:
        return
    with budget.lock:
        if time.monotonic() >= budget.deadline:
            raise TimeoutError("请求执行时间预算已耗尽")
        if budget.model_calls >= budget.max_model_calls:
            raise RuntimeError("请求模型调用上限已达到")
        if planning and budget.planning_calls >= budget.max_planning_calls:
            raise RuntimeError("请求规划调用上限已达到")
        budget.model_calls += 1
        budget.planning_calls += int(planning)


def invoke_model(model, messages, *, planning=False):
    claim_model_call(planning=planning)
    kwargs = {"timeout": remaining_timeout(60)} if hasattr(model, "client") else {}
    return model.invoke(messages, **kwargs)


def run_search_task(function, seconds):
    timeout = remaining_timeout(seconds)
    if not _tool_slots.acquire(blocking=False):
        raise RuntimeError("检索执行容量已满")
    token = source_deadline.set(time.monotonic() + timeout)
    try:
        future = _tool_executor.submit(function)
    except Exception:
        _tool_slots.release()
        raise
    finally:
        source_deadline.reset(token)
    future.add_done_callback(lambda completed: _tool_slots.release())
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise TimeoutError("检索来源超时") from None


def search_budget_scope(function):
    @wraps(function)
    def scoped(*args, **kwargs):
        settings = get_settings()
        token = search_budget.set(SearchBudget(
            time.monotonic() + getattr(settings, "agent_search_budget_seconds", 120),
            getattr(settings, "agent_max_search_calls", 24),
            max_model_calls=getattr(settings, "agent_max_model_calls", 16),
            max_planning_calls=getattr(settings, "agent_max_planning_calls", 4),
        ))
        try:
            return function(*args, **kwargs)
        finally:
            search_budget.reset(token)
    return scoped
