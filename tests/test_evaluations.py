from types import SimpleNamespace

from app import evaluation_service
from app.application_store import ApplicationStore
from app.schemas.evaluation import EvaluationCase, EvaluationRunRequest
from app.workflow_store import WorkflowStore


def test_builtin_retrieval_datasets_are_available_for_evaluation_ui():
    datasets = evaluation_service.list_evaluation_datasets()

    assert datasets
    dataset = next(item for item in datasets if item["dataset_id"] == "purchased_io_modules_retrieval_v1.jsonl")
    assert dataset["status"] == "ready"
    assert dataset["case_count"] > 0
    cases = evaluation_service.load_evaluation_dataset(dataset["dataset_id"], split="dev", limit=1)
    assert len(cases) == 1
    assert cases[0]["query"]
    assert cases[0]["gold_documents"]


def test_evaluation_folds_internal_spaces_in_technical_terms():
    case = EvaluationCase(
        query="设备参数",
        expected_answer_contains=["16路数字量输出"],
        required_concepts=["RS485"],
        expected_source_types=["document"],
    )
    result = {
        "answer": "支持 16 路数字量输出，通信接口为 RS 485。",
        "citations": [{"source_type": "document", "content": "RS 485"}],
    }

    evaluated = evaluation_service._evaluate_case(case, result, 1)

    assert evaluated["passed"] is True


def test_evaluation_recognizes_explicit_insufficient_evidence_as_abstention():
    case = EvaluationCase(
        query="不存在的型号是什么参数？",
        expected_outcome="abstain",
    )
    result = {
        "answer": "前提不成立，当前资料不足，无法确认该型号。",
        "evidence": {"status": "supported"},
    }

    evaluated = evaluation_service._evaluate_case(case, result, 1)

    assert evaluated["passed"] is True


def test_multi_agent_evaluation_compares_published_agents_without_conversation(monkeypatch, tmp_path):
    applications = ApplicationStore(tmp_path / "applications.json")
    workflows = WorkflowStore(tmp_path / "workflows.json")
    evaluation_path = tmp_path / "evaluations.json"
    monkeypatch.setattr(evaluation_service, "get_application_store", lambda: applications)
    monkeypatch.setattr(evaluation_service, "get_workflow_store", lambda: workflows)
    monkeypatch.setattr(
        evaluation_service,
        "get_settings",
        lambda: SimpleNamespace(evaluation_store_path=str(evaluation_path)),
    )
    evaluation_service.get_evaluation_store.cache_clear()

    def fake_run_agent(*args, **kwargs):
        return {
            "answer": "M1401 使用 RS485，提供模拟量输出。",
            "citations": [{"source_type": "document", "document_name": "M1401.pdf"}],
            "retrieval_hits": [{"source_type": "document", "document_name": "M1401.pdf", "content": "RS485"}],
            "evidence": {"status": "supported"},
            "trace": {
                "route": "knowledge_qa",
                "routing_policy": {
                    "semantic_intents": [{"intent_id": "knowledge_question"}],
                    "semantic_entities": [{"normalized": "M1401"}],
                },
                "model_calls": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        }

    monkeypatch.setattr(evaluation_service, "run_agent", fake_run_agent)
    request = EvaluationRunRequest(
        name="回归评测",
        agent_ids=["enterprise-knowledge-assistant", "evidence-reviewer"],
        cases=[EvaluationCase(
            case_id="m1401-interface",
            query="M1401 的通信接口是什么？",
            expected_answer_contains=["RS485"],
            expected_intents=["knowledge_question"],
            expected_entities=["M1401"],
            expected_source_types=["document"],
            expected_documents=["M1401.pdf"],
            required_concepts=["RS485"],
            expected_evidence_status="supported",
        )],
    )

    result = evaluation_service.run_evaluation(request)

    assert result["summary"]["total_runs"] == 2
    assert result["summary"]["passed_runs"] == 2
    assert len(result["summary"]["rankings"]) == 2
    assert result["results"][0]["passed"] is True
    assert evaluation_service.get_evaluation_store().get(result["run_id"])["run_id"] == result["run_id"]
