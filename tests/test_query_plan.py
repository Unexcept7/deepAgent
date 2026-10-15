from app.search.models import RetrievalPlan, SearchRequest
from app.search.query_plan import build_safe_retrieval_plan


def test_query_plan_keeps_entity_for_graph_and_attributes_for_documents():
    plan = build_safe_retrieval_plan(
        "M1401 模块有多少路数字量输入？通信接口是什么？",
        {
            "entities": [
                {
                    "entity_type": "device_model",
                    "value": "M1401",
                    "normalized_value": "M1401",
                    "resolved_id": "device:m1401",
                }
            ],
            "intents": [
                {
                    "query": "M1401 通信接口和输入输出路数",
                }
            ],
            "retrieval_plan": {
                "graph_queries": ["MATCH (n) RETURN n"],
                "document_queries": ["M1401 通信接口"],
                "attribute_keys": ["communication_interface"],
            },
        },
    )

    assert plan.graph_queries == ["M1401", "device:m1401"]
    assert "M1401 通信接口" in plan.document_queries
    assert "M1401 模块有多少路数字量输入？通信接口是什么？" in plan.document_queries
    assert "MATCH (n) RETURN n" not in plan.graph_queries
    assert plan.attribute_keys == ["communication_interface"]


def test_search_request_accepts_optional_retrieval_plan():
    request = SearchRequest(
        query="M1401",
        source_types=["graph", "document"],
        query_plan=RetrievalPlan(graph_queries=["M1401"]),
    )

    assert request.query_plan is not None
    assert request.query_plan.graph_queries == ["M1401"]


def test_query_plan_keeps_specific_wiring_terms_for_graph_filtering():
    plan = build_safe_retrieval_plan(
        "M1304 的 Pt100 三线制如何接线？要求图谱关系和原始接线证据",
        {
            "entities": [{"entity_type": "device_model", "value": "M1304"}],
            "retrieval_plan": {
                "graph_queries": ["M1304"],
                "attribute_keys": ["input_type"],
            },
        },
    )

    assert plan.graph_queries == ["M1304"]
    assert "Pt100" in plan.attribute_keys
    assert "三线制" in plan.attribute_keys
    assert "接线" in plan.attribute_keys
    assert "接线" in plan.relation_types


def test_query_plan_uses_explicit_product_identifiers_when_semantic_plan_is_missing():
    plan = build_safe_retrieval_plan(
        "深圳金鸽 M120 和 M160 的接口与 I/O 组合分别是什么？",
        {},
    )

    assert plan.graph_queries == ["M120", "M160"]


def test_query_plan_does_not_treat_protocol_value_as_product_identifier():
    plan = build_safe_retrieval_plan(
        "XP3018 的 RS485 通信接口是什么？",
        {},
    )

    assert plan.graph_queries == ["XP3018"]


def test_query_plan_keeps_original_query_when_semantic_queries_fill_limit():
    query = "M1503 的 AI、DI、DO 分别是多少？"
    plan = build_safe_retrieval_plan(
        query,
        {
            "retrieval_plan": {
                "document_queries": [f"M1503 扩展查询 {index}" for index in range(1, 9)],
            }
        },
    )

    assert len(plan.document_queries) == 8
    assert plan.document_queries[0] == query
    assert query in plan.document_queries
