from app.agents.conversation_context import build_semantic_context
from app.conversation import MemoryConversationStore


def test_context_contains_summary_recent_three_turns_and_dictionary_hints(monkeypatch):
    monkeypatch.setattr(
        "app.agents.conversation_context.preview_dictionary_matches",
        lambda text: [{
            "entry_id": "fault-prefix-e",
            "dictionary_type": "fault_code",
            "canonical_value": "E",
            "matched_terms": ["E203"],
            "confidence": 0.98,
        }],
    )
    history = []
    for index in range(5):
        history.extend([
            {"role": "user", "content": f"第{index}轮询问 M1101 的参数"},
            {"role": "assistant", "content": f"第{index}轮回答"},
        ])

    bundle = build_semantic_context(
        history,
        "它的 E203 是什么意思？",
        {"summary_version": 4, "topic_hint": "设备资料"},
        route_catalog=[{"route": "knowledge_qa", "keywords": ["是什么"]}],
    )
    context = bundle["prompt_context"]

    assert context["conversation_summary"]["summary_version"] == 5
    assert len([item for item in context["recent_turns"] if item["role"] == "user"]) == 3
    assert context["dictionary_hints"][0]["entry_id"] == "fault-prefix-e"
    assert context["route_catalog"][0]["route"] == "knowledge_qa"
    assert "它" in context["conversation_summary"]["unresolved_references"]
    assert bundle["next_summary"]["summary_text"]


def test_context_summary_is_persisted_and_removed_with_conversation():
    store = MemoryConversationStore()
    store.create("conversation-1", app_id="app-1")
    summary = {"summary_version": 1, "topic_hint": "北辰47"}

    store.save_context_summary("conversation-1", summary, app_id="app-1")
    assert store.get_context_summary("conversation-1", app_id="app-1") == summary

    store.clear("conversation-1", app_id="app-1")
    assert store.get_context_summary("conversation-1", app_id="app-1") == {}
