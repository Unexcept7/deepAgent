import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app import main
from app.agents import graph
from app.application_store import ApplicationStore
from app.config import Settings
from app.conversation import MemoryConversationStore
from app.schemas.tool_center import ConnectionInput, InvokeRequest, ToolInput
from app.tool_center import auth, router, service, store
from app.tool_center.auth import Principal
from app.tool_center.context import ExecutionContext, execution_context
from app.tool_center.service import ToolCenterService, managed_connectors
from app.tool_center.store import ToolCenterError, ToolCenterStore
from app.workflow_store import WorkflowStore


TOKENS = {"admin": "admin-identity-token-123456789012345", "operator": "employee-identity-token-1234567890123",
          "approver": "approver-identity-token-123456789012", "viewer": "viewer-identity-token-1234567890123", "editor": "editor-identity-token-1234567890123"}


@pytest.fixture
def center(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, tool_center_auth_tokens=json.dumps([
        {"token": token, "user_id": role, "roles": [role], "workspaces": ["default"]} for role, token in TOKENS.items()
    ]), business_api_base_url="http://localhost:9910", business_api_token="very-secret-business-token")
    repository = ToolCenterStore(tmp_path / "tools.sqlite3", settings=settings, encryption_key=Fernet.generate_key())
    applications = ApplicationStore(tmp_path / "applications.json")
    workflows = WorkflowStore(tmp_path / "workflows.json")
    for module in (auth, router, service, store, main):
        monkeypatch.setattr(module, "get_tool_center_store", lambda: repository)
    for module in (auth,):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    for module in (auth, service, main):
        monkeypatch.setattr(module, "get_application_store", lambda: applications)
    for module in (service, main):
        monkeypatch.setattr(module, "get_workflow_store", lambda: workflows)
    monkeypatch.setattr(graph, "get_workflow_store", lambda: workflows)
    monkeypatch.setattr(main, "conversation_store", MemoryConversationStore())
    return SimpleNamespace(store=repository, service=ToolCenterService(repository), apps=applications, workflows=workflows,
                           settings=settings, client=TestClient(main.app))


def headers(role="admin", workspace="default"):
    return {"Authorization": "Bearer " + TOKENS[role], "X-Workspace-Id": workspace}


def context(role="operator", app_id="enterprise-knowledge-assistant"):
    return ExecutionContext(Principal(role, (role,)), "default", app_id=app_id)


def request(parameters=None, key="test-idempotency-key", debug=False):
    return InvokeRequest(parameters=parameters or {"device_id": "B47-001", "problem": "E-203"}, idempotency_key=key, debug=debug)


def test_definitions_versions_published_runtime_and_rollback(center):
    current = center.store.get("default", "tools", "draft_work_order")
    definition = {key: value for key, value in current.items() if key in ToolInput.model_fields}
    definition.update(name="草稿 V2", base_version=1)
    draft = center.store.save("default", "tools", definition, "admin")
    assert draft["version"] == 2
    assert center.store.runtime("default", "tools", "draft_work_order")["name"] != "草稿 V2"
    center.store.publish("default", "tools", "draft_work_order", "admin", 2)
    assert center.store.runtime("default", "tools", "draft_work_order")["name"] == "草稿 V2"
    assert center.store.compare("default", "tools", "draft_work_order", 1, 2)["changes"]
    with pytest.raises(ToolCenterError):
        center.store.save("default", "tools", definition, "admin")
    rolled = center.store.rollback("default", "tools", "draft_work_order", 1, "admin")
    assert rolled["published_version"] == 3
    center.store.set_enabled("default", "tools", "draft_work_order", False, "admin")
    with pytest.raises(ToolCenterError):
        center.store.runtime("default", "tools", "draft_work_order")


@pytest.mark.parametrize("schema", [{"type": "not-a-type"}, {"type": "object", "$ref": "https://secret.example/schema"},
                                  {"type": "object", "properties": {"confirmed": {"type": "boolean"}}}])
def test_unsafe_schemas_are_rejected(schema):
    with pytest.raises(ValidationError):
        ToolInput(tool_id="bad_tool", name="不安全", description="不安全", kind="builtin", handler="draft_work_order", input_schema=schema)


def test_credential_api_masks_and_encrypts_and_checks_role(center):
    body = {"credential_id": "test-key", "name": "测试凭据", "environment": "test", "secret": {"token": "high-value-secret-556677"}}
    result = center.client.post("/api/tool-center/credentials", json=body, headers=headers())
    assert result.status_code == 200
    assert "high-value-secret" not in result.text
    assert b"high-value-secret" not in center.store.path.read_bytes()
    assert center.store.credential_secret("default", "test-key")["token"] == body["secret"]["token"]
    assert center.client.post("/api/tool-center/credentials", json=body, headers=headers("operator")).status_code == 403
    assert center.client.get("/api/tool-center/credentials", headers=headers("viewer")).status_code == 200
    assert center.store.rotate_encryption("default", "admin")["rotated_count"] == 1


def test_anonymous_forged_identity_and_workspace_are_denied(center):
    assert center.client.get("/api/tool-center/tools").status_code == 401
    assert center.client.get("/api/tool-center/tools", headers={"X-User-Id":"admin","X-Roles":"admin"}).status_code == 401
    assert center.client.get("/api/tool-center/tools", headers=headers(workspace="foreign")).status_code == 403
    assert center.client.post("/api/apps", headers=headers("viewer"), json={"name":"非法应用"}).status_code == 403


def test_local_login_rejects_non_loopback_host(center):
    result = center.client.post("/api/tool-center/session/local", headers={"X-Tool-Center-Login":"1"})
    assert result.status_code == 403
    remote = TestClient(main.app, base_url="http://attacker.example", client=("127.0.0.1",1234))
    assert remote.post("/api/tool-center/session/local",headers={"X-Tool-Center-Login":"1"}).status_code == 403


def test_cookie_session_requires_csrf_and_honors_revocation(center):
    login = center.client.post("/api/tool-center/session/login", json={"token":TOKENS["admin"]})
    assert login.status_code == 200 and "httponly" in login.headers["set-cookie"].lower()
    assert center.client.post("/api/tool-center/resources/tools/draft_work_order/enabled",json={"enabled":True}).status_code == 403
    csrf = {"X-Tool-Center-CSRF":login.json()["csrf_token"]}
    assert center.client.post("/api/tool-center/resources/tools/draft_work_order/enabled",json={"enabled":True},headers=csrf).status_code == 200
    assert center.client.post("/api/tool-center/resources/tools/draft_work_order/enabled",json={"enabled":True},headers={**csrf,"Origin":"https://attacker.example"}).status_code == 403
    center.settings.tool_center_auth_tokens = "[]"
    assert center.client.get("/api/tool-center/tools").status_code == 401


def test_tool_allowlist_is_application_specific_and_draft_does_not_grant(center):
    with pytest.raises(ToolCenterError, match="未被授权"):
        center.service.invoke(context(app_id="evidence-reviewer"), "submit_work_order", request())
    app = center.apps.get("evidence-reviewer")
    app["tools"].append({"tool_id":"submit_work_order","name":"提交","enabled":True})
    from app.schemas.application import ApplicationDefinitionInput
    payload={key:value for key,value in app.items() if key in ApplicationDefinitionInput.model_fields}
    payload["base_version"]=app["version"]
    center.apps.upsert(ApplicationDefinitionInput(**payload),app["app_id"])
    with pytest.raises(ToolCenterError, match="未被授权"):
        center.service.invoke(context(app_id="evidence-reviewer"), "submit_work_order", request())


def test_model_confirmed_boolean_cannot_bypass_gate(center):
    with pytest.raises(ToolCenterError, match="参数校验"):
        center.service.invoke(context(), "submit_work_order", request({"device_id":"B47-001","problem":"E-203","confirmed":True}))
    from app.tools.business import submit_work_order
    assert json.loads(submit_work_order.invoke({"device_id":"B47-001","problem":"E-203","confirmed":True}))["status"] == "awaiting_confirmation"


def test_confirmation_executes_once_even_with_concurrent_double_click(center, monkeypatch):
    writes=[]
    def execute(method, url, **kwargs):
        writes.append((method,url,kwargs))
        return {"order_id":"WO-001","echo_token":"very-secret-business-token"}, {}
    monkeypatch.setattr(center.service,"request",execute)
    call=center.service.invoke(context(), "create_work_order", request())
    assert call["status"] == "awaiting_confirmation" and writes == []
    repeat=center.service.invoke(context(), "submit_work_order", request())
    assert repeat["call_id"] == call["call_id"]
    with pytest.raises(ToolCenterError,match="申请人本人"):
        center.service.decide(context("admin"),call["call_id"],"approve")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:center.service.decide(context(),call["call_id"],"approve"),range(2)))
    completed=center.store.call("default",call["call_id"])[0]
    assert completed["status"] == "success" and len(writes)==1
    assert writes[0][2]["headers"]["Idempotency-Key"] == "test-idempotency-key"
    assert writes[0][2]["payload"]["device_id"] == "B47-001"
    assert "very-secret-business-token" not in json.dumps(completed)


def test_rejection_expiry_and_changed_version_block_execution(center):
    call=center.service.invoke(context(), "submit_work_order", request())
    assert center.service.decide(context(),call["call_id"],"reject")["status"] == "rejected"
    assert center.service.decide(context(),call["call_id"],"approve")["status"] == "rejected"
    call=center.service.invoke(context(), "submit_work_order", request(key="expiration-key-2"))
    center.store.update_call("default",call["call_id"],{"expires_at":0})
    assert center.service.decide(context(),call["call_id"],"approve")["status"] == "expired"
    call=center.service.invoke(context(), "submit_work_order", request(key="connection-change-key"))
    current=center.store.get("default","connections","env-business")
    definition={key:value for key,value in current.items() if key in ConnectionInput.model_fields}
    definition.update(base_version=1,endpoint="http://localhost:9930")
    center.store.save("default","connections",definition,"admin")
    center.store.publish("default","connections","env-business","admin")
    with pytest.raises(ToolCenterError,match="连接版本"):
        center.service.decide(context(),call["call_id"],"approve")


def test_readonly_live_execution_schema_and_rate_limit(center, monkeypatch):
    calls=[]
    monkeypatch.setattr(center.service,"request",lambda method,url,**kwargs:(calls.append(url) or {"device_id":"B47-001","state":"running"},{}))
    result=center.service.invoke(context(),"device_status",request({"device_id":"B47-001"}))
    assert result["status"]=="success" and calls==["http://localhost:9910/devices/B47-001/status"]
    with pytest.raises(ToolCenterError):
        center.service.invoke(context(),"get_device_status",request({"device_id":"OTHER"}))
    assert center.service.invoke(context(),"get_device_status",request({"device_id":"B47-001"}))["call_id"]==result["call_id"]


def test_uncertain_write_is_not_retried(center, monkeypatch):
    count=[]
    def timeout(*args,**kwargs):
        count.append(1)
        raise requests.Timeout("request timed out")
    monkeypatch.setattr(center.service,"request",timeout)
    call=center.service.invoke(context(),"submit_work_order",request())
    assert center.service.decide(context(),call["call_id"],"approve")["status"]=="unknown"
    assert center.service.decide(context(),call["call_id"],"approve")["status"]=="unknown"
    assert len(count)==1


def test_prod_write_debug_and_unlisted_hosts_rejected(center):
    with pytest.raises(ToolCenterError,match="生产写操作"):
        center.service.invoke(context("admin"),"submit_work_order",request(debug=True))
    with pytest.raises(ToolCenterError,match="主机"):
        center.service.check_endpoint("http://169.254.169.254/latest/meta-data")
    with pytest.raises(ValidationError):
        ConnectionInput(connection_id="unsafe",name="秘密",type="business_api",endpoint="http://user:password@localhost")


def test_managed_tools_reach_actual_executor_and_empty_sources_stay_empty(center, monkeypatch):
    monkeypatch.setattr(ToolCenterService,"request",lambda self,*args,**kwargs:({"state":"running"},{}))
    tools=center.service.managed_tools(context("admin"))
    assert {item.name for item in tools} >= {"get_device_status","submit_work_order"}
    assert "get_device_status" not in {item.name for item in center.service.managed_tools(context("admin","evidence-reviewer"))}
    call=json.loads(next(item for item in tools if item.name=="get_device_status").invoke({"device_id":"B47-001"}))
    assert call["status"]=="success"
    ctx=context("admin");ctx.source_types=();ctx.connection_ids=()
    assert managed_connectors(ctx)==[]
    from app.search.service import get_unified_search_service
    marker=execution_context.set(ctx)
    try:
        assert get_unified_search_service().connectors==[]
    finally:
        execution_context.reset(marker)


def test_agent_runtime_calls_only_governed_tools(center, monkeypatch):
    monkeypatch.setattr(ToolCenterService,"request",lambda self,*args,**kwargs:({"state":"running"},{}))
    monkeypatch.setattr(graph,"get_agent",lambda **kwargs:SimpleNamespace())
    monkeypatch.setattr(graph,"create_qwen_model",lambda:object())
    def create(**kwargs):
        tools={item.name:item for item in kwargs["tools"]}
        def invoke(payload,config=None):
            tools["get_device_status"].invoke({"device_id":"B47-001"})
            return {"messages":[AIMessage(content="设备状态")] }
        return SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(graph,"create_deep_agent",create)
    result=graph.run_agent([],"设备 B47-001 当前状态",tool_context=context("admin"))
    assert result["tool_calls"][0]["status"]=="success"
    assert result["trace"]["route"]=="business_tools"
    assert "running" in result["answer"]
    assert execution_context.get() is None


def test_import_is_draft_atomic_and_export_has_no_credentials(center):
    body={"format":"openapi","connection_id":"env-business","document":{"openapi":"3.0.3","paths":{"/devices/{device_id}/status":{"get":{"operationId":"new_status","summary":"读取状态","parameters":[{"name":"device_id","in":"path","required":True,"schema":{"type":"string"}}]}}}}}
    preview=center.client.post("/api/tool-center/imports/preview",json=body,headers=headers())
    assert preview.status_code==200
    definitions=preview.json()
    result=center.client.post("/api/tool-center/imports",headers=headers(),json={"format":"bundle","document":{"schema_version":1,"tools":definitions["tools"]}})
    assert result.status_code==200
    assert center.store.get("default","tools","new_status")["published_version"] is None
    export=center.client.get("/api/tool-center/export",headers=headers())
    assert "very-secret-business-token" not in export.text and "credentials" not in export.json()
    assert center.client.post("/api/tool-center/imports",headers=headers(),json={"format":"bundle","document":{"tools":definitions["tools"]}}).status_code==409


def test_connection_health_is_real_probe_not_config_flag(center, monkeypatch):
    monkeypatch.setattr(ToolCenterService,"request",lambda self,*args,**kwargs:({"status":"ok"},{}))
    result=center.client.post("/api/tool-center/connections/env-business/test",headers=headers(),json={})
    assert result.json()["health"]["status"]=="healthy"
    conn=next(item for item in center.service.connection_status("default") if item["connection_id"]=="env-business")
    assert conn["health"]["available"] is True


def test_editor_cannot_publish_and_viewer_cannot_invoke(center):
    assert center.client.post("/api/tool-center/resources/tools/draft_work_order/publish",json={},headers=headers("editor")).status_code==403
    body={"parameters":{"device_id":"B47-001","problem":"E-203"},"app_id":"enterprise-knowledge-assistant","idempotency_key":"viewer-key-12345"}
    assert center.client.post("/api/tool-center/tools/submit_work_order/invoke",json=body,headers=headers("viewer")).status_code==403


def publish_app(center, **updates):
    from app.schemas.application import ApplicationDefinitionInput
    current=center.apps.get("enterprise-knowledge-assistant")
    payload={key:value for key,value in current.items() if key in ApplicationDefinitionInput.model_fields}
    payload.update(updates,base_version=current["version"])
    saved=center.apps.upsert(ApplicationDefinitionInput(**payload),current["app_id"])
    return center.apps.publish(saved["app_id"],saved["version"])


def test_high_risk_requires_different_person_and_owner_can_cancel(center, monkeypatch):
    writes=[]
    monkeypatch.setattr(center.service,"request",lambda *args,**kwargs:(writes.append(1) or {"updated":True},{}))
    enabled=center.store.set_enabled("default","tools","set_device_parameter",True,"admin")
    assert enabled["version"]==2
    with pytest.raises(ToolCenterError,match="所选版本已停用"):
        center.store.runtime("default","tools","set_device_parameter")
    center.store.publish("default","tools","set_device_parameter","admin",2)
    app=center.apps.get("enterprise-knowledge-assistant")
    publish_app(center,tools=app["tools"]+[{"tool_id":"set_device_parameter","name":"设备控制","enabled":True}])
    call=center.service.invoke(context("approver"),"set_device_parameter",request({"device_id":"B47-001","parameter":"limit","value":"10"}))
    assert call["status"]=="awaiting_approval" and not writes
    with pytest.raises(ToolCenterError,match="不同人员"):
        center.service.decide(context("approver"),call["call_id"],"approve")
    with pytest.raises(ToolCenterError,match="审批者权限"):
        center.service.decide(context("operator"),call["call_id"],"approve")
    assert center.service.decide(context("admin"),call["call_id"],"approve")["status"]=="success"
    assert len(writes)==1
    canceled=center.service.invoke(context("approver"),"set_device_parameter",request({"device_id":"B47-001","parameter":"limit","value":"11"},key="cancel-control-123"))
    assert center.service.decide(context("approver"),canceled["call_id"],"reject")["status"]=="rejected"
    assert len(writes)==1


def test_pending_call_rechecks_revoked_identity_and_rotated_credentials(center):
    principal=auth.configured_principal(TOKENS["operator"])
    ctx=ExecutionContext(principal,"default",app_id="enterprise-knowledge-assistant")
    call=center.service.invoke(ctx,"submit_work_order",request())
    center.settings.tool_center_auth_tokens="[]"
    with pytest.raises(ToolCenterError,match="身份令牌已撤销"):
        center.service.decide(context(),call["call_id"],"approve")
    call=center.service.invoke(context(),"submit_work_order",request(key="rotated-credential-123"))
    center.store.save_credential("default",{"credential_id":"env-business-key","name":"轮换业务凭据","environment":"production","secret":{"token":"new-secret-998877"}},"admin")
    with pytest.raises(ToolCenterError,match="凭据已轮换"):
        center.service.decide(context(),call["call_id"],"approve")


def test_live_application_role_and_permission_restrictions(center):
    current=center.apps.get("enterprise-knowledge-assistant")
    publish_app(center,permissions={**current["permissions"],"run_roles":["approver"],"edit_roles":["admin"]})
    with pytest.raises(ToolCenterError,match="run授权范围"):
        center.service.invoke(context(),"submit_work_order",request())
    ctx=context("approver")
    ctx.principal=Principal("limited",("approver",),permissions=())
    with pytest.raises(ToolCenterError,match="权限标识"):
        center.service.invoke(ctx,"submit_work_order",request())
    assert center.client.put("/api/apps/enterprise-knowledge-assistant",headers=headers("editor"),json={"name":"无权修改"}).status_code==403
    assert center.client.put("/api/apps/missing",headers=headers("editor"),json={"name":"不存在"}).status_code==404


def test_workflow_disabled_tool_node_and_pinned_events(center, monkeypatch):
    from app.schemas.workflow import WorkflowDefinitionInput
    workflow=center.workflows.get("knowledge-research")
    payload={key:value for key,value in workflow.items() if key in WorkflowDefinitionInput.model_fields}
    payload.update(base_version=workflow["version"],nodes=workflow["nodes"]+[{"id":"real-status","name":"设备状态","type":"tool","enabled":False,"params":{"tool_id":"get_device_status"}}])
    saved=center.workflows.upsert(WorkflowDefinitionInput(**payload),"knowledge-research")
    center.workflows.publish("knowledge-research",saved["version"])
    publish_app(center,workflow_version=saved["version"])
    with pytest.raises(ToolCenterError,match="工具节点已停用"):
        center.service.invoke(context(),"get_device_status",request({"device_id":"B47-001"}))
    payload.update(base_version=saved["version"])
    payload["nodes"][-1]["enabled"]=True
    saved=center.workflows.upsert(WorkflowDefinitionInput(**payload),"knowledge-research")
    center.workflows.publish("knowledge-research",saved["version"])
    publish_app(center,workflow_version=saved["version"])
    events=[]
    ctx=context();ctx.on_event=events.append
    monkeypatch.setattr(center.service,"request",lambda *args,**kwargs:({"state":"running"},{}))
    call=center.service.invoke(ctx,"get_device_status",request({"device_id":"B47-001"}))
    assert call["workflow_version"]==saved["version"]
    assert any(item["type"]=="workflow_node" and item["node_id"]=="real-status" and item["workflow_version"]==saved["version"] for item in events)


def test_rate_limit_and_output_validation_are_enforced(center, monkeypatch):
    current=center.store.get("default","tools","get_device_status")
    payload={key:value for key,value in current.items() if key in ToolInput.model_fields}
    payload.update(base_version=current["version"],rate_limit_per_minute=1,output_schema={"type":"object","required":["state"]})
    center.store.save("default","tools",payload,"admin")
    center.store.publish("default","tools","get_device_status","admin")
    monkeypatch.setattr(center.service,"request",lambda *args,**kwargs:({"unexpected":"bad"},{}))
    call=center.service.invoke(context(),"get_device_status",request({"device_id":"B47-001"}))
    assert call["status"]=="error" and "output_schema" in call["error"]
    with pytest.raises(ToolCenterError) as error:
        center.service.invoke(context(),"get_device_status",request({"device_id":"B47-001"},key="different-key-for-rate"))
    assert error.value.status_code==429
    assert center.service.invoke(context(),"get_device_status",request({"device_id":"B47-001"}))["call_id"]==call["call_id"]


def test_dataset_catalog_respects_connections_and_no_global_fallback(center, monkeypatch):
    requests_made=[]
    def execute(self,method,url,**kwargs):
        requests_made.append(url)
        return {"code":0,"data":[{"id":"allowed","name":"可见"},{"id":"private","name":"不应显示"}]},{}
    monkeypatch.setattr(ToolCenterService,"request",execute)
    center.store.save_credential("default",{"credential_id":"env-ragflow-key","name":"RAGFlow","environment":"production","secret":{"token":"rag-test-token-5566"}},"admin")
    conn=center.store.get("default","connections","env-ragflow")
    payload={key:value for key,value in conn.items() if key in ConnectionInput.model_fields}
    payload.update(base_version=conn["version"],config={"dataset_ids":["allowed"],"auto_discover":False})
    center.store.save("default","connections",payload,"admin")
    center.store.publish("default","connections","env-ragflow","admin")
    ctx=context("admin");ctx.connection_ids=("env-ragflow",);ctx.source_types=("document",)
    assert [item["id"] for item in center.service.list_datasets(ctx)["datasets"]]==["allowed"]
    ctx.connection_ids=();requests_made.clear()
    assert center.service.list_datasets(ctx)["datasets"]==[] and requests_made==[]
    messages=graph._prefetch_dataset_list_messages(ctx)
    assert json.loads(messages[-1].content)["datasets"]==[] and requests_made==[]


def test_application_knowledge_binding_limits_published_ragflow_scope(center):
    from app.schemas.application import ApplicationDefinitionInput

    connection = center.store.get("default", "connections", "env-ragflow")
    connection_payload = {
        key: value for key, value in connection.items() if key in ConnectionInput.model_fields
    }
    connection_payload.update(
        base_version=connection["version"],
        config={"dataset_ids": ["dataset-public", "dataset-other"], "auto_discover": False},
    )
    center.store.save("default", "connections", connection_payload, "admin")
    published_connection = center.store.publish("default", "connections", "env-ragflow", "admin")

    application = center.apps.get("enterprise-knowledge-assistant")
    app_payload = {
        key: value for key, value in application.items() if key in ApplicationDefinitionInput.model_fields
    }
    app_payload.update(
        base_version=application["version"],
        knowledge_binding_mode="explicit",
        knowledge_bindings=[{
            "connection_id": "env-ragflow",
            "dataset_id": "dataset-public",
            "dataset_name": "公开设备资料",
            "published_revision": published_connection["version"],
        }],
    )
    saved = center.apps.upsert(
        ApplicationDefinitionInput(**app_payload),
        application["app_id"],
    )
    scope = center.service.resolve_application_knowledge_scope(
        saved,
        "default",
        require_document_binding=True,
    )
    assert scope["dataset_ids_by_connection"] == {"env-ragflow": ("dataset-public",)}

    ctx = context("admin")
    ctx.source_types = ("document",)
    ctx.knowledge_binding_mode = scope["mode"]
    ctx.knowledge_dataset_ids = scope["dataset_ids_by_connection"]
    connectors = managed_connectors(ctx)
    assert len(connectors) == 1
    assert connectors[0].connection["config"]["dataset_ids"] == ["dataset-public"]


def test_context_propagates_to_search_workers(center):
    from app.tool_center.context import ContextThreadPoolExecutor
    ctx=context();ctx.connection_ids=()
    marker=execution_context.set(ctx)
    try:
        with ContextThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(execution_context.get).result() is ctx
    finally:
        execution_context.reset(marker)


def test_http_search_and_chat_cannot_fall_back_to_anonymous_global_config(center, monkeypatch):
    assert center.client.get("/api/knowledge/catalog").status_code==401
    assert center.client.post("/api/search",json={"query":"设备"}).status_code==401
    assert center.client.post("/chat",json={"query":"你好"}).status_code==401
    contexts=[]
    monkeypatch.setattr(main,"run_agent",lambda history,query,**kwargs:contexts.append(kwargs["tool_context"]) or {"answer":"测试","citations":[],"retrieval_hits":[]})
    response=center.client.post("/chat",headers=headers("operator"),json={"query":"你好"})
    assert response.status_code==200
    assert contexts[0].app_id=="enterprise-knowledge-assistant"
    assert contexts[0].principal.user_id=="operator"


@pytest.mark.parametrize("protocol",["2025-11-25","2026-07-28"])
def test_mcp_http_transport_protocol_and_session_headers(center, monkeypatch, protocol):
    conn=ConnectionInput(connection_id="test-mcp",name="测试 MCP",type="mcp",environment="test",endpoint="http://localhost:9999/mcp",config={"protocol_version":protocol}).model_dump()
    exchanges=[]
    def execute(method,url,**kwargs):
        exchanges.append({**kwargs,"headers":dict(kwargs["headers"])})
        name=kwargs["payload"]["method"]
        return {"jsonrpc":"2.0","id":kwargs["payload"].get("id"),"result":{"protocolVersion":"2025-11-25"} if name=="initialize" else {"tools":[]}}, {"MCP-Session-Id":"test-session-id"} if name=="initialize" else {}
    monkeypatch.setattr(center.service,"request",execute)
    assert center.service.mcp_rpc("default",conn,"tools/list",{})[0]=={"tools":[]}
    if protocol=="2025-11-25":
        assert [item["payload"]["method"] for item in exchanges]==["initialize","notifications/initialized","tools/list"]
        assert exchanges[-1]["headers"]["Mcp-Session-Id"]=="test-session-id"
    else:
        assert len(exchanges)==1 and "Mcp-Session-Id" not in exchanges[0]["headers"]
        assert exchanges[0]["payload"]["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]==protocol


def test_bundle_failure_rolls_back_every_definition_and_rejects_bad_builtin(center):
    fresh=ToolInput(tool_id="atomic_draft",name="事务草稿",description="本地",kind="builtin",handler="draft_work_order",risk="draft").model_dump()
    existing=center.store.get("default","tools","draft_work_order")
    duplicate={key:value for key,value in existing.items() if key in ToolInput.model_fields}
    with pytest.raises(ToolCenterError):
        center.store.import_bundle("default",[],[fresh,duplicate],"admin")
    with pytest.raises(ToolCenterError,match="不存在"):
        center.store.get("default","tools","atomic_draft")
    fresh.update(handler="execute_shell",connection_id="injected-business")
    response=center.client.post("/api/tool-center/imports",headers=headers(),json={"format":"bundle","document":{"tools":[fresh],"connections":[ConnectionInput(connection_id="injected-business",name="测试",type="business_api",environment="test").model_dump()]}})
    assert response.status_code==400


def test_disabled_history_cannot_be_published_by_rollback(center):
    disabled=center.store.set_enabled("default","tools","draft_work_order",False,"admin")
    center.store.set_enabled("default","tools","draft_work_order",True,"admin")
    with pytest.raises(ToolCenterError,match="已停用的历史版本"):
        center.store.rollback("default","tools","draft_work_order",disabled["version"],"admin")
