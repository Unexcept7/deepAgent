from types import SimpleNamespace

import pytest

from app.graph.neo4j_store import GraphStoreError, Neo4jGraphStore
from app.graph.provenance import normalize_graph_relation_evidence
from app.graph.relevance import rank_graph_records
from app.schemas.graph import GraphEntityInput, GraphPathRequest, GraphRelationInput, GraphUpsertRequest


def test_graph_store_is_safe_when_not_configured():
    settings = SimpleNamespace(
        graph_provider="none",
        neo4j_uri="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="secret",
    )
    store = Neo4jGraphStore(settings)

    assert store.configured is False
    assert store.health() == {
        "configured": False,
        "available": False,
        "status": "not_configured",
    }
    with pytest.raises(GraphStoreError, match="Neo4j 未配置"):
        store.search("北辰47")


def test_graph_input_contract_and_property_normalization():
    payload = GraphUpsertRequest(
        nodes=[GraphEntityInput(
            id="device:beichen47",
            name="北辰47",
            type="Device",
            properties={"model": "R7", "metadata": {"source": "manual"}},
        )],
        relations=[GraphRelationInput(
            source_id="device:beichen47",
            target_id="sop:cooling",
            type="HANDLED_BY",
            properties={"page": 18},
        )],
    )

    normalized_node = Neo4jGraphStore._normalize_node(payload.nodes[0].model_dump())
    normalized_relation = Neo4jGraphStore._normalize_relation(payload.relations[0].model_dump())

    assert normalized_node["id"] == "device:beichen47"
    assert normalized_node["properties"]["metadata"] == '{"source": "manual"}'
    assert normalized_relation["type"] == "HANDLED_BY"
    assert normalized_relation["properties"]["page"] == 18


def test_graph_request_limits_are_bounded():
    assert GraphPathRequest(start_id="device:1", limit=50).limit == 50
    with pytest.raises(ValueError):
        GraphPathRequest(start_id="device:1", limit=51)


def test_graph_relation_provenance_decodes_stored_json_and_keeps_source_excerpt():
    relation = normalize_graph_relation_evidence(
        {"id": "device:xp3018", "name": "XP3018"},
        {"id": "protocol:modbus", "name": "Modbus RTU"},
        {
            "type": "支持",
            "confidence": 0.91,
            "evidence_ids": '["chunk-18"]',
            "evidence_records": '[{"evidence_id":"chunk-18","document_name":"XP3018.pdf","document_id":"doc-1","dataset_id":"ds-1","page":18,"excerpt":"支持 Modbus RTU。"}]',
        },
    )

    assert relation["evidence_status"] == "bound"
    assert relation["evidence_ids"] == ["chunk-18"]
    assert relation["evidence"][0]["document_name"] == "XP3018.pdf"
    assert relation["evidence"][0]["url"] == "/source-documents/ds-1/doc-1"
    assert relation["evidence"][0]["excerpt"] == "支持 Modbus RTU。"


def test_graph_filter_keeps_wiring_edges_and_drops_unrelated_composition_edges():
    records = [
        {
            "left": {"id": "device:m1304", "name": "M1304"},
            "right": {"id": "part:communication", "name": "隔离通讯电路"},
            "relation": {"type": "包含", "excerpt": "M1304 包含隔离通讯电路。"},
        },
        {
            "left": {"id": "device:m1304", "name": "M1304"},
            "right": {"id": "input:pt100", "name": "Pt100"},
            "relation": {
                "type": "支持",
                "evidence_records": [
                    {"excerpt": "Pt100 三线制接线见原始接线图。"},
                ],
            },
        },
        {
            "left": {"id": "device:m1304", "name": "M1304"},
            "right": {"id": "terminal:m1304", "name": "端子"},
            "relation": {
                "type": "包含",
                "evidence_records": [
                    {"excerpt": "Pt100 三线制应按端子图接线。"},
                ],
            },
        },
    ]

    ranked = rank_graph_records(
        records,
        attribute_terms=["Pt100", "三线制", "接线"],
        relation_types=["接线", "接线方式", "端子连接"],
    )

    names = [item["right"]["name"] for item, _ in ranked]
    assert names == ["Pt100", "端子"]
    assert all(item["relation"]["type"] != "包含" or item["right"]["name"] == "端子" for item, _ in ranked)
    assert ranked[0][1]["evidence_match"] is True


def test_graph_store_passes_attribute_and_relation_filters_as_parameters():
    class FakeSession:
        def __init__(self):
            self.statement = ""
            self.params = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, statement, params):
            self.statement = statement
            self.params = params
            return []

    class FakeDriver:
        def __init__(self):
            self.session_instance = FakeSession()

        def session(self):
            return self.session_instance

    settings = SimpleNamespace(
        graph_provider="neo4j",
        neo4j_uri="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="secret",
    )
    store = Neo4jGraphStore(settings)
    store._driver = FakeDriver()

    assert store.search_entity_neighbors(
        "M1304",
        attribute_terms=["Pt100", "三线制"],
        relation_types=["接线"],
    ) == []
    session = store._driver.session_instance
    assert "$attribute_terms" in session.statement
    assert session.params["attribute_terms"] == ["Pt100", "三线制"]
    assert session.params["relation_types"] == ["接线"]
