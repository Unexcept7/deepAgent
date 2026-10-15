from app import main
from app.conversation import MemoryConversationStore
from app.schemas.query import QueryRequest
from fastapi.testclient import TestClient


def test_chat_response_exposes_evidence_summary(monkeypatch):
    monkeypatch.setattr(main, "conversation_store", MemoryConversationStore())
    monkeypatch.setattr(
        main,
        "run_agent",
        lambda history, query: {
            "answer": "当前没有可靠证据。",
            "citations": [],
            "retrieval_hits": [],
            "claims": [{
                "entity": "FT-RS-9999",
                "attribute": "功能码",
                "value": "未知",
                "evidence_ids": [],
                "status": "unsupported",
                "reason": "没有可靠证据。",
            }],
            "requirements": [{
                "id": "R1",
                "subject": "FT-RS-9999",
                "request": "功能码",
                "output_type": "fact",
                "retrieval_query": "FT-RS-9999 功能码",
                "query_span": "支持哪些功能码",
            }],
            "results": [{
                "requirement_id": "R1",
                "subject": "FT-RS-9999",
                "request": "功能码",
                "output_type": "fact",
                "value": None,
                "status": "unsupported",
                "reason_code": "no_evidence",
                "reason": "没有可靠证据。",
                "evidence_ids": [],
                "sources": [],
            }],
            "coverage": {
                "complete": True,
                "audited": True,
                "plan_complete": True,
                "all_requirements_accounted_for": True,
                "planned_requirement_count": 1,
                "result_count": 1,
                "missing_requirement_ids": [],
                "missing_requests": [],
                "supported_count": 0,
                "unsupported_count": 1,
                "conflict_count": 0,
            },
            "evidence": {
                "status": "not_found",
                "requires_evidence": True,
                "candidate_count": 8,
                "verified_count": 0,
                "rejected_count": 8,
                "missing_entities": ["FT-RS-9999"],
            },
            "suggestions": [],
            "trace": {
                "route": "ragflow",
                "used_ragflow": True,
                "model_calls": 2,
                "ragflow_calls": 1,
                "steps": [],
            },
        },
    )

    response = main.chat(QueryRequest(query="FT-RS-9999 支持哪些功能码？"))

    assert response.evidence is not None
    assert response.evidence.status == "not_found"
    assert response.evidence.rejected_count == 8
    assert response.evidence.missing_entities == ["FT-RS-9999"]
    assert response.claims[0].status == "unsupported"
    assert response.results[0].requirement_id == "R1"
    assert response.coverage is not None
    assert response.coverage.all_requirements_accounted_for is True
    stored = main.conversation_store.get(response.conversation_id)
    assert stored[-1]["evidence"]["status"] == "not_found"
    assert stored[-1]["claims"][0]["entity"] == "FT-RS-9999"
    assert stored[-1]["results"][0]["requirement_id"] == "R1"


def test_conversation_api_scopes_history_by_app(monkeypatch):
    monkeypatch.setattr(main, "conversation_store", MemoryConversationStore())
    client = TestClient(main.app)

    knowledge = client.post(
        "/conversations",
        params={"app_id": "enterprise-knowledge-assistant"},
    )
    assert knowledge.status_code == 200
    knowledge_id = knowledge.json()["conversation_id"]
    main.conversation_store.append(
        knowledge_id,
        "user",
        "知识库上下文",
        app_id="enterprise-knowledge-assistant",
    )

    reviewer = client.post(
        "/conversations",
        params={"app_id": "evidence-reviewer"},
    )
    assert reviewer.status_code == 200

    knowledge_list = client.get(
        "/conversations",
        params={"app_id": "enterprise-knowledge-assistant"},
    )
    reviewer_list = client.get(
        "/conversations",
        params={"app_id": "evidence-reviewer"},
    )
    assert [item["conversation_id"] for item in knowledge_list.json()] == [knowledge_id]
    assert [item["conversation_id"] for item in reviewer_list.json()] == [
        reviewer.json()["conversation_id"]
    ]

    cross_read = client.get(
        f"/conversations/{knowledge_id}/messages",
        params={"app_id": "evidence-reviewer"},
    )
    assert cross_read.status_code == 409
