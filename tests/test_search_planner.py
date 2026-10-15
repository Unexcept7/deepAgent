from app.search import planner


def test_search_planner_reuses_semantic_entities_for_graph_queries(monkeypatch):
    prompt = {"prompt_id": "test", "version": 3, "name": "test"}
    monkeypatch.setattr(
        planner,
        "get_semantic_prompt_store",
        lambda: type("Store", (), {"active": lambda self: prompt})(),
    )
    monkeypatch.setattr(planner, "routing_policy_catalog", lambda: [])
    monkeypatch.setattr(
        planner,
        "build_semantic_context",
        lambda *_args, **_kwargs: {"prompt_context": {}},
    )
    monkeypatch.setattr(
        planner,
        "run_semantic_router",
        lambda query, current, **_kwargs: {
            "prompt_id": current["prompt_id"],
            "version": current["version"],
            "model": "fake",
            "duration_ms": 4,
            "result": {
                "entities": [
                    {"entity_type": "device_model", "value": "M120", "normalized_value": "M120"},
                    {"entity_type": "device_model", "value": "M160", "normalized_value": "M160"},
                ],
                "intents": [{"query": query}],
                "retrieval_plan": {
                    "graph_queries": ["M120", "M160"],
                    "attribute_keys": ["接口", "I/O 组合"],
                    "structured_queries": [{
                        "dataset": "module_interface",
                        "select": ["model", "io_combination"],
                        "filters": [{"field": "model", "operator": "in", "value": ["M120", "M160"]}],
                    }],
                },
            },
        },
    )

    result = planner.plan_search_query("深圳金鸽 M120 和 M160 的接口与 I/O 组合分别是什么？")

    assert result.plan.graph_queries == ["M120", "M160"]
    assert result.plan.attribute_keys == ["接口", "I/O 组合"]
    assert result.plan.structured_queries[0].dataset == "module_interface"
    assert result.plan.structured_queries[0].filters[0].field == "model"
    assert result.diagnostics["planner"] == "workbench_semantic_router"
    assert result.diagnostics["status"] == "success"


def test_search_planner_falls_back_to_explicit_identifier_without_model(monkeypatch):
    monkeypatch.setattr(
        planner,
        "get_semantic_prompt_store",
        lambda: type("Store", (), {"active": lambda self: None})(),
    )
    monkeypatch.setattr(planner, "routing_policy_catalog", lambda: [])

    result = planner.plan_search_query("深圳金鸽 M120 和 M160 的接口与 I/O 组合分别是什么？")

    assert result.plan.graph_queries == ["M120", "M160"]
    assert result.status == "disabled"
    assert any("确定性实体提取" in item for item in result.warnings)
