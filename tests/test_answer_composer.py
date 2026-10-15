import json

from app.agents.answer_composer import (
    compose_answer,
    fallback_answer,
    validate_answer_plan,
)


class FakeModel:
    def __init__(self, payloads):
        self.payloads = iter(payloads)

    def invoke(self, _messages):
        return type(
            "Response",
            (),
            {"content": json.dumps(next(self.payloads), ensure_ascii=False)},
        )()


CHUNKS = [{
    "evidence_id": "E-1",
    "document_name": "XP3018-AO、DO_DI说明书.pdf",
    "document_id": "doc-1",
    "dataset_id": "dataset-1",
    "page": 1,
    "content": "XP3018 系列外形总宽度为 145 mm。",
}]

RESULTS = [{
    "requirement_id": "R1",
    "subject": "XP3018",
    "request": "总体宽度",
    "output_type": "fact",
    "value": "145 mm",
    "status": "supported",
    "reason": "原文明确支持",
    "evidence_ids": ["E-1"],
    "sources": [{"document_name": CHUNKS[0]["document_name"], "page": 1}],
}]


def test_fact_plan_renders_ledger_value_and_ignores_free_form_answer():
    model = FakeModel([{
        "status": "supported",
        "answer": "XP3018 的总体宽度为 999 mm。",
        "answer_plan": {
            "segments": [{
                "type": "fact",
                "fact_id": "F:R1",
                "prefix": "XP3018-AO 的总体宽度为",
                "suffix": "。",
            }],
            "used_fact_ids": ["F:R1"],
        },
    }])

    composition = compose_answer(
        model,
        "XP3018 IO模块的长度是多少？",
        RESULTS,
        {"complete": True},
        CHUNKS,
    )

    assert composition.draft is not None
    assert composition.draft.answer == "XP3018-AO 的总体宽度为145 mm。"
    assert "999" not in composition.draft.answer
    assert composition.draft.used_evidence_ids == ["E-1"]
    assert validate_answer_plan(composition.draft, CHUNKS)["passed"] is True


def test_unknown_fact_id_fails_binding_but_never_publishes_its_value():
    model = FakeModel([{
        "status": "supported",
        "answer": "XP3018 的总体宽度为 999 mm。",
        "answer_plan": {
            "segments": [{
                "type": "fact",
                "fact_id": "F:R999",
                "prefix": "XP3018 的总体宽度为",
                "suffix": "。",
            }],
            "used_fact_ids": ["F:R999"],
        },
    }])

    composition = compose_answer(
        model,
        "XP3018 IO模块的长度是多少？",
        RESULTS,
        {"complete": True},
        CHUNKS,
    )

    assert composition.draft is not None
    assert "999" not in composition.draft.answer
    assert "145 mm" in composition.draft.answer
    validation = validate_answer_plan(composition.draft, CHUNKS)
    assert validation["passed"] is False
    assert "F:R999" in validation["invalid_fact_refs"]


def test_legacy_model_without_plan_uses_deterministic_fact_plan():
    model = FakeModel([{
        "status": "supported",
        "answer": "模型自由回答的错误值 999 mm。",
    }])

    composition = compose_answer(
        model,
        "XP3018 IO模块的长度是多少？",
        RESULTS,
        {"complete": True},
        CHUNKS,
    )

    assert composition.draft is not None
    assert composition.draft.repaired is True
    assert composition.draft.answer == "根据已验证资料：\nXP3018的总体宽度为145 mm。"
    assert validate_answer_plan(composition.draft, CHUNKS)["passed"] is True


def test_fallback_answer_keeps_only_judged_values():
    draft = fallback_answer(RESULTS, {"complete": True}, CHUNKS)

    assert draft.answer == "根据已验证资料：\nXP3018的总体宽度为145 mm。"
    assert draft.answer_plan is not None
    assert draft.answer_plan.used_fact_ids == ["F:R1"]
    assert validate_answer_plan(draft, CHUNKS)["passed"] is True
