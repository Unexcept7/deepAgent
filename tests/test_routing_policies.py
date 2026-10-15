import pytest
from langchain_core.messages import AIMessage
from app.routing_policy_store import RoutingPolicyStore, RoutingPolicyError
from app.routing_policy_engine import preview_routing_policies
from app.schemas.routing_policy import RoutingPolicyDefinitionInput


def test_publish_draft_disable_rollback_and_conflict(tmp_path):
    store = RoutingPolicyStore(tmp_path / 'routing.json')
    p = RoutingPolicyDefinitionInput(name='财务制度', policy_id='finance', priority=900,
        match={'keywords':['差旅报销']}, action={'source_types':['document'], 'top_k':7})
    saved = store.upsert(p)
    assert preview_routing_policies('差旅报销', store=store)['selected'] is None
    store.publish('finance', saved['version'])
    assert preview_routing_policies('差旅报销', store=store)['selected']['action']['top_k'] == 7
    p.base_version = 1
    p.enabled = False
    store.upsert(p)
    assert preview_routing_policies('差旅报销', store=store)['selected']['version'] == 1
    with pytest.raises(RoutingPolicyError):
        store.upsert(p)
    store.publish('finance', 2)
    assert preview_routing_policies('差旅报销', store=store)['selected'] is None
    store.rollback('finance', 1)
    assert preview_routing_policies('差旅报销', store=store)['selected']['version'] == 3
    assert 'enabled' in store.compare('finance', 2, 3)['changed_fields']


def test_priority_and_entity_conditions(tmp_path):
    store = RoutingPolicyStore(tmp_path / 'routing.json')
    for ident, priority in [('a',800),('b',900)]:
        p = RoutingPolicyDefinitionInput(policy_id=ident,name=ident,priority=priority,
            match={'keywords':['输入'], 'entity_types':['device_model']})
        store.publish(ident,store.upsert(p)['version'])
    assert preview_routing_policies('M1101 输入',store=store)['selected']['policy_id'] == 'b'
    assert preview_routing_policies('输入',store=store)['selected'] is None


def test_runtime_cannot_disable_evidence(monkeypatch):
    from app.agents import graph
    monkeypatch.setattr(graph,'evaluate_routing_policy',lambda *a,**k:{
        'policy_id':'test','policy_name':'普通对话错误配置','version':1,'route':'casual',
        'actions':{'require_evidence':False,'source_types':['document'],'top_k':7,'similarity_threshold':0.6},'reasons':[]})
    class FakeAgent:
        def invoke(self,*a,**k):
            return {'messages':[AIMessage(content='未经证据确认的答案')]}
    monkeypatch.setattr(graph,'get_agent',lambda **k:FakeAgent())
    from types import SimpleNamespace
    monkeypatch.setattr(graph,'get_settings',lambda:SimpleNamespace(claim_validation_enabled=False,max_agent_steps=6,evidence_mode='generic'))
    result=graph.run_agent([], 'M1101 有多少路输入？', source_types=['qa'])
    assert result['evidence']['requires_evidence'] is True
    assert result['trace']['routing_policy']['policy_id']=='test'
    assert result['trace']['routing_policy']['effective_top_k']==7
    assert result['trace']['routing_policy']['effective_threshold']==0.6
    assert result['trace']['routing_policy']['requested_sources']==['qa']


def test_application_threshold_takes_precedence_over_routing_policy(monkeypatch):
    from app.agents import graph
    from types import SimpleNamespace

    settings = SimpleNamespace(evidence_generic_min_similarity=0.3)
    assert graph._resolve_similarity_threshold(
        {"similarity_threshold": 0.2},
        settings=settings,
        workflow={"version": 1},
        policy_actions={"similarity_threshold": 0.6},
    ) == 0.2
    assert graph._resolve_similarity_threshold(
        {},
        settings=settings,
        workflow=None,
        policy_actions={"similarity_threshold": 0.6},
    ) == 0.6


def test_api_auth_and_preview(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.tool_center.auth import require_principal,require_manager,require_admin,Principal
    store=RoutingPolicyStore(tmp_path/'routing.json')
    monkeypatch.setattr(main,'get_routing_policy_store',lambda:store)
    import app.routing_policy_engine as engine
    monkeypatch.setattr(engine,'get_routing_policy_store',lambda:store)
    client=TestClient(main.app)
    assert client.get('/api/routing-policies').status_code==401
    principal=lambda:Principal('test',('admin',))
    for dep in (require_principal,require_manager,require_admin):
        main.app.dependency_overrides[dep]=principal
    try:
        response=client.post('/api/routing-policies/test',json={'query':'M1101 支持什么协议？'})
        assert response.status_code==200
        assert response.json()['selected']['policy_id']=='generic-knowledge-question'
        response=client.post('/api/routing-policies',json={'name':'差旅','match':{'keywords':['差旅']}})
        assert response.status_code==200
        p=response.json()['policy']
        assert client.post('/api/routing-policies/'+p['policy_id']+'/publish',json={'version':1}).status_code==200
        assert client.get('/api/routing-policies',headers={'X-Workspace-Id':'foreign'}).status_code==403
    finally:
        for dep in (require_principal,require_manager,require_admin):
            main.app.dependency_overrides.pop(dep,None)
