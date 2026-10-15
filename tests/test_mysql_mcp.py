from services.mysql_mcp.mcp import MCPService


class FakeRepository:
    def search(self, query, top_k, filters):
        return [{"id": "M1503", "content": f"matched: {query}", "filters": filters}][:top_k]

    def get_device(self, device_id):
        return {"device_id": device_id, "status": "online"}

    def list_alarms(self, device_id, days, limit):
        return [{"device_id": device_id, "days": days}][:limit]


def test_initialize_and_tools_list():
    service = MCPService(FakeRepository())

    initialize = service.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-11-25"},
    })
    listed = service.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert initialize["result"]["protocolVersion"] == "2025-11-25"
    assert {tool["name"] for tool in listed["result"]["tools"]} == {
        "equipment_search",
        "get_device_status",
        "get_alarm_history",
    }


def test_tool_call_returns_structured_content():
    service = MCPService(FakeRepository())
    response = service.handle({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "equipment_search",
            "arguments": {"query": "M1503", "top_k": 1, "filters": {"status": "online"}},
        },
    })

    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["items"][0]["id"] == "M1503"


def test_initialized_notification_has_no_response():
    service = MCPService(FakeRepository())
    assert service.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
