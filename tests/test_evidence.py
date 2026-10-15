import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents import graph
from app.agents.evidence import (
    assess_evidence,
    contextualize_query,
    extract_aspects,
    extract_identifiers,
    find_similar_identifiers,
    query_requires_evidence,
    validate_answer,
)


def chunk(content, document_name, score=0.9, document_id="doc-1"):
    return {
        "content": content,
        "document_name": document_name,
        "document_id": document_id,
        "dataset_id": "dataset-1",
        "page": 1,
        "score": score,
    }


def test_extracts_product_identifiers_but_not_protocol_tokens():
    identifiers = extract_identifiers(
        "比较 FT-RS-0808、M1501 和 IL-1000，经 RS-485 使用 Modbus-RTU。"
    )

    assert identifiers == ["FT-RS-0808", "M1501", "IL-1000"]


def test_off_mode_passes_candidates_through_without_completeness_gate():
    result = assess_evidence(
        "请返回系统中所有 FT-RS-9999 信息",
        [chunk("FT-RS-0808 支持功能码 03。", "FT-RS-0808说明书.pdf", score=0.4)],
        mode="off",
    )

    assert result["status"] == "unverified"
    assert result["candidate_count"] == 1
    assert result["provided_count"] == 1
    assert result["verified_count"] == 0
    assert result["coverage_limited"] is False
    assert len(result["chunks"]) == 1


def test_finds_nearby_model_as_typo_suggestion_without_accepting_it_as_evidence():
    query = "知识库中是否有 FT-RS-8080 的说明书？"
    candidates = [
        chunk(
            "FT-RS-0808 模块支持 Modbus RTU 通信。",
            "FT-RS-0808_MODBUS-RTU协议说明书.pdf",
        ),
        chunk("YC1008 模块通讯协议。", "YC1008说明书.pdf"),
    ]

    suggestions = find_similar_identifiers(query, candidates)

    assert suggestions[0]["requested"] == "FT-RS-8080"
    assert suggestions[0]["candidate"] == "FT-RS-0808"
    assert all(item["candidate"] != "YC1008" for item in suggestions)


def test_extracts_requested_technical_aspects():
    aspects = extract_aspects("它支持哪些功能码，默认波特率和停止位是多少？")

    assert "function_code" in aspects
    assert "communication_parameters" in aspects


def test_greeting_does_not_require_evidence_but_device_fact_does():
    assert query_requires_evidence("你好，请介绍一下你能做什么。") is False
    assert query_requires_evidence("FT-RS-0808 支持哪些功能码？") is True


def test_context_inherits_identifier_only_from_user_messages():
    history = [
        {"role": "user", "content": "请介绍 FT-RS-0808。"},
        {"role": "assistant", "content": "另一个型号是 FT-RS-9999。"},
    ]

    contextualized = contextualize_query(history, "它读取数字量输入用哪个功能码？")

    assert "FT-RS-0808" in contextualized
    assert "FT-RS-9999" not in contextualized


def test_context_summary_can_supply_an_older_reference_without_trusting_assistant_text():
    history = [
        {"role": "assistant", "content": "另一个型号是 FT-RS-9999。"},
    ]

    contextualized = contextualize_query(
        history,
        "它支持哪些功能码？",
        {"candidate_entities": [{"value": "FT-RS-0808"}]},
    )

    assert "FT-RS-0808" in contextualized
    assert "FT-RS-9999" not in contextualized


def test_contextualization_keeps_the_fault_code_and_inherits_the_device():
    contextualized = contextualize_query(
        [],
        "它的 E203 是什么意思？",
        {"candidate_entities": [{"value": "北辰47", "entity_type": "device"}]},
    )

    assert "E203" in contextualized
    assert "北辰47" in contextualized


def test_rejects_high_similarity_chunk_for_a_different_product():
    result = assess_evidence(
        "FT-RS-9999 支持哪些 Modbus 功能码？",
        [chunk(
            "FT-RS-0808 支持 01、02、05、0F 功能码。",
            "FT-RS-0808说明书.pdf",
            score=0.96,
        )],
    )

    assert result["status"] == "not_found"
    assert result["verified_count"] == 0
    assert result["missing_entities"] == ["FT-RS-9999"]
    assert result["rejection_reasons"]["subject_mismatch"] == 1


def test_accepts_matching_product_and_requested_property():
    result = assess_evidence(
        "FT-RS-0808 支持哪些 Modbus 功能码？",
        [chunk(
            "FT-RS-0808 的功能码包括 01 读取输出、02 读取输入、05 写单路和 0F 写多路。",
            "FT-RS-0808说明书.pdf",
        )],
    )

    assert result["status"] == "supported"
    assert result["verified_count"] == 1
    assert result["chunks"][0]["evidence_id"].startswith("E-")
    assert result["covered_aspects"] == ["function_code"]


def test_strict_mode_keeps_matching_low_similarity_evidence():
    result = assess_evidence(
        "FT-RS-0808 支持哪些 Modbus 功能码？",
        [chunk(
            "FT-RS-0808 的功能码包括 01、02、05 和 0F。",
            "FT-RS-0808说明书.pdf",
            score=0.12,
        )],
        min_similarity=0.2,
        generic_min_similarity=0.3,
        mode="industrial_strict",
    )

    assert result["status"] == "supported"
    assert result["verified_count"] == 1
    assert result["rejected_count"] == 0
    assert result["chunks"][0]["similarity_warning"] is True
    assert "相似度仅用于排序" in result["reason"]


def test_multiple_requested_communication_fields_require_all_subfields():
    result = assess_evidence(
        "FT-RS-0808 的默认设备地址、波特率、数据位、停止位和校验方式是什么？",
        [chunk(
            "FT-RS-0808 默认设备地址 01，默认波特率 9600 bps。",
            "FT-RS-0808说明书.pdf",
        )],
    )

    assert result["status"] == "partial"
    requirements = {item["requirement"] for item in result["missing_requirements"]}
    assert requirements == {"data_bits", "stop_bits", "parity"}


def test_multi_entity_channel_comparison_requires_input_and_output_for_each():
    result = assess_evidence(
        "比较 FT-RS-0808 和 M1501 的数字量输入输出能力。",
        [
            chunk("FT-RS-0808 有 8 路数字量输入和 8 路数字量输出。", "FT-RS-0808.pdf", document_id="1"),
            chunk("M1501 有 8 路数字量输入。", "M1501.pdf", document_id="2"),
        ],
    )

    assert result["status"] == "partial"
    assert {item["requirement"] for item in result["missing_requirements"]} == {"digital_output"}
    assert result["missing_requirements"][0]["entity"] == "M1501"


def test_rejects_same_product_chunk_when_requested_property_is_missing():
    result = assess_evidence(
        "FT-RS-0808 支持哪些 Modbus 功能码？",
        [chunk(
            "FT-RS-0808 是一款 8 路输入和 8 路输出模块。",
            "FT-RS-0808说明书.pdf",
        )],
    )

    assert result["status"] == "not_found"
    assert result["rejection_reasons"]["aspect_mismatch"] == 1


def test_rejects_navigation_mention_inside_another_model_manual():
    result = assess_evidence(
        "比较 M1501 和 M1516 的数字量输入输出能力。",
        [chunk(
            "模块列表：M1501、M1516。当前 M1508 有 4 路模拟量输入和 10 路数字量输出。",
            "M1508（4路模拟量输入、10路数字量输出）.pdf",
        )],
    )

    assert result["status"] == "not_found"
    assert result["verified_count"] == 0
    assert result["rejection_reasons"]["document_subject_mismatch"] == 1


def test_multi_product_comparison_is_partial_when_one_product_is_missing():
    result = assess_evidence(
        "比较 FT-RS-0808、M1501 和 M1516 的数字量输入输出能力。",
        [
            chunk("FT-RS-0808 有 8 路数字量输入和 8 路数字量输出。", "FT-RS-0808.pdf", document_id="1"),
            chunk("M1501 有 8 路数字量输入和 8 路数字量输出。", "M1501.pdf", document_id="2"),
        ],
    )

    assert result["status"] == "partial"
    assert result["missing_entities"] == ["M1516"]


def test_mixed_product_and_protocol_comparison_accepts_separate_sources():
    result = assess_evidence(
        "FT-RS-0808 使用的 Modbus-RTU 和 OPC UA 有什么区别？",
        [
            chunk(
                "FT-RS-0808 使用 RS-485 接口和 Modbus-RTU 协议。",
                "FT-RS-0808.pdf",
                document_id="1",
            ),
            chunk(
                "OPC UA 是面向信息模型的工业通信协议和接口规范。",
                "OPC-UA规范.pdf",
                document_id="2",
            ),
        ],
    )

    assert result["status"] == "supported"
    assert result["verified_count"] == 2


def test_all_records_question_is_never_claimed_complete_from_top_k():
    result = assess_evidence(
        "请返回系统中所有设备的信息。",
        [chunk("设备名称：FT-RS-0808。", "设备目录.pdf")],
    )

    assert result["status"] == "partial"
    assert result["coverage_limited"] is True


def test_conflicting_scalar_facts_are_reported():
    result = assess_evidence(
        "FT-RS-0808 有多少路数字量输入？",
        [
            chunk("FT-RS-0808 有 8 路数字量输入。", "旧版说明书.pdf", document_id="1"),
            chunk("FT-RS-0808 有 16 路数字量输入。", "新版说明书.pdf", document_id="2"),
        ],
    )

    assert result["status"] == "conflict"
    assert result["conflicts"][0]["values"] == ["16", "8"]


def test_unrelated_scalar_values_do_not_create_conflict_for_document_presence():
    result = assess_evidence(
        "知识库中是否有 FT-RS-0808 的说明书？请列出文件名。",
        [
            chunk(
                "FT-RS-0808 说明书。产品有 8 路输入，地址范围 1 到 63。",
                "FT-RS-0808说明书.pdf",
            ),
        ],
    )

    assert result["status"] == "supported"
    assert result["conflicts"] == []


def test_filename_output_type_does_not_create_fake_channel_conflict():
    result = assess_evidence(
        "M1401 有多少路数字量输入？",
        [
            chunk(
                "M1401 的通信接口为 RS485。",
                "M1401（8路模拟量输出）.pdf",
                document_id="analog",
            ),
            chunk(
                "M1401 的通信接口为 RS485。",
                "M1401（8路数字量输出）.pdf",
                document_id="digital",
            ),
        ],
    )

    assert result["conflicts"] == []


def test_answer_validator_rejects_new_model_and_unsupported_value():
    assessment = assess_evidence(
        "FT-RS-0808 支持哪些功能码？",
        [chunk("FT-RS-0808 支持功能码 01、02、05 和 0F。", "FT-RS-0808.pdf")],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        "FT-RS-9999 支持功能码 0x10，并有 16 路输入。",
        "FT-RS-0808 支持哪些功能码？",
        assessment["chunks"],
        [evidence_id],
    )

    assert result["passed"] is False
    assert any("FT-RS-9999" in item for item in result["violations"])
    assert any("0x10" in item or "16 路" in item for item in result["violations"])


def test_not_found_safe_answer_may_repeat_the_requested_identifier():
    result = validate_answer(
        "当前知识库没有找到 FT-RS-9999 的可靠资料，无法确认。",
        "FT-RS-9999 支持哪些功能码？",
        [],
        [],
    )

    assert result["passed"] is True


def test_answer_validator_accepts_values_supported_by_evidence():
    assessment = assess_evidence(
        "FT-RS-0808 支持哪些功能码？",
        [chunk(
            "FT-RS-0808 支持功能码 0x01、0x02、0x05 和 0x0F。",
            "FT-RS-0808.pdf",
        )],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        "FT-RS-0808 支持功能码 0x01、0x02、0x05 和 0x0F。",
        "FT-RS-0808 支持哪些功能码？",
        assessment["chunks"],
        [evidence_id],
    )

    assert result["passed"] is True


def test_internal_evidence_ids_are_not_treated_as_product_models():
    assessment = assess_evidence(
        "FT-RS-0808 支持哪些功能码？",
        [chunk(
            "FT-RS-0808 支持功能码 0x01、0x02、0x05 和 0x0F。",
            "FT-RS-0808.pdf",
        )],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        f"FT-RS-0808 支持功能码 0x01、0x02、0x05 和 0x0F。证据：{evidence_id}",
        "FT-RS-0808 支持哪些功能码？",
        assessment["chunks"],
        [evidence_id],
    )

    assert result["passed"] is True
    assert not any("型号" in item for item in result["violations"])


def test_hexadecimal_frame_value_is_not_treated_as_product_model():
    text = "FT-RS-0808 响应报文中的数据字节为 FF00，功能码为 0x05。"

    assert extract_identifiers(text) == ["FT-RS-0808"]


def test_function_code_is_not_blocked_by_hex_frame_value():
    query = "FT-RS-0808 支持哪些功能码？"
    assessment = assess_evidence(
        query,
        [chunk(
            "FT-RS-0808 响应报文中的数据字节为 FF00，支持功能码 0x05。",
            "FT-RS-0808说明书.pdf",
        )],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        "FT-RS-0808 支持功能码 0x05，响应数据为 FF00。",
        query,
        assessment["chunks"],
        [evidence_id],
    )

    assert result["passed"] is True


def test_classifier_keeps_real_hex_like_model_when_labeled_as_model():
    assert extract_identifiers("设备型号 A100 的功能码包括 0x05。") == ["A100"]


def test_classifier_ignores_common_protocol_values_without_blacklist_growth():
    text = (
        "FT-RS-0808 使用 RS-485，版本 V1.2，设备地址 01，"
        "请求帧 010300000002，CRC16 校验，证据 E-ABCDEF123456。"
    )

    assert extract_identifiers(text) == ["FT-RS-0808"]


def test_unknown_product_is_still_an_entity_and_cannot_borrow_other_product_evidence():
    result = assess_evidence(
        "FT-RS-9999 支持哪些功能码？",
        [chunk(
            "FT-RS-0808 支持功能码 0x05。",
            "FT-RS-0808说明书.pdf",
            score=0.99,
        )],
    )

    assert extract_identifiers("FT-RS-9999 支持哪些功能码？") == ["FT-RS-9999"]
    assert result["status"] == "not_found"
    assert result["missing_entities"] == ["FT-RS-9999"]


def test_generic_mode_accepts_new_document_domain_without_industrial_schema():
    query = "公司的差旅报销标准是什么？"
    assessment = assess_evidence(
        query,
        [chunk(
            "公司差旅报销标准：市内交通凭票报销，住宿标准按职级执行。",
            "员工差旅管理制度.docx",
        )],
        mode="generic",
    )

    assert assessment["status"] == "supported"
    assert assessment["requested_entities"] == []
    assert assessment["requested_aspects"] == []
    assert len(assessment["chunks"]) == 1


def test_generic_mode_uses_retrieval_candidates_without_domain_anchor_rules():
    assessment = assess_evidence(
        "这个项目的审批流程是什么？",
        [chunk(
            "Alpha-2026 需要经过部门负责人和财务负责人审批。",
            "项目管理制度.docx",
        )],
        mode="generic",
    )

    assert assessment["status"] == "supported"
    assert not assessment["rejection_reasons"].get("query_anchor_mismatch")


def test_generic_mode_keeps_entity_matched_low_similarity_evidence():
    assessment = assess_evidence(
        "M120 的接口组合是什么？",
        [chunk(
            "M120：1个以太网口、1个RS485、4 DI、4 AI、2 AO、4 DO或继电器输出。",
            "Mxx说明书.pdf",
            score=0.288,
        )],
        generic_min_similarity=0.3,
        mode="generic",
    )

    assert assessment["status"] == "supported"
    assert assessment["verified_count"] == 1
    assert assessment["rejected_count"] == 0
    assert assessment["rejection_reasons"] == {}
    assert assessment["chunks"][0]["similarity_warning"] is True
    assert any("相似度仅用于排序" in item for item in assessment["warnings"])


def test_generic_mode_still_rejects_nonmatching_entity_regardless_of_score():
    assessment = assess_evidence(
        "M120 的接口组合是什么？",
        [chunk(
            "M160：1个以太网口、1个RS485、8 DI、8 AI、8 DO。",
            "Mxx说明书.pdf",
            score=0.99,
        )],
        generic_min_similarity=0.3,
        mode="generic",
    )

    assert assessment["status"] == "not_found"
    assert assessment["verified_count"] == 0
    assert assessment["rejection_reasons"]["entity_mismatch"] == 1


def test_generic_mode_requires_each_explicit_entity_to_have_evidence():
    assessment = assess_evidence(
        "姣旇緝 FT-RS-0808 鍜?FT-RSA-0204 鐨勮緭鍏ヨ緭鍑鸿兘鍔?",
        [chunk(
            "FT-RS-0808 鏈?8 璺暟瀛楅噺杈撳叆鍜?8 璺暟瀛楅噺杈撳嚭銆?",
            "FT-RS-0808璇存槑涔?pdf",
        )],
        mode="generic",
    )

    assert assessment["status"] == "partial"
    assert assessment["covered_entities"] == ["FT-RS-0808"]
    assert assessment["missing_entities"] == ["FT-RSA-0204"]


def test_generic_answer_rejects_unrequested_explicit_entity():
    assessment = assess_evidence(
        "FT-RS-0808 鏀寔鍝簺鍔熻兘鐮?",
        [chunk("FT-RS-0808 鏀寔鍔熻兘鐮?0x05銆?", "FT-RS-0808.pdf")],
        mode="generic",
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]
    result = validate_answer(
        "FT-RSA-0204 鏀寔 0x05",
        "FT-RS-0808 鏀寔鍝簺鍔熻兘鐮?",
        assessment["chunks"],
        [evidence_id],
        mode="generic",
    )

    assert result["passed"] is False
    assert any("FT-RSA-0204" in item for item in result["violations"])


def test_generic_answer_allows_verified_delimited_model_variant():
    query = "XP3018 IO模块的长度是多少？"
    assessment = assess_evidence(
        query,
        [chunk(
            "XP3018 系列外形总宽度为 145 mm。",
            "XP3018-AO、DO_DI 共8通道.pdf",
        )],
        mode="generic",
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        "XP3018-AO 的长度为 145 mm。",
        query,
        assessment["chunks"],
        [evidence_id],
        mode="generic",
    )

    assert result["passed"] is True


def test_generic_answer_validation_does_not_classify_domain_tokens():
    assessment = assess_evidence(
        "项目 Alpha-2026 的审批流程是什么？",
        [chunk(
            "项目 Alpha-2026 需要经过部门负责人和财务负责人审批。",
            "项目管理制度.docx",
        )],
        mode="generic",
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        "项目 Alpha-2026 需要部门负责人和财务负责人审批。",
        "项目 Alpha-2026 的审批流程是什么？",
        assessment["chunks"],
        [evidence_id],
        mode="generic",
    )

    assert result["passed"] is True


def test_answer_validator_accepts_slash_joined_requested_identifiers():
    query = "比较 FT-RS-0808、M1501 和 M1516 的数字量输入输出能力。"
    assessment = assess_evidence(
        query,
        [
            chunk("FT-RS-0808 有 8 路数字量输入和 8 路数字量输出。", "FT-RS-0808.pdf"),
            chunk("M1501 有 8 路数字量输入和 8 路数字量输出。", "M1501.pdf"),
            chunk("M1516 有 8 路数字量输入和 8 路数字量输出。", "M1516.pdf"),
        ],
    )
    evidence_ids = [item["evidence_id"] for item in assessment["chunks"]]

    result = validate_answer(
        "FT-RS-0808、M1501/M1516 均为 8 路输入和 8 路输出。",
        query,
        assessment["chunks"],
        evidence_ids,
    )

    assert result["passed"] is True


def test_hex_function_code_is_supported_by_plain_hex_source_notation():
    assessment = assess_evidence(
        "FT-RS-0808 支持哪些功能码？",
        [chunk("FT-RS-0808 支持功能码 01、02、05 和 0F。", "FT-RS-0808.pdf")],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        "FT-RS-0808 支持功能码 0x0F。",
        "FT-RS-0808 支持哪些功能码？",
        assessment["chunks"],
        [evidence_id],
    )

    assert result["passed"] is True


def test_answer_validator_requires_evidence_ids_for_factual_answer():
    assessment = assess_evidence(
        "FT-RS-0808 有多少路输入？",
        [chunk("FT-RS-0808 有 8 路数字量输入。", "FT-RS-0808.pdf")],
    )

    result = validate_answer(
        "FT-RS-0808 有 8 路数字量输入。",
        "FT-RS-0808 有多少路输入？",
        assessment["chunks"],
        [],
    )

    assert result["passed"] is False
    assert any("证据 ID" in item for item in result["violations"])


def test_answer_validator_allows_server_side_evidence_binding():
    query = "FT-RS-0808 有多少路输入？"
    assessment = assess_evidence(
        query,
        [chunk("FT-RS-0808 有 8 路数字量输入。", "FT-RS-0808.pdf")],
    )
    result = validate_answer(
        "FT-RS-0808 有 8 路数字量输入。",
        query,
        assessment["chunks"],
        [],
        require_evidence_ids=False,
    )
    assert result["passed"] is True


def test_user_supplied_number_is_not_treated_as_evidence():
    assessment = assess_evidence(
        "FT-RS-0808 是 16 路输入吗？",
        [chunk("FT-RS-0808 有 8 路数字量输入。", "FT-RS-0808.pdf")],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        "是的，FT-RS-0808 有 16 路输入。",
        "FT-RS-0808 是 16 路输入吗？",
        assessment["chunks"],
        [evidence_id],
    )

    assert result["passed"] is False
    assert any("16 路" in item for item in result["violations"])


def test_labeled_communication_scalar_must_match_evidence():
    assessment = assess_evidence(
        "FT-RS-0808 的默认设备地址和波特率是什么？",
        [chunk(
            "FT-RS-0808 出厂默认设备地址为 01，默认波特率为 9600 bps。",
            "FT-RS-0808.pdf",
        )],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        "FT-RS-0808 的默认设备地址为 02，默认波特率为 19200 bps。",
        "FT-RS-0808 的默认设备地址和波特率是什么？",
        assessment["chunks"],
        [evidence_id],
    )

    assert result["passed"] is False
    assert any("default_device_address" in item for item in result["violations"])
    assert any("19200" in item for item in result["violations"])


def test_function_code_list_checks_every_value():
    assessment = assess_evidence(
        "FT-RS-0808 支持哪些功能码？",
        [chunk("FT-RS-0808 支持功能码 01、02、05 和 0F。", "FT-RS-0808.pdf")],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]

    result = validate_answer(
        "FT-RS-0808 支持功能码 3、4 和 16。",
        "FT-RS-0808 支持哪些功能码？",
        assessment["chunks"],
        [evidence_id],
    )

    assert result["passed"] is False
    assert any("功能码 3" in item for item in result["violations"])
    assert any("功能码 4" in item for item in result["violations"])
    assert any("功能码 16" in item for item in result["violations"])


class FakeAgent:
    def __init__(self, tool_payload, structured):
        self.tool_payload = tool_payload
        self.structured = structured

    def invoke(self, request):
        source_messages = request["messages"]
        return {
            "messages": [
                *source_messages,
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "search_knowledge_base",
                        "args": {"query": self.tool_payload["query"]},
                        "id": "call-1",
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(
                    name="search_knowledge_base",
                    tool_call_id="call-1",
                    content=json.dumps(self.tool_payload, ensure_ascii=False),
                ),
                AIMessage(content="draft"),
            ],
            "structured_response": self.structured,
        }


def test_run_agent_overrides_hallucinated_answer_when_evidence_not_found(monkeypatch):
    payload = {
        "query": "FT-RS-9999 支持哪些功能码？",
        "evidence_status": "not_found",
        "candidate_count": 8,
        "verified_count": 0,
        "rejected_count": 8,
        "chunks": [],
        "total": 64,
    }
    monkeypatch.setattr(
        graph,
        "get_agent",
        lambda: FakeAgent(
            payload,
            graph.AgentAnswer(
                answer="FT-RS-9999 支持 03、04 和 16 功能码。",
                used_evidence_ids=[],
            ),
        ),
    )
    monkeypatch.setattr(
        graph,
        "get_settings",
        lambda: SimpleNamespace(evidence_mode="generic", max_agent_steps=16),
    )

    result = graph.run_agent([], "FT-RS-9999 支持哪些功能码？")

    assert result["evidence"]["status"] == "not_found"
    assert result["answer"].startswith("当前知识库没有检索到")
    assert "03、04" not in result["answer"]
    assert result["citations"] == []
    assert result["retrieval_hits"] == []


def test_run_agent_does_not_override_not_found_answer_when_evidence_mode_is_off(monkeypatch):
    payload = {
        "query": "FT-RS-9999 支持哪些功能码？",
        "evidence_status": "not_found",
        "candidate_count": 8,
        "verified_count": 0,
        "rejected_count": 8,
        "chunks": [],
        "total": 64,
    }
    draft = "FT-RS-9999 支持 03、04 和 16 功能码。"
    monkeypatch.setattr(
        graph,
        "get_agent",
        lambda: FakeAgent(
            payload,
            graph.AgentAnswer(answer=draft, used_evidence_ids=[]),
        ),
    )
    monkeypatch.setattr(
        graph,
        "get_settings",
        lambda: SimpleNamespace(evidence_mode="off", max_agent_steps=16),
    )

    result = graph.run_agent([], "FT-RS-9999 支持哪些功能码？")

    assert result["answer"] == draft
    assert result["evidence"]["status"] == "not_found"
    assert result["evidence"]["non_blocking"] is True


def test_run_agent_releases_supported_answer_with_verified_citation(monkeypatch):
    query = "FT-RS-0808 支持哪些功能码？"
    monkeypatch.setattr(
        graph,
        "get_settings",
        lambda: SimpleNamespace(evidence_mode="generic", max_agent_steps=16),
    )
    assessment = assess_evidence(
        query,
        [chunk(
            "FT-RS-0808 支持功能码 0x01、0x02、0x05 和 0x0F。",
            "FT-RS-0808说明书.pdf",
        )],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]
    payload = {
        "query": query,
        "evidence_status": "supported",
        "candidate_count": 1,
        "verified_count": 1,
        "rejected_count": 0,
        "chunks": assessment["chunks"],
        "total": 1,
    }
    answer = "FT-RS-0808 支持功能码 0x01、0x02、0x05 和 0x0F。"
    monkeypatch.setattr(
        graph,
        "get_agent",
        lambda: FakeAgent(
            payload,
            graph.AgentAnswer(answer=answer, used_evidence_ids=[evidence_id]),
        ),
    )

    result = graph.run_agent([], query)

    assert result["evidence"]["status"] == "supported"
    assert result["answer"] == answer
    assert result["citations"][0]["document_name"] == "FT-RS-0808说明书.pdf"
    assert result["retrieval_hits"][0]["evidence_id"] == evidence_id


def test_run_agent_binds_plain_answer_to_verified_evidence(monkeypatch):
    query = "FT-RS-0808 支持哪些功能码？"
    assessment = assess_evidence(
        query,
        [chunk(
            "FT-RS-0808 支持功能码 0x01、0x02、0x05 和 0x0F。",
            "FT-RS-0808说明书.pdf",
        )],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]
    payload = {
        "query": query,
        "evidence_status": "supported",
        "candidate_count": 1,
        "verified_count": 1,
        "rejected_count": 0,
        "chunks": assessment["chunks"],
        "total": 1,
    }

    class PlainAnswerAgent:
        def invoke(self, request, config=None):
            return {
                "messages": [
                    *request["messages"],
                    AIMessage(
                        content="",
                        tool_calls=[{
                            "name": "search_knowledge_base",
                            "args": {"query": query},
                            "id": "call-plain",
                            "type": "tool_call",
                        }],
                    ),
                    ToolMessage(
                        name="search_knowledge_base",
                        tool_call_id="call-plain",
                        content=json.dumps(payload, ensure_ascii=False),
                    ),
                    AIMessage(content="FT-RS-0808 支持功能码 0x01、0x02、0x05 和 0x0F。"),
                ],
            }

    monkeypatch.setattr(graph, "get_agent", lambda **kwargs: PlainAnswerAgent())
    monkeypatch.setattr(
        graph,
        "get_settings",
        lambda: SimpleNamespace(
            evidence_mode="generic",
            max_agent_steps=30,
            max_ragflow_calls=1,
        ),
    )

    result = graph.run_agent([], query)

    assert result["evidence"]["status"] == "supported"
    assert result["evidence"]["used_evidence_ids"] == [evidence_id]
    assert result["answer"].startswith("FT-RS-0808 支持功能码")


def test_run_agent_blocks_unsupported_fact_even_with_a_real_evidence_id(monkeypatch):
    query = "FT-RS-0808 支持哪些功能码？"
    assessment = assess_evidence(
        query,
        [chunk(
            "FT-RS-0808 支持功能码 0x01、0x02、0x05 和 0x0F。",
            "FT-RS-0808说明书.pdf",
        )],
    )
    evidence_id = assessment["chunks"][0]["evidence_id"]
    payload = {
        "query": query,
        "evidence_status": "supported",
        "candidate_count": 1,
        "verified_count": 1,
        "rejected_count": 0,
        "chunks": assessment["chunks"],
        "total": 1,
    }
    monkeypatch.setattr(
        graph,
        "get_agent",
        lambda: FakeAgent(
            payload,
            graph.AgentAnswer(
                answer="FT-RS-0808 还支持功能码 0x10。",
                used_evidence_ids=[evidence_id],
            ),
        ),
    )
    monkeypatch.setattr(
        graph,
        "get_settings",
        lambda: SimpleNamespace(evidence_mode="industrial_strict"),
    )

    result = graph.run_agent([], query)

    assert result["evidence"]["status"] == "answer_blocked"
    assert "0x10" not in result["answer"]
    assert any("0x10" in item for item in result["evidence"]["violations"])


def test_run_agent_blocks_factual_answer_when_model_skips_retrieval(monkeypatch):
    class NoToolAgent:
        def invoke(self, request):
            return {
                "messages": [*request["messages"], AIMessage(content="它支持 16 路输入。")],
                "structured_response": graph.AgentAnswer(answer="它支持 16 路输入。"),
            }

    monkeypatch.setattr(graph, "get_agent", lambda: NoToolAgent())

    result = graph.run_agent([], "FT-RS-0808 有多少路输入？")

    assert result["evidence"]["status"] == "not_retrieved"
    assert "16 路" not in result["answer"]
    assert result["trace"]["used_ragflow"] is False


def test_multi_entity_prefetch_retrieves_each_requested_product(monkeypatch):
    calls = []

    class FakeSearchTool:
        def invoke(self, payload):
            calls.append(payload["query"])
            return json.dumps({
                "query": payload["query"],
                "evidence_status": "supported",
                "candidate_count": 1,
                "verified_count": 1,
                "rejected_count": 0,
                "chunks": [],
                "total": 1,
            }, ensure_ascii=False)

    monkeypatch.setattr(graph, "search_knowledge_base", FakeSearchTool())
    messages = graph._prefetch_multi_entity_messages(
        "比较 FT-RS-0808、M1501 和 M1516 的数字量输入输出能力。"
    )

    assert len(messages) == 4
    assert calls == [
        "FT-RS-0808 数字量输入输出能力",
        "M1501 数字量输入输出能力",
        "M1516 数字量输入输出能力",
    ]


def test_multi_entity_prefetch_keeps_acquisition_channel_queries_neutral(monkeypatch):
    calls = []

    class FakeSearchTool:
        def invoke(self, payload):
            calls.append(payload["query"])
            return json.dumps({"chunks": []}, ensure_ascii=False)

    monkeypatch.setattr(graph, "search_knowledge_base", FakeSearchTool())
    query = "XP2001、XP2006、XP2018 分别是几通道采集模块？请返回每个型号的对应手册。"

    assert extract_aspects(query) == ["channels_generic"]
    graph._prefetch_multi_entity_messages(query)

    assert calls == [
        "XP2001 通道数 采集模块 对应手册",
        "XP2006 通道数 采集模块 对应手册",
        "XP2018 通道数 采集模块 对应手册",
    ]


def test_document_presence_fallback_uses_only_verified_matching_filenames():
    answer = graph._build_document_presence_answer(
        "知识库中是否有 XP2001 的说明书？",
        [
            chunk("XP2001 单路数据采集模块。", "XP2001单路数据采集模块.docx", document_id="1"),
            chunk("XP2001 单路数据采集模块。", "XP2001单路数据采集模块.pdf", document_id="2"),
            chunk("XP2006 信号采集模块。", "XP2006信号采集模块（6通道）.pdf", document_id="3"),
        ],
    )

    assert answer == (
        "有。当前知识库已收录与 XP2001 匹配的资料：\n"
        "- `XP2001单路数据采集模块.pdf`\n"
        "- `XP2001单路数据采集模块.docx`"
    )


def test_document_presence_fallback_does_not_answer_parameter_question():
    assert graph._build_document_presence_answer(
        "XP2001 有多少路采集通道？",
        [chunk("XP2001 单路数据采集模块。", "XP2001单路数据采集模块.pdf")],
    ) is None


def test_run_agent_keeps_direct_non_factual_answer_without_ragflow(monkeypatch):
    class DirectAgent:
        def invoke(self, request):
            return {
                "messages": [*request["messages"], AIMessage(content="你好！")],
                "structured_response": graph.AgentAnswer(answer="你好！我可以查询企业知识库。"),
            }

    monkeypatch.setattr(graph, "get_agent", lambda: DirectAgent())

    result = graph.run_agent([], "你好，请介绍一下你能做什么。")

    assert result["answer"] == "你好！我可以查询企业知识库。"
    assert result["evidence"]["requires_evidence"] is False


def test_run_agent_returns_clean_quota_error_instead_of_raising(monkeypatch):
    class QuotaAgent:
        def invoke(self, request):
            raise RuntimeError(
                "403 AllocationQuota.FreeTierOnly: Free quota exhausted."
            )

    monkeypatch.setattr(graph, "get_agent", lambda: QuotaAgent())

    result = graph.run_agent([], "FT-RS-0808 支持哪些功能码？")

    assert result["trace"]["route"] == "error"
    assert result["evidence"]["status"] == "agent_error"
    assert "额度" in result["answer"]
    assert "FT-RS-0808" not in result["answer"]
