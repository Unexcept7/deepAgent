import pytest

from app.agents.evidence import _value_supported
from app.agents.claims import fact_value_supported
from app.agents.answer_composer import fallback_answer, validate_answer_plan
from app.evaluation_service import _evaluate_case
from app.schemas.evaluation import EvaluationCase


@pytest.mark.parametrize("source", ["124 V", "-24 V", "0.24 V", "24 mV"])
def test_measurement_cannot_match_subnumber_sign_or_wrong_unit(source):
    assert not _value_supported("24 V", source)


def test_equivalent_decimal_measurement_still_matches():
    assert _value_supported("24 V", "额定电压24.0 V")


@pytest.mark.parametrize("separator", ["；", "，", " "])
def test_qualifier_and_value_must_belong_to_same_fact(separator):
    chunks = [{"evidence_id": "E1", "content": f"M1401 额定电压124 V{separator}最大电压48 V。"}]
    assert not fact_value_supported("48 V", "M1401", chunks, attribute="额定电压")
    assert fact_value_supported("124 V", "M1401", chunks, attribute="额定电压")
    assert fact_value_supported("48 V", "M1401", chunks, attribute="最大电压")


def test_wrong_numeric_value_cannot_be_published_from_valid_evidence_id():
    chunks = [{"evidence_id": "E1", "content": "M1401 额定电压124 V。", "document_name": "M1401.pdf"}]
    row = {"requirement_id": "R1", "subject": "M1401", "request": "额定电压", "status": "supported", "value": "24 V", "evidence_ids": ["E1"]}
    draft = fallback_answer([row], {"complete": False}, chunks)
    assert draft.fact_ledger[0].status == "unsupported"
    assert "为24 V" not in draft.answer
    assert not draft.used_evidence_ids
    assert validate_answer_plan(draft, chunks)["passed"]


def test_structured_evaluation_cannot_combine_two_models_values():
    case = EvaluationCase(query="额定电压", expected_facts=[{"subject": "M1401", "attribute": "额定电压", "value": "24", "unit": "V"}])
    assert not _evaluate_case(case, {"answer": "M1401 额定电压124 V，M1401-AO 额定电压24 V。"}, 1)["passed"]
    assert _evaluate_case(case, {"answer": "M1401 额定电压24 V，M1401-AO 额定电压124 V。"}, 1)["passed"]


@pytest.mark.parametrize("value", ["124 V", "0.24 V", "-24 V", "24 mV"])
def test_structured_evaluation_uses_complete_measurements(value):
    case = EvaluationCase(query="额定电压", expected_facts=[{"subject": "M1401", "attribute": "额定电压", "value": "24", "unit": "V"}])
    assert not _evaluate_case(case, {"answer": f"M1401 额定电压{value}。"}, 1)["passed"]


@pytest.mark.parametrize("expected,answer,passed", [
    ("不支持 RS232", "不支持 RS232。", True),
    ("不支持 RS232", "支持 RS232。", False),
    ("RS232", "不支持 RS232。", False),
    ("RS485", "支持 RS485，不支持 RS232。", True),
])
def test_answer_scoring_preserves_expected_polarity(expected, answer, passed):
    case = EvaluationCase(query="通信协议", expected_answer_contains=[expected])
    assert _evaluate_case(case, {"answer": answer}, 1)["passed"] is passed


def test_structured_negative_fact_does_not_negate_attribute_label():
    case = EvaluationCase(query="通信协议", expected_facts=[{"subject": "M1401", "attribute": "协议", "value": "不支持 RS232"}])
    assert _evaluate_case(case, {"answer": "M1401 的通信协议不支持 RS232。"}, 1)["passed"]
    assert not _evaluate_case(case, {"answer": "M1401 的通信协议支持 RS232。"}, 1)["passed"]


def test_fact_scope_is_preserved_without_comma_between_models():
    chunks = [{"evidence_id": "E1", "content": "M1401 额定电压124 V M1401-AO 额定电压24 V。"}]
    assert not fact_value_supported("24 V", "M1401", chunks, attribute="额定电压")
    assert fact_value_supported("24 V", "M1401-AO", chunks, attribute="额定电压")


def test_duplicate_attribute_evidence_cannot_suppress_missing_requirements():
    import json
    from langchain_core.messages import ToolMessage
    from app.agents.graph import _summarize_evidence, _missing_retrieval_requirements, _should_expand_evidence_retrieval
    chunks = [{"evidence_id": "E1", "content": "M1401 电压24 V。"}, {"evidence_id": "E2", "content": "M1401 输入电压24 V。"}]
    messages = [ToolMessage(name="search_knowledge_base", tool_call_id="initial", content=json.dumps({"chunks": chunks}))]
    requirements = [{"id": "R1", "retrieval_query": "M1401 电压"}, {"id": "R2", "retrieval_query": "M1401 协议"}, {"id": "R3", "retrieval_query": "M1401 功能码"}]
    assessment = _summarize_evidence(messages, "M1401 电压、协议、功能码", evidence_mode="generic")
    missing = _missing_retrieval_requirements(messages, requirements, "generic")
    assert [item["id"] for item in missing] == ["R2", "R3"]
    assert _should_expand_evidence_retrieval(assessment, requirement_count=3, missing_requirements=missing)


def test_run_agent_expands_only_missing_requirements_when_chunk_count_looks_sufficient(monkeypatch):
    import json
    from types import SimpleNamespace
    from app.agents import graph
    from app.agents.answer_composer import AnswerComposition
    requirements = [{"id": f"R{index}", "subject": "M1401", "request": attribute, "output_type": "fact", "retrieval_query": f"M1401 {attribute}"} for index, attribute in enumerate(("电压", "协议", "功能码"), 1)]
    chunks = [
        {"evidence_id": "E1", "content": "M1401 额定电压24 V。", "document_name": "M1401.pdf"},
        {"evidence_id": "E2", "content": "M1401 输入电压24 V。", "document_name": "M1401.pdf"},
        {"evidence_id": "E3", "content": "M1401 支持 RS485。", "document_name": "M1401.pdf"},
        {"evidence_id": "E4", "content": "M1401 支持功能码0x01。", "document_name": "M1401.pdf"},
    ]
    calls = []
    def search(payload):
        calls.append((payload["query"], payload["top_k"]))
        hits = chunks[:2] if payload["top_k"] == 2 else [chunks[2] if "协议" in payload["query"] else chunks[3]]
        return json.dumps({"chunks": hits, "total": len(hits)}, ensure_ascii=False)
    def evaluate(model, query, plan, evidence, **kwargs):
        assert {item["evidence_id"] for item in evidence} == {"E1", "E2", "E3", "E4"}
        values = ["24 V", "RS485", "功能码0x01"]
        ids = ["E1", "E3", "E4"]
        results = [{"requirement_id": item["id"], "subject": item["subject"], "request": item["request"], "output_type": "fact", "status": "supported", "value": value, "evidence_ids": [evidence_id]} for item, value, evidence_id in zip(plan, values, ids)]
        return SimpleNamespace(results=results, coverage={"complete": True, "supported_count": 3}, warning=None, model_calls=0, token_usage={})
    monkeypatch.setattr(graph, "get_settings", lambda: SimpleNamespace(evidence_mode="generic", claim_validation_enabled=True, unified_search_initial_top_k=2, unified_search_top_k=4, requirement_plan_audit_enabled=True, model_routing_enabled=False))
    monkeypatch.setattr(graph, "get_agent", lambda **kwargs: SimpleNamespace(get_graph=lambda: None))
    monkeypatch.setattr(graph, "_try_semantic_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(graph, "evaluate_routing_policy", lambda *args, **kwargs: {})
    monkeypatch.setattr(graph, "_create_stage_model", lambda *args, **kwargs: object())
    monkeypatch.setattr(graph, "plan_requirements", lambda *args, **kwargs: SimpleNamespace(requirements=requirements, warning=None, model_calls=0, token_usage={}))
    monkeypatch.setattr(graph, "audit_requirement_plan", lambda *args, **kwargs: SimpleNamespace(requirements=requirements, complete=True, warning=None, model_calls=0, token_usage={}))
    monkeypatch.setattr(graph, "search_knowledge_base", SimpleNamespace(invoke=search))
    monkeypatch.setattr(graph, "evaluate_requirements", evaluate)
    monkeypatch.setattr(graph, "compose_answer", lambda model, query, results, coverage, evidence, **kwargs: AnswerComposition(draft=fallback_answer(results, coverage, evidence)))
    result = graph.run_agent([], "M1401 的电压、协议和功能码是什么？")
    assert [query for query, top_k in calls if top_k == 4] == ["M1401 协议", "M1401 功能码"]
    assert result["trace"]["routing_policy"]["retrieval_expanded"] is True
    assert result["evidence"]["status"] == "supported"
    assert "RS485" in result["answer"] and "0x01" in result["answer"]
