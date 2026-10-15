from app.dictionary_store import DictionaryStore, preview_dictionary_matches
from app.schemas.dictionary import DictionaryDefinitionInput


def definition(**overrides):
    values = {
        "dictionary_id": "test-dictionary",
        "name": "测试业务词典",
        "description": "测试用途",
        "enabled": True,
        "entries": [
            {
                "entry_id": "subject-custom-bus",
                "dictionary_type": "technical_subject",
                "canonical_value": "CustomBus",
                "aliases": ["custom bus", "自定义总线"],
            },
            {
                "entry_id": "fault-prefix-x",
                "dictionary_type": "fault_code",
                "canonical_value": "X",
                "min_digits": 3,
                "max_digits": 4,
            },
        ],
    }
    values.update(overrides)
    return DictionaryDefinitionInput.model_validate(values)


def test_dictionary_store_versions_publish_rollback_compare(tmp_path):
    store = DictionaryStore(tmp_path / "dictionaries.json")
    saved = store.upsert(definition())
    assert saved["status"] == "draft"
    store.publish("test-dictionary", saved["version"])
    assert store.runtime("test-dictionary")["version"] == 1
    saved = store.upsert(definition(base_version=1, description="第二版"), "test-dictionary")
    assert saved["version"] == 2
    assert store.get_version("test-dictionary", 1)["description"] == "测试用途"
    assert store.compare("test-dictionary", 1, 2)["changed_fields"] == ["description"]
    store.publish("test-dictionary", 2)
    rolled = store.rollback("test-dictionary", 1)
    assert rolled["version"] == 3
    assert rolled["published_version"] == 3
    assert rolled["description"] == "测试用途"


def test_dictionary_test_panel_matches_configured_terms(tmp_path):
    store = DictionaryStore(tmp_path / "dictionaries.json")
    saved = store.upsert(definition())
    store.publish("test-dictionary", saved["version"])
    runtime = store.runtime("test-dictionary")
    hits = preview_dictionary_matches("请查询自定义总线的 X203 故障码", runtime)
    assert {item["dictionary_type"] for item in hits} == {"technical_subject", "fault_code"}
    assert preview_dictionary_matches("普通问题 E203", runtime) == []


def test_runtime_evidence_uses_published_dictionary(monkeypatch, tmp_path):
    from app.agents import evidence

    store = DictionaryStore(tmp_path / "dictionaries.json")
    saved = store.upsert(definition())
    store.publish("test-dictionary", saved["version"])
    runtime = store.runtime("test-dictionary")
    monkeypatch.setattr(evidence, "get_dictionary_store", lambda: type(
        "RuntimeStore", (), {
            "runtime": lambda _self: runtime,
            "runtime_entries": lambda _self, dictionary_type: [
                item for item in runtime.get("entries", [])
                if item.get("dictionary_type") == dictionary_type and item.get("enabled", True)
            ],
        },
    )())
    assert evidence.extract_technical_subjects("设备支持自定义总线") == ["CustomBus"]


def test_dictionary_api_auth_and_crud(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.tool_center.auth import Principal, require_admin, require_manager, require_principal

    store = DictionaryStore(tmp_path / "dictionaries.json")
    monkeypatch.setattr(main, "get_dictionary_store", lambda: store)
    client = TestClient(main.app)
    assert client.get("/api/dictionaries").status_code == 401
    principal = lambda: Principal("test", ("admin",))
    for dependency in (require_principal, require_manager, require_admin):
        main.app.dependency_overrides[dependency] = principal
    try:
        response = client.post("/api/dictionaries", json=definition().model_dump(mode="json"))
        assert response.status_code == 200
        assert client.post("/api/dictionaries/test-dictionary/publish", json={"version": 1}).status_code == 200
        response = client.post("/api/dictionaries/test", json={"dictionary_id": "test-dictionary", "query": "自定义总线 X203"})
        assert response.status_code == 200
        assert response.json()["version"] == 1
        assert client.get("/api/dictionaries", headers={"X-Workspace-Id": "foreign"}).status_code == 403
    finally:
        for dependency in (require_principal, require_manager, require_admin):
            main.app.dependency_overrides.pop(dependency, None)
