import json

from app.policies.risk import RiskLevel, decide_action
from app.tools.business import draft_work_order, submit_work_order


def test_risk_policy_requires_confirmation_and_approval():
    assert decide_action(RiskLevel.CONFIRM).requires_confirmation
    assert not decide_action(RiskLevel.CONFIRM).allowed
    assert decide_action(RiskLevel.CONFIRM, confirmed=True).allowed
    assert decide_action(RiskLevel.DEVICE_CONTROL, approved=False, user_can_approve=False).requires_approval


def test_draft_work_order_never_writes_to_backend():
    payload = json.loads(draft_work_order.invoke({
        "device_id": "B47-001",
        "problem": "E-203",
        "priority": "urgent",
    }))
    assert payload["status"] == "draft"
    assert payload["risk"] == "draft"


def test_submit_work_order_stops_before_backend_without_confirmation():
    payload = json.loads(submit_work_order.invoke({
        "device_id": "B47-001",
        "problem": "E-203",
    }))
    assert payload["status"] == "awaiting_confirmation"
    assert payload["requires_confirmation"] is True
