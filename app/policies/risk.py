"""业务工具风险分级和最小执行策略。"""

from dataclasses import dataclass
from enum import IntEnum


class RiskLevel(IntEnum):
    READ_ONLY = 0
    DRAFT = 2
    CONFIRM = 3
    APPROVAL = 4
    DEVICE_CONTROL = 5


@dataclass(frozen=True)
class ActionDecision:
    allowed: bool
    requires_confirmation: bool
    requires_approval: bool
    reason: str


def decide_action(
    risk: RiskLevel,
    *,
    confirmed: bool = False,
    approved: bool = False,
    user_can_approve: bool = False,
) -> ActionDecision:
    """只根据明确的外部状态作决定，不让模型自行授予权限。"""
    if risk <= RiskLevel.READ_ONLY:
        return ActionDecision(True, False, False, "只读操作")
    if risk == RiskLevel.DRAFT:
        return ActionDecision(True, False, False, "允许生成草稿，不执行写入")
    if risk == RiskLevel.CONFIRM:
        if not confirmed:
            return ActionDecision(False, True, False, "需要用户确认后执行")
        return ActionDecision(True, False, False, "用户已确认")
    if risk >= RiskLevel.DEVICE_CONTROL:
        if not approved or not user_can_approve:
            return ActionDecision(False, False, True, "高风险操作需要具备审批权限的人工审批")
        return ActionDecision(True, False, False, "审批已通过")
    if not approved:
        return ActionDecision(False, False, True, "需要人工审批")
    return ActionDecision(True, False, False, "审批已通过")
