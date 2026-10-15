import pytest
from types import SimpleNamespace

from app.search.models import RetrievalPlan, SearchRequest, StructuredQueryPlan
from app.search.structured_query import (
    StructuredQueryPolicyError,
    merge_sql_scope,
    normalize_sql_hits,
    sql_request_payload,
    validate_structured_queries,
)
from app.tool_center.service import ToolCenterService
from app.tool_center.store import ToolCenterError


def test_application_scope_can_only_narrow_connection_scope():
    merged = merge_sql_scope(
        {
            "datasets": {
                "alarm": {"fields": ["device_id", "code", "occurred_at"]},
                "work_order": {"fields": ["device_id", "status"]},
            },
            "max_rows": 100,
            "allow_natural_query": False,
        },
        {
            "datasets": {"alarm": {"fields": ["device_id", "code"]}},
            "max_rows": 20,
            "allow_natural_query": True,
        },
    )

    assert set(merged["datasets"]) == {"alarm"}
    assert merged["datasets"]["alarm"]["fields"] == ["code", "device_id"]
    assert merged["max_rows"] == 20
    assert merged["allow_natural_query"] is False


def test_structured_query_is_checked_against_dataset_and_field_allowlists():
    allowed = StructuredQueryPlan(
        dataset="alarm",
        select=["device_id", "code"],
        filters=[{"field": "code", "operator": "eq", "value": "E-203"}],
    )
    assert validate_structured_queries(
        [allowed],
        {"datasets": {"alarm": {"fields": ["device_id", "code"]}}},
        require_allowlist=True,
    ) == [allowed]

    with pytest.raises(StructuredQueryPolicyError, match="字段未授权"):
        validate_structured_queries(
            [allowed.model_copy(update={"select": ["device_secret"]})],
            {"datasets": {"alarm": {"fields": ["device_id", "code"]}}},
            require_allowlist=True,
        )

    with pytest.raises(StructuredQueryPolicyError, match="数据集未授权"):
        validate_structured_queries(
            [allowed.model_copy(update={"dataset": "payroll"})],
            {"datasets": {"alarm": {"fields": ["device_id", "code"]}}},
            require_allowlist=True,
        )


def test_gateway_payload_carries_trusted_context_and_no_sql():
    plan = StructuredQueryPlan(dataset="alarm", select=["device_id"])
    request = SearchRequest(
        query="设备 E-203 最近的报警",
        top_k=5,
        tenant_id="factory-a",
        user_id="operator-1",
        app_id="enterprise-assistant",
    )

    payload = sql_request_payload(request, [plan], max_rows=20)

    assert payload["structured_queries"][0]["dataset"] == "alarm"
    assert payload["limit"] == 5
    assert payload["context"] == {
        "tenant_id": "factory-a",
        "user_id": "operator-1",
        "app_id": "enterprise-assistant",
    }
    assert "sql" not in payload


def test_sql_rows_keep_stable_row_level_provenance():
    hits = normalize_sql_hits(
        {
            "query_id": "q-123",
            "data_as_of": "2026-10-09T10:00:00+08:00",
            "schema_version": "v2",
            "items": [{
                "dataset": "alarm",
                "row_key": "alarm-7",
                "fields": {"device_id": "E-203", "code": "E-203"},
                "updated_at": "2026-10-08T09:00:00+08:00",
            }],
        },
        source_name="业务报警网关",
        evidence_prefix="conn-sql:sql",
    )

    assert len(hits) == 1
    assert hits[0].evidence_id == "conn-sql:sql:alarm-7"
    assert hits[0].metadata["query_id"] == "q-123"
    assert hits[0].metadata["schema_version"] == "v2"
    assert hits[0].updated_at == "2026-10-08T09:00:00+08:00"


def test_sql_connector_sends_logical_plan_to_gateway(monkeypatch):
    from app.search import connectors

    monkeypatch.setattr(
        connectors,
        "get_settings",
        lambda: type("Settings", (), {
            "sql_search_url": "http://localhost:9100/search",
            "sql_search_token": "gateway-token",
            "business_api_token": None,
            "sql_search_timeout_seconds": 8,
            "sql_search_max_rows": 100,
        })(),
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"items": [{"row_key": "7", "fields": {"device_id": "E-203"}}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(connectors.requests, "post", fake_post)
    request = SearchRequest(
        query="E-203 的报警",
        app_id="enterprise-assistant",
        query_plan=RetrievalPlan(
            structured_queries=[StructuredQueryPlan(dataset="alarm", select=["device_id"])]
        ),
    )

    hits = connectors.SQLSearchConnector().search(request)

    assert captured["url"].endswith("/search")
    assert captured["headers"]["Authorization"] == "Bearer gateway-token"
    assert captured["json"]["structured_queries"][0]["dataset"] == "alarm"
    assert hits[0].evidence_id == "sql:7"


def test_managed_sql_connection_enforces_production_allowlist():
    class Store:
        settings = SimpleNamespace(
            tool_center_allowed_hosts="localhost",
            ragflow_base_url="",
            neo4j_uri="",
            business_api_base_url="",
            sql_search_url="",
            mcp_search_url="",
        )

        def credential_secret(self, _workspace, _credential_id):
            return {}

        def credentials(self, _workspace):
            return []

    service = ToolCenterService.__new__(ToolCenterService)
    service.store = Store()
    captured = {}
    service.request = lambda *args, **kwargs: (captured.update(kwargs) or {"items": []}, {})
    connection = {
        "connection_id": "sql-1",
        "name": "生产业务数据",
        "type": "sql",
        "endpoint": "http://localhost:9100/search",
        "environment": "production",
        "timeout_seconds": 8,
        "auth_mode": "none",
        "config": {
            "enforce_allowlist": True,
            "allow_natural_query": False,
            "datasets": {"alarm": {"fields": ["device_id", "code"]}},
        },
    }
    request = SearchRequest(
        query="E-203 的报警",
        query_plan=RetrievalPlan(
            structured_queries=[StructuredQueryPlan(dataset="alarm", select=["device_id"])]
        ),
    )

    service.search_connection("default", connection, request)
    assert captured["payload"]["structured_queries"][0]["dataset"] == "alarm"

    with pytest.raises(ToolCenterError, match="要求结构化查询计划"):
        service.search_connection(
            "default",
            connection,
            SearchRequest(query="查报警"),
        )
