"""Context assembly for the semantic intent/entity router.

The router receives three different kinds of context:

* a small, persisted conversation state for continuity across long chats;
* the last few raw turns so exact model names and codes are not lost by
  summarisation;
* deterministic dictionary matches and route-policy candidates as hints.

None of these hints are evidence.  Evidence still has to come from the
configured retrieval tools and pass the backend evidence gate.
"""

from __future__ import annotations

import copy
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Callable
from app.config import get_settings

from app.agents.evidence import (
    canonical_identifier,
    extract_aspects,
    extract_identifiers,
    extract_technical_subjects,
)
from app.dictionary_store import preview_dictionary_matches


RECENT_TURN_COUNT = 3
MAX_RECENT_MESSAGE_CHARS = 1600
MAX_RECENT_MESSAGES = RECENT_TURN_COUNT * 2
MAX_SUMMARY_CHARS = 2400
MAX_CANDIDATE_ENTITIES = 20
MAX_DICTIONARY_HINTS = 24
MAX_RECENT_QUERIES = 6

FOLLOW_UP_MARKERS = (
    "它",
    "这个设备",
    "该设备",
    "这个模块",
    "上述",
    "刚才",
    "前面",
    "同一个",
    "这个故障",
    "这个错误",
)


def _text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _valid_messages(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for message in history or []:
        role = str(message.get("role") or "").strip().lower()
        content = _text(message.get("content"), MAX_RECENT_MESSAGE_CHARS)
        if role not in {"user", "assistant"} or not content:
            continue
        result.append({"role": role, "content": content})
    return result


def _recent_turns(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return the last three user/assistant exchanges, not arbitrary messages."""
    user_indexes = [
        index for index, message in enumerate(history)
        if message.get("role") == "user"
    ]
    if not user_indexes:
        return history[-MAX_RECENT_MESSAGES:]
    start = user_indexes[max(0, len(user_indexes) - RECENT_TURN_COUNT)]
    return history[start:][-MAX_RECENT_MESSAGES:]


def _dictionary_hints(text: str) -> list[dict[str, Any]]:
    try:
        return [
            {
                "entry_id": item.get("entry_id"),
                "dictionary_type": item.get("dictionary_type"),
                "canonical_value": item.get("canonical_value"),
                "matched_terms": list(item.get("matched_terms") or []),
                "confidence": item.get("confidence", 0),
            }
            for item in preview_dictionary_matches(text)[:MAX_DICTIONARY_HINTS]
        ]
    except Exception:
        # A dictionary outage must not make the semantic router unavailable.
        return []


def _collect_candidate_entities(messages: list[dict[str, str]], query: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, text in [
        ("current_query", query),
        *[("conversation_user", item.get("content", "")) for item in reversed(messages) if item.get("role") == "user"],
    ]:
        for value in extract_identifiers(text):
            key = canonical_identifier(value)
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append({
                "value": value,
                "normalized_value": key,
                "source": source,
                "entity_type": "candidate_identifier",
                "confidence": 0.5,
            })
            if len(candidates) >= MAX_CANDIDATE_ENTITIES:
                return candidates
    return candidates


def _recent_queries(messages: list[dict[str, str]], query: str) -> list[str]:
    values = [
        item["content"]
        for item in messages
        if item.get("role") == "user" and item.get("content")
    ]
    values.append(_text(query, MAX_RECENT_MESSAGE_CHARS))
    return list(dict.fromkeys(values))[-MAX_RECENT_QUERIES:]


def _summary_text(summary: dict[str, Any]) -> str:
    topic = str(summary.get("topic_hint") or "未确定")
    goal = str(summary.get("user_goal") or "未确定")
    entities = "、".join(
        str(item.get("value") or "")
        for item in summary.get("candidate_entities") or []
        if item.get("value")
    ) or "无"
    subjects = "、".join(str(item) for item in summary.get("technical_subjects") or []) or "无"
    aspects = "、".join(str(item) for item in summary.get("aspects") or []) or "无"
    return _text(
        f"主题：{topic}；用户目标：{goal}；候选对象：{entities}；"
        f"技术主题：{subjects}；查询属性：{aspects}",
        MAX_SUMMARY_CHARS,
    )


def build_semantic_context(
    history: list[dict[str, Any]] | None,
    query: str,
    previous_summary: dict[str, Any] | None = None,
    *,
    route_catalog: list[dict[str, Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Build the bounded semantic-router context and the next persisted state."""
    messages = _valid_messages(history)
    recent_turns = _recent_turns(messages)
    current_query = _text(query, 4000)
    combined_user_text = "\n".join(
        [item.get("content", "") for item in messages if item.get("role") == "user"]
        + [current_query]
    )
    candidate_entities = _collect_candidate_entities(messages, current_query)
    current_entities = [item for item in candidate_entities if item["source"] == "current_query"]
    active_entities = current_entities
    if not active_entities:
        for message in reversed(messages):
            if message["role"] == "user":
                active_entities = _collect_candidate_entities([], message["content"])
                if active_entities:
                    break
    if active_entities:
        candidate_entities = active_entities
    technical_subjects = extract_technical_subjects(combined_user_text)
    aspects = extract_aspects(combined_user_text)
    dictionary_hints = _dictionary_hints(current_query)
    if not dictionary_hints and recent_turns:
        dictionary_hints = _dictionary_hints(
            "\n".join(item.get("content", "") for item in recent_turns)
        )

    prior = copy.deepcopy(previous_summary or {})
    is_follow_up = any(marker in current_query for marker in FOLLOW_UP_MARKERS)
    prior_entities = prior.get("candidate_entities") or []
    merged_entities: list[dict[str, Any]] = []
    seen_entities: set[str] = set()
    for item in (candidate_entities or (prior_entities if is_follow_up else [])):
        value = _text(item.get("value"), 240)
        key = canonical_identifier(value) or value.casefold()
        if not value or key in seen_entities:
            continue
        seen_entities.add(key)
        merged_entities.append({
            "value": value,
            "normalized_value": _text(item.get("normalized_value") or key, 240),
            "source": item.get("source") or "conversation_summary",
            "entity_type": item.get("entity_type") or "candidate_identifier",
            "confidence": float(item.get("confidence") or 0.5),
        })
    merged_entities = merged_entities[-MAX_CANDIDATE_ENTITIES:]

    recent_user_queries = _recent_queries(messages, current_query)
    topic_hint = (
        prior.get("topic_hint")
        if is_follow_up and prior.get("topic_hint")
        else (recent_user_queries[-2] if len(recent_user_queries) > 1 else current_query)
    )
    user_goal = current_query or prior.get("user_goal") or ""
    unresolved_references = [
        marker for marker in FOLLOW_UP_MARKERS if marker in current_query
    ]
    next_summary = {
        "summary_version": int(prior.get("summary_version") or 0) + 1,
        "topic_hint": _text(topic_hint, 360),
        "user_goal": _text(user_goal, 500),
        "candidate_entities": merged_entities,
        "technical_subjects": list(dict.fromkeys(
            [*((prior.get("technical_subjects") or []) if is_follow_up else []), *technical_subjects]
        ))[-20:],
        "aspects": list(dict.fromkeys(
            [*((prior.get("aspects") or []) if is_follow_up else []), *aspects]
        ))[-20:],
        "unresolved_references": unresolved_references,
        "recent_user_queries": recent_user_queries,
    }
    next_summary["summary_text"] = _summary_text(next_summary)

    prompt_context = {
        "temporal_context": {
            "now": (clock() if clock else datetime.now(ZoneInfo(get_settings().business_timezone))).astimezone(ZoneInfo(get_settings().business_timezone)).isoformat(),
            "timezone": get_settings().business_timezone,
        },
        "conversation_summary": next_summary,
        "recent_turns": recent_turns,
        "dictionary_hints": dictionary_hints,
        "route_catalog": route_catalog or [],
        "context_priority": [
            "current_query",
            "recent_raw_turns",
            "conversation_summary",
            "dictionary_hints",
            "route_catalog_keywords",
        ],
        "safety_note": (
            "上下文仅用于意图、实体和指代消解；助手历史回答、摘要和词典命中都不是事实证据。"
        ),
    }
    return {
        "prompt_context": prompt_context,
        "next_summary": next_summary,
        "diagnostics": {
            "summary_version": next_summary["summary_version"],
            "recent_turn_count": sum(1 for item in recent_turns if item.get("role") == "user"),
            "dictionary_hint_count": len(dictionary_hints),
            "candidate_entity_count": len(merged_entities),
            "unresolved_references": unresolved_references,
        },
    }
