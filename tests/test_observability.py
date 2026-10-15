from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.observability import (
    TraceCallbackHandler,
    TraceRecorder,
    current_trace_recorder,
    extract_token_usage,
    summarize_trace_spans,
    trace_scope,
)
from app.schemas.response import AgentResponse


def test_extract_token_usage_supports_langchain_message_metadata():
    message = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 17, "output_tokens": 5, "total_tokens": 22},
    )

    assert extract_token_usage(message) == {
        "input_tokens": 17,
        "output_tokens": 5,
        "total_tokens": 22,
    }


def test_trace_recorder_exports_parent_spans_and_totals():
    events = []
    recorder = TraceRecorder(trace_id="trace-test", on_event=events.append)
    parent = recorder.record(
        span_type="stage",
        name="阶段：检索",
        component="workflow",
        status="running",
    )
    child = recorder.record(
        span_type="llm",
        name="大模型：需求规划",
        component="qwen",
        parent_span_id=parent,
        status="success",
        duration_ms=12,
        model_calls=1,
        input_tokens=30,
        output_tokens=8,
        token_source="provider",
    )
    recorder.record(
        span_id=parent,
        span_type="stage",
        name="阶段：检索",
        component="workflow",
        status="success",
        duration_ms=25,
    )

    snapshot = recorder.export(total_duration_ms=40)
    assert snapshot["trace_id"] == "trace-test"
    assert snapshot["span_count"] == 2
    assert snapshot["model_calls"] == 1
    assert snapshot["token_usage"]["total_tokens"] == 38
    assert snapshot["spans"][1]["parent_span_id"] == parent
    assert snapshot["spans"][1]["span_id"] == child
    assert any(event["type"] == "trace_span" for event in events)


def test_trace_summary_uses_unique_visible_llm_spans_for_all_token_totals():
    spans = [
        {
            "span_id": "semantic-router",
            "type": "llm",
            "model_calls": 1,
            "input_tokens": 4000,
            "output_tokens": 849,
            "total_tokens": 4849,
            "token_source": "provider",
        },
        {
            "span_id": "requirement-plan",
            "type": "llm",
            "model_calls": 1,
            "input_tokens": 562,
            "output_tokens": 144,
            "total_tokens": 706,
            "token_source": "provider",
        },
        {
            "span_id": "requirement-plan",
            "type": "llm",
            "model_calls": 1,
            "input_tokens": 562,
            "output_tokens": 144,
            "total_tokens": 706,
            "token_source": "provider",
        },
        {"span_id": "ragflow", "type": "retriever"},
    ]

    summary = summarize_trace_spans(
        spans,
        trace_id="trace-test",
        total_duration_ms=32210,
    )

    assert summary["span_count"] == 3
    assert summary["model_calls"] == 2
    assert summary["input_tokens"] == 4562
    assert summary["output_tokens"] == 993
    assert summary["total_tokens"] == 5555
    assert summary["token_usage"]["total_tokens"] == 5555
    assert summary["tool_span_count"] == 1


def test_callback_handler_records_provider_usage_without_exposing_content():
    @trace_scope
    def invoke_callback():
        recorder = current_trace_recorder()
        handler = TraceCallbackHandler()
        run_id = uuid4()
        handler.on_chat_model_start(
            {"kwargs": {"model": "qwen-test"}, "id": ["langchain_openai"]},
            [[HumanMessage(content="private prompt")]],
            run_id=run_id,
        )
        handler.on_llm_end(
            LLMResult(generations=[[
                ChatGeneration(message=AIMessage(
                    content="private answer",
                    usage_metadata={"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
                )),
            ]]),
            run_id=run_id,
        )
        return recorder.export()

    snapshot = invoke_callback()
    span = next(item for item in snapshot["spans"] if item["type"] == "llm")
    assert span["model"] == "qwen-test"
    assert span["input_tokens"] == 11
    assert span["output_tokens"] == 4
    assert span["total_tokens"] == 15
    assert "private prompt" not in str(span)
    assert "private answer" not in str(span)


def test_agent_response_schema_preserves_trace_spans():
    response = AgentResponse.model_validate({
        "conversation_id": "conversation-1",
        "answer": "ok",
        "trace": {
            "route": "ragflow",
            "trace_id": "trace-1",
            "token_source": "provider",
            "spans": [{
                "span_id": "span-1",
                "type": "retriever",
                "name": "RAGFlow 知识库",
                "component": "document",
                "status": "success",
                "duration_ms": 8,
                "tool_name": "search_knowledge_base",
            }],
        },
    })

    assert response.trace is not None
    assert response.trace.spans[0].component == "document"
    assert response.trace.spans[0].duration_ms == 8
