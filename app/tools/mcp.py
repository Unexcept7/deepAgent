"""MCP 工具目录和治理元数据。

MCP Server 的实际连接可由企业网关实现；这里先定义统一的工具目录，
让前端和 Agent 可以展示作用、风险和权限，而不会把任意远程命令暴露
给模型。
"""

from dataclasses import asdict, dataclass

from app.policies.risk import RiskLevel


@dataclass(frozen=True)
class MCPToolSpec:
    name: str
    description: str
    risk: RiskLevel
    read_only: bool
    required_permission: str


MCP_TOOL_CATALOG = [
    MCPToolSpec("get_device_status", "查询设备当前状态和关键参数", RiskLevel.READ_ONLY, True, "device:read"),
    MCPToolSpec("get_alarm_history", "查询设备历史报警", RiskLevel.READ_ONLY, True, "alarm:read"),
    MCPToolSpec("draft_work_order", "生成维修工单草稿", RiskLevel.DRAFT, True, "work_order:draft"),
    MCPToolSpec("submit_work_order", "提交正式维修工单", RiskLevel.CONFIRM, False, "work_order:create"),
    MCPToolSpec("set_device_parameter", "修改设备参数（必须审批）", RiskLevel.DEVICE_CONTROL, False, "device:control"),
]


def tool_catalog() -> list[dict]:
    return [{**asdict(item), "risk": item.risk.name} for item in MCP_TOOL_CATALOG]
