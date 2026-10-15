"""Fact-bound answer composition for the requirement pipeline.

The requirement judge is the owner of factual values.  The answer model may
choose order, wording around a fact, and whether to ask a follow-up question,
but it can only place a value into the published answer by referring to a
``fact_id`` from the server-created ledger.  This keeps the backend contract
small: it validates identity and binding, rather than attempting to
re-interpret every natural-language sentence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.agents.claims import (
    _compact_error,
    _invoke_json,
    _payload_model_calls,
    _payload_token_usage,
)
from app.schemas.answer import (
    AnswerClaim,
    AnswerDraft,
    AnswerFact,
    AnswerPlan,
    AnswerPlanSegment,
)
from app.schemas.evidence import EvidenceItem


_ANSWER_COMPOSER_PROMPT = """你是工业知识库的最终答案组装器。

请根据用户问题、已完成证据裁判的结果和服务器生成的事实账本，生成一个自然、直接、准确的中文回答。

只输出 JSON 对象：
{
  "status":"supported|partial|ambiguous|conflict|not_found",
  "answer_plan":{
    "segments":[
      {"type":"text","text":"连接语或限定说明"},
      {"type":"fact","fact_id":"F:R1","prefix":"对象的属性为","suffix":"。"}
    ],
    "used_fact_ids":["F:R1"]
  },
  "uncertainty":"没有时填空字符串",
  "follow_up":"没有时填空字符串"
}

规则：
1. 事实账本中的 `value` 是唯一允许发布的事实值；不得在 `text`、`prefix` 或 `suffix` 中重新编造、改写或计算技术数值。
2. 每个 `fact` 段只能引用账本中状态为 `supported` 且绑定了 Evidence ID 的事实；`fact_id` 必须原样复制，不能编造。
3. `text` 段只用于连接语、范围说明、冲突/未找到说明，不要在其中写入未经事实段绑定的型号、尺寸、参数、日期或其他技术事实。
4. supported 事实可以直接组织回答；unsupported 或 conflict 不得生成 fact 段，应明确说明当前资料无法安全确认或存在冲突。
5. 可以保留用户问题中的对象简称作为连接语，但不能把不同对象、系列和子型号悄悄合并。
6. `used_fact_ids` 必须是 answer_plan 中实际使用的 fact ID 的并集。
7. 不要输出旧的 `answer` 字段，不要输出 Markdown 代码块、解释或 JSON 之外的文字。
"""


@dataclass
class AnswerComposition:
    draft: AnswerDraft | None
    warning: str | None = None
    model_calls: int = 0
    token_usage: dict[str, int] | None = None


def _allowed_evidence_ids(chunks: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("evidence_id"))
        for item in chunks
        if str(item.get("evidence_id") or "").strip()
    }


def _valid_ids(values: Any, allowed: set[str]) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        str(item).strip()
        for item in values
        if str(item).strip() in allowed
    ))


def _invalid_ids(values: Any, allowed: set[str]) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        str(item).strip()
        for item in values
        if str(item).strip() and str(item).strip() not in allowed
    ))


def _normalize_status(value: Any) -> str:
    status = str(value or "unsupported").strip().casefold()
    return status if status in {"supported", "unsupported", "conflict"} else "unsupported"


def _fact_id_for(requirement_id: str, index: int, used: set[str]) -> str:
    base = f"F:{requirement_id or f'R{index}'}"
    fact_id = base
    suffix = 2
    while fact_id in used:
        fact_id = f"{base}:{suffix}"
        suffix += 1
    return fact_id


def build_fact_ledger(
    results: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> list[AnswerFact]:
    """Convert judged requirement rows into stable, source-bound facts."""
    allowed_ids = _allowed_evidence_ids(chunks)
    facts: list[AnswerFact] = []
    used_fact_ids: set[str] = set()
    for index, item in enumerate(results, start=1):
        requirement_id = str(item.get("requirement_id") or f"R{index}").strip()
        evidence_ids = _valid_ids(item.get("evidence_ids"), allowed_ids)
        status = _normalize_status(item.get("status"))
        value = str(item.get("value") or "").strip()
        if status == "supported" and (not value or not evidence_ids):
            status = "unsupported"
        source_names = list(dict.fromkeys(
            str(source.get("document_name") or "").strip()
            for source in item.get("sources") or []
            if isinstance(source, dict) and str(source.get("document_name") or "").strip()
        ))
        facts.append(AnswerFact(
            fact_id=_fact_id_for(requirement_id, index, used_fact_ids),
            requirement_id=requirement_id,
            entity=str(item.get("subject") or "未指定对象").strip(),
            attribute=str(item.get("request") or "未指定事项").strip(),
            value=value,
            evidence_ids=evidence_ids,
            status=status,
            reason=str(item.get("reason") or "").strip(),
            source_names=source_names,
        ))
        used_fact_ids.add(facts[-1].fact_id)
    return facts


def _result_context(
    results: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "sequence": index + 1,
            "requirement_id": item.get("requirement_id"),
            "subject": item.get("subject"),
            "request": item.get("request"),
            "output_type": item.get("output_type"),
            "value": item.get("value"),
            "status": item.get("status"),
            "reason": item.get("reason"),
            "evidence_ids": list(item.get("evidence_ids") or []),
            "sources": [
                {
                    "document_name": source.get("document_name"),
                    "page": source.get("page"),
                }
                for source in item.get("sources") or []
                if isinstance(source, dict) and source.get("document_name")
            ],
        }
        for index, item in enumerate(results)
    ] + [{"coverage": coverage}]


def _fact_context(facts: list[AnswerFact]) -> list[dict[str, Any]]:
    return [
        {
            "fact_id": fact.fact_id,
            "requirement_id": fact.requirement_id,
            "entity": fact.entity,
            "attribute": fact.attribute,
            "value": fact.value,
            "status": fact.status,
            "evidence_ids": list(fact.evidence_ids),
            "reason": fact.reason,
            "source_names": list(fact.source_names),
        }
        for fact in facts
    ]


def _evidence_context(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    total_chars = 0
    for item in chunks:
        try:
            evidence_item = EvidenceItem.model_validate({
                **item,
                "source_type": item.get("source_type") or "document",
                "document_name": item.get("document_name") or "未知文档",
            })
        except ValidationError:
            continue
        evidence_id = evidence_item.evidence_id.strip()
        content = evidence_item.content.strip()
        if not evidence_id or not content:
            continue
        remaining = 24000 - total_chars
        if remaining <= 0:
            break
        excerpt = content[: min(4000, remaining)]
        evidence.append({
            "evidence_id": evidence_id,
            "document_name": evidence_item.document_name,
            "document_id": evidence_item.document_id,
            "dataset_id": evidence_item.dataset_id,
            "page": evidence_item.page,
            "source_type": evidence_item.source_type,
            "content": excerpt,
        })
        total_chars += len(excerpt)
    return evidence


def _compose_input(
    query: str,
    results: list[dict[str, Any]],
    coverage: dict[str, Any],
    chunks: list[dict[str, Any]],
    facts: list[AnswerFact],
    *,
    previous_draft: AnswerDraft | None = None,
    violations: list[str] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "original_query": query,
        "fact_ledger": _fact_context(facts),
        "judged_results": _result_context(results, coverage),
        "evidence": _evidence_context(chunks),
    }
    if previous_draft is not None:
        payload["previous_draft"] = previous_draft.model_dump(mode="json")
        payload["binding_violations"] = list(violations or [])
        payload["repair_instruction"] = (
            "只修复 answer_plan 的 fact_id、结构和使用列表；事实值必须继续从 fact_ledger 选择，"
            "不要引入新的事实，不要把 unsupported 或 conflict 改成 supported。"
        )
    return json.dumps(payload, ensure_ascii=False)


def _claims_from_results(
    results: list[dict[str, Any]],
    facts: list[AnswerFact],
) -> list[AnswerClaim]:
    facts_by_requirement = {fact.requirement_id: fact for fact in facts}
    claims: list[AnswerClaim] = []
    for index, item in enumerate(results, start=1):
        requirement_id = str(item.get("requirement_id") or "").strip()
        fact = facts_by_requirement.get(requirement_id)
        status = fact.status if fact else _normalize_status(item.get("status"))
        subject = str(item.get("subject") or "未指定对象").strip()
        attribute = str(item.get("request") or "未指定事项").strip()
        value = (fact.value if fact and fact.value else str(item.get("value") or "").strip())
        if status == "supported" and value:
            text = f"{subject}的{attribute}为{value}。"
        elif status == "conflict":
            text = f"{subject}的{attribute}存在资料冲突，当前无法安全确认。"
        else:
            text = f"当前资料无法确认{subject}的{attribute}。"
        claims.append(AnswerClaim(
            claim_id=f"C{index}",
            fact_id=fact.fact_id if fact else None,
            text=text,
            entity=subject,
            attribute=attribute,
            value=value,
            evidence_ids=list(fact.evidence_ids if fact else item.get("evidence_ids") or []),
            status=status,
            reason=str(item.get("reason") or "").strip(),
        ))
    return claims


def _fallback_plan(
    facts: list[AnswerFact],
    coverage: dict[str, Any],
) -> AnswerPlan:
    segments: list[AnswerPlanSegment] = [
        AnswerPlanSegment(type="text", text="根据已验证资料：")
    ]
    used_fact_ids: list[str] = []
    for fact in facts:
        if fact.status == "supported" and fact.value and fact.evidence_ids:
            segments.append(AnswerPlanSegment(
                type="fact",
                fact_id=fact.fact_id,
                prefix=f"{fact.entity}的{fact.attribute}为",
                suffix="。",
            ))
            used_fact_ids.append(fact.fact_id)
        elif fact.status == "conflict":
            segments.append(AnswerPlanSegment(
                type="text",
                text=f"{fact.entity}的{fact.attribute}存在资料冲突，当前无法安全确认。",
            ))
        else:
            segments.append(AnswerPlanSegment(
                type="text",
                text=f"当前资料无法确认{fact.entity}的{fact.attribute}。",
            ))
    if not facts:
        segments.append(AnswerPlanSegment(
            type="text",
            text="当前没有形成可发布的证据结果。",
        ))
    elif not coverage.get("complete", True):
        segments.append(AnswerPlanSegment(
            type="text",
            text="以上仅包含当前资料能够支持的部分，未确认内容不会被推断。",
        ))
    return AnswerPlan(segments=segments, used_fact_ids=used_fact_ids)


def _render_plan(
    plan: AnswerPlan,
    facts: list[AnswerFact],
) -> tuple[str, list[str], list[str], int]:
    facts_by_id = {fact.fact_id: fact for fact in facts}
    rendered: list[str] = []
    rendered_fact_ids: list[str] = []
    invalid_fact_ids = list(plan.invalid_fact_ids)
    dropped_segments = int(plan.dropped_segments or 0)
    for segment in plan.segments[:32]:
        if segment.type == "text":
            text = segment.text.strip()
            if text:
                rendered.append(text)
            else:
                dropped_segments += 1
            continue
        fact = facts_by_id.get(segment.fact_id.strip())
        if (
            fact is None
            or fact.status != "supported"
            or not fact.value
            or not fact.evidence_ids
        ):
            if segment.fact_id.strip() and segment.fact_id.strip() not in invalid_fact_ids:
                invalid_fact_ids.append(segment.fact_id.strip())
            dropped_segments += 1
            continue
        rendered.append(f"{segment.prefix}{fact.value}{segment.suffix}")
        rendered_fact_ids.append(fact.fact_id)
    rendered_fact_ids = list(dict.fromkeys(rendered_fact_ids))
    invalid_fact_ids = list(dict.fromkeys(item for item in invalid_fact_ids if item))
    return "\n".join(rendered).strip(), rendered_fact_ids, invalid_fact_ids, dropped_segments


def _status_from_results(
    results: list[dict[str, Any]],
    coverage: dict[str, Any],
    rendered_fact_ids: list[str],
) -> str:
    statuses = {_normalize_status(item.get("status")) for item in results}
    if "conflict" in statuses and not rendered_fact_ids:
        return "conflict"
    if rendered_fact_ids:
        return "supported" if coverage.get("complete", True) else "partial"
    if "supported" in statuses:
        return "partial"
    if "conflict" in statuses:
        return "conflict"
    return "not_found"


def _parse_plan(
    raw_plan: Any,
    facts: list[AnswerFact],
    coverage: dict[str, Any],
) -> tuple[AnswerPlan, str, list[str]]:
    facts_by_id = {fact.fact_id: fact for fact in facts}
    if not isinstance(raw_plan, dict):
        fallback = _fallback_plan(facts, coverage)
        return fallback, "", []
    raw_segments = raw_plan.get("segments")
    segments: list[AnswerPlanSegment] = []
    invalid_fact_ids: list[str] = []
    dropped_segments = 0
    if isinstance(raw_segments, list):
        for raw_segment in raw_segments[:32]:
            if not isinstance(raw_segment, dict):
                dropped_segments += 1
                continue
            segment_type = str(raw_segment.get("type") or "").strip().casefold()
            if segment_type == "text":
                text = str(raw_segment.get("text") or "").strip()
                if text:
                    segments.append(AnswerPlanSegment(type="text", text=text))
                else:
                    dropped_segments += 1
                continue
            if segment_type == "fact":
                fact_id = str(raw_segment.get("fact_id") or "").strip()
                if not fact_id:
                    dropped_segments += 1
                    continue
                segments.append(AnswerPlanSegment(
                    type="fact",
                    fact_id=fact_id,
                    prefix=str(raw_segment.get("prefix") or ""),
                    suffix=str(raw_segment.get("suffix") or ""),
                ))
                if fact_id not in facts_by_id:
                    invalid_fact_ids.append(fact_id)
                continue
            dropped_segments += 1
    else:
        dropped_segments += 1
    supplied_used = raw_plan.get("used_fact_ids")
    if isinstance(supplied_used, list):
        for value in supplied_used:
            fact_id = str(value).strip()
            if fact_id and fact_id not in facts_by_id:
                invalid_fact_ids.append(fact_id)
    plan = AnswerPlan(
        segments=segments,
        used_fact_ids=[],
        invalid_fact_ids=list(dict.fromkeys(invalid_fact_ids)),
        dropped_segments=dropped_segments,
    )
    _, rendered_fact_ids, invalid_from_render, dropped = _render_plan(plan, facts)
    invalid_fact_ids = list(dict.fromkeys([
        *plan.invalid_fact_ids,
        *invalid_from_render,
    ]))
    plan = plan.model_copy(update={
        "used_fact_ids": rendered_fact_ids,
        "invalid_fact_ids": invalid_fact_ids,
        "dropped_segments": dropped,
    })
    return plan, "rendered", invalid_fact_ids


def _parse_draft(
    payload: dict[str, Any],
    *,
    facts: list[AnswerFact],
    results: list[dict[str, Any]],
    coverage: dict[str, Any],
    allowed_ids: set[str],
    repaired: bool = False,
) -> AnswerDraft:
    plan, plan_kind, invalid_fact_ids = _parse_plan(
        payload.get("answer_plan"),
        facts,
        coverage,
    )
    rendered_answer, rendered_fact_ids, invalid_from_render, dropped_segments = _render_plan(
        plan,
        facts,
    )
    invalid_fact_ids = list(dict.fromkeys([
        *invalid_fact_ids,
        *invalid_from_render,
    ]))
    if not rendered_answer:
        fallback_plan = _fallback_plan(facts, coverage)
        fallback_answer, fallback_fact_ids, _, fallback_dropped = _render_plan(
            fallback_plan,
            facts,
        )
        plan = fallback_plan.model_copy(update={
            "invalid_fact_ids": invalid_fact_ids,
            "dropped_segments": dropped_segments + fallback_dropped,
            "used_fact_ids": fallback_fact_ids,
        })
        rendered_answer = fallback_answer or "当前没有形成可发布的证据结果。"
        rendered_fact_ids = fallback_fact_ids
        repaired = True
    else:
        plan = plan.model_copy(update={
            "invalid_fact_ids": invalid_fact_ids,
            "dropped_segments": dropped_segments,
            "used_fact_ids": rendered_fact_ids,
        })

    raw_used_evidence_ids = payload.get("used_evidence_ids")
    invalid_evidence_ids = _invalid_ids(raw_used_evidence_ids, allowed_ids)
    used_evidence_ids = list(dict.fromkeys(
        evidence_id
        for fact in facts
        if fact.fact_id in rendered_fact_ids
        for evidence_id in fact.evidence_ids
        if evidence_id in allowed_ids
    ))
    if raw_used_evidence_ids is not None:
        # Old model profiles sometimes still send this field.  It is accepted
        # only as a diagnostic; it never adds evidence to the published plan.
        for evidence_id in _valid_ids(raw_used_evidence_ids, allowed_ids):
            if evidence_id not in used_evidence_ids:
                invalid_evidence_ids.append(evidence_id)

    claims = _claims_from_results(results, facts)
    status = _status_from_results(results, coverage, rendered_fact_ids)
    if plan_kind == "" and facts:
        repaired = True
    return AnswerDraft(
        status=status,
        answer=rendered_answer,
        answer_plan=plan,
        fact_ledger=facts,
        claims=claims,
        used_evidence_ids=used_evidence_ids,
        invalid_evidence_ids=list(dict.fromkeys(invalid_evidence_ids)),
        uncertainty=str(payload.get("uncertainty") or "").strip() or None,
        follow_up=str(payload.get("follow_up") or "").strip() or None,
        repaired=repaired,
    )


def compose_answer(
    model: Any,
    query: str,
    results: list[dict[str, Any]],
    coverage: dict[str, Any],
    chunks: list[dict[str, Any]],
    *,
    attachments: list[dict[str, Any]] | None = None,
    previous_draft: AnswerDraft | None = None,
    violations: list[str] | None = None,
) -> AnswerComposition:
    """Ask the model for a fact-ID plan and render it from the server ledger."""
    facts = build_fact_ledger(results, chunks)
    allowed_ids = _allowed_evidence_ids(chunks)
    try:
        payload = _invoke_json(
            model,
            _ANSWER_COMPOSER_PROMPT,
            _compose_input(
                query,
                results,
                coverage,
                chunks,
                facts,
                previous_draft=previous_draft,
                violations=violations,
            ),
            trace_name="答案组装与事实绑定修复" if previous_draft else "答案组装",
            attachments=attachments,
        )
        draft = _parse_draft(
            payload,
            facts=facts,
            results=results,
            coverage=coverage,
            allowed_ids=allowed_ids,
            repaired=previous_draft is not None,
        )
        return AnswerComposition(
            draft=draft,
            model_calls=_payload_model_calls(payload),
            token_usage=_payload_token_usage(payload),
        )
    except (ValidationError, TypeError, ValueError, KeyError) as exc:
        return AnswerComposition(
            draft=None,
            warning=f"答案组装失败：{_compact_error(exc)}",
            model_calls=1,
            token_usage={},
        )
    except Exception as exc:  # provider/network failures are handled by fallback
        return AnswerComposition(
            draft=None,
            warning=f"答案组装失败：{_compact_error(exc)}",
            model_calls=1,
            token_usage={},
        )


def validate_answer_plan(
    draft: AnswerDraft,
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate only fact-ID/source binding and plan structure.

    This deliberately does not compare arbitrary prose with source text.  The
    factual value in every ``fact`` segment already comes from the server-side
    ledger, so the backend only needs to reject unknown IDs, unsupported facts,
    and missing source bindings.
    """
    allowed_ids = _allowed_evidence_ids(chunks)
    violations: list[str] = []
    plan = draft.answer_plan
    fact_by_id = {fact.fact_id: fact for fact in draft.fact_ledger}
    if plan is None:
        violations.append("答案没有返回事实引用计划")
    if draft.invalid_evidence_ids:
        violations.append(
            "答案草稿引用了本轮不存在的证据 ID："
            + "、".join(draft.invalid_evidence_ids)
        )
    if not draft.answer.strip():
        violations.append("答案草稿为空")
    rendered_fact_ids: list[str] = []
    invalid_fact_ids: list[str] = []
    if plan is not None:
        invalid_fact_ids.extend(plan.invalid_fact_ids)
        for segment in plan.segments:
            if segment.type != "fact":
                continue
            fact_id = segment.fact_id.strip()
            fact = fact_by_id.get(fact_id)
            if fact is None:
                invalid_fact_ids.append(fact_id or "<空 fact_id>")
                continue
            if fact.status != "supported":
                invalid_fact_ids.append(fact_id)
                violations.append(f"事实 {fact_id} 的裁判状态不是 supported")
                continue
            if not fact.evidence_ids:
                invalid_fact_ids.append(fact_id)
                violations.append(f"事实 {fact_id} 没有绑定证据 ID")
                continue
            unknown_evidence_ids = sorted(set(fact.evidence_ids) - allowed_ids)
            if unknown_evidence_ids:
                violations.append(
                    f"事实 {fact_id} 的证据不在本轮候选集中："
                    + "、".join(unknown_evidence_ids)
                )
                continue
            rendered_fact_ids.append(fact_id)
        expected_used = list(dict.fromkeys(rendered_fact_ids))
        if list(dict.fromkeys(plan.used_fact_ids)) != expected_used:
            violations.append("事实计划的 used_fact_ids 与实际 fact 段不一致")
        if plan.dropped_segments:
            violations.append(f"事实计划丢弃了 {plan.dropped_segments} 个无效段")
    invalid_fact_ids = list(dict.fromkeys(item for item in invalid_fact_ids if item))
    if invalid_fact_ids:
        violations.append("事实计划引用了不存在或不可发布的 fact_id：" + "、".join(invalid_fact_ids))
    supported_fact_count = sum(
        1
        for fact in draft.fact_ledger
        if fact.status == "supported" and fact.value and fact.evidence_ids
    )
    if supported_fact_count and not rendered_fact_ids:
        violations.append("事实账本存在可发布事实，但答案没有引用任何事实段")
    used_evidence_ids = list(dict.fromkeys(
        evidence_id
        for fact in draft.fact_ledger
        if fact.fact_id in rendered_fact_ids
        for evidence_id in fact.evidence_ids
        if evidence_id in allowed_ids
    ))
    return {
        "passed": not violations,
        "violations": list(dict.fromkeys(violations)),
        "available_evidence_ids": sorted(allowed_ids),
        "used_evidence_ids": used_evidence_ids,
        "fact_count": len(draft.fact_ledger),
        "bound_fact_count": supported_fact_count,
        "rendered_fact_count": len(list(dict.fromkeys(rendered_fact_ids))),
        "rendered_fact_ids": list(dict.fromkeys(rendered_fact_ids)),
        "invalid_fact_refs": invalid_fact_ids,
        "dropped_segments": int(plan.dropped_segments) if plan is not None else 0,
    }


def fallback_answer(
    results: list[dict[str, Any]],
    coverage: dict[str, Any],
    chunks: list[dict[str, Any]] | None = None,
) -> AnswerDraft:
    """Render a safe answer directly from judged results when the model fails."""
    facts = build_fact_ledger(results, chunks or [])
    plan = _fallback_plan(facts, coverage)
    answer, rendered_fact_ids, invalid_fact_ids, dropped_segments = _render_plan(plan, facts)
    plan = plan.model_copy(update={
        "used_fact_ids": rendered_fact_ids,
        "invalid_fact_ids": invalid_fact_ids,
        "dropped_segments": dropped_segments,
    })
    status = _status_from_results(results, coverage, rendered_fact_ids)
    used_evidence_ids = list(dict.fromkeys(
        evidence_id
        for fact in facts
        if fact.fact_id in rendered_fact_ids
        for evidence_id in fact.evidence_ids
    ))
    return AnswerDraft(
        status=status,
        answer=answer or "当前没有形成可发布的证据结果。",
        answer_plan=plan,
        fact_ledger=facts,
        claims=_claims_from_results(results, facts),
        used_evidence_ids=used_evidence_ids,
        repaired=True,
    )
