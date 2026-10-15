import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.agents import graph
from app.agents.history_aware import (
    build_clarification_suggestions,
    build_related_question_suggestions,
    has_context_reference,
    resolve_history_aware_query,
)
from app.conversation import MemoryConversationStore
from app.schemas.query import QueryRequest


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("它的深度是多少？", "XP3018 的深度是多少？"),
        ("深度是多少？", "XP3018 深度是多少？"),
        ("这个模块支持哪些功能码？", "XP3018 支持哪些功能码？"),
        ("该设备的工作电压是 24V 还是 12V？", "XP3018 的工作电压是 24V 还是 12V？"),
        ("它的接口是什么？I/O 组合又是什么？", "XP3018 的接口是什么？I/O 组合又是什么？"),
    ],
)
def test_clarification_suggestions_preserve_the_current_request(query, expected):
    assert build_clarification_suggestions(query, ["XP3018"]) == [
        {"text": expected, "query": expected},
    ]


def test_clarification_suggestions_prefer_named_objects_over_summary_topics():
    suggestions = build_clarification_suggestions(
        "它的深度是多少？",
        ["M120", "m120", "M160", "XP3018", "I/O 组合", "接口", ""],
    )

    assert [item["query"] for item in suggestions] == [
        "M120 的深度是多少？",
        "M160 的深度是多少？",
        "XP3018 的深度是多少？",
    ]


def test_clarification_suggestions_support_chinese_names_and_are_bounded():
    suggestions = build_clarification_suggestions(
        "它的接线图在哪一页？",
        ["温度采集模块", "压力采集模块"],
    )
    assert suggestions[0]["query"] == "温度采集模块 的接线图在哪一页？"
    assert len(build_clarification_suggestions("深度是多少？", [f"M{index}" for index in range(10)])) == 4
    assert build_clarification_suggestions("", ["XP3018"]) == []


def test_related_questions_replace_debug_actions_with_neighboring_facts():
    suggestions = build_related_question_suggestions(
        "M1401 的量程参数是多少？",
        ["查看逐项证据和来源", "换一种更具体的方式检索"],
    )

    assert [item["query"] for item in suggestions] == [
        "M1401 支持哪些输入或输出类型？",
        "M1401 各输入类型的接线方式是什么？",
        "M1401 的通信参数和寄存器地址是什么？",
        "M1401 的供电电压和功耗是多少？",
    ]
    assert all("证据" not in item["query"] for item in suggestions)
    assert all("来源" not in item["query"] for item in suggestions)


def test_related_questions_keep_useful_model_questions_and_make_them_standalone():
    suggestions = build_related_question_suggestions(
        "M1401 的量程参数是多少？",
        ["它支持哪些输入类型？", "查看证据片段"],
    )

    assert suggestions[0] == {
        "text": "M1401 支持哪些输入类型？",
        "query": "M1401 支持哪些输入类型？",
    }
    assert all("证据" not in item["query"] for item in suggestions)


def test_related_questions_only_exist_for_contextual_follow_ups():
    assert has_context_reference("它的深度是多少？")
    assert has_context_reference("上述设备支持什么协议？")
    assert not has_context_reference("M1401 的深度是多少？")


def test_selecting_a_clarification_question_resolves_the_ambiguity_without_a_model():
    history = [{"role": "user", "content": "比较 M120、M160 和 XP3018 的接口"}]

    def forbidden_model():
        raise AssertionError("An explicit user choice must not require another rewrite model")

    ambiguous = resolve_history_aware_query(history, "它的深度是多少？", model_factory=forbidden_model)
    assert ambiguous["status"] == "ambiguous"
    for suggestion in build_clarification_suggestions("它的深度是多少？", ambiguous["candidates"]):
        selected = resolve_history_aware_query(history, suggestion["query"], model_factory=forbidden_model)
        assert selected["status"] == "unchanged"
        assert len(selected["explicit_entities"]) == 1
        assert selected["query"] == suggestion["query"]


def test_agent_clarifies_with_specific_questions_without_retrieving_or_calling_a_model(monkeypatch):
    monkeypatch.setattr(graph, "routing_policy_catalog", lambda: [])
    monkeypatch.setattr(graph, "build_semantic_context", lambda *args, **kwargs: {"next_summary": {}})

    def forbidden_call(*args, **kwargs):
        raise AssertionError("An ambiguous query must wait for the user's selection")

    monkeypatch.setattr(graph, "_create_stage_model", forbidden_call)
    monkeypatch.setattr(graph, "get_agent", forbidden_call)
    result = graph.run_agent(
        [{"role": "user", "content": "M120 和 M160 的接口分别是什么？"}],
        "它的深度是多少？",
    )

    assert result["evidence"]["status"] == "context_ambiguous"
    assert result["trace"]["used_ragflow"] is False
    assert [item["query"] for item in result["suggestions"]] == [
        "M120 的深度是多少？", "M160 的深度是多少？",
    ]
    assert result["retrieval_hits"] == []


@pytest.mark.parametrize("streaming", [False, True])
def test_clarification_suggestions_survive_api_serialization_and_history(monkeypatch, streaming):
    store = MemoryConversationStore()
    monkeypatch.setattr(main, "conversation_store", store)
    result = graph._context_ambiguity_result(
        "它的深度是多少？",
        {"status": "ambiguous", "candidates": ["M120", "M160", "XP3018"]},
        1,
    )
    monkeypatch.setattr(main, "run_agent", lambda *args, **kwargs: result)
    monkeypatch.setattr(main, "_record_application_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_tool_execution_context", lambda *args, **kwargs: None)

    if streaming:
        response = TestClient(main.app).post("/chat/stream", json={"query": "它的深度是多少？"})
        assert response.status_code == 200
        final_block = next(block for block in response.text.split("\n\n") if "event: final" in block)
        payload = json.loads(next(line[6:] for line in final_block.splitlines() if line.startswith("data: ")))
    else:
        payload = main.chat(QueryRequest(query="它的深度是多少？")).model_dump()

    assert payload["suggestions"] == result["suggestions"]
    assert payload["evidence"]["status"] == "context_ambiguous"
    stored = store.get(payload["conversation_id"])
    assert stored[-1]["suggestions"] == result["suggestions"]


def test_old_clarification_history_gets_specific_choices_without_mutating_the_store(monkeypatch):
    store = MemoryConversationStore()
    monkeypatch.setattr(main, "conversation_store", store)
    store.create("clarification-history", app_id="app-1")
    store.append("clarification-history", "user", "它的深度是多少？", app_id="app-1")
    old_suggestions = [{"text": "查询 M120 的相关参数", "query": "查询 M120 的相关参数"}]
    store.append(
        "clarification-history", "assistant", "请选择设备", app_id="app-1",
        evidence={"status": "context_ambiguous"},
        trace={"context_resolution": {"candidates": ["M120", "M160", "接口"]}},
        suggestions=old_suggestions,
    )
    updated_at = store.get_metadata("clarification-history", app_id="app-1")["updated_at"]

    payload = main.conversation_messages("clarification-history", app_id="app-1")
    assert [item["query"] for item in payload["messages"][-1]["suggestions"]] == [
        "M120 的深度是多少？", "M160 的深度是多少？",
    ]
    assert store.get("clarification-history", app_id="app-1")[-1]["suggestions"] == old_suggestions
    assert store.get_metadata("clarification-history", app_id="app-1")["updated_at"] == updated_at


def test_production_page_serves_the_suggestions_assets():
    client = TestClient(main.app)
    page = client.get("/")
    assert page.status_code == 200
    assert '/assets/suggestions.js?v=20261009-1' in page.text
    assert '/assets/suggestions.css?v=20261009-1' in page.text
    assert client.get("/assets/suggestions.js?v=20261009-1").status_code == 200
    assert client.get("/assets/suggestions.css?v=20261009-1").status_code == 200
