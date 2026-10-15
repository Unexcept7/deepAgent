from langchain_core.messages import AIMessage, AIMessageChunk

from app.agents import graph


class FakeStreamingAgent:
    def get_graph(self):
        return object()

    def stream(self, payload, config=None, stream_mode=None):
        assert stream_mode == ["updates", "messages"]
        yield ("messages", (AIMessageChunk(content="流式回答"), {"node": "model"}))
        yield ("updates", {"model": {"messages": [AIMessage(content="流式回答")]}})


def test_run_agent_emits_langgraph_stream_events(monkeypatch):
    monkeypatch.setattr(graph, "get_agent", lambda **kwargs: FakeStreamingAgent())

    events = []
    result = graph.run_agent([], "你好", on_event=events.append)

    assert result["answer"] == "流式回答"
    assert [item["type"] for item in events if item["type"] != "trace_span"] == [
        "run_started",
        "agent_started",
        "token",
        "node_update",
    ]
    assert next(item for item in events if item["type"] == "token")["content"] == "流式回答"
