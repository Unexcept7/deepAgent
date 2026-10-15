import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from app.agents import graph
from app.schemas.workflow import WorkflowDefinitionInput
from app.workflow_store import WorkflowError, WorkflowStore


def test_workflow_store_persists_versions_templates_and_diff(tmp_path):
    store = WorkflowStore(tmp_path / "workflows.json")
    current = store.get("knowledge-research")
    payload = WorkflowDefinitionInput(
        workflow_id=current["workflow_id"],
        name=current["name"],
        description="版本化测试",
        enabled=current["enabled"],
        params=current["params"],
        nodes=current["nodes"],
        edges=current["edges"],
        base_version=current["version"],
    )

    draft = store.upsert(payload, current["workflow_id"])
    assert draft["version"] == 2
    assert draft["status"] == "draft"
    published = store.publish(current["workflow_id"], version=2)
    assert published["published_version"] == 2

    diff = store.compare(current["workflow_id"], 1, 2)
    assert diff["changed"] is True
    assert "description" in diff["changed_fields"]
    rolled_back = store.rollback(current["workflow_id"], 1, publish=True)
    assert rolled_back["version"] == 3
    assert rolled_back["published_version"] == 3
    assert len(store.templates()) >= 3


def test_workflow_for_run_defaults_to_published_and_rejects_draft(tmp_path):
    store = WorkflowStore(tmp_path / "workflows.json")
    current = store.get("knowledge-research")
    payload = WorkflowDefinitionInput(
        workflow_id=current["workflow_id"],
        name=current["name"],
        description="未发布草稿",
        enabled=current["enabled"],
        params=current["params"],
        nodes=current["nodes"],
        edges=current["edges"],
        base_version=current["version"],
    )
    draft = store.upsert(payload, current["workflow_id"])

    assert store.for_run("knowledge-research")["version"] == current["published_version"]
    with pytest.raises(WorkflowError, match="草稿|未发布"):
        store.for_run("knowledge-research", draft["version"])

    published = store.publish("knowledge-research", draft["version"])
    assert store.for_run("knowledge-research")["version"] == published["version"]


def test_parallel_workflow_prefetch_keeps_branch_evidence(monkeypatch, tmp_path):
    workflow = WorkflowStore(tmp_path / "workflows.json").get("equipment-diagnosis")

    def fake_invoke(payload):
        source = ",".join(payload.get("source_types") or [])
        return json.dumps({
            "query": payload["query"],
            "hits": [{
                "content": source,
                "title": source,
                "source_type": "document",
                "source_name": source,
                "evidence_id": f"test:{source}",
                "score": 0.9,
            }],
            "errors": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(graph, "unified_search", SimpleNamespace(invoke=fake_invoke))
    events = []
    messages = graph._prefetch_parallel_workflow_messages(
        "北辰47 e203",
        workflow,
        default_source_types=["document", "qa"],
        filters={},
        top_k=4,
        callback=events.append,
        workflow_run_id="test-run",
    )

    assert len(messages) == 3
    assert all(isinstance(item, (ToolMessage, AIMessage)) for item in messages)
    assert {item["node_id"] for item in events if item.get("type") == "workflow_node"} >= {
        "parallel-search",
        "manual",
        "case",
    }


def test_run_agent_emits_real_workflow_node_statuses(monkeypatch, tmp_path):
    store = WorkflowStore(tmp_path / "workflows.json")
    monkeypatch.setattr(graph, "get_workflow_store", lambda: store)
    fake_agent = SimpleNamespace(
        get_graph=lambda: object(),
        stream=lambda payload, config=None, stream_mode=None: iter([
            ("messages", (AIMessageChunk(content="测试回答"), {"node": "model"})),
            ("updates", {"model": {"messages": [AIMessage(content="测试回答")]}}),
        ]),
    )
    monkeypatch.setattr(graph, "get_agent", lambda **kwargs: fake_agent)
    events = []

    result = graph.run_agent(
        [],
        "你好",
        on_event=events.append,
        workflow_id="knowledge-research",
    )

    assert result["answer"] == "测试回答"
    node_events = [item for item in events if item.get("type") == "workflow_node"]
    assert any(item.get("node_id") == "route" and item.get("status") == "success" for item in node_events)
    assert any(item.get("node_id") == "answer" and item.get("status") == "success" for item in node_events)
    assert store.runs("knowledge-research")[-1]["status"] == "success"
