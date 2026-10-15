import json

import pytest
from langchain_core.messages import AIMessage

from app.agents import semantic_router
from app.semantic_prompt_store import SemanticPromptStore
from app.schemas.semantic_prompt import SemanticPromptDefinitionInput


def definition(**overrides):
    values = {
        "prompt_id": "test-router",
        "name": "测试语义路由",
        "description": "测试用途",
        "enabled": True,
        "model": "qwen3.8-max",
        "temperature": 0,
        "max_tokens": 512,
        "fallback_mode": "rule_only",
        "system_prompt": "请识别用户问题中的意图和实体，并严格输出 JSON。",
        "intents": [{
            "intent_id": "knowledge_question",
            "name": "知识问答",
            "route": "knowledge_qa",
            "description": "查询企业资料",
            "examples": ["E203 是什么"],
            "exclusions": [],
        }],
        "entities": [{
            "entity_type": "fault_code",
            "name": "故障码",
            "description": "故障码",
            "examples": ["E203"],
        }],
    }
    values.update(overrides)
    return SemanticPromptDefinitionInput.model_validate(values)


def test_prompt_store_versions_publish_rollback_compare(tmp_path):
    store = SemanticPromptStore(tmp_path / "semantic-prompts.json")
    saved = store.upsert(definition())
    assert saved["status"] == "draft"
    store.publish("test-router", saved["version"])
    assert store.active("test-router")["version"] == 1
    saved = store.upsert(definition(base_version=1, description="第二版"), "test-router")
    assert saved["version"] == 2
    assert store.get_version("test-router", 1)["description"] == "测试用途"
    assert store.compare("test-router", 1, 2)["changed_fields"] == ["description"]
    store.publish("test-router", 2)
    rolled = store.rollback("test-router", 1)
    assert rolled["version"] == 3
    assert rolled["published_version"] == 3
    assert rolled["description"] == "测试用途"


def test_prompt_composition_keeps_backend_boundary():
    text = semantic_router.compose_semantic_router_prompt(
        definition().model_dump(mode="json"),
        [{"route": "knowledge_qa", "keywords": ["故障码"], "policy_id": "fault-code"}],
    )
    assert "不可被业务 Prompt" in text
    assert "knowledge_question" in text
    assert "fault_code" in text
    assert "故障码" in text
    assert "输出契约" in text


def test_semantic_router_validates_known_catalog(monkeypatch):
    class FakeModel:
        model = "qwen3.8-max"

        def bind(self, **kwargs):
            self.bound_kwargs = kwargs
            return self

        def invoke(self, _messages):
            return AIMessage(
                content=json.dumps({
                    "intents": [{
                        "intent_id": "knowledge_question",
                        "route": "knowledge_qa",
                        "query": "E203 是什么",
                        "confidence": 0.95,
                        "entity_types": ["fault_code"],
                        "requires_evidence": True,
                        "requires_confirmation": False,
                    }],
                    "entities": [{
                        "entity_type": "fault_code",
                        "value": "E203",
                        "normalized_value": "E203",
                        "resolved_id": None,
                        "confidence": 0.99,
                    }],
                    "needs_clarification": False,
                    "ambiguity": "",
                }, ensure_ascii=False),
                response_metadata={"token_usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}},
            )

    fake_model = FakeModel()
    monkeypatch.setattr(semantic_router, "_create_model", lambda _prompt: fake_model)
    result = semantic_router.run_semantic_router("北辰47 的 E203 是什么？", definition().model_dump(mode="json"))
    assert result["error"] is None
    assert result["result"]["intents"][0]["intent_id"] == "knowledge_question"
    assert result["token_usage"]["total_tokens"] == 20
    assert fake_model.bound_kwargs == {"response_format": {"type": "json_object"}}


def test_semantic_router_uses_model_profile_limits(monkeypatch):
    calls = {}

    def fake_profile(profile_id, **kwargs):
        calls["profile_id"] = profile_id
        calls["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(semantic_router, "create_model_for_profile", fake_profile)
    semantic_router._create_model({"temperature": 0, "max_tokens": 2200}, "balanced")

    assert calls == {"profile_id": "balanced", "kwargs": {}}


def test_runtime_semantic_router_is_primary_classifier_with_safe_fallback(monkeypatch):
    from app.agents import graph

    prompt = definition().model_dump(mode="json")
    prompt.update({"prompt_id": "runtime", "version": 3, "name": "运行时 Prompt", "enabled": True})
    monkeypatch.setattr(graph, "get_semantic_prompt_store", lambda: type("Store", (), {"active": lambda self: prompt})())
    monkeypatch.setattr(graph, "run_semantic_router", lambda query, current: {
        "prompt_id": current["prompt_id"], "version": current["version"], "model": current["model"],
        "provider": "fake", "duration_ms": 5, "token_usage": {"total_tokens": 9},
        "result": {"intents": [{"intent_id": "knowledge_question", "route": "knowledge_qa"}],
                   "entities": [{"entity_type": "fault_code", "value": "E203"}],
                   "needs_clarification": False, "ambiguity": ""},
        "raw_output": "{}", "error": None,
    })
    policy = {"policy_id": None, "matched_count": 0, "route": "unknown", "actions": {}, "reasons": []}
    graph._try_semantic_route("E203 是什么？", policy, None)
    assert policy["route"] == "knowledge_qa"
    assert policy["actions"]["require_evidence"] is True
    assert policy["semantic_router"]["version"] == 3

    policy = {"policy_id": "one-rule", "matched_count": 1, "route": "knowledge_qa", "actions": {}, "reasons": []}
    graph._try_semantic_route("E203 是什么？", policy, None)
    assert policy["route"] == "knowledge_qa"
    assert policy["route_origin"] == "semantic_model"
    assert policy["semantic_router"]["version"] == 3


def test_semantic_prompt_api_auth_and_crud(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.tool_center.auth import Principal, require_admin, require_manager, require_principal

    store = SemanticPromptStore(tmp_path / "semantic-prompts.json")
    monkeypatch.setattr(main, "get_semantic_prompt_store", lambda: store)
    monkeypatch.setattr(main, "run_semantic_router", lambda query, prompt: {
        "query": query, "prompt_id": prompt["prompt_id"], "version": prompt["version"], "result": {},
    })
    client = TestClient(main.app)
    assert client.get("/api/semantic-prompts").status_code == 401
    principal = lambda: Principal("test", ("admin",))
    for dependency in (require_principal, require_manager, require_admin):
        main.app.dependency_overrides[dependency] = principal
    try:
        response = client.post("/api/semantic-prompts", json=definition().model_dump(mode="json"))
        assert response.status_code == 200
        prompt = response.json()["prompt"]
        assert client.post("/api/semantic-prompts/test-router/publish", json={"version": 1}).status_code == 200
        response = client.post("/api/semantic-prompts/test", json={"prompt_id": "test-router", "query": "E203 是什么"})
        assert response.status_code == 200
        assert response.json()["version"] == 1
        assert client.get("/api/semantic-prompts", headers={"X-Workspace-Id": "foreign"}).status_code == 403
    finally:
        for dependency in (require_principal, require_manager, require_admin):
            main.app.dependency_overrides.pop(dependency, None)
