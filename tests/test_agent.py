import json
import pytest
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from app.agents.graph import (
    _build_execution_trace,
    _collect_citations,
    _collect_graph_relations,
    _collect_retrieval_hits,
    _last_assistant_text,
    _merge_final_trace_summary,
    _summarize_evidence,
    _should_expand_evidence_retrieval,
    get_agent,
)
from app.agents import graph


@pytest.fixture(autouse=True)
def isolate_routing_configuration(monkeypatch):
    # Existing agent tests supply retrieval doubles; never read deployment policies.
    monkeypatch.setattr(graph, "evaluate_routing_policy", lambda *a, **k: {})


def test_deep_agent_compiles():
    assert get_agent() is not None


def test_agent_messages_reject_late_system_and_unsupported_history_roles():
    prefetch = ToolMessage(
        name="unified_search",
        tool_call_id="prefetch-1",
        content="{}",
    )

    messages = graph._build_agent_messages(
        [
            {"role": "system", "content": "不应进入历史消息"},
            {"role": "user", "content": "之前的问题"},
            {"role": "tool", "content": "不应进入历史消息"},
            {"role": "assistant", "content": "之前的回答"},
        ],
        "当前问题",
        [],
        [prefetch],
    )

    dict_messages = [item for item in messages if isinstance(item, dict)]
    assert [item["role"] for item in dict_messages] == ["user", "assistant", "user"]
    assert not any(item.get("role") == "system" for item in dict_messages)
    assert messages[-1] is prefetch


def test_progressive_retrieval_expands_only_when_evidence_is_weak():
    assert _should_expand_evidence_retrieval({"status": "supported", "verified_count": 3}) is False
    assert _should_expand_evidence_retrieval({"status": "not_found", "verified_count": 0}) is True
    assert _should_expand_evidence_retrieval({"status": "conflict", "verified_count": 2}) is True
    assert _should_expand_evidence_retrieval(
        {"status": "supported", "verified_count": 1},
        requirement_count=3,
    ) is True


def test_final_trace_summary_replaces_legacy_message_only_totals():
    trace = {
        "model_calls": 5,
        "input_tokens": 500,
        "output_tokens": 100,
        "total_tokens": 600,
    }
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
            "span_id": "answer",
            "type": "llm",
            "model_calls": 1,
            "input_tokens": 923,
            "output_tokens": 152,
            "total_tokens": 1075,
            "token_source": "provider",
        },
        {"span_id": "document", "type": "retriever"},
    ]

    _merge_final_trace_summary(
        trace,
        spans,
        trace_id="trace-final",
        total_duration_ms=32210,
    )

    assert trace["trace_id"] == "trace-final"
    assert trace["model_calls"] == 2
    assert trace["input_tokens"] == 4923
    assert trace["output_tokens"] == 1001
    assert trace["total_tokens"] == 5924
    assert trace["tool_span_count"] == 1
    assert trace["total_duration_ms"] == 32210


def test_collects_citations_from_ragflow_tool_message():
    message = ToolMessage(
        name="search_knowledge_base",
        tool_call_id="call-1",
        content=json.dumps({
            "chunks": [
                {
                    "document_name": "manual.pdf",
                    "document_id": "doc-1",
                    "dataset_id": "dataset-1",
                    "page": 5,
                    "score": 0.9,
                },
                {
                    "document_name": "manual.pdf",
                    "document_id": "doc-1",
                    "dataset_id": "dataset-1",
                    "page": 8,
                    "score": 0.8,
                },
            ]
        }),
    )
    citations = _collect_citations([message])
    assert citations == [
        {
            "document_name": "manual.pdf",
            "page": 5,
            "score": 0.9,
            "url": "/source-documents/dataset-1/doc-1",
        },
        {
            "document_name": "manual.pdf",
            "page": 8,
            "score": 0.8,
            "url": "/source-documents/dataset-1/doc-1",
        },
    ]


def test_collects_only_used_evidence_and_keeps_chunk_excerpt():
    message = ToolMessage(
        name="search_knowledge_base",
        tool_call_id="call-2",
        content=json.dumps({
            "chunks": [
                {
                    "evidence_id": "E-page-13",
                    "chunk_id": "chunk-13",
                    "document_name": "M1401（8路模拟量输出）.pdf",
                    "document_id": "doc-1",
                    "dataset_id": "dataset-1",
                    "page": 13,
                    "content": "第十三页目录内容，没有描述模块用途。",
                    "score": 0.91,
                },
                {
                    "evidence_id": "E-page-18",
                    "chunk_id": "chunk-18",
                    "document_name": "M1401（8路模拟量输出）.pdf",
                    "document_id": "doc-1",
                    "dataset_id": "dataset-1",
                    "page": 18,
                    "content": "该模块具有8路模拟量输出。",
                    "score": 0.86,
                },
            ]
        }, ensure_ascii=False),
    )

    citations = _collect_citations([message], {"E-page-18"})

    assert len(citations) == 1
    assert citations[0]["evidence_id"] == "E-page-18"
    assert citations[0]["page"] == 18
    assert citations[0]["excerpt"] == "该模块具有8路模拟量输出。"


def test_evidence_collectors_skip_malformed_chunk_entries():
    message = ToolMessage(
        name="search_knowledge_base",
        tool_call_id="malformed-chunks",
        content=json.dumps({
            "chunks": [None, {"document_name": "manual.pdf", "content": "有效证据"}],
        }),
    )

    assert len(_collect_citations([message])) == 1
    assert len(_collect_retrieval_hits([message])) == 1
    assert _build_execution_trace([message], 1)["ragflow_calls"] == 1


def test_evidence_summary_skips_malformed_tool_payload_items():
    message = ToolMessage(
        name="search_knowledge_base",
        tool_call_id="malformed-summary",
        content=json.dumps({
            "chunks": [None, {
                "document_name": "manual.pdf",
                "content": "M1401 的通信参数见手册。",
                "evidence_id": "E-1",
                "score": 0.9,
            }],
            "similar_entities": [None],
        }),
    )

    summary = _summarize_evidence([message], "M1401 的通信参数是什么？")

    assert summary["candidate_count"] == 1
    assert len(summary["chunks"]) == 1


def test_graph_relations_are_separate_from_document_citations():
    message = ToolMessage(
        name="unified_search",
        tool_call_id="graph-call-1",
        content=json.dumps({
            "hits": [{
                "source_type": "graph",
                "title": "XP3018",
                "content": "XP3018 -[支持]-> Modbus RTU",
                "evidence_id": "graph:relation-1",
                "metadata": {
                    "graph_query": "XP3018",
                    "graph_relation": {
                        "relation_id": "graph:relation-1",
                        "source_entity": "XP3018",
                        "source_id": "device:xp3018",
                        "relation": "支持",
                        "target_entity": "Modbus RTU",
                        "target_id": "protocol:modbus-rtu",
                        "confidence": 0.92,
                        "evidence_ids": ["chunk-18"],
                        "evidence_status": "bound",
                        "evidence": [{
                            "evidence_id": "chunk-18",
                            "document_name": "XP3018说明书.pdf",
                            "document_id": "doc-1",
                            "dataset_id": "dataset-1",
                            "page": 18,
                            "chunk_id": "chunk-18",
                            "excerpt": "支持 Modbus RTU 通讯。",
                            "url": "/source-documents/dataset-1/doc-1",
                        }],
                    },
                },
            }],
        }, ensure_ascii=False),
    )

    assert _collect_citations([message]) == []
    assert _collect_retrieval_hits([message]) == []
    relations = _collect_graph_relations([message])
    assert relations[0]["relation_id"] == "graph:relation-1"
    assert relations[0]["evidence"][0]["evidence_id"] == "chunk-18"
    assert relations[0]["evidence"][0]["page"] == 18


def test_trace_reports_ragflow_usage_and_tokens():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "search_knowledge_base",
                "args": {"query": "OPC UA"},
                "id": "call-1",
                "type": "tool_call",
            }],
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        ),
        ToolMessage(
            name="search_knowledge_base",
            tool_call_id="call-1",
            content=json.dumps({
                "query": "OPC UA",
                "chunks": [{"document_name": "manual.pdf"}],
                "total": 1,
            }),
        ),
        AIMessage(
            content="最终回答",
            usage_metadata={"input_tokens": 200, "output_tokens": 40, "total_tokens": 240},
        ),
    ]

    trace = _build_execution_trace(messages, total_duration_ms=1234)

    assert trace["route"] == "ragflow"
    assert trace["used_ragflow"] is True
    assert trace["model_calls"] == 2
    assert trace["ragflow_calls"] == 1
    assert trace["total_tokens"] == 360
    assert "manual.pdf" in trace["steps"][1]["detail"]


def test_collects_ragflow_retrieval_hits():
    message = ToolMessage(
        name="search_knowledge_base",
        tool_call_id="call-1",
        content=json.dumps({
            "chunks": [{
                "content": "功能码 02 用于读取开关量输入状态。",
                "document_name": "ft-rs-0808.pdf",
                "document_id": "doc-1",
                "dataset_id": "dataset-1",
                "page": 2,
                "score": 0.82,
            }],
        }),
    )

    assert _collect_retrieval_hits([message]) == [{
        "evidence_id": None,
        "document_name": "ft-rs-0808.pdf",
        "page": 2,
        "score": 0.82,
        "content": "功能码 02 用于读取开关量输入状态。",
        "url": "/source-documents/dataset-1/doc-1",
    }]


def test_trace_reports_direct_model_answer():
    trace = _build_execution_trace([AIMessage(content="你好")], total_duration_ms=100)

    assert trace["route"] == "llm_only"
    assert trace["used_ragflow"] is False
    assert trace["model_calls"] == 1
    assert trace["ragflow_calls"] == 0


def test_last_assistant_text_reads_openai_content_blocks():
    message = AIMessage(content=[
        {"type": "reasoning", "text": "内部推理"},
        {"type": "text", "text": "M1304 的资料存在差异。"},
    ])

    assert _last_assistant_text([message]) == "M1304 的资料存在差异。"


def test_last_assistant_text_logs_when_no_textual_answer(caplog):
    caplog.set_level("WARNING", logger="app.agents.graph")

    assert _last_assistant_text([AIMessage(content=[])]) == ""
    assert "Agent final answer is empty after content normalization" in caplog.text
    assert "last_content_type=list" in caplog.text


def test_run_agent_requirement_pipeline_publishes_fact_and_manual(monkeypatch):
    outputs = iter([
        json.dumps({"requirements": [
            {
                "subject": "XP2001",
                "request": "通道数",
                "output_type": "fact",
                "retrieval_query": "XP2001 通道数",
                "query_span": "几通道",
            },
            {
                "subject": "XP2001",
                "request": "对应手册",
                "output_type": "source",
                "retrieval_query": "XP2001 对应手册",
                "query_span": "对应手册",
            },
        ]}, ensure_ascii=False),
        json.dumps({
            "complete": True,
            "missing_requirements": [],
            "reasons": [],
        }, ensure_ascii=False),
        json.dumps({"results": [
            {"requirement_id": "R1", "value": "单路", "evidence_ids": ["E-1"]},
            {
                "requirement_id": "R2",
                "value": "XP2001单路数据采集模块.pdf",
                "evidence_ids": ["E-1"],
            },
        ]}, ensure_ascii=False),
        json.dumps({
            "plan_complete": True,
            "missing_requests": [],
            "verdicts": [
                {"requirement_id": "R1", "status": "supported", "reason": "原文支持", "evidence_ids": ["E-1"]},
                {"requirement_id": "R2", "status": "supported", "reason": "来源支持", "evidence_ids": ["E-1"]},
            ],
        }, ensure_ascii=False),
        json.dumps({
            "answer": "XP2001 是单路数据采集模块，对应手册为《XP2001单路数据采集模块.pdf》。",
            "used_requirement_ids": ["R1", "R2"],
        }, ensure_ascii=False),
    ])

    class FakeModel:
        def invoke(self, _messages):
            return type("Response", (), {
                "content": next(outputs),
                "usage_metadata": {"input_tokens": 100, "output_tokens": 20},
            })()

    class ProductionAgentDouble:
        def get_graph(self):
            return object()

        def invoke(self, *_args, **_kwargs):
            raise AssertionError("事实型 requirement 流程不应生成自由文本草稿")

    class FakeSearchTool:
        def invoke(self, payload):
            return json.dumps({
                "query": payload["query"],
                "evidence_status": "supported",
                "candidate_count": 1,
                "verified_count": 1,
                "rejected_count": 0,
                "chunks": [{
                    "evidence_id": "E-1",
                    "content": "XP2001 是单路数据采集模块。",
                    "document_name": "XP2001单路数据采集模块.pdf",
                    "document_id": "doc-1",
                    "dataset_id": "dataset-1",
                    "page": 1,
                    "score": 0.91,
                }],
                "total": 1,
            }, ensure_ascii=False)

    settings = SimpleNamespace(
        evidence_mode="generic",
        max_agent_steps=30,
        max_ragflow_calls=1,
        claim_validation_enabled=True,
        requirement_plan_audit_enabled=True,
        requirement_max_items=12,
        requirement_max_parallel_searches=4,
    )
    monkeypatch.setattr(graph, "get_settings", lambda: settings)
    monkeypatch.setattr(graph, "get_agent", lambda **_kwargs: ProductionAgentDouble())
    monkeypatch.setattr(graph, "create_qwen_model", lambda: FakeModel())
    monkeypatch.setattr(graph, "search_knowledge_base", FakeSearchTool())

    result = graph.run_agent([], "XP2001 是几通道？请返回对应手册。")

    assert len(result["requirements"]) == 2
    assert len(result["results"]) == 2
    assert result["coverage"]["complete"] is True
    assert result["results"][1]["value"] == "XP2001单路数据采集模块.pdf"
    assert result["answer"] == (
        "根据已验证资料：\n"
        "XP2001的通道数为单路。\n"
        "XP2001的对应手册为XP2001单路数据采集模块.pdf。"
    )
    assert result["answer_draft"]["answer_plan"]["used_fact_ids"] == ["F:R1", "F:R2"]
    assert result["trace"]["routing_policy"]["answer_assembly"]["gate"] == "fact_plan_repaired"
    assert "逐项需求结果" not in result["answer"]
    # The semantic router attempt is also a visible LLM span, even when the
    # provider fails before returning usage metadata.
    assert result["trace"]["model_calls"] == 6
    assert result["trace"]["ragflow_calls"] == 2
    assert result["trace"]["input_tokens"] == 500
    assert result["trace"]["output_tokens"] == 100
    assert result["trace"]["total_tokens"] == 600
    assert result["trace"]["span_count"] >= 5
    assert any(item["type"] == "llm" and "需求规划" in item["name"] for item in result["trace"]["spans"])
    assert any(item["type"] == "retriever" and item["component"] == "document" for item in result["trace"]["spans"])
