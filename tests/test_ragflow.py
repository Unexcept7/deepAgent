import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.tools import ragflow


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


def test_configured_dataset_ids_splits_both_dataset_settings():
    config = Settings(
        ragflow_dataset_id="dataset-1,dataset-2",
        ragflow_dataset_ids="dataset-2,dataset-3",
    )

    assert config.configured_dataset_ids() == [
        "dataset-1",
        "dataset-2",
        "dataset-3",
    ]


def settings(auto_discover: bool = False, dataset_ids=None):
    return SimpleNamespace(
        ragflow_base_url="http://localhost:9380",
        ragflow_api_key="test-key",
        ragflow_auto_discover_datasets=auto_discover,
        ragflow_retrieval_top_k=24,
        configured_dataset_ids=lambda: dataset_ids if dataset_ids is not None else ["dataset-1"],
    )


def test_search_normalizes_ragflow_chunks(monkeypatch):
    monkeypatch.setattr(ragflow, "get_settings", lambda: settings())
    monkeypatch.setattr(
        ragflow.requests,
        "post",
        lambda *args, **kwargs: FakeResponse({
            "code": 0,
            "data": {
                "total": 1,
                "chunks": [{
                    "content": "OPC UA supports information modeling.",
                    "document_keyword": "opc-ua.pdf",
                    "document_id": "document-1",
                    "dataset_id": "dataset-1",
                    "positions": [[12, 0, 0]],
                    "similarity": 0.91,
                }],
            },
        }),
    )
    result = ragflow.RAGFlowClient().search("介绍 OPC UA")
    assert result["chunks"][0]["document_name"] == "opc-ua.pdf"
    assert result["chunks"][0]["document_id"] == "document-1"
    assert result["chunks"][0]["page"] == 12
    assert result["dataset_ids"] == ["dataset-1"]


def test_search_treats_malformed_chunk_payload_as_empty(monkeypatch):
    monkeypatch.setattr(ragflow, "get_settings", lambda: settings())
    monkeypatch.setattr(
        ragflow.requests,
        "post",
        lambda *args, **kwargs: FakeResponse({
            "code": 0,
            "data": {"total": "invalid", "chunks": {"unexpected": "object"}},
        }),
    )

    result = ragflow.RAGFlowClient().search("测试问题")

    assert result["chunks"] == []
    assert result["total"] == 0


def test_list_datasets_skips_malformed_items(monkeypatch):
    monkeypatch.setattr(ragflow, "get_settings", lambda: settings(auto_discover=True))
    monkeypatch.setattr(
        ragflow.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({
            "code": 0,
            "data": [None, {"id": "dataset-1", "name": "工业协议"}],
        }),
    )

    assert ragflow.RAGFlowClient().list_datasets() == [
        {"id": "dataset-1", "name": "工业协议"},
    ]


def test_list_datasets_rejects_non_object_response(monkeypatch):
    monkeypatch.setattr(ragflow, "get_settings", lambda: settings(auto_discover=True))
    monkeypatch.setattr(
        ragflow.requests,
        "get",
        lambda *args, **kwargs: FakeResponse([]),
    )

    with pytest.raises(RuntimeError, match="不是 JSON 对象"):
        ragflow.RAGFlowClient().list_datasets()


def test_extract_page_accepts_flat_and_string_positions():
    assert ragflow.RAGFlowClient._extract_page([13]) == 13
    assert ragflow.RAGFlowClient._extract_page([["18", 0, 0]]) == 18
    assert ragflow.RAGFlowClient._extract_page([], {"page_num": "7"}) == 7
    assert ragflow.RAGFlowClient._extract_page([], {"metadata": {"page_number": 9}}) == 9
    assert ragflow.RAGFlowClient._extract_page({"page": 11}) == 11
    assert ragflow.RAGFlowClient._extract_page([], {"page": 0}) is None


def test_search_applies_retrieval_and_chunk_budgets(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ragflow,
        "get_settings",
        lambda: settings(),
    )

    def fake_post(*args, **kwargs):
        captured["body"] = kwargs["json"]
        return FakeResponse({
            "code": 0,
            "data": {
                "total": 1,
                "chunks": [{
                    "content": "OPC UA " + ("long content " * 500),
                    "document_keyword": "opc-ua.pdf",
                    "document_id": "document-1",
                    "dataset_id": "dataset-1",
                    "similarity": 0.91,
                }],
            },
        })

    monkeypatch.setattr(ragflow.requests, "post", fake_post)
    result = ragflow.RAGFlowClient().search("介绍 OPC UA", top_k=20)

    assert captured["body"]["page_size"] == 20
    assert captured["body"]["top_k"] == 24
    assert captured["body"]["similarity_threshold"] == 0.0


def test_search_keeps_retrieval_pool_at_least_final_page_size(monkeypatch):
    config = settings()
    config.ragflow_top_k = 50
    config.ragflow_retrieval_top_k = 24
    monkeypatch.setattr(ragflow, "get_settings", lambda: config)
    captured = {}

    def fake_post(*args, **kwargs):
        captured["body"] = kwargs["json"]
        return FakeResponse({"code": 0, "data": {"total": 0, "chunks": []}})

    monkeypatch.setattr(ragflow.requests, "post", fake_post)
    ragflow.RAGFlowClient().search("测试问题", top_k=30)

    assert captured["body"]["page_size"] == 30
    assert captured["body"]["top_k"] == 30


def test_search_sends_configured_reranker_id(monkeypatch):
    config = settings()
    config.ragflow_rerank_id = "qwen3-rerank"
    monkeypatch.setattr(ragflow, "get_settings", lambda: config)
    captured = {}

    def fake_post(*args, **kwargs):
        captured["body"] = kwargs["json"]
        return FakeResponse({"code": 0, "data": {"total": 0, "chunks": []}})

    monkeypatch.setattr(ragflow.requests, "post", fake_post)
    result = ragflow.RAGFlowClient().search("测试问题")

    assert captured["body"]["rerank_id"] == "qwen3-rerank"
    assert result["rerank_id"] == "qwen3-rerank"


def test_search_probes_failing_rerank_candidate_without_logging_content(monkeypatch, caplog):
    config = settings()
    config.ragflow_rerank_id = "qwen3-rerank"
    monkeypatch.setattr(ragflow, "get_settings", lambda: config)
    calls = []
    probe_chunks = [
        {
            "content": f"candidate-{index}",
            "document_keyword": "设备手册.pdf",
            "document_id": "document-1",
            "dataset_id": "dataset-1",
            "chunk_id": f"chunk-{index}",
        }
        for index in range(34)
    ]
    probe_chunks[33]["content"] = "private candidate body must not appear in logs"

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        if len(calls) == 1:
            return FakeResponse({
                "code": 102,
                "message": "ValueError: document index:33 is invalid, it should not be larger than 50000: input.documents",
            })
        return FakeResponse({
            "code": 0,
            "data": {"total": len(probe_chunks), "chunks": probe_chunks},
        })

    monkeypatch.setattr(ragflow.requests, "post", fake_post)
    caplog.set_level("WARNING", logger=ragflow.__name__)

    with pytest.raises(RuntimeError, match="document index:33"):
        ragflow.RAGFlowClient().search("M1401 的接线方式")

    assert calls[0]["rerank_id"] == "qwen3-rerank"
    assert "rerank_id" not in calls[1]
    assert calls[1]["page_size"] == 34
    assert calls[1]["top_k"] == 34
    assert '"target_index": 33' in caplog.text
    assert '"candidate_present": true' in caplog.text
    assert '"document_name": "设备手册.pdf"' in caplog.text
    assert "private candidate body must not appear in logs" not in caplog.text


def test_search_does_not_auto_discover_when_dataset_ids_are_bound(monkeypatch):
    monkeypatch.setattr(
        ragflow,
        "get_settings",
        lambda: settings(auto_discover=True, dataset_ids=["bound-1", "bound-2"]),
    )
    monkeypatch.setattr(
        ragflow.RAGFlowClient,
        "list_datasets",
        lambda self: (_ for _ in ()).throw(AssertionError("不应自动发现全部知识库")),
    )
    captured = {}

    def fake_post(*args, **kwargs):
        captured["body"] = kwargs["json"]
        return FakeResponse({"code": 0, "data": {"total": 0, "chunks": []}})

    monkeypatch.setattr(ragflow.requests, "post", fake_post)
    result = ragflow.RAGFlowClient().search("测试问题")

    assert captured["body"]["dataset_ids"] == ["bound-1", "bound-2"]
    assert result["dataset_ids"] == ["bound-1", "bound-2"]


def test_search_auto_discovers_only_when_no_dataset_is_bound(monkeypatch):
    monkeypatch.setattr(
        ragflow,
        "get_settings",
        lambda: settings(auto_discover=True, dataset_ids=[]),
    )
    monkeypatch.setattr(
        ragflow.RAGFlowClient,
        "list_datasets",
        lambda self: [
            {"id": "discovered-1", "name": "知识库 1"},
            {"id": "discovered-2", "name": "知识库 2"},
        ],
    )
    captured = {}

    def fake_post(*args, **kwargs):
        captured["body"] = kwargs["json"]
        return FakeResponse({"code": 0, "data": {"total": 0, "chunks": []}})

    monkeypatch.setattr(ragflow.requests, "post", fake_post)
    result = ragflow.RAGFlowClient().search("测试问题")

    assert captured["body"]["dataset_ids"] == ["discovered-1", "discovered-2"]
    assert result["dataset_ids"] == ["discovered-1", "discovered-2"]


def test_search_tool_returns_json(monkeypatch):
    monkeypatch.setattr(
        ragflow.RAGFlowClient,
        "search",
        lambda self, query, top_k: {"query": query, "chunks": [], "total": 0},
    )
    result = ragflow.search_knowledge_base.invoke({"query": "测试问题", "top_k": 5})
    assert json.loads(result)["query"] == "测试问题"


def test_list_datasets_respects_ragflow_page_size_limit(monkeypatch):
    monkeypatch.setattr(ragflow, "get_settings", lambda: settings(auto_discover=True))
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs["params"])
        return FakeResponse({
            "code": 0,
            "data": [{"id": "dataset-1", "name": "工业协议"}],
        })

    monkeypatch.setattr(ragflow.requests, "get", fake_get)
    datasets = ragflow.RAGFlowClient().list_datasets()

    assert datasets == [{"id": "dataset-1", "name": "工业协议"}]
    assert calls == [{"page": 1, "page_size": 100}]


def test_list_documents_reads_current_ragflow_docs_shape(monkeypatch):
    monkeypatch.setattr(ragflow, "get_settings", lambda: settings())
    monkeypatch.setattr(
        ragflow.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({
            "code": 0,
            "data": {
                "docs": [{
                    "id": "document-1",
                    "name": "设备手册.pdf",
                    "run": "DONE",
                    "chunk_count": 26,
                }],
                "total": 1,
            },
        }),
    )

    documents = ragflow.RAGFlowClient().list_documents("dataset-1")

    assert documents == [{
        "id": "document-1",
        "name": "设备手册.pdf",
        "status": "DONE",
        "chunk_count": 26,
        "updated_at": None,
    }]
