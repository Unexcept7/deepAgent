import pytest

from app.conversation import ConversationAccessError, ConversationStore


def test_conversation_keeps_recent_messages():
    store = ConversationStore(max_messages=2)
    store.append("demo", "user", "介绍 OPC UA")
    store.append("demo", "assistant", "OPC UA 是工业互操作协议")
    store.append("demo", "user", "它和 MQTT 有什么区别？")
    history = store.get("demo")
    assert len(history) == 2
    assert history[-1]["content"] == "它和 MQTT 有什么区别？"


def test_conversation_keeps_only_ten_recent_sessions():
    store = ConversationStore(max_messages=2, max_conversations=10)
    for index in range(11):
        conversation_id = f"demo-{index}"
        store.create(conversation_id)
        store.append(conversation_id, "user", f"第 {index} 个问题")

    conversations = store.list_conversations()

    assert len(conversations) == 10
    assert not store.get("demo-0")
    assert conversations[0]["conversation_id"] == "demo-10"


def test_touch_refreshes_activity_time_without_adding_message():
    store = ConversationStore()
    store.append("older", "user", "旧会话")
    store.append("newer", "user", "新会话")
    before = store.get_metadata("older")
    before_messages = store.get("older")

    touched = store.touch("older")

    assert touched is not None
    assert touched["updated_at"] >= before["updated_at"]
    assert store.get("older") == before_messages
    assert store.list_conversations()[0]["conversation_id"] == "older"


def test_conversation_preserves_assistant_extras():
    store = ConversationStore()
    store.create("demo")
    store.append(
        "demo",
        "assistant",
        "回答",
        citations=[{"document_name": "manual.pdf"}],
        retrieval_hits=[{"document_name": "manual.pdf", "content": "命中片段"}],
        trace={"used_ragflow": True},
    )

    message = store.get("demo")[0]
    assert message["citations"][0]["document_name"] == "manual.pdf"
    assert message["retrieval_hits"][0]["content"] == "命中片段"
    assert message["trace"]["used_ragflow"] is True


def test_conversation_isolated_by_app_id():
    store = ConversationStore()
    store.create("knowledge-session", app_id="enterprise-knowledge-assistant")
    store.append(
        "knowledge-session",
        "user",
        "知识库问题",
        app_id="enterprise-knowledge-assistant",
    )
    store.create("review-session", app_id="evidence-reviewer")
    store.append(
        "review-session",
        "user",
        "证据审核问题",
        app_id="evidence-reviewer",
    )

    assert [
        item["conversation_id"]
        for item in store.list_conversations("enterprise-knowledge-assistant")
    ] == ["knowledge-session"]
    assert [
        item["conversation_id"]
        for item in store.list_conversations("evidence-reviewer")
    ] == ["review-session"]

    with pytest.raises(ConversationAccessError):
        store.get("knowledge-session", app_id="evidence-reviewer")
    with pytest.raises(ConversationAccessError):
        store.append(
            "knowledge-session",
            "assistant",
            "不能写入其他 Agent 的会话",
            app_id="evidence-reviewer",
        )
    with pytest.raises(ConversationAccessError):
        store.clear("knowledge-session", app_id="evidence-reviewer")
