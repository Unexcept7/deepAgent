"""制造业业务工具。

这些工具只负责调用企业侧 API 和返回可审计结果；权限、确认和审批
通过 ``risk.py`` 做显式判断。未配置 API 时返回结构化错误，不使用模型
记忆伪造设备状态或工单编号。
"""

import json
from typing import Any

import requests
from langchain_core.tools import tool

from app.config import get_settings
from app.policies.risk import RiskLevel, decide_action


class BusinessActionClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = str(settings.business_api_base_url or "").strip().rstrip("/")
        self.token = str(settings.business_api_token or "").strip()
        self.timeout = max(1, int(settings.business_api_timeout_seconds))

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            return {"error": "BUSINESS_API_BASE_URL 未配置", "configured": False}
        response = requests.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            headers=self.headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else {"data": body}


@tool
def get_device_status(device_id: str) -> str:
    """只读查询设备当前状态、关键参数和报警摘要。"""
    if not str(device_id or "").strip():
        return json.dumps({"error": "device_id 不能为空"}, ensure_ascii=False)
    try:
        result = BusinessActionClient().request("GET", f"devices/{device_id}/status", {})
    except Exception as exc:
        result = {"error": str(exc), "device_id": device_id}
    return json.dumps({"tool": "get_device_status", "risk": "read_only", **result}, ensure_ascii=False)


@tool
def get_alarm_history(device_id: str, days: int = 30) -> str:
    """只读查询设备最近报警；days 由工具层限制在 1 到 365 天。"""
    if not str(device_id or "").strip():
        return json.dumps({"error": "device_id 不能为空"}, ensure_ascii=False)
    days = max(1, min(int(days), 365))
    try:
        result = BusinessActionClient().request("GET", f"devices/{device_id}/alarms?days={days}", {})
    except Exception as exc:
        result = {"error": str(exc), "device_id": device_id, "days": days}
    return json.dumps({"tool": "get_alarm_history", "risk": "read_only", **result}, ensure_ascii=False)


@tool
def draft_work_order(
    device_id: str,
    problem: str,
    priority: str = "medium",
    suggested_action: str = "",
) -> str:
    """生成本地工单草稿，不会写入企业工单系统。"""
    if not device_id or not problem:
        return json.dumps({"error": "device_id 和 problem 不能为空"}, ensure_ascii=False)
    return json.dumps({
        "tool": "draft_work_order",
        "risk": "draft",
        "status": "draft",
        "device_id": device_id,
        "problem": problem,
        "priority": priority if priority in {"low", "medium", "high", "urgent"} else "medium",
        "suggested_action": suggested_action,
        "message": "工单草稿已生成，提交前仍需用户确认。",
    }, ensure_ascii=False)


@tool
def submit_work_order(
    device_id: str,
    problem: str,
    priority: str = "medium",
    confirmed: bool = False,
) -> str:
    """提交正式工单；必须传入用户明确确认，且仍受企业 API 权限控制。"""
    # Deprecated model-facing boolean is never proof of a human decision.
    # Formal writes are executed only by tool_center.service after claiming a
    # server-owned confirmation record bound to identity and payload.
    decision = decide_action(RiskLevel.CONFIRM, confirmed=False)
    if not decision.allowed:
        return json.dumps({
            "tool": "submit_work_order",
            "status": "awaiting_confirmation",
            "requires_confirmation": decision.requires_confirmation,
            "message": decision.reason,
        }, ensure_ascii=False)
    if not device_id or not problem:
        return json.dumps({"error": "device_id 和 problem 不能为空"}, ensure_ascii=False)
    try:
        result = BusinessActionClient().request("POST", "work-orders", {
            "device_id": device_id,
            "problem": problem,
            "priority": priority,
        })
    except Exception as exc:
        result = {"error": str(exc)}
    return json.dumps({"tool": "submit_work_order", "risk": "confirm", **result}, ensure_ascii=False)


BUSINESS_TOOLS = [
    get_device_status,
    get_alarm_history,
    draft_work_order,
    submit_work_order,
]
