import json
import logging
import time
from queue import Queue
from pathlib import Path
from threading import Thread
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.agents.graph import run_agent
from app.agents.history_aware import build_clarification_suggestions, has_context_reference
from app.agents.model import clear_model_caches, create_model_for_profile
from app.agents.semantic_router import run_semantic_router
from app.conversation import ConversationAccessError, conversation_store
from app.config import get_settings
from app.schemas.query import QueryRequest
from app.schemas.tool_center import InvokeRequest, KnowledgeBindingRequest
from app.schemas.response import AgentResponse
from app.tools.ragflow import RAGFlowClient, search_knowledge_base
from app.search.models import SearchRequest
from app.search.planner import plan_search_query
from app.search.service import get_unified_search_service
from app.tools.mcp import tool_catalog
from app.graph.neo4j_store import GraphStoreError, Neo4jGraphStore
from app.schemas.graph import GraphPathRequest, GraphUpsertRequest
from app.schemas.graph import GraphExtractionRequest
from app.graph.pipeline import extract_graph_from_chunks
from app.schemas.workflow import (
    WorkflowDefinitionInput,
    WorkflowNodeDebugRequest,
    WorkflowPublishRequest,
    WorkflowRollbackRequest,
    WorkflowTemplateRequest,
)
from app.schemas.routing_policy import (
    RoutingPolicyDefinitionInput,
    RoutingPolicyPublishRequest,
    RoutingPolicyRollbackRequest,
    RoutingPolicyTestRequest,
)
from app.schemas.semantic_prompt import (
    SemanticPromptDefinitionInput,
    SemanticPromptPublishRequest,
    SemanticPromptRollbackRequest,
    SemanticPromptTestRequest,
)
from app.schemas.model_registry import (
    ModelProfileDefinitionInput,
    ModelRoutingUpdateInput,
    ModelTestRequest,
)
from app.schemas.dictionary import (
    DictionaryDefinitionInput,
    DictionaryPublishRequest,
    DictionaryRollbackRequest,
    DictionaryTestRequest,
)
from app.schemas.application import (
    ApplicationDefinitionInput,
    ApplicationPublishRequest,
    ApplicationRollbackRequest,
)
from app.schemas.evaluation import EvaluationRunRequest
from app.application_store import ApplicationError, get_application_store
from app.evaluation_service import (
    EvaluationError,
    get_evaluation_store,
    list_evaluation_agents,
    list_evaluation_datasets,
    load_evaluation_dataset,
    run_evaluation,
)
from app.workflow_store import WorkflowError, get_workflow_store
from app.routing_policy_engine import preview_routing_policies
from app.routing_policy_store import RoutingPolicyError, get_routing_policy_store
from app.semantic_prompt_store import SemanticPromptError, get_semantic_prompt_store
from app.model_registry import ModelRegistryError, get_model_registry
from app.dictionary_store import DictionaryError, get_dictionary_store, preview_dictionary_matches
from app.tool_center.auth import authorize_application, optional_principal, require_admin, require_manager, require_principal, workspace_for
from app.tool_center.context import ExecutionContext, execution_context
from app.tool_center.router import router as tool_center_router
from app.tool_center.service import INTERNAL_TOOLS, ToolCenterService, canonical_tool, managed_connectors
from app.tool_center.store import ToolCenterError, get_tool_center_store

logger = logging.getLogger(__name__)
app = FastAPI(title="Knowledge Base DeepAgent", version="1.0.0")
STATIC_DIR = Path(__file__).parent / "static"
app.include_router(tool_center_router)


@app.exception_handler(ToolCenterError)
async def tool_center_error(request: Request, exc: ToolCenterError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/assets/{filename}", include_in_schema=False)
def static_asset(filename: str):
    if filename not in {
        "tool-center.js", "tool-center.css", "trace-view.js",
          "routing-center.js", "routing-center.css",
          "semantic-prompt.js", "semantic-prompt.css",
          "model-center.js", "model-center.css",
          "dictionary-center.js", "dictionary-center.css",
        "evaluation-center.js", "evaluation-center.css",
        "suggestions.js", "suggestions.css",
        "workbench-theme.css",
        "list-pagination.js", "list-pagination.css",
    }:
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(STATIC_DIR / filename)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    # 生产入口只使用真实 API 驱动的页面；index_demo.html 仅保留为历史参考，
    # 不会被服务端默认暴露，也不会在服务异常时生成虚构答案。
    return FileResponse(STATIC_DIR / "index_production.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/search/sources")
def search_sources(app_id: str | None = None, http_request: Request = None) -> dict:
    """返回统一搜索各连接器的配置状态，供 Dify 风格管理页使用。"""
    principal = require_principal(http_request) if app_id and http_request is not None else None
    context = _application_search_context(app_id, http_request, principal) if principal and app_id else None
    token = execution_context.set(context)
    try:
        service = get_unified_search_service()
        return {"app_id": app_id, "sources": service.source_status()}
    finally:
        execution_context.reset(token)


@app.get("/api/graph/status")
def graph_status() -> dict:
    """检查图谱配置和 Neo4j 连通性，不返回敏感连接信息。"""
    return {"graph": Neo4jGraphStore().health()}


@app.post("/api/graph/paths")
def graph_paths(request: GraphPathRequest) -> dict:
    """查询实体四跳以内的关系路径，供图谱浏览和调试页面使用。"""
    try:
        paths = Neo4jGraphStore().find_paths(
            request.start_id,
            target_type=request.target_type,
            tenant_id=request.tenant_id,
            limit=request.limit,
        )
        return {"start_id": request.start_id, "paths": paths, "total": len(paths)}
    except GraphStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Graph path query failed")
        raise HTTPException(status_code=502, detail=f"图谱查询失败：{exc}") from exc


@app.post("/api/graph/upsert")
def graph_upsert(
    request: GraphUpsertRequest,
    x_graph_write_token: str | None = Header(default=None),
) -> dict:
    """受保护的实体关系导入接口。

    该接口供文档编译任务或企业 ETL 调用，不纳入 Agent 的普通工具目录。
    生产环境还应由 API 网关补充用户、租户和审批身份。
    """
    settings = get_settings()
    expected = str(getattr(settings, "graph_write_token", "") or "")
    if not expected or x_graph_write_token != expected:
        raise HTTPException(status_code=403, detail="图谱写入未授权")
    try:
        payload = request.model_dump()
        tenant_id = payload.pop("tenant_id", None)
        if tenant_id:
            for node in payload["nodes"]:
                node["tenant_id"] = node.get("tenant_id") or tenant_id
        result = Neo4jGraphStore(settings).upsert(payload["nodes"], payload["relations"])
        return {"status": "ok", **result}
    except GraphStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Graph upsert failed")
        raise HTTPException(status_code=502, detail=f"图谱写入失败：{exc}") from exc


@app.get("/api/graph/pipeline/documents")
def graph_pipeline_documents(dataset_id: str) -> dict:
    """读取 RAGFlow 文档目录，供图谱抽取页面选择输入。"""
    try:
        documents = RAGFlowClient().list_documents(dataset_id)
        return {"dataset_id": dataset_id, "documents": documents, "total": len(documents)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"RAGFlow 文档目录读取失败：{exc}") from exc


@app.post("/api/graph/pipeline/extract")
def graph_pipeline_extract(request: GraphExtractionRequest) -> dict:
    """从指定 RAGFlow 文档或请求中的 Chunk 生成待审核图谱草稿。"""
    try:
        chunks = [item.model_dump() for item in request.chunks]
        if not chunks:
            if not request.dataset_id or not request.document_id:
                raise HTTPException(status_code=400, detail="请选择 RAGFlow 知识库和文档，或直接提交 Chunk")
            chunks = RAGFlowClient().list_document_chunks(
                request.dataset_id,
                request.document_id,
                page_size=min(request.max_chunks, 100),
            )[: request.max_chunks]
        result = extract_graph_from_chunks(chunks, tenant_id=request.tenant_id)
        return {
            "status": "preview",
            "dataset_id": request.dataset_id,
            "document_id": request.document_id,
            "chunks": chunks,
            **result,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Graph extraction failed")
        raise HTTPException(status_code=502, detail=f"实体关系抽取失败：{exc}") from exc


@app.get("/api/tools/catalog")
def tools_catalog(http_request: Request, principal=Depends(require_principal)) -> dict:
    """返回 Dify 风格工具配置页所需的安全元数据。"""
    workspace = workspace_for(http_request, principal)
    tools = get_tool_center_store().list(workspace, "tools")
    tools.extend({"tool_id": tid, "name": name, "description": "后端知识证据流水线内部能力",
                  "risk": "read_only", "read_only": True, "kind": "internal", "required_permission": "knowledge:read",
                  "enabled": True, "version": 1, "published_version": 1}
                 for tid, name in (("unified_search", "统一搜索"), ("evidence_review", "证据校验"), ("coverage_audit", "覆盖审计")))
    return {"tools": tools}


@app.get("/api/apps")
def applications(status: str | None = None, category: str | None = None) -> dict:
    """返回应用中心的真实定义、生命周期状态和运行摘要。"""
    try:
        return {"apps": get_application_store().list(status=status, category=category)}
    except ApplicationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/apps/{app_id}")
def application_detail(app_id: str) -> dict:
    try:
        application = get_application_store().get(app_id)
        workflow = None
        if application.get("workflow_id"):
            try:
                workflow = get_workflow_store().get(str(application["workflow_id"]))
            except WorkflowError:
                workflow = {"workflow_id": application["workflow_id"], "status": "missing"}
        return {"app": application, "workflow": workflow}
    except ApplicationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/apps", dependencies=[Depends(require_manager)])
def create_application(request: ApplicationDefinitionInput) -> dict:
    try:
        return {"app": get_application_store().upsert(request)}
    except ApplicationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/apps/{app_id}", dependencies=[Depends(require_manager)])
def update_application(app_id: str, request: ApplicationDefinitionInput) -> dict:
    try:
        return {"app": get_application_store().upsert(request, app_id)}
    except ApplicationError as exc:
        status_code = 409 if "版本" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/apps/{app_id}/publish", dependencies=[Depends(require_admin)])
def publish_application(app_id: str, request: ApplicationPublishRequest | None = None) -> dict:
    request = request or ApplicationPublishRequest()
    try:
        application = get_application_store().get(app_id)
        workspace = (application.get("permissions") or {}).get("workspace_id", "default")
        # Publishing an explicit application scope is the point where the
        # selected RAGFlow datasets and the connection's published revision
        # become an auditable runtime contract.  Legacy ``inherit`` apps keep
        # their existing behavior.
        ToolCenterService().resolve_application_knowledge_scope(
            application,
            workspace,
            require_document_binding=True,
        )
        for binding in application.get("tools", []):
            tid = canonical_tool(binding["tool_id"])
            if binding.get("enabled", True) and tid not in INTERNAL_TOOLS:
                get_tool_center_store().runtime((application.get("permissions") or {}).get("workspace_id", "default"), "tools", tid)
        return {"app": get_application_store().publish(app_id, request.version, request.note)}
    except ApplicationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ToolCenterError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/api/apps/{app_id}/rollback", dependencies=[Depends(require_admin)])
def rollback_application(app_id: str, request: ApplicationRollbackRequest) -> dict:
    try:
        return {"app": get_application_store().rollback(
            app_id,
            request.version,
            publish=request.publish,
            note=request.note,
        )}
    except ApplicationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/apps/{app_id}/compare")
def compare_application(app_id: str, from_version: int, to_version: int) -> dict:
    try:
        return get_application_store().compare(app_id, from_version, to_version)
    except ApplicationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/apps/{app_id}/runs")
def application_runs(app_id: str) -> dict:
    try:
        return {"app_id": app_id, "runs": get_application_store().runs(app_id)}
    except ApplicationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/apps/{app_id}/knowledge/options")
def application_knowledge_options(
    app_id: str,
    http_request: Request,
    principal=Depends(require_principal),
) -> dict:
    """Return safe RAGFlow dataset choices for one application.

    The endpoint exposes names, IDs and published connection state only.  API
    keys and document maintenance stay behind the Knowledge Center/tool-center
    APIs; the application center only needs these choices to build its binding
    editor.
    """
    try:
        application = get_application_store().get(app_id)
        workspace = workspace_for(http_request, principal)
        app_workspace = (application.get("permissions") or {}).get("workspace_id", "default")
        if app_workspace != workspace:
            raise HTTPException(status_code=403, detail="应用不属于当前工作区")
        catalog = ToolCenterService().knowledge_dataset_management(
            ExecutionContext(principal, workspace)
        )
        return {
            "app_id": app_id,
            "knowledge_binding_mode": application.get("knowledge_binding_mode", "inherit"),
            "knowledge_bindings": application.get("knowledge_bindings") or [],
            "connections": catalog.get("connections") or [],
            "warnings": catalog.get("warnings") or [],
        }
    except ApplicationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/evaluations/agents")
def evaluation_agents() -> dict:
    """返回可参与对比的已发布 Agent。"""
    try:
        return {"agents": list_evaluation_agents()}
    except EvaluationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/evaluations/datasets")
def evaluation_datasets() -> dict:
    """返回项目内置的 JSONL 检索金标集目录，不接管 RAGFlow 文档。"""
    return {"datasets": list_evaluation_datasets()}


@app.get("/api/evaluations/datasets/{dataset_id}")
def evaluation_dataset_detail(dataset_id: str, split: str | None = None, limit: int = 100) -> dict:
    try:
        cases = load_evaluation_dataset(dataset_id, split=split, limit=limit)
        return {"dataset_id": dataset_id, "split": split, "cases": cases, "total": len(cases)}
    except EvaluationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/evaluations")
def evaluations(limit: int = 20) -> dict:
    """返回最近评测记录。"""
    return {"runs": get_evaluation_store().list(limit)}


@app.get("/api/evaluations/{run_id}")
def evaluation_detail(run_id: str) -> dict:
    try:
        return get_evaluation_store().get(run_id)
    except EvaluationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/evaluations/run")
def evaluation_run(request: EvaluationRunRequest) -> dict:
    """在同一问题集上运行多个已发布 Agent，并保存可审计结果。"""
    try:
        return run_evaluation(request)
    except EvaluationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Multi-agent evaluation failed")
        raise HTTPException(status_code=502, detail=f"多智能体评测失败：{exc}") from exc


@app.get("/api/workflows")
def workflows() -> dict:
    """返回 JSON/版本化工作流定义，而不是静态流程卡片。"""
    return {"workflows": get_workflow_store().list()}


def routing_workspace(request: Request, principal=Depends(require_principal)):
    if workspace_for(request, principal) != "default":
        raise HTTPException(status_code=403, detail="路由策略当前仅适用于默认工作区")


@app.get("/api/routing-policies", dependencies=[Depends(routing_workspace)])
def routing_policies() -> dict:
    """返回已配置的路由策略及其当前草稿版本摘要。"""
    return {"policies": get_routing_policy_store().list()}


@app.get("/api/routing-policies/{policy_id}", dependencies=[Depends(routing_workspace)])
def routing_policy_detail(policy_id: str) -> dict:
    try:
        return {"policy": get_routing_policy_store().get(policy_id)}
    except RoutingPolicyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/routing-policies", dependencies=[Depends(require_manager), Depends(routing_workspace)])
def create_routing_policy(request: RoutingPolicyDefinitionInput) -> dict:
    try:
        return {"policy": get_routing_policy_store().upsert(request)}
    except RoutingPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/routing-policies/{policy_id}", dependencies=[Depends(require_manager), Depends(routing_workspace)])
def update_routing_policy(policy_id: str, request: RoutingPolicyDefinitionInput) -> dict:
    try:
        return {"policy": get_routing_policy_store().upsert(request, policy_id)}
    except RoutingPolicyError as exc:
        status_code = 409 if "版本" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/routing-policies/{policy_id}/publish", dependencies=[Depends(require_admin), Depends(routing_workspace)])
def publish_routing_policy(policy_id: str, request: RoutingPolicyPublishRequest | None = None) -> dict:
    request = request or RoutingPolicyPublishRequest()
    try:
        return {"policy": get_routing_policy_store().publish(policy_id, request.version, request.note)}
    except RoutingPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/routing-policies/{policy_id}/rollback", dependencies=[Depends(require_admin), Depends(routing_workspace)])
def rollback_routing_policy(policy_id: str, request: RoutingPolicyRollbackRequest) -> dict:
    try:
        return {"policy": get_routing_policy_store().rollback(
            policy_id,
            request.version,
            publish=request.publish,
            note=request.note,
        )}
    except RoutingPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/routing-policies/{policy_id}/compare", dependencies=[Depends(routing_workspace)])
def compare_routing_policy(policy_id: str, from_version: int, to_version: int) -> dict:
    try:
        return get_routing_policy_store().compare(policy_id, from_version, to_version)
    except RoutingPolicyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/routing-policies/test", dependencies=[Depends(require_manager), Depends(routing_workspace)])
def test_routing_policy(request: RoutingPolicyTestRequest) -> dict:
    """只执行确定性预览，不触发 RAGFlow、Neo4j 或业务 API。"""
    return preview_routing_policies(request.query, workflow_id=request.workflow_id)


def semantic_prompt_workspace(request: Request, principal=Depends(require_principal)):
    if workspace_for(request, principal) != "default":
        raise HTTPException(status_code=403, detail="语义路由 Prompt 当前仅适用于默认工作区")


@app.get("/api/semantic-prompts", dependencies=[Depends(semantic_prompt_workspace)])
def semantic_prompts() -> dict:
    """返回可维护的语义路由 Prompt 摘要，不把完整 Prompt 暴露到列表页。"""
    return {"prompts": get_semantic_prompt_store().list()}


@app.get("/api/semantic-prompts/{prompt_id}", dependencies=[Depends(semantic_prompt_workspace)])
def semantic_prompt_detail(prompt_id: str) -> dict:
    try:
        return {"prompt": get_semantic_prompt_store().get(prompt_id)}
    except SemanticPromptError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/semantic-prompts", dependencies=[Depends(require_manager), Depends(semantic_prompt_workspace)])
def create_semantic_prompt(request: SemanticPromptDefinitionInput) -> dict:
    try:
        return {"prompt": get_semantic_prompt_store().upsert(request)}
    except SemanticPromptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/semantic-prompts/{prompt_id}", dependencies=[Depends(require_manager), Depends(semantic_prompt_workspace)])
def update_semantic_prompt(prompt_id: str, request: SemanticPromptDefinitionInput) -> dict:
    try:
        return {"prompt": get_semantic_prompt_store().upsert(request, prompt_id)}
    except SemanticPromptError as exc:
        status_code = 409 if "版本" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/semantic-prompts/{prompt_id}/publish", dependencies=[Depends(require_admin), Depends(semantic_prompt_workspace)])
def publish_semantic_prompt(prompt_id: str, request: SemanticPromptPublishRequest | None = None) -> dict:
    request = request or SemanticPromptPublishRequest()
    try:
        return {"prompt": get_semantic_prompt_store().publish(prompt_id, request.version, request.note)}
    except SemanticPromptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/semantic-prompts/{prompt_id}/rollback", dependencies=[Depends(require_admin), Depends(semantic_prompt_workspace)])
def rollback_semantic_prompt(prompt_id: str, request: SemanticPromptRollbackRequest) -> dict:
    try:
        return {"prompt": get_semantic_prompt_store().rollback(
            prompt_id,
            request.version,
            publish=request.publish,
            note=request.note,
        )}
    except SemanticPromptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/semantic-prompts/{prompt_id}/compare", dependencies=[Depends(semantic_prompt_workspace)])
def compare_semantic_prompt(prompt_id: str, from_version: int, to_version: int) -> dict:
    try:
        return get_semantic_prompt_store().compare(prompt_id, from_version, to_version)
    except SemanticPromptError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/semantic-prompts/test", dependencies=[Depends(require_manager), Depends(semantic_prompt_workspace)])
def test_semantic_prompt(request: SemanticPromptTestRequest) -> dict:
    """用真实模型试运行 Prompt；不会执行检索、业务 API 或工具。"""
    try:
        prompt = get_semantic_prompt_store().get_version(request.prompt_id, request.version)
        # Prompt 中的历史 model 字段继续保留用于兼容；试运行也优先
        # 使用模型中心的阶段路由，方便运维人员验证实际线上档案。
        selected_profile_id = None
        try:
            selection = get_model_registry().select("semantic_router", query=request.query)
            candidate_id = selection.get("profile_id")
            if any(item.get("profile_id") == candidate_id and item.get("ready") for item in get_model_registry().profiles()):
                selected_profile_id = candidate_id
        except ModelRegistryError:
            selected_profile_id = None
        if selected_profile_id:
            try:
                return run_semantic_router(request.query, prompt, model_profile_id=selected_profile_id)
            except TypeError as exc:
                # Keep old test doubles and external adapters that still
                # expose run_semantic_router(query, prompt) compatible.
                if "model_profile_id" not in str(exc):
                    raise
        return run_semantic_router(request.query, prompt)
    except SemanticPromptError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"语义路由模型不可用：{exc}") from exc
    except Exception as exc:
        logger.exception("Semantic prompt test failed")
        raise HTTPException(status_code=502, detail=f"语义路由试运行失败：{exc}") from exc


def model_registry_workspace(request: Request, principal=Depends(require_principal)):
    if workspace_for(request, principal) != "default":
        raise HTTPException(status_code=403, detail="模型中心当前仅适用于默认工作区")


@app.get("/api/model-registry", dependencies=[Depends(model_registry_workspace)])
def model_registry() -> dict:
    """Return non-secret model profiles and stage routing rules."""

    return get_model_registry().snapshot()


@app.post("/api/model-profiles", dependencies=[Depends(require_manager), Depends(model_registry_workspace)])
def create_model_profile(request: ModelProfileDefinitionInput) -> dict:
    try:
        profile = get_model_registry().upsert_profile(request)
        clear_model_caches()
        return {"profile": profile, "registry": get_model_registry().snapshot()}
    except ModelRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/model-profiles/{profile_id}", dependencies=[Depends(require_manager), Depends(model_registry_workspace)])
def update_model_profile(profile_id: str, request: ModelProfileDefinitionInput) -> dict:
    if request.profile_id != profile_id:
        raise HTTPException(status_code=409, detail="请求路径中的模型 ID 与配置内容不一致")
    try:
        profile = get_model_registry().upsert_profile(request)
        clear_model_caches()
        return {"profile": profile, "registry": get_model_registry().snapshot()}
    except ModelRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/model-registry/routing", dependencies=[Depends(require_manager), Depends(model_registry_workspace)])
def update_model_routing(request: ModelRoutingUpdateInput) -> dict:
    try:
        result = get_model_registry().update_routing(request.routing)
        clear_model_caches()
        return result
    except ModelRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/model-registry/test", dependencies=[Depends(require_manager), Depends(model_registry_workspace)])
def test_model_profile(request: ModelTestRequest) -> dict:
    """Run an explicit, user-triggered connectivity test for one profile."""

    started = time.perf_counter()
    try:
        model = create_model_for_profile(request.profile_id)
        response = model.invoke(request.prompt)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or item.get("content") or "") if isinstance(item, dict) else str(item)
                for item in content
            )
        return {
            "profile_id": request.profile_id,
            "model": getattr(model, "model_name", None) or getattr(model, "model", None),
            "provider": getattr(model, "deepagent_provider", None) or "OpenAI-compatible",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "status": "success",
            "preview": str(content or "")[:500],
        }
    except ModelRegistryError as exc:
        logger.warning("Model profile is not ready for %s: %s", request.profile_id, exc)
        try:
            diagnostics = get_model_registry().diagnose(request.profile_id)
        except ModelRegistryError:
            diagnostics = []
        return {
            "profile_id": request.profile_id,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "status": "error",
            "error_code": "profile_not_ready",
            "error": str(exc),
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        logger.warning("Model profile test failed for %s: %s", request.profile_id, exc)
        return {
            "profile_id": request.profile_id,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "status": "error",
            "error": str(exc),
        }


def dictionary_workspace(request: Request, principal=Depends(require_principal)):
    if workspace_for(request, principal) != "default":
        raise HTTPException(status_code=403, detail="业务词典当前仅适用于默认工作区")


@app.get("/api/dictionaries", dependencies=[Depends(dictionary_workspace)])
def dictionaries() -> dict:
    return {"dictionaries": get_dictionary_store().list()}


@app.get("/api/dictionaries/{dictionary_id}", dependencies=[Depends(dictionary_workspace)])
def dictionary_detail(dictionary_id: str) -> dict:
    try:
        return {"dictionary": get_dictionary_store().get(dictionary_id)}
    except DictionaryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/dictionaries", dependencies=[Depends(require_manager), Depends(dictionary_workspace)])
def create_dictionary(request: DictionaryDefinitionInput) -> dict:
    try:
        return {"dictionary": get_dictionary_store().upsert(request)}
    except DictionaryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/dictionaries/{dictionary_id}", dependencies=[Depends(require_manager), Depends(dictionary_workspace)])
def update_dictionary(dictionary_id: str, request: DictionaryDefinitionInput) -> dict:
    try:
        return {"dictionary": get_dictionary_store().upsert(request, dictionary_id)}
    except DictionaryError as exc:
        status_code = 409 if "版本" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/dictionaries/{dictionary_id}/publish", dependencies=[Depends(require_admin), Depends(dictionary_workspace)])
def publish_dictionary(dictionary_id: str, request: DictionaryPublishRequest | None = None) -> dict:
    request = request or DictionaryPublishRequest()
    try:
        return {"dictionary": get_dictionary_store().publish(dictionary_id, request.version, request.note)}
    except DictionaryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/dictionaries/{dictionary_id}/rollback", dependencies=[Depends(require_admin), Depends(dictionary_workspace)])
def rollback_dictionary(dictionary_id: str, request: DictionaryRollbackRequest) -> dict:
    try:
        return {"dictionary": get_dictionary_store().rollback(
            dictionary_id,
            request.version,
            publish=request.publish,
            note=request.note,
        )}
    except DictionaryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/dictionaries/{dictionary_id}/compare", dependencies=[Depends(dictionary_workspace)])
def compare_dictionary(dictionary_id: str, from_version: int, to_version: int) -> dict:
    try:
        return get_dictionary_store().compare(dictionary_id, from_version, to_version)
    except DictionaryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/dictionaries/test", dependencies=[Depends(require_manager), Depends(dictionary_workspace)])
def test_dictionary(request: DictionaryTestRequest) -> dict:
    try:
        dictionary = get_dictionary_store().get_version(request.dictionary_id, request.version)
        return {
            "dictionary_id": request.dictionary_id,
            "version": dictionary.get("version"),
            "query": request.query,
            "hits": preview_dictionary_matches(request.query, dictionary),
        }
    except DictionaryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/workflows/{workflow_id}")
def workflow_detail(workflow_id: str) -> dict:
    """读取一个工作流的当前草稿、发布版本和画布图。"""
    try:
        return {"workflow": get_workflow_store().get(workflow_id)}
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/workflows", dependencies=[Depends(require_manager)])
def create_workflow(request: WorkflowDefinitionInput) -> dict:
    """创建工作流，首次保存为 draft，发布需要显式调用 publish。"""
    try:
        workflow = get_workflow_store().upsert(request)
        return {"workflow": workflow}
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/workflows/{workflow_id}", dependencies=[Depends(require_manager)])
def update_workflow(workflow_id: str, request: WorkflowDefinitionInput) -> dict:
    """保存画布、节点、边和运行参数，生成一个新的草稿版本。"""
    try:
        workflow = get_workflow_store().upsert(request, workflow_id)
        return {"workflow": workflow}
    except WorkflowError as exc:
        status_code = 409 if "版本" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/workflows/{workflow_id}/publish", dependencies=[Depends(require_admin)])
def publish_workflow(workflow_id: str, request: WorkflowPublishRequest | None = None) -> dict:
    """发布当前草稿；运行时可以明确看到 published_version。"""
    request = request or WorkflowPublishRequest()
    try:
        workflow = get_workflow_store().publish(workflow_id, request.version, request.note)
        return {"workflow": workflow}
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/workflows/{workflow_id}/rollback", dependencies=[Depends(require_admin)])
def rollback_workflow(workflow_id: str, request: WorkflowRollbackRequest) -> dict:
    """把已发布版本复制为新版本，保留回滚审计记录。"""
    try:
        workflow = get_workflow_store().rollback(
            workflow_id,
            request.version,
            publish=request.publish,
            note=request.note,
        )
        return {"workflow": workflow}
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/workflows/{workflow_id}/compare")
def compare_workflow(workflow_id: str, from_version: int, to_version: int) -> dict:
    """返回参数、节点和边的结构化版本差异。"""
    try:
        return get_workflow_store().compare(workflow_id, from_version, to_version)
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/workflows/{workflow_id}/runs")
def workflow_runs(workflow_id: str) -> dict:
    try:
        return {"workflow_id": workflow_id, "runs": get_workflow_store().runs(workflow_id)}
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/workflow-templates")
def workflow_templates() -> dict:
    """返回可复制的工作流模板。"""
    return {"templates": get_workflow_store().templates()}


@app.post("/api/workflows/from-template", dependencies=[Depends(require_manager)])
def create_workflow_from_template(request: WorkflowTemplateRequest) -> dict:
    try:
        workflow = get_workflow_store().create_from_template(
            request.template_id,
            workflow_id=request.workflow_id,
            name=request.name,
        )
        return {"workflow": workflow}
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/workflows/{workflow_id}/debug-node", dependencies=[Depends(require_manager)])
def debug_workflow_node(workflow_id: str, request: WorkflowNodeDebugRequest, http_request: Request = None) -> dict:
    """调试一个节点；检索节点会真实调用连接器，其余节点返回可审计预览。"""
    try:
        workflow = get_workflow_store().get(workflow_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    node = next((item for item in workflow.get("nodes", []) if item.get("id") == request.node_id), None)
    if node is None:
        raise HTTPException(status_code=404, detail=f"节点不存在：{request.node_id}")

    node_type = str(node.get("type") or "")
    params = dict(workflow.get("params") or {})
    params.update(node.get("params") or {})
    debug_input = dict(request.input or {})
    query_text = str(debug_input.get("query") or "工作流节点调试").strip()
    if node_type == "tool" or params.get("tool_id") or params.get("tool"):
        if not node.get("enabled", True):
            raise HTTPException(status_code=409, detail="该工具节点已停用")
        if http_request is None:
            raise HTTPException(status_code=401, detail="工具调试需要真实用户身份")
        principal = require_manager(http_request)
        context = ExecutionContext(principal, workspace_for(http_request, principal), workflow_id=workflow_id)
        invocation = ToolCenterService().invoke(context, params.get("tool_id") or params.get("tool"), InvokeRequest(
            parameters=debug_input, idempotency_key=str(uuid4()), debug=True, workflow_id=workflow_id))
        return {"workflow_id": workflow_id, "node": node, "status": invocation["status"], "result": invocation}
    if node_type in {"unified_search", "search"}:
        allowed_sources = {"document", "qa", "graph", "sql", "api", "mcp"}
        source_types = [
            str(item) for item in (debug_input.get("source_types") or params.get("source_types") or [])
            if str(item) in allowed_sources
        ]
        search_request = SearchRequest(
            query=query_text,
            source_types=source_types,
            top_k=max(1, min(int(debug_input.get("top_k") or params.get("top_k") or 12), 50)),
            timeout_ms=max(100, min(int(params.get("timeout_seconds") or 8) * 1000, 60000)),
            filters=debug_input.get("filters") or {},
        )
        result = get_unified_search_service().search(search_request)
        return {
            "workflow_id": workflow_id,
            "workflow_version": workflow.get("version"),
            "node": node,
            "status": "success" if not result.errors else "partial",
            "result": result.model_dump(mode="json"),
        }
    if node_type == "ragflow_search":
        try:
            content = search_knowledge_base.invoke({
                "query": query_text,
                "top_k": max(1, min(int(debug_input.get("top_k") or params.get("top_k") or 8), 50)),
            })
            try:
                content = json.loads(str(content))
            except json.JSONDecodeError:
                content = {"raw": str(content)}
            return {"workflow_id": workflow_id, "workflow_version": workflow.get("version"), "node": node, "status": "success", "result": content}
        except Exception as exc:
            return {"workflow_id": workflow_id, "workflow_version": workflow.get("version"), "node": node, "status": "error", "error": str(exc)}
    if node_type == "human_confirmation" or node.get("human_confirmation"):
        return {
            "workflow_id": workflow_id,
            "workflow_version": workflow.get("version"),
            "node": node,
            "status": "waiting_confirmation",
            "confirmation": {"required": True, "actions": params.get("actions") or ["确认后继续"]},
        }
    return {
        "workflow_id": workflow_id,
        "workflow_version": workflow.get("version"),
        "node": node,
        "status": "preview",
        "result": {
            "message": "该节点当前由 DeepAgent 运行时组合执行，单节点调试已完成参数解析。",
            "input": debug_input,
            "effective_params": params,
        },
    }


@app.get("/api/knowledge/management")
def knowledge_management(http_request: Request, principal=Depends(require_principal)) -> dict:
    """Return RAGFlow connections and editable dataset bindings for the workspace."""
    context = ExecutionContext(principal, workspace_for(http_request, principal))
    return ToolCenterService().knowledge_dataset_management(context)


@app.put("/api/knowledge/connections/{connection_id}/datasets")
def update_knowledge_binding(
    connection_id: str,
    payload: KnowledgeBindingRequest,
    http_request: Request,
    principal=Depends(require_manager),
) -> dict:
    """Save a RAGFlow dataset allow-list as a connection draft."""
    workspace = workspace_for(http_request, principal)
    connection = ToolCenterService().update_ragflow_dataset_ids(
        workspace,
        connection_id,
        payload.dataset_ids,
        principal.user_id,
        payload.base_version,
    )
    return {
        "connection": connection,
        "dataset_ids": connection.get("config", {}).get("dataset_ids", []),
        "published": connection.get("published_version") == connection.get("version"),
        "message": "知识库 ID 已保存为草稿；发布后才会进入实际检索。",
    }


@app.get("/api/knowledge/catalog")
def knowledge_catalog(http_request: Request = None) -> dict:
    """读取真实 RAGFlow 知识库目录；未配置时返回明确状态。"""
    if http_request is not None:
        principal = require_principal(http_request)
        return ToolCenterService().list_datasets(ExecutionContext(principal, workspace_for(http_request, principal)))
    try:
        datasets = RAGFlowClient().list_datasets()
        return {"datasets": datasets, "configured": True, "warnings": []}
    except Exception as exc:
        return {"datasets": [], "configured": False, "warnings": [str(exc)]}


@app.post("/api/search")
def unified_search_api(request: SearchRequest, http_request: Request = None) -> dict:
    """Search published sources and plan graph entities like the workbench."""
    principal = require_principal(http_request) if http_request is not None else None
    context = (
        _application_search_context(request.app_id, http_request, principal)
        if principal and request.app_id
        else ExecutionContext(principal, workspace_for(http_request, principal)) if principal else None
    )
    token = execution_context.set(context)
    try:
        request.user_id = principal.user_id if principal else None
        request.tenant_id = context.workspace_id if context else None
        request.app_id = context.app_id if context else request.app_id
        service = get_unified_search_service()
        plan_result = None
        requested_sources = set(request.source_types or [])
        if request.source_types is not None:
            graph_requested = "graph" in requested_sources
        elif context is not None and context.source_types is not None:
            graph_requested = "graph" in set(context.source_types)
        else:
            graph_requested = "graph" in {
                item.strip().lower()
                for item in str(get_settings().search_sources or "").split(",")
                if item.strip()
            }
        graph_available = any(
            item.get("source_type") == "graph" and item.get("available")
            for item in service.source_status()
        )
        sql_requested = (
            "sql" in requested_sources
            if request.source_types is not None
            else context is not None and context.source_types is not None and "sql" in set(context.source_types)
            if context is not None
            else "sql" in {
                item.strip().lower()
                for item in str(get_settings().search_sources or "").split(",")
                if item.strip()
            }
        )
        sql_available = any(
            item.get("source_type") == "sql" and item.get("available")
            for item in service.source_status()
        )
        if request.query_plan is None and ((graph_requested and graph_available) or (sql_requested and sql_available)):
            plan_result = plan_search_query(request.query)
            request.query_plan = plan_result.plan

        response = service.search(request).model_dump(mode="json")
        if plan_result is not None:
            response["query_plan"] = plan_result.plan.model_dump(mode="json")
            response["query_plan_diagnostics"] = plan_result.diagnostics
            response["warnings"] = [
                *response.get("warnings", []),
                *plan_result.warnings,
            ]
        elif request.query_plan is not None:
            response["query_plan"] = request.query_plan.model_dump(mode="json")
            response["query_plan_diagnostics"] = {
                "planner": "caller_provided",
                "status": "provided",
                "graph_queries": list(request.query_plan.graph_queries),
                "document_queries": list(request.query_plan.document_queries),
                "structured_queries": [item.model_dump(mode="json") for item in request.query_plan.structured_queries],
                "attribute_keys": list(request.query_plan.attribute_keys),
                "relation_types": list(request.query_plan.relation_types),
            }
        return response
    finally:
        execution_context.reset(token)


@app.get("/quick-prompts")
def quick_prompts(
    conversation_id: str | None = None,
    app_id: str | None = None,
) -> dict:
    """Return context-aware starter prompts without calling Qwen."""
    try:
        history = (
            conversation_store.get(conversation_id, app_id=app_id)
            if conversation_id
            else []
        )
    except ConversationAccessError as exc:
        raise HTTPException(status_code=409, detail="该会话属于其他 Agent，不能读取") from exc
    last_user = next(
        (
            str(item.get("content") or "").strip()
            for item in reversed(history)
            if item.get("role") == "user" and item.get("content")
        ),
        "",
    )

    bound_datasets: list[dict[str, str]] = []
    if not last_user:
        try:
            bound_datasets = RAGFlowClient().list_configured_datasets()
        except Exception as exc:
            # Quick prompts must not make the chat page unavailable when RAGFlow
            # is temporarily down.  The frontend will use the fallback below.
            logger.warning("Failed to load bound datasets for quick prompts: %s", exc)

    if last_user:
        prompts = [
            {
                "text": "查看本轮检索片段",
                "query": "请展示刚才问题命中的知识库原文片段",
            },
            {
                "text": "继续查询通信参数",
                "query": "请继续查询刚才问题涉及对象的通信参数",
            },
            {
                "text": "查看对应来源文档",
                "query": "请查看刚才回答引用的来源文档",
            },
            {
                "text": "比较相关型号差异",
                "query": "请比较刚才问题涉及的相关型号差异",
            },
        ]
        source = "conversation"
    else:
        prompts = [
            {
                "text": f"查询“{item['name']}”中的资料",
                "query": f"请查询知识库“{item['name']}”中的主要设备和技术资料",
            }
            for item in bound_datasets[:2]
        ]
        prompts.extend([
            {
                "text": "查看已绑定知识库",
                "query": "当前系统有哪些已绑定知识库？",
            },
            {
                "text": "比较知识库中的设备型号",
                "query": "请比较当前知识库中的两个设备型号",
            },
        ])
        source = "configured_datasets"

    if not prompts:
        prompts = [
            {"text": "查询当前知识库", "query": "当前系统有哪些已绑定知识库？"},
            {"text": "查询设备技术参数", "query": "请查询知识库中的设备技术参数"},
            {"text": "查看协议资料", "query": "请查询知识库中的工业通信协议资料"},
            {"text": "比较设备型号", "query": "请比较知识库中的两个设备型号"},
        ]
        source = "fallback"

    return {"source": source, "items": prompts[:4]}


@app.post("/conversations")
def create_conversation(app_id: str | None = None) -> dict:
    conversation_id = str(uuid4())
    return conversation_store.create(conversation_id, app_id=app_id)


@app.get("/conversations")
def list_conversations(app_id: str | None = None) -> list[dict]:
    return conversation_store.list_conversations(app_id=app_id)


@app.get("/conversations/{conversation_id}/messages")
def conversation_messages(
    conversation_id: str,
    app_id: str | None = None,
) -> dict:
    try:
        messages = []
        last_user_query = ""
        for message in conversation_store.get(conversation_id, app_id=app_id):
            if message.get("role") == "user":
                last_user_query = str(message.get("content") or "")
            elif (message.get("evidence") or {}).get("status") == "context_ambiguous":
                context = (message.get("trace") or {}).get("context_resolution") or {}
                suggestions = build_clarification_suggestions(
                    str(context.get("query") or last_user_query),
                    context.get("candidates") or [],
                )
                if suggestions:
                    # Upgrade old generic choices only in the response; reading
                    # history must not mutate saved messages or activity times.
                    message = {**message, "suggestions": suggestions}
            elif message.get("suggestions") and not has_context_reference(last_user_query):
                # Do not keep stale generic follow-up buttons on historical
                # answers whose user question already named its object.
                message = {**message, "suggestions": []}
            messages.append(message)
        return {
            "conversation_id": conversation_id,
            "app_id": app_id,
            "messages": messages,
        }
    except ConversationAccessError as exc:
        raise HTTPException(status_code=409, detail="该会话属于其他 Agent，不能读取") from exc


@app.get("/source-documents/{dataset_id}/{document_id}")
def source_document(dataset_id: str, document_id: str) -> StreamingResponse:
    """Proxy a RAGFlow source document so its API key stays on the server."""
    try:
        upstream = RAGFlowClient().open_document(dataset_id, document_id)
    except Exception as exc:
        logger.exception("Failed to open RAGFlow source document")
        raise HTTPException(status_code=502, detail=f"无法获取来源文档：{exc}") from exc

    def iter_content():
        try:
            yield from upstream.iter_content(chunk_size=64 * 1024)
        finally:
            upstream.close()

    media_type = (upstream.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    headers = {}
    disposition = upstream.headers.get("Content-Disposition")
    if media_type == "application/pdf":
        # Keep legacy source links previewable in the browser as well.
        headers["Content-Disposition"] = "inline"
    elif disposition:
        headers["Content-Disposition"] = disposition
    return StreamingResponse(
        iter_content(),
        media_type=media_type,
        headers=headers,
    )


def _sse_event(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _get_conversation_summary(
    conversation_id: str,
    app_id: str | None,
) -> dict:
    getter = getattr(conversation_store, "get_context_summary", None)
    if not callable(getter):
        return {}
    try:
        value = getter(conversation_id, app_id=app_id)
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        logger.warning("读取会话语义摘要失败：%s", exc)
        return {}


def _save_conversation_summary(
    conversation_id: str,
    app_id: str | None,
    result: dict | None,
) -> None:
    """Persist only the bounded semantic state, never the full prompt context."""
    trace = (result or {}).get("trace") or {}
    routing_policy = trace.get("routing_policy") or {}
    semantic_context = routing_policy.get("semantic_context") or {}
    summary = semantic_context.get("next_summary")
    saver = getattr(conversation_store, "save_context_summary", None)
    if not isinstance(summary, dict) or not callable(saver):
        return
    try:
        saver(conversation_id, summary, app_id=app_id)
    except Exception as exc:
        # Context continuity is useful but must never turn a successful answer
        # into an API failure.
        logger.warning("保存会话语义摘要失败：%s", exc)


def _prepare_conversation(
    conversation_id: str | None,
    app_id: str | None,
) -> tuple[str, list[dict]]:
    """创建或校验会话，并返回当前 Agent 可读取的历史消息。"""
    current_id = conversation_id or str(uuid4())
    try:
        # 先写入归属元数据，避免后续 append 把 app_id 丢掉。
        conversation_store.create(current_id, app_id=app_id)
        history = conversation_store.get(current_id, app_id=app_id)
    except ConversationAccessError as exc:
        raise HTTPException(
            status_code=409,
            detail="该会话属于其他 Agent，不能跨 Agent 继续对话",
        ) from exc
    return current_id, history


def _touch_conversation(
    conversation_id: str,
    app_id: str | None,
) -> None:
    """Refresh activity time for every request, including failed runs."""
    toucher = getattr(conversation_store, "touch", None)
    if not callable(toucher):
        return
    try:
        toucher(conversation_id, app_id=app_id)
    except Exception as exc:  # pragma: no cover - storage failure is non-fatal
        logger.warning("刷新会话活动时间失败：%s", exc)


def _record_application_run(
    request: QueryRequest,
    result: dict | None = None,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Persist the real runtime outcome for an application-center launch."""
    if not request.app_id:
        return
    try:
        application = get_application_store().for_run(request.app_id)
        version = int(
            application.get("published_version")
            or application.get("version")
            or 1
        )
        result = result or {}
        trace = result.get("trace") or {}
        if status == "success" and trace.get("route") == "error":
            status = "error"
        run_id = str(result.get("workflow_run_id") or f"app-{uuid4()}")
        duration = result.get("total_duration_ms")
        get_application_store().record_run(
            request.app_id,
            version,
            run_id,
            status,
            duration_ms=int(duration) if duration is not None else None,
            error=error or (result.get("evidence") or {}).get("reason"),
        )
    except Exception:
        # Runtime telemetry must never turn a successful user answer into a
        # failed request; the application API remains independently usable.
        logger.warning("Unable to record application run: %s", request.app_id, exc_info=True)


def _apply_application_context(request: QueryRequest) -> None:
    """Make an application definition authoritative for its runtime launch."""
    if not request.app_id:
        return
    try:
        application = get_application_store().for_run(request.app_id)
    except ApplicationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not application.get("enabled", True) or application.get("status") == "disabled":
        raise HTTPException(status_code=409, detail="应用已停用，不能运行")

    # Do not let a client-side request bypass the application's bound graph or
    # source policy. Explicit workflow parameters remain useful for previews.
    if application.get("workflow_id"):
        request.workflow_id = str(application["workflow_id"])
    if application.get("workflow_version") is not None:
        request.workflow_version = int(application["workflow_version"])
    enabled_sources = [
        str(item.get("source_type"))
        for item in application.get("data_sources") or []
        if item.get("enabled", True) and item.get("source_type")
    ]
    # 应用配置定义的是本 Agent 允许使用的数据源上限；本轮请求仍可在
    # 这个上限内缩小范围，不能因为 Agent 启用了图谱就强制每轮调用图谱。
    allowed_sources = set(enabled_sources)
    if request.source_types is not None:
        request.source_types = [
            source for source in request.source_types
            if source in allowed_sources
        ]
    else:
        request.source_types = enabled_sources
    runtime = dict(application.get("runtime") or {})
    runtime.update(request.workflow_params or {})
    request.workflow_params = runtime


def _application_source_policies(application: dict, workspace: str) -> dict[str, dict]:
    """Resolve per-application structured-data scopes to connection IDs."""
    type_by_source = {
        "document": "ragflow",
        "graph": "neo4j",
        "api": "business_api",
        "sql": "sql",
        "qa": "qa",
        "mcp": "mcp",
    }
    definitions = ToolCenterService().store.list(workspace, "connections")
    by_id = {str(item.get("connection_id")): item for item in definitions}
    environment = str((application.get("runtime") or {}).get("environment") or "production")
    result: dict[str, dict] = {}
    for source in application.get("data_sources") or []:
        if not isinstance(source, dict) or source.get("enabled", True) is False:
            continue
        config = source.get("config") or {}
        scope = config.get("data_scope")
        if not isinstance(scope, dict):
            continue
        selected = [str(item).strip() for item in config.get("connection_ids") or [] if str(item).strip()]
        if not selected:
            expected_type = type_by_source.get(str(source.get("source_type") or ""))
            selected = [
                connection_id for connection_id, definition in by_id.items()
                if definition.get("type") == expected_type
                and str(definition.get("environment") or "production") == environment
            ]
        for connection_id in selected:
            if connection_id in by_id:
                result[connection_id] = scope
    return result


def _application_search_context(app_id: str, http_request: Request, principal):
    """Build the same published app scope for direct unified-search calls."""
    try:
        application = get_application_store().for_run(app_id)
    except ApplicationError as exc:
        raise ToolCenterError(str(exc), 404) from exc
    if not application.get("enabled", True) or application.get("status") == "disabled":
        raise ToolCenterError("应用已停用，不能搜索", 409)
    workspace = workspace_for(http_request, principal)
    authorize_application(principal, application, workspace, "run")
    knowledge_scope = ToolCenterService().resolve_application_knowledge_scope(
        application,
        workspace,
        require_document_binding=False,
    )
    sources = [item for item in application.get("data_sources") or [] if item.get("enabled", True)]
    explicit_connections = any("connection_ids" in (item.get("config") or {}) for item in sources)
    connection_ids = (
        tuple(
            str(connection_id)
            for item in sources
            for connection_id in (item.get("config") or {}).get("connection_ids", [])
            if str(connection_id).strip()
        )
        if explicit_connections
        else None
    )
    environment = str((application.get("runtime") or {}).get("environment") or "production")
    if environment not in {"test", "production"}:
        raise ToolCenterError("应用运行环境配置无效", 409)
    return ExecutionContext(
        principal,
        workspace,
        app_id=app_id,
        environment=environment,
        connection_ids=connection_ids,
        source_types=tuple(item["source_type"] for item in sources if item.get("source_type")),
        knowledge_binding_mode=knowledge_scope["mode"],
        knowledge_dataset_ids=knowledge_scope["dataset_ids_by_connection"],
        knowledge_binding_revisions=knowledge_scope["published_revisions"],
        source_policies=_application_source_policies(application, workspace),
    )


def _tool_execution_context(request: QueryRequest, http_request: Request | None):
    if http_request is None:
        return None
    principal = require_principal(http_request)
    if not request.app_id:
        request.app_id = "enterprise-knowledge-assistant"
        _apply_application_context(request)
    if not principal.has("admin", "operator", "approver", "editor"):
        raise ToolCenterError("该用户只有查看权限，不能运行 Agent", 403)
    workspace = workspace_for(http_request, principal)
    application = get_application_store().for_run(request.app_id)
    if (application.get("permissions") or {}).get("workspace_id", "default") != workspace:
        raise ToolCenterError("应用不属于当前工作区", 403)
    authorize_application(principal, application, workspace, "run")
    knowledge_scope = ToolCenterService().resolve_application_knowledge_scope(
        application,
        workspace,
        require_document_binding=True,
    )
    sources = [item for item in application.get("data_sources", []) if item.get("enabled", True)]
    explicit_connections = any("connection_ids" in item.get("config", {}) for item in sources)
    connection_ids = tuple(cid for item in sources for cid in item.get("config", {}).get("connection_ids", [])) if explicit_connections else None
    environment = application.get("runtime", {}).get("environment", "production")
    if environment not in {"test", "production"}:
        raise ToolCenterError("应用运行环境配置无效", 409)
    return ExecutionContext(principal, workspace, app_id=request.app_id,
        workflow_id=request.workflow_id, environment=environment, connection_ids=connection_ids,
        source_types=tuple(item["source_type"] for item in application.get("data_sources", []) if item.get("enabled", True)),
        knowledge_binding_mode=knowledge_scope["mode"],
        knowledge_dataset_ids=knowledge_scope["dataset_ids_by_connection"],
        knowledge_binding_revisions=knowledge_scope["published_revisions"],
        source_policies=_application_source_policies(application, workspace),
    )

def _agent_history(stored_history: list[dict]) -> list[dict]:
    """Keep conversation text plus validated image metadata for the agent."""
    history: list[dict] = []
    for item in stored_history:
        if item.get("role") not in {"user", "assistant"} or not item.get("content"):
            continue
        message = {"role": item["role"], "content": item["content"]}
        if item.get("attachments"):
            message["attachments"] = item["attachments"]
        history.append(message)
    return history


@app.post("/chat/stream")
def chat_stream(request: QueryRequest, http_request: Request = None) -> StreamingResponse:
    """Stream LangGraph progress and Qwen message chunks as SSE."""
    _apply_application_context(request)
    tool_context = _tool_execution_context(request, http_request)
    conversation_id, stored_history = _prepare_conversation(
        request.conversation_id,
        request.app_id,
    )
    _touch_conversation(conversation_id, request.app_id)
    history = _agent_history(stored_history)
    attachments = [item.model_dump(mode="json") for item in request.attachments]
    conversation_summary = _get_conversation_summary(conversation_id, request.app_id)

    def event_generator():
        events: Queue[dict | None] = Queue()

        def emit(event: dict) -> None:
            events.put(event)

        def worker() -> None:
            try:
                try:
                    run_kwargs = {
                        "on_event": emit,
                        "source_types": request.source_types,
                        "search_filters": request.filters,
                        "workflow_id": request.workflow_id,
                        "workflow_version": request.workflow_version,
                        "workflow_params": request.workflow_params,
                        "tool_context": tool_context,
                        "conversation_summary": conversation_summary,
                    }
                    if attachments:
                        run_kwargs["attachments"] = attachments
                    result = run_agent(history, request.query, **run_kwargs)
                except TypeError as exc:
                    # 兼容旧的外部集成和只接受两个参数的测试替身。
                    if "unexpected keyword" not in str(exc):
                        raise
                    result = run_agent(history, request.query, on_event=emit)
                answer = result["answer"]
                _save_conversation_summary(conversation_id, request.app_id, result)
                user_metadata = {"app_id": request.app_id}
                if attachments:
                    user_metadata["attachments"] = attachments
                conversation_store.append(
                    conversation_id,
                    "user",
                    request.query,
                    **user_metadata,
                )
                conversation_store.append(
                    conversation_id,
                    "assistant",
                    answer,
                    app_id=request.app_id,
                    citations=result.get("citations", []),
                    retrieval_hits=result.get("retrieval_hits", []),
                    graph_relations=result.get("graph_relations", []),
                    evidence=result.get("evidence"),
                    claims=result.get("claims", []),
                    requirements=result.get("requirements", []),
                    results=result.get("results", []),
                    coverage=result.get("coverage"),
                    suggestions=result.get("suggestions", []),
                    trace=result.get("trace"),
                    tool_calls=result.get("tool_calls", []),
                )
                _record_application_run(request, result, status="success")
                events.put({"type": "completed", "result": result})
            except Exception as exc:
                logger.exception("Streaming agent invocation failed")
                _record_application_run(request, status="error", error=str(exc))
                events.put({"type": "fatal_error", "message": str(exc)})
            finally:
                events.put(None)

        Thread(target=worker, name="deepagent-stream", daemon=True).start()
        yield _sse_event("conversation", {"conversation_id": conversation_id})

        while True:
            item = events.get()
            if item is None:
                break

            event_type = str(item.get("type") or "message")
            if event_type == "completed":
                result = item["result"]
                yield _sse_event(
                    "final",
                    {"conversation_id": conversation_id, **result},
                )
                yield _sse_event("done", {"conversation_id": conversation_id})
                continue

            if event_type == "fatal_error":
                yield _sse_event("error", {
                    "conversation_id": conversation_id,
                    "message": item.get("message", "流式请求失败"),
                })
                yield _sse_event("done", {"conversation_id": conversation_id})
                continue

            data = {key: value for key, value in item.items() if key != "type"}
            yield _sse_event(event_type, data)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/conversations/{conversation_id}")
def clear_conversation(
    conversation_id: str,
    app_id: str | None = None,
) -> dict[str, str]:
    try:
        conversation_store.clear(conversation_id, app_id=app_id)
    except ConversationAccessError as exc:
        raise HTTPException(status_code=409, detail="该会话属于其他 Agent，不能删除") from exc
    return {"status": "cleared", "conversation_id": conversation_id}


@app.post("/chat", response_model=AgentResponse)
def chat(request: QueryRequest, http_request: Request = None) -> AgentResponse:
    _apply_application_context(request)
    tool_context = _tool_execution_context(request, http_request)
    conversation_id, stored_history = _prepare_conversation(
        request.conversation_id,
        request.app_id,
    )
    _touch_conversation(conversation_id, request.app_id)
    history = _agent_history(stored_history)
    attachments = [item.model_dump(mode="json") for item in request.attachments]
    conversation_summary = _get_conversation_summary(conversation_id, request.app_id)
    try:
        try:
            run_kwargs = {
                "source_types": request.source_types,
                "search_filters": request.filters,
                "workflow_id": request.workflow_id,
                "workflow_version": request.workflow_version,
                "workflow_params": request.workflow_params,
                "tool_context": tool_context,
                "conversation_summary": conversation_summary,
            }
            if attachments:
                run_kwargs["attachments"] = attachments
            result = run_agent(history, request.query, **run_kwargs)
        except TypeError as exc:
            # 兼容旧的外部集成和只接受两个参数的测试替身。
            if "unexpected keyword" not in str(exc):
                raise
            result = run_agent(history, request.query)
    except Exception as exc:
        logger.exception("Agent invocation failed")
        result = {
            "answer": f"处理问题时发生错误：{exc}",
            "citations": [],
            "retrieval_hits": [],
            "graph_relations": [],
            "claims": [],
            "requirements": [],
            "results": [],
            "coverage": None,
            "evidence": {
                "status": "agent_error",
                "requires_evidence": True,
                "reason": str(exc),
                "candidate_count": 0,
                "verified_count": 0,
                "rejected_count": 0,
            },
            "suggestions": [
                {"text": "查看当前知识库", "query": "当前有哪些知识库？"},
            ],
            "trace": {
                "route": "error",
                "used_ragflow": False,
                "model_calls": 0,
                "ragflow_calls": 0,
                "steps": [{
                    "name": "请求处理失败",
                    "detail": str(exc),
                    "status": "error",
                }],
            },
        }

    answer = result["answer"]
    _save_conversation_summary(conversation_id, request.app_id, result)
    user_metadata = {"app_id": request.app_id}
    if attachments:
        user_metadata["attachments"] = attachments
    conversation_store.append(
        conversation_id,
        "user",
        request.query,
        **user_metadata,
    )
    conversation_store.append(
        conversation_id,
        "assistant",
        answer,
        app_id=request.app_id,
        citations=result.get("citations", []),
        retrieval_hits=result.get("retrieval_hits", []),
        graph_relations=result.get("graph_relations", []),
        evidence=result.get("evidence"),
        claims=result.get("claims", []),
        requirements=result.get("requirements", []),
        results=result.get("results", []),
        coverage=result.get("coverage"),
        answer_draft=result.get("answer_draft"),
        suggestions=result.get("suggestions", []),
        trace=result.get("trace"),
        tool_calls=result.get("tool_calls", []),
    )
    _record_application_run(request, result, status="success")
    return AgentResponse(
        conversation_id=conversation_id,
        answer=answer,
        tool_calls=result.get("tool_calls", []),
        citations=result.get("citations", []),
        retrieval_hits=result.get("retrieval_hits", []),
        graph_relations=result.get("graph_relations", []),
        evidence=result.get("evidence"),
        claims=result.get("claims", []),
        requirements=result.get("requirements", []),
        results=result.get("results", []),
        coverage=result.get("coverage"),
        answer_draft=result.get("answer_draft"),
        suggestions=result.get("suggestions", []),
        trace=result.get("trace"),
    )
