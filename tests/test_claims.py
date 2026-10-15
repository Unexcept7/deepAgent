import json

from app.agents.claims import (
    audit_requirement_plan,
    evaluate_requirements,
    plan_requirements,
)


class FakeModel:
    def __init__(self, outputs):
        self.outputs = iter(outputs)

    def invoke(self, _messages):
        return type("Response", (), {"content": next(self.outputs)})()


CHUNKS = [{
    "evidence_id": "E-1",
    "document_name": "XP2001.pdf",
    "page": 1,
    "content": "XP2001 是单路数据采集模块。",
}]


def test_plan_requirements_keeps_fact_and_source_as_separate_items():
    model = FakeModel([json.dumps({"requirements": [
        {
            "subject": "XP2001",
            "request": "通道数",
            "output_type": "fact",
            "retrieval_query": "XP2001 通道数",
            "query_span": "XP2001 分别是几通道",
        },
        {
            "subject": "XP2001",
            "request": "对应手册",
            "output_type": "source",
            "retrieval_query": "XP2001 对应手册",
            "query_span": "返回每个型号的对应手册",
        },
        {
            "subject": "XP2006",
            "request": "通道数",
            "output_type": "fact",
            "retrieval_query": "XP2006 通道数",
            "query_span": "XP2006 分别是几通道",
        },
        {
            "subject": "XP2006",
            "request": "对应手册",
            "output_type": "source",
            "retrieval_query": "XP2006 对应手册",
            "query_span": "返回每个型号的对应手册",
        },
    ]}, ensure_ascii=False)])

    plan = plan_requirements(
        model,
        "XP2001、XP2006 分别是几通道？请返回每个型号的对应手册。",
    )

    assert [item["id"] for item in plan.requirements] == ["R1", "R2", "R3", "R4"]
    assert [item["output_type"] for item in plan.requirements] == [
        "fact", "source", "fact", "source",
    ]
    assert plan.warning is None


def test_plan_requirements_retries_once_after_malformed_json():
    model = FakeModel([
        '{"requirements":[{"subject":"XP2001"',
        json.dumps({"requirements": [{
            "subject": "XP2001",
            "request": "通道数",
            "output_type": "fact",
            "retrieval_query": "XP2001 通道数",
        }]}, ensure_ascii=False),
    ])

    plan = plan_requirements(model, "XP2001 是几通道？")

    assert plan.warning is None
    assert plan.model_calls == 2
    assert plan.requirements[0]["subject"] == "XP2001"


def test_plan_requirements_follows_question_span_order_when_model_reorders_items():
    model = FakeModel([json.dumps({"requirements": [
        {
            "subject": "M1401",
            "request": "模块用途",
            "output_type": "fact",
            "retrieval_query": "M1401 模块用途",
            "query_span": "模块用途是什么",
        },
        {
            "subject": "M1401",
            "request": "通信接口",
            "output_type": "fact",
            "retrieval_query": "M1401 通信接口",
            "query_span": "通信接口是什么",
        },
        {
            "subject": "M1401",
            "request": "数字量输入路数",
            "output_type": "fact",
            "retrieval_query": "M1401 数字量输入路数",
            "query_span": "有多少路数字量输入",
        },
    ]}, ensure_ascii=False)])

    plan = plan_requirements(
        model,
        "M1401 有多少路数字量输入？通信接口是什么？模块用途是什么？",
    )

    assert [item["request"] for item in plan.requirements] == [
        "数字量输入路数",
        "通信接口",
        "模块用途",
    ]
    assert [item["id"] for item in plan.requirements] == ["R1", "R2", "R3"]


def test_malformed_json_warning_does_not_expose_parser_details():
    model = FakeModel(['{"requirements":[{"subject":"XP2001"'])

    plan = plan_requirements(model, "XP2001 是几通道？")

    assert plan.warning is not None
    assert "JSON 解析失败" in plan.warning
    assert "Expecting ',' delimiter" not in plan.warning


def test_content_comparison_plan_requires_content_evidence_not_only_source_names():
    class CapturingModel:
        def __init__(self):
            self.messages = []

        def invoke(self, messages):
            self.messages = messages
            return type("Response", (), {"content": json.dumps({"requirements": [
                {
                    "subject": "系列 A 协议资料",
                    "request": "对应资料",
                    "output_type": "source",
                    "retrieval_query": "系列 A 协议资料 对应手册",
                    "query_span": "资料",
                },
                {
                    "subject": "系列 A 协议资料",
                    "request": "适用范围和覆盖主题",
                    "output_type": "fact",
                    "retrieval_query": "系列 A 协议资料 适用范围 覆盖主题",
                    "query_span": "如何区分",
                },
            ]}, ensure_ascii=False)})()

    model = CapturingModel()
    plan = plan_requirements(
        model,
        "如何根据两份资料的实际内容区分应使用哪一份？",
    )

    system_prompt = model.messages[0][1]
    assert "不能把文件名、标题或来源元数据当作内容证据" in system_prompt
    assert [item["output_type"] for item in plan.requirements] == ["source", "fact"]
    assert "适用范围" in plan.requirements[1]["request"]


def test_requirement_audit_adds_missing_source_before_retrieval():
    current = [{
        "id": "R1",
        "subject": "XP2001",
        "request": "通道数",
        "output_type": "fact",
        "retrieval_query": "XP2001 通道数",
        "query_span": "几通道",
    }]
    model = FakeModel([json.dumps({
        "complete": False,
        "missing_requirements": [{
            "subject": "XP2001",
            "request": "对应手册",
            "output_type": "source",
            "retrieval_query": "XP2001 对应手册",
            "query_span": "返回对应手册",
        }],
        "reasons": ["遗漏了来源输出要求"],
    }, ensure_ascii=False)])

    audit = audit_requirement_plan(
        model,
        "XP2001 是几通道？请返回对应手册。",
        current,
    )

    assert audit.complete is True
    assert [item["id"] for item in audit.requirements] == ["R1", "R2"]
    assert audit.requirements[1]["output_type"] == "source"
    assert audit.requirements[1]["retrieval_query"] == "XP2001 对应手册"


REQUIREMENTS = [
    {
        "id": "R1",
        "subject": "XP2001",
        "request": "通道数",
        "output_type": "fact",
        "retrieval_query": "XP2001 通道数",
        "query_span": "几通道",
    },
    {
        "id": "R2",
        "subject": "XP2001",
        "request": "对应手册",
        "output_type": "source",
        "retrieval_query": "XP2001 对应手册",
        "query_span": "对应手册",
    },
]

REQUIREMENT_CHUNKS = [{
    "evidence_id": "E-1",
    "document_name": "XP2001单路数据采集模块.pdf",
    "document_id": "doc-1",
    "dataset_id": "dataset-1",
    "page": 1,
    "score": 0.91,
    "content": "XP2001 是单路数据采集模块。",
}]


def test_evaluate_requirements_returns_sources_and_complete_coverage():
    model = FakeModel([
        json.dumps({"results": [
            {"requirement_id": "R1", "value": "单路", "evidence_ids": ["E-1"]},
            {
                "requirement_id": "R2",
                "value": "XP2001单路数据采集模块.pdf",
                "evidence_ids": ["E-1"],
            },
        ]}, ensure_ascii=False),
        json.dumps({
            "plan_complete": True,
            "missing_requests": [],
            "verdicts": [
                {
                    "requirement_id": "R1",
                    "status": "supported",
                    "reason": "原文明确为单路。",
                    "evidence_ids": ["E-1"],
                },
                {
                    "requirement_id": "R2",
                    "status": "supported",
                    "reason": "来源元数据给出了准确手册名。",
                    "evidence_ids": ["E-1"],
                },
            ],
        }, ensure_ascii=False),
    ])

    evaluation = evaluate_requirements(
        model,
        "XP2001 是几通道？请返回对应手册。",
        REQUIREMENTS,
        REQUIREMENT_CHUNKS,
    )

    assert len(evaluation.results) == 2
    assert evaluation.results[1]["value"] == "XP2001单路数据采集模块.pdf"
    assert evaluation.results[1]["sources"][0]["url"] == "/source-documents/dataset-1/doc-1"
    assert evaluation.coverage["complete"] is True
    assert evaluation.coverage["all_requirements_accounted_for"] is True


def test_evaluate_requirements_binds_source_when_model_omits_long_evidence_id():
    model = FakeModel([
        json.dumps({"results": [
            {"requirement_id": "R1", "value": "单路", "evidence_ids": []},
        ]}, ensure_ascii=False),
        json.dumps({
            "plan_complete": True,
            "missing_requests": [],
            "verdicts": [{
                "requirement_id": "R1",
                "status": "supported",
                "reason": "正文明确支持",
                "evidence_ids": [],
            }],
        }, ensure_ascii=False),
    ])
    requirements = [REQUIREMENTS[0]]

    evaluation = evaluate_requirements(
        model,
        "XP2001 是几通道？",
        requirements,
        REQUIREMENT_CHUNKS,
    )

    result = evaluation.results[0]
    assert result["status"] == "supported"
    assert result["evidence_ids"] == ["E-1"]
    assert result["source_binding_status"] == "complete_deterministic"
    assert "自动完成证据绑定" in result["reason"]
    assert evaluation.coverage["supported_count"] == 1


def test_supported_requirement_without_resolvable_source_is_downgraded():
    model = FakeModel([
        json.dumps({"results": [
            {"requirement_id": "R1", "value": "单路", "evidence_ids": ["E-1"]},
            {"requirement_id": "R2", "value": "XP2001.pdf", "evidence_ids": ["E-1"]},
        ]}, ensure_ascii=False),
        json.dumps({
            "plan_complete": True,
            "missing_requests": [],
            "verdicts": [
                {"requirement_id": "R1", "status": "supported", "reason": "支持", "evidence_ids": ["E-1"]},
                {"requirement_id": "R2", "status": "supported", "reason": "支持", "evidence_ids": ["E-1"]},
            ],
        }, ensure_ascii=False),
    ])
    chunks = [{"evidence_id": "E-1", "page": 1, "content": "XP2001 是单路数据采集模块。"}]

    evaluation = evaluate_requirements(
        model,
        "XP2001 是几通道？请返回对应手册。",
        REQUIREMENTS,
        chunks,
    )

    assert {item["status"] for item in evaluation.results} == {"unsupported"}
    assert all(item["evidence_ids"] == [] for item in evaluation.results)
    assert all(item["sources"] == [] for item in evaluation.results)
    assert all(item["source_binding_status"] == "missing" for item in evaluation.results)
    assert evaluation.coverage["source_binding_complete"] is False
    assert evaluation.coverage["source_binding_failures"] == ["R1", "R2"]
    assert "来源绑定" in (evaluation.warning or "")


def test_sources_group_same_document_page_and_keep_all_evidence_ids():
    model = FakeModel([
        json.dumps({"results": [
            {"requirement_id": "R1", "value": "存在冲突", "evidence_ids": ["E-1", "E-2"]},
            {"requirement_id": "R2", "value": "XP2001.pdf", "evidence_ids": ["E-1"]},
        ]}, ensure_ascii=False),
        json.dumps({
            "plan_complete": True,
            "missing_requests": [],
            "verdicts": [
                {"requirement_id": "R1", "status": "conflict", "reason": "两段内容冲突", "evidence_ids": ["E-1", "E-2"]},
                {"requirement_id": "R2", "status": "supported", "reason": "支持", "evidence_ids": ["E-1"]},
            ],
        }, ensure_ascii=False),
    ])
    chunks = [
        {"evidence_id": "E-1", "document_name": "XP2001.pdf", "document_id": "doc-1", "dataset_id": "ds-1", "page": 1, "content": "第一种说法。"},
        {"evidence_id": "E-2", "document_name": "XP2001.pdf", "document_id": "doc-1", "dataset_id": "ds-1", "page": 1, "content": "第二种说法。"},
    ]

    evaluation = evaluate_requirements(
        model,
        "XP2001 的资料是否冲突？请返回对应手册。",
        REQUIREMENTS,
        chunks,
    )

    conflict_source = evaluation.results[0]["sources"][0]
    assert evaluation.results[0]["status"] == "conflict"
    assert conflict_source["evidence_ids"] == ["E-1", "E-2"]
    assert evaluation.results[0]["source_binding_status"] == "complete"


def test_missing_verdict_is_published_as_unsupported_and_fails_audit():
    model = FakeModel([
        '{"results":[{"requirement_id":"R1","value":"单路","evidence_ids":["E-1"]}]}',
        json.dumps({
            "plan_complete": True,
            "missing_requests": [],
            "verdicts": [{
                "requirement_id": "R1",
                "status": "supported",
                "reason": "原文明确支持。",
                "evidence_ids": ["E-1"],
            }],
        }, ensure_ascii=False),
    ])

    evaluation = evaluate_requirements(
        model,
        "XP2001 是几通道？请返回对应手册。",
        REQUIREMENTS,
        REQUIREMENT_CHUNKS,
    )

    assert len(evaluation.results) == 2
    assert evaluation.results[1]["status"] == "unsupported"
    assert evaluation.results[1]["reason_code"] == "judge_missing"
    assert evaluation.coverage["complete"] is False
    assert "R2" in evaluation.coverage["missing_requests"][0]


def test_no_evidence_still_publishes_every_requirement():
    evaluation = evaluate_requirements(
        FakeModel([]),
        "XP2001 是几通道？请返回对应手册。",
        REQUIREMENTS,
        [],
    )

    assert len(evaluation.results) == 2
    assert {item["status"] for item in evaluation.results} == {"unsupported"}
    assert evaluation.coverage["all_requirements_accounted_for"] is True
    assert evaluation.coverage["audited"] is False


def test_requirement_results_keep_requested_manual_as_a_judged_fact():
    evaluation = evaluate_requirements(
        FakeModel([
            json.dumps({"results": [
                {"requirement_id": "R1", "value": "单路", "evidence_ids": ["E-1"]},
                {"requirement_id": "R2", "value": "XP2001单路数据采集模块.pdf", "evidence_ids": ["E-1"]},
            ]}, ensure_ascii=False),
            json.dumps({
                "plan_complete": True,
                "missing_requests": [],
                "verdicts": [
                    {"requirement_id": "R1", "status": "supported", "reason": "支持", "evidence_ids": ["E-1"]},
                    {"requirement_id": "R2", "status": "supported", "reason": "支持", "evidence_ids": ["E-1"]},
                ],
            }, ensure_ascii=False),
        ]),
        "XP2001 是几通道？请返回对应手册。",
        REQUIREMENTS,
        REQUIREMENT_CHUNKS,
    )

    assert [item["value"] for item in evaluation.results] == [
        "单路",
        "XP2001单路数据采集模块.pdf",
    ]
    assert evaluation.coverage["complete"] is True
