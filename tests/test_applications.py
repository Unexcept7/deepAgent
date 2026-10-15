from app import main
from app.application_store import ApplicationStore
from app.schemas.application import ApplicationDefinitionInput
from app.schemas.query import QueryRequest
from app.workflow_store import WorkflowStore


def test_application_store_persists_versions_and_run_history(tmp_path):
    store = ApplicationStore(tmp_path / "applications.json")
    current = store.get("enterprise-knowledge-assistant")

    payload = ApplicationDefinitionInput(
        app_id=current["app_id"],
        name=current["name"],
        description="版本化应用测试",
        category=current["category"],
        type=current["type"],
        icon=current["icon"],
        enabled=True,
        tags=current["tags"],
        workflow_id=current["workflow_id"],
        workflow_version=current["workflow_version"],
        workflow_bindings=current["workflow_bindings"],
        data_sources=current["data_sources"],
        tools=current["tools"],
        runtime=current["runtime"],
        permissions=current["permissions"],
        channels=current["channels"],
        base_version=current["version"],
    )

    draft = store.upsert(payload, current["app_id"])
    assert draft["version"] == 2
    assert draft["status"] == "draft"

    published = store.publish(current["app_id"], version=2)
    assert published["published_version"] == 2
    store.record_run(current["app_id"], 2, "run-test", "success", duration_ms=17)
    assert store.runs(current["app_id"])[-1]["duration_ms"] == 17


def test_application_run_is_recorded_from_chat_context(monkeypatch, tmp_path):
    store = ApplicationStore(tmp_path / "applications.json")
    monkeypatch.setattr(main, "get_application_store", lambda: store)

    main._record_application_run(
        QueryRequest(query="北辰47 e203", app_id="enterprise-knowledge-assistant"),
        {
            "workflow_run_id": "workflow-run-1",
            "total_duration_ms": 42,
            "trace": {"route": "workflow"},
        },
        status="success",
    )

    run = store.runs("enterprise-knowledge-assistant")[-1]
    assert run["run_id"] == "workflow-run-1"
    assert run["status"] == "success"
    assert run["duration_ms"] == 42


def test_application_context_controls_workflow_sources_and_runtime(monkeypatch, tmp_path):
    store = ApplicationStore(tmp_path / "applications.json")
    monkeypatch.setattr(main, "get_application_store", lambda: store)
    request = QueryRequest(
        query="北辰47 e203",
        app_id="enterprise-knowledge-assistant",
        workflow_id="untrusted-workflow",
        source_types=["document", "qa"],
        workflow_params={"top_k": 5},
    )

    main._apply_application_context(request)

    assert request.workflow_id == "knowledge-research"
    assert request.workflow_version == 1
    assert request.source_types == ["document", "qa"]
    assert request.workflow_params["top_k"] == 5
    assert request.workflow_params["similarity_threshold"] == 0.3


def test_application_center_similarity_threshold_is_forwarded_to_runtime(monkeypatch, tmp_path):
    store = ApplicationStore(tmp_path / "applications.json")
    current = store.get("enterprise-knowledge-assistant")
    runtime = {**current["runtime"], "similarity_threshold": 0.2}
    payload = ApplicationDefinitionInput(
        app_id=current["app_id"],
        name=current["name"],
        description=current["description"],
        category=current["category"],
        type=current["type"],
        icon=current["icon"],
        enabled=True,
        tags=current["tags"],
        workflow_id=current["workflow_id"],
        workflow_version=current["workflow_version"],
        workflow_bindings=current["workflow_bindings"],
        data_sources=current["data_sources"],
        tools=current["tools"],
        runtime=runtime,
        permissions=current["permissions"],
        channels=current["channels"],
        base_version=current["version"],
    )
    draft = store.upsert(payload, current["app_id"])
    store.publish(current["app_id"], version=draft["version"])
    monkeypatch.setattr(main, "get_application_store", lambda: store)

    request = QueryRequest(query="XP3018 的宽度是多少？", app_id="enterprise-knowledge-assistant")
    main._apply_application_context(request)

    assert request.workflow_params["similarity_threshold"] == 0.2


def test_builtin_agents_have_distinct_workflow_bindings(tmp_path):
    applications = ApplicationStore(tmp_path / "applications.json").list()
    workflows = WorkflowStore(tmp_path / "workflows.json").list()
    bindings = {item["app_id"]: item["workflow_id"] for item in applications}

    assert bindings["enterprise-knowledge-assistant"] == "knowledge-research"
    assert bindings["evidence-reviewer"] == "evidence-review"
    assert {item["workflow_id"] for item in workflows} >= {
        "knowledge-research",
        "evidence-review",
    }
