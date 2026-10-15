from app.search.models import SearchHit, SearchRequest
from app.search.service import UnifiedSearchService
from app.search.connectors import SearchConnector


class FakeConnector(SearchConnector):
    def __init__(self, source_type, hits, available=True):
        self.source_type = source_type
        self.name = f"fake-{source_type}"
        self.hits = hits
        self._available = available

    @property
    def available(self):
        return self._available

    def search(self, request):
        return self.hits


class FailingConnector(FakeConnector):
    def __init__(self, source_type, error="upstream down"):
        super().__init__(source_type, [])
        self.calls = 0
        self.error = error

    def search(self, request):
        self.calls += 1
        raise RuntimeError(self.error)


def hit(source_type, title, score, content="同一证据"):
    return SearchHit(
        source_type=source_type,
        source_name=f"{source_type}-source",
        title=title,
        content=content,
        score=score,
        authority=0.9,
        freshness=0.9,
        entity_match=0.8,
        evidence_id=f"{source_type}:{title}",
    )


def test_unified_search_merges_sources_and_returns_trace(monkeypatch):
    monkeypatch.setattr(
        "app.search.service.get_settings",
        lambda: type("Settings", (), {
            "search_sources": "document,qa,graph",
            "search_max_parallel": 3,
        })(),
    )
    service = UnifiedSearchService([
        FakeConnector("document", [hit("document", "手册", 0.7)]),
        FakeConnector("qa", [hit("qa", "标准答案", 0.8)]),
        FakeConnector("graph", [hit("graph", "设备关系", 0.9)]),
    ])

    response = service.search(SearchRequest(query="E-203", top_k=5))

    assert response.total == 3
    assert response.sources_used == ["document", "graph", "qa"]
    assert response.hits[0].title == "设备关系"
    assert len(response.trace) == 3
    assert all(item["status"] == "success" for item in response.trace)


def test_unified_search_skips_unavailable_connector(monkeypatch):
    monkeypatch.setattr(
        "app.search.service.get_settings",
        lambda: type("Settings", (), {
            "search_sources": "document,graph",
            "search_max_parallel": 2,
        })(),
    )
    service = UnifiedSearchService([
        FakeConnector("document", [hit("document", "手册", 0.7)]),
        FakeConnector("graph", [], available=False),
    ])

    response = service.search(SearchRequest(query="E-203"))

    assert response.total == 1
    assert any("fake-graph 未配置" in warning for warning in response.warnings)
    assert not response.errors


def test_unified_search_respects_explicit_empty_source_scope(monkeypatch):
    monkeypatch.setattr(
        "app.search.service.get_settings",
        lambda: type("Settings", (), {
            "search_sources": "document,qa",
            "search_max_parallel": 2,
        })(),
    )
    service = UnifiedSearchService([
        FakeConnector("document", [hit("document", "手册", 0.7)]),
        FakeConnector("qa", [hit("qa", "标准答案", 0.8)]),
    ])

    response = service.search(SearchRequest(query="E-203", source_types=[]))

    assert response.total == 0
    assert response.sources_requested == []
    assert any("没有可用" in warning for warning in response.warnings)


def test_unified_search_opens_circuit_after_repeated_connector_failures(monkeypatch):
    monkeypatch.setattr(
        "app.search.service.get_settings",
        lambda: type("Settings", (), {
            "search_sources": "document",
            "search_max_parallel": 1,
            "search_circuit_failure_threshold": 1,
            "search_circuit_cooldown_seconds": 60,
        })(),
    )
    connector = FailingConnector("document")
    service = UnifiedSearchService([connector])

    first = service.search(SearchRequest(query="E-203"))
    second = service.search(SearchRequest(query="E-203"))

    assert first.errors
    assert second.total == 0
    assert connector.calls == 1
    assert any("熔断" in warning for warning in second.warnings)
    assert service.source_status()[0]["state"] == "circuit_open"


def test_graph_connector_uses_identifiers_instead_of_full_natural_language_query(monkeypatch):
    from app.search import connectors

    class FakeGraphStore:
        configured = True

        def __init__(self, _settings):
            self.queries = []

        def search_entity_neighbors(self, query, **_kwargs):
            self.queries.append(query)
            return []

    monkeypatch.setattr(connectors, "Neo4jGraphStore", FakeGraphStore)
    connector = connectors.Neo4jSearchConnector()

    result = connector.search(
        SearchRequest(query="深圳金鸽 M120 和 M160 的接口与 I/O 组合分别是什么？")
    )

    assert result == []
    assert connector.store.queries == ["M120", "M160"]


def test_qa_connector_matches_chinese_technical_terms(monkeypatch, tmp_path):
    from app.search import connectors

    qa_path = tmp_path / "qa.json"
    qa_path.write_text(
        '[{"question":"M1304 的通信参数是什么？","answer":"默认波特率为 19200bps。"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        connectors,
        "get_settings",
        lambda: type("Settings", (), {"qa_data_path": str(qa_path)})(),
    )

    hits = connectors.QAFileConnector().search(
        SearchRequest(query="请查询 M1304 默认通信参数")
    )

    assert len(hits) == 1
    assert hits[0].title == "M1304 的通信参数是什么？"


def test_ragflow_connector_deduplicates_document_queries(monkeypatch):
    from app.search import connectors
    from app.search.models import RetrievalPlan

    calls = []

    class FakeClient:
        def search(self, query, top_k):
            calls.append((query, top_k))
            return {"chunks": [None]}

    monkeypatch.setattr(connectors, "RAGFlowClient", FakeClient)
    monkeypatch.setattr(
        connectors,
        "get_settings",
        lambda: type("Settings", (), {
            "ragflow_base_url": "http://ragflow",
            "ragflow_api_key": "key",
        })(),
    )

    connector = connectors.RAGFlowSearchConnector()
    result = connector.search(SearchRequest(
        query="M1401 接线",
        query_plan=RetrievalPlan(document_queries=["M1401 接线", "M1401 接线"]),
    ))

    assert result == []
    assert calls == [("M1401 接线", 12)]
