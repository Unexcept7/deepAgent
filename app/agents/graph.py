import json
import logging
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any
from uuid import uuid4

from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, Field

from app.agents.evidence import (
    assess_evidence,
    build_safe_answer,
    canonical_identifier,
    extract_aspects,
    extract_identifiers,
    normalize_evidence_mode,
    public_evidence_summary,
    query_requires_evidence,
    validate_answer,
)
from app.agents.conversation_context import build_semantic_context
from app.agents.history_aware import (
    build_clarification_suggestions,
    build_related_question_suggestions,
    has_context_reference,
    resolve_history_aware_query,
)
from app.agents.claims import (
    audit_requirement_plan,
    evaluate_requirements,
    plan_requirements,
    requirement_results_to_claims,
)
from app.agents.answer_composer import (
    compose_answer,
    fallback_answer,
    validate_answer_plan,
)
from app.agents.model import create_model_for_profile, create_model_for_stage, create_qwen_model
from app.agents.prompts import SYSTEM_PROMPT
from app.agents.semantic_router import run_semantic_router
from app.config import get_settings
from app.graph.provenance import normalize_graph_relation_evidence
from app.multimodal import multimodal_content, normalize_attachments
from app.model_registry import plan_model_routing
from app.search.query_plan import build_safe_retrieval_plan
from app.search.budget import search_budget_scope, claim_model_call, search_budget, remaining_timeout
from app.tools.ragflow import RAGFLOW_TOOLS, list_knowledge_bases, search_knowledge_base
from app.tools.unified import unified_search
from app.tool_center.context import ContextThreadPoolExecutor as ThreadPoolExecutor, ExecutionContext, runtime_scope
from app.tool_center.service import ToolCenterService
from app.observability import (
    TraceCallbackHandler,
    current_trace_recorder,
    extract_token_usage,
    summarize_trace_spans,
    trace_scope,
)
from app.routing_policy_engine import evaluate_routing_policy, routing_policy_catalog
from app.semantic_prompt_store import get_semantic_prompt_store
from app.workflow_store import WorkflowError, get_workflow_store


logger = logging.getLogger(__name__)


class AgentAnswer(BaseModel):
    answer: str = Field(description="给用户的最终中文回答")
    used_evidence_ids: list[str] = Field(
        default_factory=list,
        description="本答案实际使用的 RAGFlow 证据 ID；没有使用证据时为空",
    )
    suggested_questions: list[str] = Field(
        default_factory=list,
        description="0 到 4 个适合继续点击提问的问题",
    )


MAX_RETRIEVAL_HITS = 12
MAX_RETRIEVAL_CONTENT_LENGTH = 5000
MAX_PREFETCH_ENTITIES = 4


def _model_profile_id(model_routing: dict[str, Any], stage: str) -> str | None:
    return str(
        ((model_routing.get("stages") or {}).get(stage) or {}).get("profile_id") or ""
    ) or None


def _create_stage_model(
    stage: str,
    *,
    query: str = "",
    attachments: list[dict[str, Any]] | None = None,
    complexity: dict[str, Any] | None = None,
    profile_id: str | None = None,
):
    """Use registry routing when enabled, otherwise preserve the legacy seam."""

    if not bool(getattr(get_settings(), "model_routing_enabled", False)):
        return create_qwen_model()
    return create_model_for_stage(
        stage,
        query=query,
        attachments=attachments,
        complexity=complexity,
        profile_id=profile_id,
    )

PREFETCH_ASPECT_LABELS = {
    "function_code": "功能码",
    "register_address": "寄存器地址",
    "message_frame": "报文格式",
    "channels_io": "数字量输入输出能力",
    "channels_generic": "通道数 采集模块 对应手册",
    "communication_parameters": "通信参数",
    "electrical": "电气参数",
    "performance": "性能参数",
    "physical_environment": "环境与安装参数",
    "version": "版本信息",
    "manufacturer": "厂商信息",
    "document_presence": "说明书和技术资料",
    "protocol_interface": "通信协议与接口",
}


EXCLUDED_DEEPAGENT_TOOLS = frozenset({
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "delete",
    "glob",
    "grep",
    "execute",
    "task",
    "write_todos",
})


class RetrievalGuardMiddleware(AgentMiddleware):
    """Block empty/duplicate searches while allowing valid multi-searches."""

    def __init__(self, evidence_mode: str | None = None) -> None:
        # The effective mode belongs to the selected application/workflow.
        # Keeping it on the middleware instance prevents a concurrent run from
        # accidentally inheriting the process-wide .env setting.
        self.evidence_mode = normalize_evidence_mode(
            evidence_mode
            if evidence_mode is not None
            else getattr(get_settings(), "evidence_mode", "generic")
        )

    @staticmethod
    def _tool_name(tool: Any) -> str:
        if isinstance(tool, dict):
            return str(tool.get("name") or "")
        return str(getattr(tool, "name", "") or "")

    def wrap_model_call(self, request, handler):
        """Stop the retrieval loop after the configured successful searches.

        A guarded ToolMessage alone is not always enough to stop an LLM agent:
        the model may keep inventing a new query after being told that a search
        was blocked. Once the useful retrieval budget is consumed, remove the
        retrieval tool from the next model request and let the model produce
        the structured answer from the evidence already in state.
        """
        claim_model_call()
        if search_budget.get() is not None and hasattr(request, "model_settings"):
            request = request.override(model_settings={**request.model_settings, "timeout": remaining_timeout(60)})
        completed_searches = 0
        for message in (request.state or {}).get("messages", []):
            if not isinstance(message, ToolMessage):
                continue
            if message.name != "search_knowledge_base":
                continue
            try:
                payload = json.loads(message.content)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not payload.get("guarded"):
                completed_searches += 1

        settings = get_settings()
        max_search_calls = max(
            1,
            min(int(getattr(settings, "max_ragflow_calls", 1)), 4),
        )
        if completed_searches < max_search_calls:
            return handler(request)

        filtered_tools = [
            tool
            for tool in request.tools
            if self._tool_name(tool) != "search_knowledge_base"
        ]
        if len(filtered_tools) == len(request.tools):
            return handler(request)
        return handler(request.override(tools=filtered_tools))

    @staticmethod
    def _latest_user_text(state: dict[str, Any]) -> str:
        def text_content(content: Any) -> str:
            if isinstance(content, list):
                return " ".join(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ).strip()
            return str(content or "")

        for message in reversed((state or {}).get("messages", [])):
            if isinstance(message, dict) and message.get("role") == "user":
                return text_content(message.get("content"))
            if getattr(message, "type", None) == "human":
                return text_content(getattr(message, "content", ""))
        return ""

    @staticmethod
    def _call_key(tool_call: dict[str, Any]) -> str:
        args = tool_call.get("args") or {}
        # Changing top_k must not bypass duplicate-query protection.
        return str(args.get("query") or "").strip().casefold()

    def wrap_tool_call(self, request, handler):
        tool_call = request.tool_call
        tool_name = tool_call.get("name")
        if tool_name == "list_knowledge_bases":
            user_text = self._latest_user_text(request.state or {})
            explicitly_requests_list = (
                "知识库" in user_text
                and any(term in user_text for term in ("哪些", "名称", "列表", "id", "信息"))
            )
            if explicitly_requests_list:
                already_called = any(
                    getattr(message, "name", None) == "list_knowledge_bases"
                    for message in (request.state or {}).get("messages", [])
                )
                if not already_called:
                    return handler(request)
                reason = "知识库列表已经读取过；请直接使用已有列表结果回答。"
            else:
                reason = "当前问题不是知识库列表查询；请使用 search_knowledge_base。"
            return ToolMessage(
                content=json.dumps({
                    "guarded": True,
                    "datasets": [],
                    "total": 0,
                    "error": reason,
                }, ensure_ascii=False),
                name=tool_name,
                tool_call_id=str(tool_call.get("id") or "guarded-dataset-list"),
            )

        if tool_name != "search_knowledge_base":
            return handler(request)

        query = self._call_key(tool_call)
        previous_keys: set[str] = set()
        previous_search_count = 0
        current_call_id = str(tool_call.get("id") or "")
        for message in (request.state or {}).get("messages", []):
            if not isinstance(message, AIMessage):
                continue
            for previous_call in message.tool_calls or []:
                if previous_call.get("name") != "search_knowledge_base":
                    continue
                # The current model message is already present in request.state
                # when wrap_tool_call runs. Do not treat that same call as a
                # historical duplicate.
                if str(previous_call.get("id") or "") == current_call_id:
                    continue
                previous_search_count += 1
                previous_keys.add(self._call_key(previous_call))

        latest_user_text = self._latest_user_text(request.state or {})
        requested_entities = extract_identifiers(latest_user_text)
        evidence_mode = self.evidence_mode
        settings = get_settings()
        configured_limit = max(
            1,
            min(int(getattr(settings, "max_ragflow_calls", 1)), 4),
        )
        max_search_calls = (
            min(configured_limit, max(1, min(4, len(requested_entities) or 1)))
            if evidence_mode == "industrial_strict"
            else configured_limit
        )

        if not query:
            reason = "检索 query 不能为空；请直接根据已有工具结果回答。"
        elif query in previous_keys:
            reason = "相同检索词已经执行过；请使用上一条检索结果，不要重复检索。"
        elif previous_search_count >= max_search_calls:
            reason = "本轮已达到最大独立检索次数；请基于已有检索结果回答。"
        else:
            return handler(request)

        return ToolMessage(
            content=json.dumps({
                "guarded": True,
                "query": query,
                "chunks": [],
                "total": 0,
                "error": reason,
            }, ensure_ascii=False),
            name="search_knowledge_base",
            tool_call_id=str(tool_call.get("id") or "guarded-search"),
        )

def _is_dataset_list_query(query: str) -> bool:
    normalized = query.casefold()
    return "知识库" in query and any(
        term in normalized for term in ("哪些", "名称", "列表", "id", "信息")
    )


def _is_business_action_query(query: str) -> bool:
    """Narrow operational route; knowledge questions still force retrieval."""
    if any(term in query for term in ("是什么", "含义", "原理", "手册", "说明书", "依据", "诊断", "原因", "知识库", "怎么")):
        return False
    return any(term in query for term in ("实时状态", "当前状态", "历史报警", "报警记录", "工单草稿", "提交工单", "创建工单", "提交维修工单", "修改设备参数"))


@lru_cache(maxsize=32)
def get_agent(
    search_limit: int = 1,
    tool_mode: str = "search",
    evidence_mode: str | None = None,
    model_profile_id: str | None = None,
):
    register_harness_profile(
        "openai",
        HarnessProfile(excluded_tools=EXCLUDED_DEEPAGENT_TOOLS),
    )
    if tool_mode == "list":
        tools = [RAGFLOW_TOOLS[1]]
    elif tool_mode == "none":
        tools = []
    elif tool_mode == "all":
        # ``all`` is用于管理后台/高级 Agent 的完整只读检索能力。生产总控
        # 默认仍使用后端预取来保证证据门控；统一搜索工具可以在自主规划
        # 场景中一次性聚合文档、QA、图谱和业务 API。
        tools = [*RAGFLOW_TOOLS, unified_search]
    else:
        # 统一搜索是总控 Agent 的首选入口；保留原始 RAGFlow 工具用于
        # 兼容旧版提示词、证据回溯和测试替身。
        tools = [unified_search, RAGFLOW_TOOLS[0]]
    return create_deep_agent(
        model=create_model_for_profile(model_profile_id) if model_profile_id else create_qwen_model(),
        tools=tools,
        middleware=[RetrievalGuardMiddleware(evidence_mode=evidence_mode)],
        system_prompt=SYSTEM_PROMPT,
        name="knowledge_assistant",
    )


def _prefetch_multi_entity_messages(query: str, *, top_k: int = 8) -> list[Any]:
    """Retrieve each explicit entity before claim extraction and judging.

    Splitting a comparison into one retrieval call per product prevents a
    first product's chunks from being incorrectly presented as evidence for
    every requested product.
    """
    identifiers = extract_identifiers(query)
    if len(identifiers) <= 1:
        return []

    aspects = extract_aspects(query)
    if "channels_io" in aspects:
        aspects = [aspect for aspect in aspects if aspect != "channels_generic"]
    residual = " ".join(PREFETCH_ASPECT_LABELS.get(item, item) for item in aspects)
    if "批量" in query:
        residual = f"{residual} 批量读写".strip()
    if not residual:
        residual = query
        for identifier in identifiers:
            residual = residual.replace(identifier, " ")
        residual = residual.replace("/", " ")
        residual = residual.replace("、", " ").replace("，", " ").replace(",", " ")
        residual = residual.replace("比较", " ").replace("对比", " ")
        residual = residual.replace("和", " ").replace("与", " ").replace("及", " ")
        residual = residual.replace("的", " ")
        residual = residual.replace("。", " ").replace("？", " ").replace("?", " ")
        residual = " ".join(residual.split()).strip(" 、，,和与及")[:160]
    residual = residual or "相关技术参数"

    entity_queries = [
        f"{identifier} {residual}".strip()
        for identifier in identifiers[:MAX_PREFETCH_ENTITIES]
    ]

    def retrieve(entity_query: str) -> str:
        try:
            content = search_knowledge_base.invoke({
                "query": entity_query,
                "top_k": max(1, min(int(top_k), 50)),
            })
        except Exception as exc:
            content = json.dumps({
                "error": str(exc),
                "query": entity_query,
                "chunks": [],
                "total": 0,
            }, ensure_ascii=False)

        return str(content)

    # Independent product searches are safe to run concurrently and avoid
    # multiplying the latency of a comparison by the number of products.
    with ThreadPoolExecutor(max_workers=len(entity_queries)) as executor:
        retrieved_contents = list(executor.map(retrieve, entity_queries))

    tool_calls: list[dict[str, Any]] = []
    tool_messages: list[ToolMessage] = []
    for index, (entity_query, content) in enumerate(
        zip(entity_queries, retrieved_contents),
        start=1,
    ):
        tool_call_id = f"prefetch-search-{index}"
        tool_calls.append({
            "name": "search_knowledge_base",
                "args": {"query": entity_query, "top_k": max(1, min(int(top_k), 50))},
            "id": tool_call_id,
            "type": "tool_call",
        })
        tool_messages.append(ToolMessage(
            name="search_knowledge_base",
            tool_call_id=tool_call_id,
            content=str(content),
        ))

    if not tool_calls:
        return []
    return [
        AIMessage(
            content="",
            tool_calls=tool_calls,
            additional_kwargs={"synthetic_prefetch": True},
        ),
        *tool_messages,
    ]


def _prefetch_single_query_messages(query: str, *, top_k: int = 8) -> list[Any]:
    """Force one compact RAGFlow retrieval before factual claim processing."""
    search_query = " ".join(query.split())

    tool_call_id = "prefetch-search-1"
    try:
        content = search_knowledge_base.invoke({
            "query": search_query,
            "top_k": max(1, min(int(top_k), 50)),
        })
    except Exception as exc:
        content = json.dumps({
            "error": str(exc),
            "query": search_query,
            "chunks": [],
            "total": 0,
        }, ensure_ascii=False)
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "search_knowledge_base",
                "args": {"query": search_query, "top_k": max(1, min(int(top_k), 50))},
                "id": tool_call_id,
                "type": "tool_call",
            }],
            additional_kwargs={"synthetic_prefetch": True},
        ),
        ToolMessage(
            name="search_knowledge_base",
            tool_call_id=tool_call_id,
            content=str(content),
        ),
    ]


def _prefetch_requirement_messages(
    requirements: list[dict[str, Any]],
    *,
    max_workers: int = 4,
    top_k: int = 8,
) -> list[Any]:
    """Retrieve evidence independently for every planned user requirement."""
    if not requirements:
        return []

    def retrieve(requirement: dict[str, Any]) -> str:
        requirement_id = str(requirement.get("id") or "")
        retrieval_query = str(requirement.get("retrieval_query") or "").strip()
        try:
            content = search_knowledge_base.invoke({
                "query": retrieval_query,
                "top_k": max(1, min(int(top_k), 50)),
            })
            payload = json.loads(str(content))
            if not isinstance(payload, dict):
                raise ValueError("RAGFlow 工具返回值不是 JSON 对象")
            payload["requirement_id"] = requirement_id
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({
                "error": str(exc),
                "query": retrieval_query,
                "requirement_id": requirement_id,
                "chunks": [],
                "total": 0,
            }, ensure_ascii=False)

    worker_count = max(1, min(int(max_workers), len(requirements), 8))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        contents = list(executor.map(retrieve, requirements))

    tool_calls: list[dict[str, Any]] = []
    tool_messages: list[ToolMessage] = []
    for index, (requirement, content) in enumerate(
        zip(requirements, contents),
        start=1,
    ):
        tool_call_id = f"prefetch-requirement-{index}"
        retrieval_query = str(requirement.get("retrieval_query") or "")
        tool_calls.append({
            "name": "search_knowledge_base",
            "args": {"query": retrieval_query, "top_k": max(1, min(int(top_k), 50))},
            "id": tool_call_id,
            "type": "tool_call",
        })
        tool_messages.append(ToolMessage(
            name="search_knowledge_base",
            tool_call_id=tool_call_id,
            content=content,
        ))

    return [
        AIMessage(
            content="",
            tool_calls=tool_calls,
            additional_kwargs={"synthetic_prefetch": True},
        ),
        *tool_messages,
    ]


def _prefetch_dataset_list_messages(tool_context=None) -> list[Any]:
    """Force the dataset-list tool for explicit knowledge-base list questions."""
    tool_call_id = "prefetch-dataset-list-1"
    try:
        if tool_context:
            from app.tool_center.service import ToolCenterService
            content = json.dumps(ToolCenterService().list_datasets(tool_context), ensure_ascii=False)
        else:
            content = list_knowledge_bases.invoke({})
    except Exception as exc:
        content = json.dumps({
            "error": str(exc),
            "datasets": [],
            "total": 0,
        }, ensure_ascii=False)
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "list_knowledge_bases",
                "args": {},
                "id": tool_call_id,
                "type": "tool_call",
            }],
            additional_kwargs={"synthetic_prefetch": True},
        ),
        ToolMessage(
            name="list_knowledge_bases",
            tool_call_id=tool_call_id,
            content=str(content),
        ),
    ]


def _prefetch_unified_search_messages(
    query: str,
    *,
    source_types: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    top_k: int = 12,
    query_plan: dict[str, Any] | None = None,
) -> list[Any]:
    """用统一搜索服务生成一组可供 Agent 消费的合成工具消息。

    统一搜索的 hits 会在后处理阶段转换为证据片段，因此图谱、QA 和
    业务 API 的结果可以和传统 RAGFlow chunk 一样参与引用与证据校验。
    """
    tool_call_id = "prefetch-unified-search-1"
    try:
        content = unified_search.invoke({
            "query": query,
            "source_types": source_types if source_types is not None else None,
            "top_k": max(1, min(int(top_k), 50)),
            "filters": filters or {},
            "query_plan": query_plan or {},
        })
    except Exception as exc:
        content = json.dumps({"query": query, "hits": [], "errors": [str(exc)]}, ensure_ascii=False)
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "unified_search",
                "args": {
                    "query": query,
                    "source_types": source_types if source_types is not None else None,
                    "top_k": max(1, min(int(top_k), 50)),
                    "filters": filters or {},
                    "query_plan": query_plan or {},
                },
                "id": tool_call_id,
                "type": "tool_call",
            }],
            additional_kwargs={"synthetic_prefetch": True},
        ),
        ToolMessage(
            name="unified_search",
            tool_call_id=tool_call_id,
            content=str(content),
        ),
    ]


def _workflow_condition_matches(expression: Any, confirmation: bool) -> bool:
    normalized = " ".join(str(expression or "").casefold().split())
    if not normalized or normalized == "true":
        return True
    if normalized == "false":
        return False
    if normalized == "requires_confirmation == true":
        return confirmation
    if normalized == "requires_confirmation == false":
        return not confirmation
    return False


def _workflow_branch_allowed(workflow: dict, node_id: str, confirmation: bool, visited: frozenset = frozenset()) -> bool:
    if node_id in visited:
        return False
    node = next((item for item in workflow.get("nodes", []) if str(item.get("id")) == node_id), None)
    if not node or not node.get("enabled", True) or not _workflow_condition_matches(node.get("condition"), confirmation):
        return False
    incoming = [edge for edge in workflow.get("edges", []) if str(edge.get("target")) == node_id]
    return not incoming or any(
        edge.get("enabled", True)
        and _workflow_condition_matches(edge.get("condition"), confirmation)
        and _workflow_branch_allowed(workflow, str(edge.get("source")), confirmation, visited | {node_id})
        for edge in incoming
    )


def _prefetch_parallel_workflow_messages(
    query: str,
    workflow: dict[str, Any] | None,
    *,
    default_source_types: list[str],
    filters: dict[str, Any] | None,
    top_k: int,
    callback: Callable[[dict[str, Any]], None] | None,
    workflow_run_id: str | None,
    query_plan: dict[str, Any] | None = None,
) -> list[Any]:
    """Execute configured parallel search branches concurrently.

    A parallel canvas node is meaningful only when its outgoing edges point to
    executable search/API nodes.  Each branch keeps its own source scope and
    result message, while the downstream evidence gate still sees one ordered
    message stream.
    """
    if not workflow:
        return []
    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    confirmation = any(term in query for term in ("提交", "写入", "创建工单", "导入", "删除", "执行动作", "发布"))
    parallel_nodes = [item for item in nodes if item.get("type") == "parallel" and item.get("enabled", True) and _workflow_branch_allowed(workflow, str(item.get("id")), confirmation)]
    if not any(item.get("type") == "parallel" and item.get("enabled", True) for item in nodes):
        return []
    by_id = {str(item.get("id")): item for item in nodes}
    branches: list[dict[str, Any]] = []
    for parallel in parallel_nodes:
        for edge in edges:
            if edge.get("source") != parallel.get("id") or edge.get("enabled", True) is False:
                continue
            target = by_id.get(str(edge.get("target")))
            if target and target.get("enabled", True) and target not in branches and _workflow_condition_matches(edge.get("condition"), confirmation) and _workflow_branch_allowed(workflow, str(target.get("id")), confirmation):
                branches.append(target)
    if not branches:
        return [ToolMessage(name="unified_search", tool_call_id="conditional-search-skipped", content=json.dumps({"query": query, "hits": [], "warnings": ["搜索分支条件未满足，已跳过"]}, ensure_ascii=False))]

    _emit_workflow_node(
        callback,
        workflow,
        workflow_run_id,
        aliases=(str(parallel_nodes[0].get("id") or "parallel"),),
        status="running",
        detail=f"并行分支启动：{len(branches)} 个",
    )

    def retrieve(branch: dict[str, Any]) -> tuple[dict[str, Any], str]:
        params = dict(branch.get("params") or {})
        configured_branch_sources = params.get("source_types")
        branch_sources = [
            str(item)
            for item in (
                configured_branch_sources
                if configured_branch_sources is not None
                else default_source_types
            )
        ]
        branch_top_k = _bounded_int(params.get("top_k") or top_k, top_k, 1, 50)
        try:
            content = unified_search.invoke({
                "query": query,
                "source_types": branch_sources,
                "top_k": branch_top_k,
                "filters": filters or {},
                "query_plan": query_plan or {},
            })
        except Exception as exc:
            content = json.dumps({"query": query, "hits": [], "errors": [str(exc)]}, ensure_ascii=False)
        return branch, str(content)

    worker_limit = _bounded_int(
        (workflow.get("params") or {}).get("max_parallel", 8),
        8,
        1,
        8,
    )
    worker_count = max(1, min(len(branches), worker_limit))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        branch_results = list(executor.map(retrieve, branches))

    tool_calls: list[dict[str, Any]] = []
    tool_messages: list[ToolMessage] = []
    for index, (branch, content) in enumerate(branch_results, start=1):
        tool_call_id = f"prefetch-parallel-{index}"
        tool_calls.append({
            "name": "unified_search",
            "args": {"query": query, "source_types": ((branch.get("params") or {}).get("source_types") if (branch.get("params") or {}).get("source_types") is not None else default_source_types), "top_k": (branch.get("params") or {}).get("top_k") or top_k, "filters": filters or {}, "query_plan": query_plan or {}},
            "id": tool_call_id,
            "type": "tool_call",
        })
        tool_messages.append(ToolMessage(name="unified_search", tool_call_id=tool_call_id, content=content))
        payload = _parse_tool_payload(content)
        _emit_workflow_node(
            callback,
            workflow,
            workflow_run_id,
            aliases=(str(branch.get("id") or ""),),
            status="success" if not payload.get("errors") else "error",
            detail=f"分支命中 {len(payload.get('hits') or [])} 条" + (f"；{';'.join(str(item) for item in payload.get('errors') or [])}" if payload.get("errors") else ""),
        )
    _emit_workflow_node(
        callback,
        workflow,
        workflow_run_id,
        aliases=(str(parallel_nodes[0].get("id") or "parallel"),),
        status="success" if tool_messages else "error",
        detail=f"并行分支完成：{len(tool_messages)} 个",
    )
    return [
        AIMessage(
            content="",
            tool_calls=tool_calls,
            additional_kwargs={"synthetic_prefetch": True, "workflow_parallel": True},
        ),
        *tool_messages,
    ]


def _emit_stream_event(
    callback: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
) -> None:
    if callback is not None:
        callback(event)


def _build_agent_messages(
    history: list[dict[str, Any]],
    retrieval_query: str,
    normalized_attachments: list[dict[str, Any]],
    prefetch_messages: list[Any] | None = None,
) -> list[Any]:
    """Build provider-safe messages for the final Agent invocation.

    ``create_deep_agent`` owns the system prompt.  The message list passed to
    it must therefore contain only user/assistant history followed by any
    synthetic tool messages; appending another system message later breaks
    OpenAI-compatible providers that require system messages at the start.
    """
    messages: list[Any] = []
    for item in history:
        role = item.get("role")
        if role not in {"user", "assistant"}:
            logger.warning("Ignoring unsupported historical message role: %r", role)
            continue
        content = str(item.get("content") or "")
        prior_attachments: list[dict[str, Any]] = []
        try:
            prior_attachments = normalize_attachments(item.get("attachments") or [])
        except ValueError:
            logger.warning("Ignoring invalid historical image attachment")
        messages.append({
            "role": role,
            "content": multimodal_content(content, prior_attachments),
        })
    messages.append({
        "role": "user",
        "content": multimodal_content(retrieval_query, normalized_attachments),
    })
    messages.extend(prefetch_messages or [])
    return messages


def _workflow_node(
    workflow: dict[str, Any] | None,
    *aliases: str,
) -> dict[str, Any] | None:
    """Resolve a semantic runtime stage to a configurable canvas node."""
    if not workflow:
        return None
    nodes = workflow.get("nodes") or []
    for alias in aliases:
        for node in nodes:
            if str(node.get("id") or "") == alias or str(node.get("type") or "") == alias:
                return node
    return None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return max(minimum, min(default, maximum))


def _resolve_similarity_threshold(
    workflow_params: dict[str, Any],
    *,
    settings: Any,
    workflow: dict[str, Any] | None,
    policy_actions: dict[str, Any] | None = None,
) -> float:
    """Resolve the configured threshold without replacing an app value.

    Application/workflow parameters are explicit runtime policy. A routing
    policy threshold is only a fallback for runs that do not provide one.
    Similarity remains a ranking/diagnostic signal; this helper only resolves
    the value passed to the evidence assessment layer.
    """
    configured = workflow_params.get("similarity_threshold")
    has_explicit_value = configured is not None
    fallback = (
        getattr(settings, "evidence_generic_min_similarity", 0.0)
        if workflow
        else 0.0
    )
    value = configured if has_explicit_value else fallback
    try:
        threshold = max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        threshold = 0.0
    if not has_explicit_value:
        try:
            threshold = max(
                threshold,
                min(float((policy_actions or {}).get("similarity_threshold", 0.0)), 1.0),
            )
        except (TypeError, ValueError):
            pass
    return threshold


def _should_expand_evidence_retrieval(
    assessment: dict[str, Any],
    *,
    requirement_count: int = 0,
    missing_requirements: list[dict] | None = None,
) -> bool:
    """决定首轮候选是否需要扩大，不把 Top-K 缩减变成准确率风险。"""
    if missing_requirements:
        return True
    if not assessment:
        return True
    status = str(assessment.get("status") or "").strip().lower()
    if status in {"retrieval_error", "not_found", "partial", "conflict"}:
        return True
    verified_count = int(assessment.get("verified_count") or 0)
    if verified_count <= 0:
        return True
    if requirement_count > 1 and verified_count < min(requirement_count, 2):
        return True
    return False


def _missing_retrieval_requirements(messages: list[Any], requirements: list[dict], evidence_mode: str) -> list[dict]:
    return [
        requirement for requirement in requirements
        if _summarize_evidence(messages, str(requirement.get("retrieval_query") or ""), evidence_mode="industrial_strict" if evidence_mode == "generic" else evidence_mode).get("status") != "supported"
    ]


def _emit_workflow_node(
    callback: Callable[[dict[str, Any]], None] | None,
    workflow: dict[str, Any] | None,
    workflow_run_id: str | None,
    *,
    aliases: tuple[str, ...],
    status: str,
    detail: str,
    duration_ms: int | None = None,
) -> None:
    node = _workflow_node(workflow, *aliases)
    if node is None:
        return
    event: dict[str, Any] = {
        "type": "workflow_node",
        "workflow_id": workflow.get("workflow_id") or workflow.get("id"),
        "workflow_version": workflow.get("version"),
        "workflow_run_id": workflow_run_id,
        "node_id": node.get("id"),
        "node_name": node.get("name") or node.get("id"),
        "node_type": node.get("type"),
        "status": status,
        "detail": detail,
    }
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    recorder = current_trace_recorder()
    if recorder is not None:
        recorder.record_workflow_node(event)
    _emit_stream_event(callback, event)


def _message_content_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return ""


def _merge_stream_update(state: dict[str, Any], update: Any) -> None:
    """Merge LangGraph update chunks into the final state used by run_agent."""
    if not isinstance(update, dict):
        return

    # ``updates`` mode normally has the shape {node_name: {state_key: value}}.
    # Accept a flat state update as well for compatibility with test doubles and
    # different LangGraph versions.
    nested_updates = [
        value for value in update.values()
        if isinstance(value, dict)
    ]
    updates = nested_updates or [update]
    for node_update in updates:
        for key, value in node_update.items():
            if key == "messages":
                if isinstance(value, (list, tuple)):
                    state.setdefault("messages", []).extend(value)
                elif value is not None:
                    state.setdefault("messages", []).append(value)
            else:
                state[key] = value


def _stream_agent(
    agent: Any,
    invoke_input: dict[str, Any],
    settings: Any,
    callback: Callable[[dict[str, Any]], None],
    *,
    publish_tokens: bool = True,
) -> dict[str, Any]:
    """Stream LangGraph updates/messages while reconstructing invoke-like state."""
    state: dict[str, Any] = {
        "messages": list(invoke_input.get("messages") or []),
    }
    config = {
        "recursion_limit": max(
            2,
            int(getattr(settings, "max_agent_steps", 6)),
        )
    }
    if current_trace_recorder() is not None:
        config["callbacks"] = [TraceCallbackHandler()]
    callback({"type": "agent_started"})

    stream = agent.stream(
        invoke_input,
        config=config,
        stream_mode=["updates", "messages"],
    )
    for item in stream:
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and item[0] in {"updates", "messages", "values"}
        ):
            mode, chunk = item
        else:
            mode, chunk = "updates", item

        if mode == "updates":
            _merge_stream_update(state, chunk)
            nodes = list(chunk.keys()) if isinstance(chunk, dict) else []
            callback({
                "type": "node_update",
                "nodes": [str(node) for node in nodes],
            })
        elif mode == "messages":
            message = chunk[0] if isinstance(chunk, tuple) and chunk else chunk
            text = _message_content_text(message)
            if text and publish_tokens:
                callback({"type": "token", "content": text})

    return state


def _try_semantic_route(
    query: str,
    routing_policy: dict[str, Any],
    recorder: Any | None,
    *,
    semantic_context: dict[str, Any] | None = None,
    route_catalog: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    model_profile_id: str | None = None,
) -> dict[str, Any] | None:
    """Use the published semantic Prompt as the classifier and rules as guards.

    Published route policies still provide the vocabulary, source defaults and
    permission boundary, but a single deterministic keyword match no longer
    prevents the LLM from seeing the full conversation context.  If the model
    is unavailable or fails validation, the existing rule result remains the
    safe fallback.
    """
    try:
        prompt = get_semantic_prompt_store().active()
    except Exception as exc:  # pragma: no cover - storage failure is non-fatal
        logger.warning("Semantic prompt lookup failed: %s", exc)
        return None
    if not prompt:
        return None
    context_payload = dict((semantic_context or {}).get("prompt_context") or {})
    context_diagnostics = (semantic_context or {}).get("diagnostics") or {}
    routing_policy["semantic_context"] = {
        "conversation_summary": context_payload.get("conversation_summary") or {},
        "recent_turns": context_payload.get("recent_turns") or [],
        "dictionary_hints": context_payload.get("dictionary_hints") or [],
        "route_catalog": route_catalog or [],
        "context_priority": context_payload.get("context_priority") or [],
        "diagnostics": context_diagnostics,
        "next_summary": (semantic_context or {}).get("next_summary") or {},
    }
    semantic_started_at = time.perf_counter()
    try:
        try:
            semantic_kwargs = {
                "context": context_payload,
                "route_catalog": route_catalog or [],
            }
            if attachments:
                semantic_kwargs["attachments"] = attachments
            if model_profile_id:
                semantic_kwargs["model_profile_id"] = model_profile_id
            result = run_semantic_router(query, prompt, **semantic_kwargs)
        except TypeError as exc:
            # Keep older test doubles and external integrations that still
            # expose run_semantic_router(query, prompt) compatible.
            if "unexpected keyword" not in str(exc):
                raise
            result = run_semantic_router(query, prompt)
    except Exception as exc:  # pragma: no cover - model connectivity is non-fatal
        logger.warning("Semantic router failed; keeping deterministic route: %s", exc)
        duration_ms = round((time.perf_counter() - semantic_started_at) * 1000)
        failure = {
            "prompt_id": prompt.get("prompt_id") or prompt.get("id"),
            "version": prompt.get("version"),
            "model_profile_id": model_profile_id,
            "model": prompt.get("model"),
            "provider": "ChatOpenAI",
            "duration_ms": duration_ms,
            "token_usage": {},
            "error": str(exc),
        }
        routing_policy["semantic_router"] = failure
        if recorder is not None:
            recorder.record_llm_stage(
                "意图和实体识别",
                duration_ms=duration_ms,
                model=prompt.get("model"),
                provider="ChatOpenAI",
                status="error",
                error=str(exc),
                input_chars=len(query) + len(json.dumps(context_payload, ensure_ascii=False)),
            )
        routing_policy["semantic_warning"] = f"语义路由未执行，已回退规则：{exc}"
        return None

    summary = {
        key: result.get(key)
        for key in (
            "prompt_id", "version", "model_profile_id", "model", "provider", "duration_ms",
            "token_usage", "result", "error",
            "context_chars", "input_chars", "route_catalog_count",
        )
        if result.get(key) is not None
    }
    routing_policy["semantic_router"] = summary
    if recorder is not None:
        recorder.record_llm_stage(
            "意图和实体识别",
            duration_ms=int(round(float(result.get("duration_ms") or 0))),
            model=result.get("model"),
            provider=result.get("provider"),
            usage=result.get("token_usage") or {},
            status="error" if result.get("error") else "success",
            error=result.get("error"),
            input_chars=int(result.get("input_chars") or len(query)),
            output_chars=len(str(result.get("raw_output") or "")),
        )
    if result.get("error"):
        routing_policy["semantic_warning"] = f"语义输出未通过后端校验：{result['error']}"
        return summary

    prediction = result.get("result") or {}
    intents = prediction.get("intents") or []
    entities = prediction.get("entities") or []
    routing_policy["semantic_intents"] = intents
    routing_policy["semantic_entities"] = entities
    routing_policy["semantic_retrieval_plan"] = prediction.get("retrieval_plan") or {}
    if semantic_context is not None:
        next_summary = semantic_context.setdefault("next_summary", {})
        candidate_entities = list(next_summary.get("candidate_entities") or [])
        known_entity_keys = {
            str(item.get("normalized_value") or item.get("value") or "").casefold()
            for item in candidate_entities
        }
        for entity in entities:
            value = str(entity.get("value") or "").strip()
            if not value:
                continue
            normalized = str(entity.get("normalized_value") or value).strip()
            user_text = "\n".join([query, *[str(item.get("content") or "") for item in context_payload.get("recent_turns", []) if item.get("role") == "user"]])
            if value.casefold() not in user_text.casefold() or normalized.casefold() != value.casefold():
                continue
            key = normalized.casefold()
            if key in known_entity_keys:
                continue
            candidate_entities.append({
                "value": value,
                "normalized_value": normalized,
                "source": entity.get("source") or "semantic_model",
                "entity_type": entity.get("entity_type") or "semantic_entity",
                "confidence": float(entity.get("confidence") or 0),
            })
            known_entity_keys.add(key)
        next_summary["candidate_entities"] = candidate_entities[-20:]
        if context_payload.get("conversation_summary") is not next_summary:
            context_payload["conversation_summary"] = next_summary
    routing_policy["semantic_intent_queries"] = [
        str(item.get("query") or query).strip()
        for item in intents
        if str(item.get("query") or query).strip()
    ]
    routing_policy["semantic_requires_evidence"] = any(
        bool(item.get("requires_evidence")) for item in intents
    )
    routing_policy["semantic_requires_confirmation"] = any(
        bool(item.get("requires_confirmation")) for item in intents
    )
    routes = {str(item.get("route") or "") for item in intents if item.get("route")}
    if prediction.get("needs_clarification") or len(intents) == 0:
        routing_policy["semantic_needs_clarification"] = True
        routing_policy["semantic_warning"] = prediction.get("ambiguity") or "语义路由未形成唯一意图，继续使用规则结果"
        return summary
    if len(routes) > 1:
        routing_policy["semantic_routes"] = sorted(routes)
        routing_policy["semantic_warning"] = "检测到多个不同路由意图；本轮保留全部识别结果，但暂不自动跨路由执行"
        return summary

    semantic_route = next(iter(routes), "")
    if semantic_route not in {"knowledge_qa", "business_query", "business_action", "casual", "dataset_list"}:
        routing_policy["semantic_warning"] = f"未知语义路由 {semantic_route}，继续使用规则结果"
        return summary
    if routing_policy.get("policy_id"):
        routing_policy["rule_selection"] = {
            key: routing_policy.get(key)
            for key in ("policy_id", "policy_name", "version", "route", "matched_count")
        }
    rule_route = str(routing_policy.get("route") or "unknown")
    rule_actions = dict(routing_policy.get("actions") or {})
    routing_policy["policy_id"] = None
    routing_policy["policy_name"] = f"语义 Prompt：{prompt.get('name') or prompt.get('prompt_id')}"
    routing_policy["version"] = prompt.get("version")
    routing_policy["route"] = semantic_route
    routing_policy["route_origin"] = "semantic_model"
    routing_policy["rule_detected_entity_types"] = list(routing_policy.get("detected_entity_types") or [])
    routing_policy["detected_entity_types"] = list(dict.fromkeys(
        str(item.get("entity_type"))
        for item in entities
        if item.get("entity_type")
    ))
    # A rule action may be reused only when it describes the same route.  A
    # keyword match for a business action must never leak its write-oriented
    # parameters into an LLM-selected knowledge route.
    actions = rule_actions if rule_route == semantic_route else {}
    actions["route"] = semantic_route
    if semantic_route == "knowledge_qa":
        actions["require_evidence"] = True
    elif semantic_route in {"business_query", "casual", "dataset_list"}:
        actions.setdefault("require_evidence", False)
    elif semantic_route == "business_action":
        actions["human_confirmation"] = True
    if routing_policy.get("semantic_requires_confirmation"):
        actions["human_confirmation"] = True
    routing_policy["actions"] = actions
    routing_policy["reasons"] = [
        f"大模型语义识别：{', '.join(str(item.get('intent_id')) for item in intents)}",
        *(routing_policy.get("reasons") or []),
    ]
    return summary


@trace_scope
@runtime_scope
@search_budget_scope
def run_agent(
    history: list[dict[str, Any]],
    query: str,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    source_types: list[str] | None = None,
    search_filters: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    workflow_version: int | None = None,
    workflow_params: dict[str, Any] | None = None,
    tool_context: ExecutionContext | None = None,
    conversation_summary: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    recorder = current_trace_recorder()
    if recorder is not None:
        recorder.set_callback(on_event)
    _emit_stream_event(on_event, {"type": "run_started", "query": query})
    try:
        route_catalog = routing_policy_catalog()
    except Exception as exc:  # pragma: no cover - catalog is only a hint
        logger.warning("Routing policy catalog unavailable: %s", exc)
        route_catalog = []
    semantic_context = build_semantic_context(
        history,
        query,
        conversation_summary,
        route_catalog=route_catalog,
    )
    normalized_attachments = normalize_attachments(attachments or [])
    context_resolution = resolve_history_aware_query(
        history,
        query,
        semantic_context.get("next_summary") or {},
        model_factory=(
            lambda: _create_stage_model(
                "context_rewrite",
                query=query,
                attachments=normalized_attachments,
            )
        ),
    )
    retrieval_query = str(context_resolution.get("query") or query)
    if context_resolution.get("status") != "unchanged":
        _emit_stream_event(on_event, {
            "type": "context_resolution",
            **context_resolution,
        })
    if context_resolution.get("status") == "ambiguous":
        total_duration_ms = round((time.perf_counter() - started_at) * 1000)
        result = _context_ambiguity_result(
            query,
            context_resolution,
            total_duration_ms,
            trace_recorder=recorder,
        )
        _emit_stream_event(on_event, {
            "type": "context_clarification_required",
            "message": result["answer"],
        })
        return result
    messages = _build_agent_messages(
        history,
        retrieval_query,
        normalized_attachments,
    )
    settings = get_settings()
    workflow: dict[str, Any] | None = None
    workflow_run_id: str | None = None
    effective_workflow_params: dict[str, Any] = {}
    if workflow_id:
        try:
            workflow = get_workflow_store().for_run(workflow_id, workflow_version)
            workflow_run_id = str(uuid4())
            if recorder is not None:
                recorder.set_trace_id(workflow_run_id)
            effective_workflow_params = dict(workflow.get("params") or {})
            effective_workflow_params.update(workflow_params or {})
        except WorkflowError as exc:
            total_duration_ms = round((time.perf_counter() - started_at) * 1000)
            error_result = _agent_error_result(query, exc, total_duration_ms, trace_recorder=recorder)
            _emit_stream_event(on_event, {
                "type": "workflow_node",
                "workflow_id": workflow_id,
                "workflow_version": workflow_version,
                "status": "error",
                "detail": str(exc),
            })
            _emit_stream_event(on_event, {"type": "error", "message": str(exc)})
            return error_result
    try:
        routing_policy = evaluate_routing_policy(retrieval_query, workflow_id=workflow_id) if not tool_context or tool_context.workspace_id == "default" else {}
    except Exception as exc:  # pragma: no cover - policy storage failure must not break the assistant
        logger.warning("Routing policy evaluation failed: %s", exc)
        routing_policy = {
            "policy_id": None,
            "policy_name": None,
            "category": None,
            "version": None,
            "route": "unknown",
            "reasons": [],
            "actions": {},
            "detected_entity_types": [],
            "matched_count": 0,
            "warning": str(exc),
        }
    routing_policy["context_resolution"] = context_resolution
    if bool(getattr(settings, "model_routing_enabled", False)):
        try:
            model_routing = plan_model_routing(
                retrieval_query,
                attachments=normalized_attachments,
            )
        except Exception as exc:  # pragma: no cover - registry is an optional compatibility layer
            logger.warning("Model routing plan unavailable; using legacy model: %s", exc)
            model_routing = {
                "complexity": {"level": "normal", "score": 0, "signals": []},
                "stages": {},
                "warning": str(exc),
            }
    else:
        # Lightweight integrations and older deployments can explicitly turn
        # the registry off and keep the original single-model path.
        model_routing = {
            "complexity": {"level": "normal", "score": 0, "signals": []},
            "stages": {},
            "disabled": True,
        }
    routing_policy["model_routing"] = model_routing
    semantic_model_profile_id = (
        (model_routing.get("stages") or {}).get("semantic_router") or {}
    ).get("profile_id")
    # The published semantic Prompt is the classifier.  Rules are still
    # supplied as vocabulary and remain the safe fallback/permission boundary.
    # Start the workflow route node before the semantic model so the model
    # span is rendered beneath this stage and its duration is included in the
    # stage wall-clock time.
    _emit_workflow_node(
        on_event,
        workflow,
        workflow_run_id,
        aliases=("route", "router"),
        status="running",
        detail="正在解析用户意图、实体和工作流入口",
    )
    _try_semantic_route(
        retrieval_query,
        routing_policy,
        recorder,
        semantic_context=semantic_context,
        route_catalog=route_catalog,
        attachments=normalized_attachments,
        model_profile_id=semantic_model_profile_id,
    )
    retrieval_plan = build_safe_retrieval_plan(
        retrieval_query,
        {
            "entities": routing_policy.get("semantic_entities") or [],
            "intents": routing_policy.get("semantic_intents") or [],
            "retrieval_plan": routing_policy.get("semantic_retrieval_plan") or {},
        },
    )
    routing_policy["retrieval_plan"] = retrieval_plan.model_dump(mode="json")
    routing_policy.setdefault("semantic_context", {
        "conversation_summary": semantic_context["prompt_context"].get("conversation_summary") or {},
        "recent_turns": semantic_context["prompt_context"].get("recent_turns") or [],
        "dictionary_hints": semantic_context["prompt_context"].get("dictionary_hints") or [],
        "route_catalog": semantic_context["prompt_context"].get("route_catalog") or [],
        "context_priority": semantic_context["prompt_context"].get("context_priority") or [],
        "diagnostics": semantic_context.get("diagnostics") or {},
        "next_summary": semantic_context.get("next_summary") or {},
    })
    policy_actions = dict(routing_policy.get("actions") or {})
    policy_route = str(routing_policy.get("route") or "unknown")
    dataset_list_query = _is_dataset_list_query(query) or policy_route == "dataset_list"
    # 请求级数据源范围由前端传入，但最终仍会经过统一搜索服务的配置
    # 和权限过滤；这里保存到事件上下文，供后续自主工具调用使用。
    explicit_source_types = source_types is not None
    requested_source_types = list(
        source_types
        if source_types is not None
        else (
            effective_workflow_params.get("source_types")
            or effective_workflow_params.get("sources")
            or []
        )
    )
    requested_search_filters = dict(search_filters or {})
    workflow_top_k = _bounded_int(
        effective_workflow_params.get(
            "top_k",
            getattr(settings, "unified_search_top_k", 12),
        ),
        12,
        1,
        50,
    )
    workflow_requirement_max = _bounded_int(
        effective_workflow_params.get(
            "requirement_max_items",
            getattr(settings, "requirement_max_items", 12),
        ),
        12,
        1,
        30,
    )
    workflow_similarity_threshold = _resolve_similarity_threshold(
        effective_workflow_params,
        settings=settings,
        workflow=workflow,
        policy_actions=policy_actions,
    )
    policy_business = policy_route in {"business_query", "business_action"} and not any(
        term in query for term in ("是什么", "含义", "原理", "手册", "说明书", "依据", "诊断", "原因", "怎么")
    )
    business_action = bool(tool_context and (_is_business_action_query(query) or policy_business))
    requires_evidence = (
        (query_requires_evidence(query) and not business_action)
        or bool(policy_actions.get("require_evidence"))
        or bool(routing_policy.get("semantic_requires_evidence"))
        or policy_route == "knowledge_qa"
    )
    if policy_actions:
        workflow_top_k = _bounded_int(policy_actions.get("top_k"), workflow_top_k, 1, 50)
        if policy_actions.get("source_types") and not explicit_source_types:
            requested_source_types = list(policy_actions["source_types"])
        if tool_context:
            tool_context.routing_confirmation = bool(policy_actions.get("human_confirmation"))
    workflow_initial_top_k = _bounded_int(
        effective_workflow_params.get(
            "initial_top_k",
            getattr(settings, "unified_search_initial_top_k", 6),
        ),
        min(workflow_top_k, getattr(settings, "unified_search_initial_top_k", 6)),
        1,
        workflow_top_k,
    )
    routing_policy["effective_route"] = "dataset_list" if dataset_list_query else "knowledge_qa" if requires_evidence else "business_tools" if business_action else "casual"
    routing_policy["requires_evidence"] = requires_evidence
    routing_policy["effective_top_k"] = workflow_top_k
    routing_policy["initial_top_k"] = workflow_initial_top_k
    routing_policy["effective_threshold"] = workflow_similarity_threshold
    routing_policy["requested_sources"] = requested_source_types
    agent_model_profile_id = (
        (model_routing.get("stages") or {}).get("agent") or {}
    ).get("profile_id")
    evidence_mode = normalize_evidence_mode(
        effective_workflow_params.get(
            "evidence_mode",
            getattr(get_settings(), "evidence_mode", "generic"),
        )
    )
    prefetch_messages: list[Any] = []
    retrieval_expanded = False
    requirements: list[dict[str, Any]] = []
    requirement_plan_warning: str | None = None
    requirement_plan_model_calls = 0
    requirement_token_usage: dict[str, int] = {}
    requirement_plan_audited = False
    requirement_pipeline_enabled = (
        requires_evidence
        and not dataset_list_query
        and bool(getattr(settings, "claim_validation_enabled", False))
    )
    _emit_stream_event(on_event, {
        "type": "workflow_started",
        "workflow_id": workflow_id,
        "workflow_version": workflow.get("version") if workflow else None,
        "workflow_run_id": workflow_run_id,
        "node_count": len(workflow.get("nodes") or []) if workflow else 0,
    }) if workflow else None
    if workflow:
        confirmation_requested = any(
            term in query
            for term in ("提交", "写入", "创建工单", "导入", "删除", "执行动作", "发布")
        )
        for configured_node in workflow.get("nodes") or []:
            if configured_node.get("human_confirmation") and configured_node.get("enabled", True):
                _emit_workflow_node(
                    on_event,
                    workflow,
                    workflow_run_id,
                    aliases=(str(configured_node.get("id") or ""),),
                    status="waiting_confirmation",
                    detail="该节点配置为人工确认点；当前运行只允许生成草稿，不会自动提交写操作",
                )
            if configured_node.get("type") == "condition" and configured_node.get("enabled", True):
                expression = str(configured_node.get("condition") or "requires_confirmation == true")
                passed = _workflow_condition_matches(expression, confirmation_requested)
                _emit_workflow_node(
                    on_event,
                    workflow,
                    workflow_run_id,
                    aliases=(str(configured_node.get("id") or "condition"),),
                    status="success" if passed else "skipped",
                    detail=f"条件 {expression}：{'通过' if passed else '未满足，跳过分支'}",
                )
    try:
        _emit_workflow_node(
            on_event,
            workflow,
            workflow_run_id,
            aliases=("route", "router"),
            status="success",
            detail=("已判定为需要证据的知识问题" if requires_evidence else "业务工具查询" if business_action else "普通对话")
            + (f"；策略：{routing_policy['policy_name']} v{routing_policy['version']}" if routing_policy.get("policy_id") else "；使用默认路由")
            + "；" + "；".join(routing_policy.get("reasons") or []),
        )
        if requirement_pipeline_enabled:
            _emit_stream_event(on_event, {"type": "requirement_planning"})
            plan_started_at = time.perf_counter()
            _emit_workflow_node(
                on_event,
                workflow,
                workflow_run_id,
                aliases=("plan", "requirement_plan"),
                status="running",
                detail="正在拆分可独立检索和发布的需求项",
            )
            plan = plan_requirements(
                _create_stage_model(
                    "requirement_plan",
                    query=retrieval_query,
                    attachments=normalized_attachments,
                    complexity=model_routing.get("complexity"),
                    profile_id=_model_profile_id(model_routing, "requirement_plan"),
                ),
                retrieval_query,
                max_requirements=max(
                    1,
                    workflow_requirement_max,
                ),
                attachments=normalized_attachments,
            )
            requirements = plan.requirements
            requirement_plan_warning = plan.warning
            requirement_plan_model_calls = plan.model_calls
            requirement_token_usage = _merge_trace_token_usage(
                requirement_token_usage,
                plan.token_usage,
            )
            simple_requirement = (
                len(requirements) == 1
                and len(extract_identifiers(retrieval_query)) <= 1
                and len(extract_aspects(retrieval_query)) <= 1
                and not any(term in retrieval_query for term in ("对比", "比较", "分别", "和", "及", "、", "以及"))
            )
            if bool(getattr(settings, "requirement_plan_audit_enabled", True)) and not simple_requirement:
                audit = audit_requirement_plan(
                    _create_stage_model(
                        "requirement_audit",
                        query=retrieval_query,
                        attachments=normalized_attachments,
                        complexity=model_routing.get("complexity"),
                        profile_id=_model_profile_id(model_routing, "requirement_audit"),
                    ),
                    retrieval_query,
                    requirements,
                    max_requirements=max(
                        1,
                        workflow_requirement_max,
                    ),
                    attachments=normalized_attachments,
                )
                requirements = audit.requirements
                requirement_plan_audited = audit.complete
                requirement_plan_model_calls += audit.model_calls
                requirement_token_usage = _merge_trace_token_usage(
                    requirement_token_usage,
                    audit.token_usage,
                )
                if audit.warning:
                    requirement_plan_warning = "；".join(
                        item for item in [requirement_plan_warning, audit.warning] if item
                    )
            _emit_workflow_node(
                on_event,
                workflow,
                workflow_run_id,
                aliases=("plan", "requirement_plan"),
                status="success" if not requirement_plan_warning else "partial",
                detail=f"已生成 {len(requirements)} 个需求项",
                duration_ms=round((time.perf_counter() - plan_started_at) * 1000),
            )
        else:
            _emit_workflow_node(
                on_event,
                workflow,
                workflow_run_id,
                aliases=("plan", "requirement_plan"),
                status="skipped",
                detail="本轮不是事实型知识问题，跳过需求规划",
            )
        if dataset_list_query:
            # Retrieval is forced by the backend for evidence-required routes;
            # Qwen is only responsible for composing the final answer.
            tool_mode = "none"
            search_limit = 1
        elif requires_evidence:
            tool_mode = "none"
            search_limit = max(1, min(len(extract_identifiers(retrieval_query)) or 1, 4))
        else:
            # Casual conversation must not expose RAGFlow tools.  Qwen still
            # handles the language understanding, but the route is enforced
            # by the backend instead of relying only on the system prompt.
            tool_mode = "none"
            search_limit = 1
        try:
            agent = get_agent(
                search_limit=search_limit,
                tool_mode=tool_mode,
                evidence_mode=evidence_mode,
                model_profile_id=agent_model_profile_id,
            )
        except TypeError as exc:
            # Keep tests and lightweight integrations that monkeypatch the
            # legacy zero-argument get_agent() compatible.
            if "unexpected keyword" not in str(exc):
                raise
            agent = get_agent()
        tool_calls: dict[str, dict] = {}
        if tool_context:
            tool_context.workflow_id = workflow_id
            tool_context.trace_id = workflow_run_id or tool_context.trace_id or (recorder.trace_id if recorder else str(uuid4()))
            def emit_tool(event):
                if event.get("type") == "tool_call" and event.get("call_id"):
                    tool_calls[event["call_id"]] = {key: value for key, value in event.items() if key != "type"}
                if recorder is not None:
                    recorder.record_tool_event(event)
                _emit_stream_event(on_event, event)
            tool_context.on_event = emit_tool
            managed_tools = ToolCenterService().managed_tools(tool_context)
            if managed_tools:
                register_harness_profile("openai", HarnessProfile(excluded_tools=EXCLUDED_DEEPAGENT_TOOLS))
                agent = create_deep_agent(
                    model=_create_stage_model(
                        "agent",
                        query=retrieval_query,
                        attachments=normalized_attachments,
                        complexity=model_routing.get("complexity"),
                        profile_id=agent_model_profile_id,
                    ),
                    tools=managed_tools,
                    middleware=[RetrievalGuardMiddleware(evidence_mode=evidence_mode)],
                    system_prompt=SYSTEM_PROMPT + "\n业务工具调用权限由后端控制。awaiting_confirmation/awaiting_approval 只表示已申请，"
                    "绝不表示已经提交或成功。提示用户点击确认/审批卡片，不能代替用户确认，也不要重复申请同一动作。"
                    "\n普通工单草稿不是业务系统的正式工单；没有实际工具结果时不能编造设备状态或工单编号。",
                    name="governed_enterprise_agent",
                )
        # Production DeepAgent graphs expose get_graph().  Lightweight test
        # doubles may already provide their own ToolMessages, so leave those
        # unchanged for backwards-compatible unit/integration testing.
        if hasattr(agent, "get_graph"):
            if requires_evidence or dataset_list_query:
                _emit_stream_event(on_event, {
                    "type": "retrieval_started",
                    "query": retrieval_query,
                })
            search_started_at = time.perf_counter()
            _emit_workflow_node(
                on_event,
                workflow,
                workflow_run_id,
                aliases=("search", "unified_search", "parallel-search", "manual"),
                status="running",
                detail=(
                    f"正在按配置检索；首轮 Top-K={workflow_initial_top_k}，"
                    f"扩大上限={workflow_top_k}，来源={','.join(requested_source_types) or '服务端默认'}"
                ),
            )
            # 显式数据源选择，或企业已配置图谱/业务连接器时，走统一搜索。
            # 默认关闭自动切换是为了兼容已有 RAGFlow 证据测试和部署；
            # 生产环境可通过 UNIFIED_SEARCH_PREFETCH_ENABLED=true 开启。
            use_unified_prefetch = bool(
                tool_context
                or requested_source_types
                or source_types is not None
                or getattr(settings, "unified_search_prefetch_enabled", False)
                or getattr(settings, "qa_data_path", None)
                or getattr(settings, "neo4j_password", None)
                or getattr(settings, "business_api_base_url", None)
                or getattr(settings, "sql_search_url", None)
                or getattr(settings, "mcp_search_url", None)
            )
            def prefetch_at_top_k(top_k: int, targeted_requirements: list[dict] | None = None) -> list[Any]:
                if workflow:
                    search_nodes = [node for node in workflow.get("nodes", []) if node.get("type") in {"search", "unified_search", "parallel", "api"}]
                    confirmation = any(term in query for term in ("提交", "写入", "创建工单", "导入", "删除", "执行动作", "发布"))
                    if search_nodes and not any(_workflow_branch_allowed(workflow, str(node.get("id")), confirmation) for node in search_nodes):
                        return [ToolMessage(name="unified_search", tool_call_id="conditional-search-skipped", content=json.dumps({"query": retrieval_query, "hits": [], "warnings": ["所有搜索分支条件未满足，已跳过"]}, ensure_ascii=False))]
                if use_unified_prefetch and not dataset_list_query and requires_evidence:
                    if targeted_requirements is not None:
                        targeted_plan = retrieval_plan.model_copy(update={
                            "document_queries": list(dict.fromkeys(str(item.get("retrieval_query") or retrieval_query) for item in targeted_requirements)),
                            "graph_queries": list(dict.fromkeys(str(item.get("subject") or "") for item in targeted_requirements if item.get("subject"))),
                        })
                        return _prefetch_unified_search_messages(
                            retrieval_query, source_types=requested_source_types, filters=requested_search_filters,
                            top_k=top_k, query_plan=targeted_plan.model_dump(mode="json"),
                        )
                    messages_at_top_k = _prefetch_parallel_workflow_messages(
                        retrieval_query,
                        workflow,
                        default_source_types=requested_source_types,
                        filters=requested_search_filters,
                        top_k=top_k,
                        callback=on_event,
                        workflow_run_id=workflow_run_id,
                        query_plan=retrieval_plan.model_dump(mode="json"),
                    )
                    if not messages_at_top_k:
                        messages_at_top_k = _prefetch_unified_search_messages(
                            retrieval_query,
                            source_types=requested_source_types,
                            filters=requested_search_filters,
                            top_k=top_k,
                            query_plan=retrieval_plan.model_dump(mode="json"),
                        )
                    return messages_at_top_k
                if dataset_list_query:
                    return _prefetch_dataset_list_messages(tool_context)
                if requires_evidence:
                    if requirement_pipeline_enabled and requirements:
                        return _prefetch_requirement_messages(
                            targeted_requirements if targeted_requirements is not None else requirements,
                            max_workers=max(
                                1,
                                min(
                                    int(getattr(settings, "requirement_max_parallel_searches", 4)),
                                    8,
                                ),
                            ),
                            top_k=top_k,
                        )
                    if len(extract_identifiers(retrieval_query)) > 1:
                        return _prefetch_multi_entity_messages(
                            retrieval_query,
                            top_k=top_k,
                        )
                    return _prefetch_single_query_messages(
                        retrieval_query,
                        top_k=top_k,
                    )
                return []

            prefetch_messages = prefetch_at_top_k(workflow_initial_top_k)
            if (
                requires_evidence
                and workflow_initial_top_k < workflow_top_k
                and prefetch_messages
            ):
                first_pass_assessment = _summarize_evidence(
                    prefetch_messages,
                    retrieval_query,
                    min_similarity=workflow_similarity_threshold,
                    generic_min_similarity=workflow_similarity_threshold,
                    evidence_mode=evidence_mode,
                )
                missing_requirements = _missing_retrieval_requirements(prefetch_messages, requirements, evidence_mode) if requirements else None
                if _should_expand_evidence_retrieval(
                    first_pass_assessment,
                    requirement_count=len(requirements),
                    missing_requirements=missing_requirements,
                ):
                    expanded_messages = prefetch_at_top_k(workflow_top_k, missing_requirements) if missing_requirements is None or missing_requirements else []
                    if expanded_messages:
                        prefetch_messages = [*prefetch_messages, *expanded_messages]
                        retrieval_expanded = True
            routing_policy["retrieval_expanded"] = retrieval_expanded
            if prefetch_messages:
                _emit_stream_event(on_event, {
                    "type": "retrieval_completed",
                    "message_count": len(prefetch_messages),
                })
                _emit_workflow_node(
                    on_event,
                    workflow,
                    workflow_run_id,
                    aliases=("search", "unified_search", "parallel-search", "manual"),
                    status="success",
                    detail=(
                        f"检索节点返回 {max(0, len(prefetch_messages) - 1)} 条工具结果；"
                        + (
                            f"证据不足，已扩大到 Top-K={workflow_top_k}"
                            if retrieval_expanded
                            else f"首轮 Top-K={workflow_initial_top_k}"
                        )
                    ),
                    duration_ms=round((time.perf_counter() - search_started_at) * 1000),
                )
            else:
                _emit_workflow_node(
                    on_event,
                    workflow,
                    workflow_run_id,
                    aliases=("search", "unified_search", "parallel-search", "manual"),
                    status="error" if requires_evidence else "skipped",
                    detail="本轮没有生成检索结果" if requires_evidence else "普通对话跳过检索",
                    duration_ms=round((time.perf_counter() - search_started_at) * 1000),
                )
        # The DeepAgent owns the initial system prompt.  Search scope and
        # filters are already enforced by the backend connectors/context; do
        # not append a second system message after user/tool messages because
        # OpenAI-compatible providers require system messages at the start.
        agent_messages = _build_agent_messages(
            messages,
            retrieval_query,
            normalized_attachments,
            prefetch_messages,
        )
        # Fact questions publish only structured, judge-labelled claims. Once
        # backend prefetch has supplied the evidence, skip the old free-form
        # answer call and spend model calls on extraction plus judging instead.
        use_requirement_pipeline = (
            requirement_pipeline_enabled
            and bool(prefetch_messages)
        )
        if use_requirement_pipeline:
            _emit_stream_event(on_event, {"type": "claim_processing"})
            result = {"messages": agent_messages}
        else:
            invoke_input = {"messages": agent_messages}
            if on_event is not None and hasattr(agent, "stream"):
                result = _stream_agent(agent, invoke_input, settings, on_event, publish_tokens=not requires_evidence)
            else:
                try:
                    result = agent.invoke(
                        invoke_input,
                        config={
                            "recursion_limit": max(
                                2,
                                int(getattr(settings, "max_agent_steps", 6)),
                            ),
                            **({"callbacks": [TraceCallbackHandler()]} if current_trace_recorder() is not None else {}),
                        },
                    )
                except TypeError as exc:
                    # Keep lightweight test doubles and legacy integrations that only
                    # accept the original one-argument invoke signature compatible.
                    if "config" not in str(exc):
                        raise
                    result = agent.invoke(invoke_input)
    except Exception as exc:
        total_duration_ms = round((time.perf_counter() - started_at) * 1000)
        error_result = _agent_error_result(query, exc, total_duration_ms, trace_recorder=recorder)
        if workflow and workflow_run_id:
            try:
                get_workflow_store().record_run(
                    str(workflow.get("workflow_id") or workflow.get("id")),
                    int(workflow.get("version") or 0),
                    workflow_run_id,
                    "error",
                    duration_ms=total_duration_ms,
                    error=str(exc),
                )
            except Exception:
                pass
            _emit_workflow_node(
                on_event,
                workflow,
                workflow_run_id,
                aliases=("answer", "diagnose", "notify"),
                status="error",
                detail=str(exc),
                duration_ms=total_duration_ms,
            )
        _emit_stream_event(on_event, {
            "type": "error",
            "message": error_result["evidence"].get("reason") or str(exc),
        })
        return error_result
    result_messages = result.get("messages", [])
    # Include deterministic prefetch ToolMessages in evidence/citation
    # processing, while excluding ordinary conversation history.
    generated_messages = result_messages[len(messages):]
    structured = result.get("structured_response")
    plain_response = False

    if isinstance(structured, AgentAnswer):
        answer = structured.answer
        suggested = structured.suggested_questions[:4]
        used_evidence_ids = structured.used_evidence_ids
    elif isinstance(structured, dict):
        answer = str(structured.get("answer", ""))
        suggested = [str(item) for item in structured.get("suggested_questions", [])[:4]]
        used_evidence_ids = [str(item) for item in structured.get("used_evidence_ids", [])]
    else:
        answer = _last_assistant_text(result_messages)
        suggested = []
        used_evidence_ids = []
        plain_response = True

    evidence_started_at = time.perf_counter()
    _emit_workflow_node(
        on_event,
        workflow,
        workflow_run_id,
        aliases=("evidence", "evidence_gate"),
        status="running",
        detail=(
            f"正在执行证据底线校验；相似度参考值={workflow_similarity_threshold:.2f}"
            "（仅用于排序和诊断）"
        ),
    )
    evidence = _summarize_evidence(
        result_messages,
        retrieval_query,
        min_similarity=workflow_similarity_threshold,
        generic_min_similarity=workflow_similarity_threshold,
        evidence_mode=evidence_mode,
    )
    _emit_workflow_node(
        on_event,
        workflow,
        workflow_run_id,
        aliases=("evidence", "evidence_gate"),
        status="success" if not evidence.get("has_error") else "error",
        detail=f"候选 {evidence.get('candidate_count', 0)} 个；通过 {evidence.get('verified_count', 0)} 个",
        duration_ms=round((time.perf_counter() - evidence_started_at) * 1000),
    )
    verified_chunks = evidence.pop("chunks", [])
    used_ragflow = evidence.get("ragflow_calls", 0) > 0
    final_status = str(evidence.get("status") or "not_retrieved")
    claims: list[dict[str, Any]] = []
    requirement_results: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {
        "complete": False,
        "audited": False,
        "plan_complete": False,
        "all_requirements_accounted_for": False,
        "planned_requirement_count": len(requirements),
        "result_count": 0,
        "missing_requirement_ids": [str(item.get("id")) for item in requirements],
        "missing_requests": [],
        "supported_count": 0,
        "unsupported_count": 0,
        "conflict_count": 0,
    }
    requirement_warning: str | None = requirement_plan_warning
    requirement_evaluation_model_calls = 0
    answer_draft_payload: dict[str, Any] | None = None
    answer_assembly: dict[str, Any] = {
        "mode": str(getattr(settings, "answer_assembly_mode", "fact_binding")),
        "binding_mode": "fact_plan",
        "gate": "not_run",
        "repair_attempted": False,
        "repair_succeeded": False,
        "fact_count": 0,
        "bound_fact_count": 0,
        "rendered_fact_count": 0,
        "invalid_fact_refs": [],
        "dropped_segments": 0,
        "violations": [],
    }

    # The live DeepAgent no longer uses ToolStrategy for its final response.
    # ToolStrategy can repeatedly call the model when a provider returns an
    # invalid or incomplete structured payload.  Bind the plain answer to the
    # verified chunks in this turn on the server instead.  Legacy/test agents
    # that still return AgentAnswer keep their explicit evidence IDs.
    if plain_response and verified_chunks and not requirement_pipeline_enabled:
        used_evidence_ids = []

    if requires_evidence:
        if evidence.get("has_error") and not verified_chunks:
            final_status = "retrieval_error"
            answer = build_safe_answer(final_status, evidence)
            suggested = []
        elif not used_ragflow:
            final_status = "not_retrieved"
            answer = build_safe_answer(final_status, evidence)
            suggested = ["查看当前知识库", "重新检索这个问题"]
        elif evidence_mode != "off" and final_status == "not_found":
            answer = build_safe_answer(final_status, evidence)
            suggested = _safe_missing_evidence_suggestions(evidence)
        elif evidence_mode != "off" and final_status == "partial":
            answer = build_safe_answer(final_status, evidence)
            suggested = _safe_missing_evidence_suggestions(evidence)
        elif evidence_mode != "off" and final_status == "conflict":
            answer = build_safe_answer(final_status, evidence)
            suggested = ["查看冲突证据片段", "如何确认资料的有效版本？"]
        elif evidence_mode != "off" and not requirement_pipeline_enabled:
            validation = validate_answer(
                answer,
                retrieval_query,
                verified_chunks,
                used_evidence_ids,
                require_evidence_ids=True,
                mode=evidence_mode,
            )
            evidence.update(validation)
            if not validation["passed"]:
                document_answer = _build_document_presence_answer(
                    retrieval_query,
                    verified_chunks,
                )
                if document_answer:
                    # For a document-presence question, verified filenames are
                    # sufficient evidence. Do not turn an unrelated model
                    # mention in the LLM draft into a failed user request.
                    answer = document_answer
                    suggested = ["查看已验证证据片段"]
                    evidence.pop("violations", None)
                    final_status = "supported"
                else:
                    final_status = "answer_blocked"
                    answer = build_safe_answer(final_status, evidence)
                    suggested = ["查看已验证证据片段", "补充更明确的设备资料"]

    # Every planned requirement gets a published row. The judge audits both
    # evidence support and whether the plan covered the original user request.
    if requirement_pipeline_enabled and requirements:
        judge_started_at = time.perf_counter()
        _emit_workflow_node(
            on_event,
            workflow,
            workflow_run_id,
            aliases=("judge", "claim_judge"),
            status="running",
            detail="正在逐项生成结果、裁判证据并执行覆盖审计",
        )
        evaluation = evaluate_requirements(
            _create_stage_model(
                "claim_judge",
                query=retrieval_query,
                attachments=normalized_attachments,
                complexity=model_routing.get("complexity"),
                profile_id=_model_profile_id(model_routing, "claim_judge"),
            ),
            retrieval_query,
            requirements,
            verified_chunks,
            plan_warning=requirement_plan_warning,
            plan_audited=requirement_plan_audited,
            attachments=normalized_attachments,
        )
        requirement_results = evaluation.results
        if evidence.get("has_error"):
            for item in requirement_results:
                if item.get("status") == "unsupported" and item.get("reason_code") == "no_evidence":
                    item["reason_code"] = "source_error"
                    item["reason"] = "部分检索来源故障，当前无法确认；这不表示资料中没有答案。"
        coverage = evaluation.coverage
        requirement_warning = evaluation.warning
        claims = requirement_results_to_claims(requirement_results)
        composition = compose_answer(
            _create_stage_model(
                "answer",
                query=retrieval_query,
                attachments=normalized_attachments,
                complexity=model_routing.get("complexity"),
                profile_id=_model_profile_id(model_routing, "answer"),
            ),
            retrieval_query,
            requirement_results,
            coverage,
            verified_chunks,
            attachments=normalized_attachments,
        )
        draft = composition.draft
        composition_warnings: list[str] = []
        repair_model_calls = 0
        repair_token_usage: dict[str, int] = {}
        if composition.warning:
            composition_warnings.append(composition.warning)

        # The requirement judge owns semantic support.  The answer composer
        # only returns a plan of fact IDs; factual values are rendered from
        # the server-created ledger in the draft.
        draft_validation = (
            validate_answer_plan(draft, verified_chunks)
            if draft is not None
            else {
                "passed": False,
                "violations": ["答案组装没有返回可校验的草稿"],
                "used_evidence_ids": [],
                "fact_count": 0,
                "bound_fact_count": 0,
                "rendered_fact_count": 0,
                "invalid_fact_refs": [],
                "dropped_segments": 0,
            }
        )
        initial_gate = "repair_required"
        if draft_validation["passed"]:
            initial_gate = (
                "fact_plan_repaired"
                if draft is not None and draft.repaired
                else "fact_plan_rendered"
            )
        answer_assembly.update({
            "gate": initial_gate,
            "fact_count": draft_validation.get("fact_count", 0),
            "bound_fact_count": draft_validation.get("bound_fact_count", 0),
            "rendered_fact_count": draft_validation.get("rendered_fact_count", 0),
            "invalid_fact_refs": list(draft_validation.get("invalid_fact_refs") or []),
            "dropped_segments": draft_validation.get("dropped_segments", 0),
            "violations": list(draft_validation.get("violations") or []),
        })

        repair_attempts = max(
            0,
            min(int(getattr(settings, "answer_repair_max_attempts", 1)), 1),
        )
        if not draft_validation["passed"] and draft is not None and repair_attempts:
            answer_assembly["repair_attempted"] = True
            repair = compose_answer(
                _create_stage_model(
                    "answer_repair",
                    query=retrieval_query,
                    attachments=normalized_attachments,
                    complexity=model_routing.get("complexity"),
                    profile_id=_model_profile_id(model_routing, "answer_repair")
                    or _model_profile_id(model_routing, "answer"),
                ),
                retrieval_query,
                requirement_results,
                coverage,
                verified_chunks,
                attachments=normalized_attachments,
                previous_draft=draft,
                violations=draft_validation.get("violations") or [],
            )
            repair_model_calls = repair.model_calls
            repair_token_usage = repair.token_usage or {}
            if repair.warning:
                composition_warnings.append(repair.warning)
            if repair.draft is not None:
                repaired_validation = validate_answer_plan(repair.draft, verified_chunks)
                if repaired_validation["passed"]:
                    draft = repair.draft
                    draft_validation = repaired_validation
                    answer_assembly["repair_succeeded"] = True
                    answer_assembly["gate"] = "fact_plan_repaired"
                    answer_assembly["violations"] = []
                else:
                    draft = repair.draft
                    draft_validation = repaired_validation
                    answer_assembly["fact_count"] = repaired_validation.get("fact_count", 0)
                    answer_assembly["bound_fact_count"] = repaired_validation.get("bound_fact_count", 0)
                    answer_assembly["rendered_fact_count"] = repaired_validation.get("rendered_fact_count", 0)
                    answer_assembly["invalid_fact_refs"] = list(repaired_validation.get("invalid_fact_refs") or [])
                    answer_assembly["dropped_segments"] = repaired_validation.get("dropped_segments", 0)
                    answer_assembly["violations"] = list(
                        repaired_validation.get("violations") or []
                    )
                    composition_warnings.append(
                        "答案修复后仍未通过事实引用计划校验"
                    )

        if draft is None or not draft_validation["passed"]:
            # This is a model/plan failure fallback.  It publishes only values
            # already accepted by the requirement judge and never preserves a
            # free-form model answer containing unbound values.
            draft = fallback_answer(requirement_results, coverage, verified_chunks)
            fallback_validation = validate_answer_plan(draft, verified_chunks)
            draft_validation = fallback_validation
            answer_assembly["gate"] = "fact_plan_fallback"
            answer_assembly["fact_count"] = fallback_validation.get("fact_count", 0)
            answer_assembly["bound_fact_count"] = fallback_validation.get("bound_fact_count", 0)
            answer_assembly["rendered_fact_count"] = fallback_validation.get("rendered_fact_count", 0)
            answer_assembly["invalid_fact_refs"] = list(fallback_validation.get("invalid_fact_refs") or [])
            answer_assembly["dropped_segments"] = fallback_validation.get("dropped_segments", 0)
            composition_warnings.append(
                "答案事实计划未通过结构校验，已发布已裁判的安全结果"
            )

        answer = draft.answer
        answer_draft_payload = draft.model_dump(mode="json")
        claims = [
            {
                "fact_id": claim.fact_id,
                "entity": claim.entity,
                "attribute": claim.attribute,
                "value": claim.value,
                "evidence_ids": list(claim.evidence_ids),
                "status": claim.status,
                "reason": claim.reason or claim.text,
            }
            for claim in draft.claims
        ] or requirement_results_to_claims(requirement_results)
        used_evidence_ids = list(dict.fromkeys(
            str(evidence_id)
            for evidence_id in draft.used_evidence_ids
            if str(evidence_id).strip()
        ))
        evidence["used_evidence_ids"] = used_evidence_ids
        requirement_evaluation_model_calls = (
            evaluation.model_calls
            + composition.model_calls
            + repair_model_calls
        )
        requirement_token_usage = _merge_trace_token_usage(
            requirement_token_usage,
            evaluation.token_usage,
            composition.token_usage or {},
            repair_token_usage,
        )
        if composition_warnings:
            requirement_warning = "；".join(
                item for item in [requirement_warning, *composition_warnings] if item
            ) or None
        if requirement_warning:
            evidence.setdefault("warnings", []).append(requirement_warning)

        if final_status not in {"retrieval_error", "not_retrieved"}:
            if coverage.get("conflict_count"):
                final_status = "conflict"
            elif (
                coverage.get("complete")
                and coverage.get("supported_count") == len(requirement_results)
            ):
                final_status = "supported"
            elif coverage.get("supported_count"):
                final_status = "partial"
            else:
                final_status = "not_found"
        _emit_workflow_node(
            on_event,
            workflow,
            workflow_run_id,
            aliases=("judge", "claim_judge"),
            status="success" if not requirement_warning else "partial",
            detail=(
                f"已发布 {len(requirement_results)} 个需求结果；"
                f"支持 {coverage.get('supported_count', 0)} 个，未支持 {coverage.get('unsupported_count', 0)} 个"
            ),
            duration_ms=round((time.perf_counter() - judge_started_at) * 1000),
        )
    else:
        _emit_workflow_node(
            on_event,
            workflow,
            workflow_run_id,
            aliases=("judge", "claim_judge"),
            status="skipped",
            detail="本轮没有可独立裁判的需求项",
        )

    # A supported answer should lead to neighboring factual questions.  Do
    # not expose evidence/debug actions as if they were user questions; those
    # remain available through the evidence and execution-detail panels.
    if has_context_reference(query) and final_status == "supported":
        suggested = [
            item["query"]
            for item in build_related_question_suggestions(retrieval_query, suggested)
        ]
    elif not has_context_reference(query):
        # Recommendations are reserved for contextual follow-ups.  An
        # explicit question already has its object and should end cleanly.
        suggested = []

    evidence["status"] = final_status if requires_evidence else (evidence.get("status") or "not_required")
    # A verified retrieval candidate is not automatically a citation: it may
    # be relevant to the query but not support any sentence in the final answer.
    if requires_evidence:
        evidence["used_evidence_ids"] = list(dict.fromkeys(
            str(item).strip()
            for item in (evidence.get("used_evidence_ids") or used_evidence_ids)
            if str(item).strip()
        ))
    evidence_summary = public_evidence_summary(evidence, requires_evidence=requires_evidence)
    verified_evidence_ids = {
        str(item.get("evidence_id"))
        for item in verified_chunks
        if item.get("evidence_id")
    }
    citation_evidence_ids = (
        set(evidence.get("used_evidence_ids") or [])
        if requires_evidence
        else None
    )

    answer_stage_started_at = time.perf_counter()
    _emit_workflow_node(
        on_event,
        workflow,
        workflow_run_id,
        aliases=("answer", "diagnose", "notify"),
        status="running",
        detail="正在生成可发布回答和引用结果",
    )
    # This value is only an initial snapshot for payload construction.  It is
    # refreshed again immediately before returning so post-agent evidence
    # judging, answer packaging, trace export, and persistence are included.
    total_duration_ms = round((time.perf_counter() - started_at) * 1000)
    result_payload = {
        "answer": answer or "暂时没有生成有效回答，请换一种方式描述问题。",
        "answer_draft": answer_draft_payload,
        "context_resolution": context_resolution,
        "suggestions": [{"text": item, "query": item} for item in suggested if item.strip()],
        "citations": _collect_citations(
            generated_messages,
            allowed_evidence_ids=citation_evidence_ids,
        ),
        "retrieval_hits": _collect_retrieval_hits(
            generated_messages,
            allowed_evidence_ids=verified_evidence_ids if requires_evidence else None,
        ),
        "graph_relations": _collect_graph_relations(generated_messages),
        "evidence": evidence_summary,
        "claims": claims,
        "requirements": requirements,
        "results": requirement_results,
        "coverage": coverage,
        "tool_calls": list(tool_calls.values()),
        "trace": _build_execution_trace(
            generated_messages,
            total_duration_ms,
            evidence_summary=evidence_summary,
            requirements=requirements if requirement_pipeline_enabled else None,
            requirement_results=requirement_results,
            coverage=coverage,
            requirement_warning=requirement_warning,
            requirement_model_calls=(
                requirement_plan_model_calls + requirement_evaluation_model_calls
            ),
            requirement_token_usage=requirement_token_usage,
            workflow_id=workflow_id,
            workflow_version=workflow.get("version") if workflow else None,
            workflow_run_id=workflow_run_id,
            trace_id=recorder.trace_id if recorder else workflow_run_id,
            trace_spans=recorder.export().get("spans") if recorder else None,
        ),
    }
    routing_policy["answer_assembly"] = answer_assembly
    result_payload["trace"]["routing_policy"] = routing_policy
    result_payload["trace"]["context_resolution"] = context_resolution
    budget = search_budget.get()
    if budget is not None:
        result_payload["trace"]["request_budget"] = {
            "search_calls": budget.calls,
            "max_search_calls": budget.max_calls,
            "model_calls": budget.model_calls,
            "max_model_calls": budget.max_model_calls,
            "planning_calls": budget.planning_calls,
            "max_planning_calls": budget.max_planning_calls,
        }
    if business_action and not requires_evidence:
        labels = {"awaiting_confirmation": "等待你点击确认，尚未提交", "awaiting_approval": "等待其他审批者批准，尚未执行",
                  "success": "执行成功", "error": "执行失败", "unknown": "结果不确定，请核对业务系统，不能自动重试",
                  "running": "执行中", "expired": "确认已过期", "rejected": "已拒绝"}
        summaries = []
        for call in tool_calls.values():
            text = f"{call['tool_name']}：{labels.get(call['status'], call['status'])}。"
            if call.get("result"):
                text += "\n" + json.dumps(call["result"], ensure_ascii=False, default=str)[:3000]
            if call.get("error"):
                text += "\n" + call["error"]
            summaries.append(text)
        result_payload["answer"] = "\n\n".join(summaries) or "本轮没有执行任何业务工具，不能确认设备状态或工单提交结果。请检查 Agent 工具授权并提供必填参数。"
        result_payload["evidence"] = {"status": "business_action", "requires_evidence": False, "reason": "结果以真实工具调用记录为准，不替代技术知识证据"}
        result_payload["trace"]["route"] = "business_tools"
    for call in tool_calls.values():
        result_payload["trace"]["steps"].append({
            "name": call["tool_name"],
            "detail": f"真实调用 {call['call_id']} · {call['status']}",
            "status": "error" if call["status"] in {"error", "unknown"} else call["status"],
        })
    if workflow and workflow_run_id:
        _emit_workflow_node(
            on_event,
            workflow,
            workflow_run_id,
            aliases=("answer", "diagnose", "notify"),
            status="success",
            detail=f"回答已生成；最终证据状态={evidence_summary.get('status', 'unknown')}",
            duration_ms=round((time.perf_counter() - answer_stage_started_at) * 1000),
        )
    total_duration_ms = round((time.perf_counter() - started_at) * 1000)
    if recorder is not None:
        final_trace = recorder.export(total_duration_ms=total_duration_ms)
        existing_spans = result_payload["trace"].get("spans") or []
        final_span_ids = {str(item.get("span_id")) for item in final_trace.get("spans", [])}
        merged_spans = [
            *final_trace.get("spans", []),
            *[
                item for item in existing_spans
                if str(item.get("span_id")) not in final_span_ids
            ],
        ]
        _merge_final_trace_summary(
            result_payload["trace"],
            merged_spans,
            trace_id=final_trace.get("trace_id"),
            total_duration_ms=total_duration_ms,
        )
    else:
        result_payload["trace"]["total_duration_ms"] = total_duration_ms
    if workflow and workflow_run_id:
        try:
            get_workflow_store().record_run(
                str(workflow.get("workflow_id") or workflow.get("id")),
                int(workflow.get("version") or 0),
                workflow_run_id,
                "success",
                duration_ms=total_duration_ms,
            )
        except Exception:
            pass
    return result_payload


def _last_assistant_text(messages: list[Any]) -> str:
    assistant_messages = [
        message for message in messages
        if isinstance(message, AIMessage)
    ]
    for message in reversed(assistant_messages):
        text = _message_content_text(message).strip()
        if text:
            return text

    last_message = assistant_messages[-1] if assistant_messages else None
    raw_content = getattr(last_message, "content", None)
    content_type = type(raw_content).__name__ if raw_content is not None else "NoneType"
    content_blocks = None
    if isinstance(raw_content, list):
        content_blocks = [
            str(item.get("type") or "unknown")
            if isinstance(item, dict)
            else type(item).__name__
            for item in raw_content[:8]
        ]
    logger.warning(
        "Agent final answer is empty after content normalization: "
        "message_count=%d, assistant_message_count=%d, last_content_type=%s, "
        "last_content_blocks=%s, last_tool_call_count=%d, message_types=%s",
        len(messages),
        len(assistant_messages),
        content_type,
        content_blocks,
        len(getattr(last_message, "tool_calls", None) or []) if last_message else 0,
        [
            getattr(message, "type", type(message).__name__)
            for message in messages[-8:]
        ],
    )
    return ""


def _collect_citations(
    messages: list[Any],
    allowed_evidence_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    # Citation identity is evidence/chunk-level.  Deduplicating only by
    # document ID makes the first retrieved page stand in for every later
    # page, which is how an unrelated page can be displayed.
    seen: set[tuple[str, str, str, str, str]] = set()
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in {"search_knowledge_base", "unified_search"}:
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, json.JSONDecodeError):
            continue
        chunks = payload.get("chunks", [])
        if not isinstance(chunks, list):
            chunks = []
        if message.name == "unified_search":
            chunks = [
                {
                    "document_name": item.get("title") or item.get("source_name") or "未知来源",
                    "document_id": item.get("document_id"),
                    "dataset_id": item.get("dataset_id"),
                    "chunk_id": item.get("chunk_id") or item.get("id"),
                    "page": item.get("page"),
                    "score": item.get("score"),
                    "evidence_id": item.get("evidence_id"),
                    "content": item.get("content", ""),
                    "url": item.get("url"),
                    "source_type": item.get("source_type"),
                    "source_name": item.get("source_name"),
                    "version": item.get("version"),
                    "updated_at": item.get("updated_at"),
                    "metadata": item.get("metadata") or {},
                }
                for item in (payload.get("hits") or [])
                if isinstance(item, dict)
            ]
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            # Graph edges have their own provenance panel.  Keeping the
            # synthetic edge ID out of ordinary document citations prevents
            # an entity name from being mistaken for an original PDF chunk.
            if str(chunk.get("source_type") or "").strip().lower() == "graph":
                continue
            if allowed_evidence_ids is not None and str(chunk.get("evidence_id")) not in allowed_evidence_ids:
                continue
            document_id = chunk.get("document_id")
            dataset_id = chunk.get("dataset_id")
            page = chunk.get("page")
            content = str(chunk.get("content") or "").strip()
            item = {
                "document_name": chunk.get("document_name", "未知文档"),
                "page": page,
                "score": chunk.get("score"),
                "url": chunk.get("url") or (
                    f"/source-documents/{dataset_id}/{document_id}"
                    if dataset_id and document_id
                    else None
                ),
            }
            # 兼容旧 API 的紧凑引用格式；多源结果才附加扩展元数据。
            if message.name == "unified_search" or any(
                chunk.get(key) is not None
                for key in ("source_type", "source_name", "version", "updated_at")
            ):
                item.update({
                    "source_type": chunk.get("source_type", "document"),
                    "source_name": chunk.get("source_name"),
                    "version": chunk.get("version"),
                    "updated_at": str(chunk.get("updated_at")) if chunk.get("updated_at") else None,
                })
            if chunk.get("evidence_id") is not None:
                item["evidence_id"] = str(chunk.get("evidence_id"))
            if chunk.get("chunk_id") is not None:
                item["chunk_id"] = str(chunk.get("chunk_id"))
            if content:
                item["excerpt"] = content[:320]
            key = (
                str(chunk.get("evidence_id") or ""),
                str(chunk.get("chunk_id") or ""),
                str(dataset_id or ""),
                str(document_id or item["document_name"]),
                f"{page if page is not None else ''}|{content}",
            )
            if key not in seen:
                seen.add(key)
                citations.append(item)
    return citations


def _collect_graph_relations(messages: list[Any]) -> list[dict[str, Any]]:
    """Collect graph edges and keep their original-document provenance.

    This is intentionally separate from ``citations`` and ``retrieval_hits``:
    a graph edge is a derived relationship, while a RAGFlow chunk is the
    source wording that can be opened and audited by the user.
    """

    relations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name != "unified_search":
            continue
        payload = _parse_tool_payload(message.content)
        for hit in payload.get("hits") or []:
            if not isinstance(hit, dict) or str(hit.get("source_type") or "").lower() != "graph":
                continue
            metadata = hit.get("metadata") or {}
            graph_relevance = metadata.get("graph_relevance")
            if isinstance(graph_relevance, dict) and graph_relevance.get("matched") is False:
                continue
            graph_relation = metadata.get("graph_relation")
            if isinstance(graph_relation, dict):
                normalized = dict(graph_relation)
            else:
                left = metadata.get("left") or {}
                right = metadata.get("right") or {}
                relation = metadata.get("relation") or {}
                if not all(isinstance(item, dict) for item in (left, right, relation)):
                    continue
                normalized = normalize_graph_relation_evidence(left, right, relation)
            relation_id = str(
                normalized.get("relation_id")
                or hit.get("evidence_id")
                or ""
            ).strip()
            if not relation_id or relation_id in seen:
                continue
            normalized["relation_id"] = relation_id
            normalized.setdefault("evidence_ids", [])
            normalized.setdefault("evidence", [])
            normalized.setdefault("evidence_status", "unverified")
            # Keep the graph query visible for execution-detail/debug views,
            # but do not expose the complete connector payload in the answer.
            if hit.get("metadata", {}).get("graph_query"):
                normalized["graph_query"] = str(hit["metadata"]["graph_query"])
            if isinstance(graph_relevance, dict):
                normalized["relevance"] = graph_relevance
            seen.add(relation_id)
            relations.append(normalized)
            if len(relations) >= MAX_RETRIEVAL_HITS:
                return relations
    return relations


def _collect_retrieval_hits(
    messages: list[Any],
    allowed_evidence_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Collect only evidence-gated RAGFlow chunks for client-side display."""
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in {"search_knowledge_base", "unified_search"}:
            continue
        payload = _parse_tool_payload(message.content)
        chunks = payload.get("chunks", [])
        if not isinstance(chunks, list):
            chunks = []
        if message.name == "unified_search":
            chunks = [
                {
                    "content": item.get("content", ""),
                    "chunk_id": item.get("chunk_id") or item.get("id"),
                    "title": item.get("title") or item.get("source_name") or "未知来源",
                    "document_name": item.get("title") or item.get("source_name") or "未知来源",
                    "document_id": item.get("document_id"),
                    "dataset_id": item.get("dataset_id"),
                    "page": item.get("page"),
                    "score": item.get("score"),
                    "evidence_id": item.get("evidence_id"),
                    "url": item.get("url"),
                    "source_type": item.get("source_type"),
                    "source_name": item.get("source_name"),
                    "version": item.get("version"),
                    "authority": item.get("authority"),
                    "freshness": item.get("freshness"),
                    "metadata": item.get("metadata") or {},
                }
                for item in (payload.get("hits") or [])
                if isinstance(item, dict)
            ]
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            if str(chunk.get("source_type") or "").strip().lower() == "graph":
                continue
            if allowed_evidence_ids is not None and str(chunk.get("evidence_id")) not in allowed_evidence_ids:
                continue
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            document_id = str(chunk.get("document_id") or "")
            dataset_id = str(chunk.get("dataset_id") or "")
            document_name = str(chunk.get("document_name") or "未知文档")
            page = chunk.get("page")
            key = (dataset_id, document_id, str(page), content)
            if key in seen:
                continue
            seen.add(key)
            result_hit = {
                "evidence_id": chunk.get("evidence_id"),
                "document_name": document_name,
                "page": page,
                "score": chunk.get("score"),
                "content": content[:MAX_RETRIEVAL_CONTENT_LENGTH],
                "url": chunk.get("url") or (
                    f"/source-documents/{dataset_id}/{document_id}"
                    if dataset_id and document_id
                    else None
                ),
            }
            if chunk.get("chunk_id") is not None:
                result_hit["chunk_id"] = str(chunk.get("chunk_id"))
            if message.name == "unified_search" or any(
                chunk.get(key) is not None
                for key in ("source_type", "source_name", "version", "authority", "freshness", "metadata")
            ):
                result_hit.update({
                    "source_type": chunk.get("source_type", "document"),
                    "source_name": chunk.get("source_name"),
                    "version": chunk.get("version"),
                    "authority": chunk.get("authority"),
                    "freshness": chunk.get("freshness"),
                    "metadata": chunk.get("metadata") or {},
                })
            hits.append(result_hit)
            if len(hits) >= MAX_RETRIEVAL_HITS:
                return hits

    return hits


def _merge_trace_token_usage(*usages: dict[str, int]) -> dict[str, int]:
    """Accumulate direct model-call usage not represented by LangGraph messages."""
    totals = {"input_tokens": 0, "output_tokens": 0}
    present = False
    for usage in usages:
        if not usage:
            continue
        present = True
        for key in totals:
            totals[key] += max(0, int(usage.get(key) or 0))
    return totals if present else {}


def _merge_final_trace_summary(
    trace: dict[str, Any],
    spans: list[dict[str, Any]],
    *,
    trace_id: str | None,
    total_duration_ms: int,
) -> None:
    """Make summary cards and visible spans use one canonical aggregation."""

    summary = summarize_trace_spans(
        spans,
        trace_id=trace_id,
        total_duration_ms=total_duration_ms,
    )
    trace.update({
        "trace_id": summary["trace_id"],
        "span_count": summary["span_count"],
        "tool_span_count": summary["tool_span_count"],
        "model_calls": summary["model_calls"],
        "input_tokens": summary["input_tokens"],
        "output_tokens": summary["output_tokens"],
        "total_tokens": summary["total_tokens"],
        "total_duration_ms": summary["total_duration_ms"],
        "token_source": summary["token_source"],
        "token_usage": summary["token_usage"],
        "spans": summary["spans"],
    })


def _build_execution_trace(
    messages: list[Any],
    total_duration_ms: int,
    evidence_summary: dict[str, Any] | None = None,
    requirements: list[dict[str, Any]] | None = None,
    requirement_results: list[dict[str, Any]] | None = None,
    coverage: dict[str, Any] | None = None,
    requirement_warning: str | None = None,
    requirement_model_calls: int = 0,
    requirement_token_usage: dict[str, int] | None = None,
    workflow_id: str | None = None,
    workflow_version: int | None = None,
    workflow_run_id: str | None = None,
    trace_id: str | None = None,
    trace_spans: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a safe client-facing trace from messages already produced by the agent."""
    model_calls = 0
    ragflow_calls = 0
    input_tokens = 0
    output_tokens = 0
    has_token_usage = False
    steps: list[dict[str, str]] = []

    for message in messages:
        if isinstance(message, AIMessage):
            metadata = getattr(message, "additional_kwargs", {}) or {}
            if metadata.get("synthetic_prefetch"):
                continue
            model_calls += 1
            usage = getattr(message, "usage_metadata", None) or {}
            if usage:
                has_token_usage = True
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)
            continue

        if not isinstance(message, ToolMessage):
            continue

        if message.name == "search_knowledge_base":
            payload = _parse_tool_payload(message.content)
            if payload.get("guarded"):
                continue
            ragflow_calls += 1
            chunks = [
                item for item in (payload.get("chunks") or [])
                if isinstance(item, dict)
            ]
            error = payload.get("error")
            documents = list(dict.fromkeys(
                str(item.get("document_name"))
                for item in chunks
                if item.get("document_name")
            ))
            if error:
                steps.append({
                    "name": "RAGFlow 知识库检索",
                    "detail": f"调用失败：{error}",
                    "status": "error",
                })
            else:
                document_text = "、".join(documents[:3])
                if len(documents) > 3:
                    document_text += f" 等 {len(documents)} 个文档"
                candidates_value = payload.get("candidate_count")
                candidates = int(
                    candidates_value if candidates_value is not None else len(chunks)
                )
                verified_value = payload.get("verified_count")
                verified = int(
                    verified_value if verified_value is not None else len(chunks)
                )
                provided_value = payload.get("provided_count")
                provided = int(
                    provided_value if provided_value is not None else len(chunks)
                )
                status = str(payload.get("evidence_status") or "unknown")
                if status == "unverified" or payload.get("validation_mode") == "off":
                    detail = (
                        f"检索词：{payload.get('query', '')}；候选 {candidates} 个；"
                        f"原样提供 {provided} 个；后端证据校验未执行；状态：{status}"
                    )
                else:
                    detail = (
                        f"检索词：{payload.get('query', '')}；候选 {candidates} 个；"
                        f"证据校验通过 {verified} 个；状态：{status}"
                    )
                if document_text:
                    detail += f"；来源：{document_text}"
                steps.append({"name": "RAGFlow 知识库检索", "detail": detail})

        elif message.name == "unified_search":
            payload = _parse_tool_payload(message.content)
            ragflow_calls += 1
            raw_hits = payload.get("hits") or []
            hits = [item for item in raw_hits if isinstance(item, dict)]
            source_names = list(dict.fromkeys(
                str(item.get("source_name") or item.get("source_type") or "未知来源")
                for item in hits
            ))
            sources_text = "、".join(source_names[:4]) or "无可用来源"
            error_text = "；".join(str(item) for item in payload.get("errors") or [])
            steps.append({
                "name": "统一搜索（多数据源）",
                "detail": (
                    f"命中 {len(hits)} 条；来源：{sources_text}"
                    + (f"；部分来源失败：{error_text}" if error_text else "")
                ),
                "status": "error" if error_text and not hits else "success",
            })

        elif message.name == "list_knowledge_bases":
            payload = _parse_tool_payload(message.content)
            if payload.get("guarded"):
                continue
            ragflow_calls += 1
            error = payload.get("error")
            if error:
                steps.append({
                    "name": "RAGFlow 知识库列表",
                    "detail": f"调用失败：{error}",
                    "status": "error",
                })
            else:
                steps.append({
                    "name": "RAGFlow 知识库列表",
                    "detail": f"读取到 {int(payload.get('total') or 0)} 个知识库",
                })

    used_ragflow = ragflow_calls > 0
    if used_ragflow:
        steps.insert(0, {
            "name": "知识问题路由",
            "detail": "后端证据策略判断本轮必须调用 RAGFlow，不允许模型跳过检索",
        })
        if requirements is None:
            steps.append({
                "name": "Qwen 生成回答",
                "detail": "模型结合对话上下文和工具返回结果生成最终答案",
            })
    else:
        steps.extend([
            {
                "name": "Qwen 分析并回答",
                "detail": "本次没有调用 RAGFlow，由模型根据对话上下文直接回答",
            },
        ])

    if evidence_summary and evidence_summary.get("requires_evidence"):
        status = str(evidence_summary.get("status") or "unknown")
        status_text = {
            "supported": "证据充分",
            "unverified": "未经过后端证据校验",
            "partial": "部分证据",
            "not_found": "未找到可靠证据",
            "retrieval_error": "知识库调用失败",
            "not_retrieved": "应检索但未检索",
            "answer_blocked": "答案被安全校验拦截",
            "conflict": "知识库证据冲突",
            "not_required": "无需知识证据",
            "business_action": "业务工具结果",
            "agent_error": "Agent 执行失败",
            "unsupported": "未支持",
            "unknown": "未知",
        }.get(status, status)
        candidate_count = int(evidence_summary.get("candidate_count") or 0)
        verified_count = int(evidence_summary.get("verified_count") or 0)
        rejected_count = int(evidence_summary.get("rejected_count") or 0)
        if status == "unverified":
            provided_count = int(evidence_summary.get("provided_count") or 0)
            detail = (
                f"结论：{status_text}；候选 {candidate_count} 个；"
                f"原样提供 {provided_count} 个；后端校验未执行"
            )
        else:
            detail = (
                f"结论：{status_text}；候选 {candidate_count} 个；"
                f"通过 {verified_count} 个；拒绝 {rejected_count} 个"
            )
        violations = evidence_summary.get("violations") or []
        if violations:
            detail += "；" + "；".join(str(item) for item in violations[:3])
        steps.append({
            "name": "证据与答案安全校验",
            "detail": detail,
            "status": "error" if status in {"retrieval_error", "not_retrieved", "answer_blocked"} else "success",
        })

    if requirements is not None:
        steps.insert(1 if used_ragflow else 0, {
            "name": "用户需求规划",
            "detail": f"拆分为 {len(requirements)} 个可独立检索和发布的需求项",
            "status": "error" if requirement_warning and not requirement_results else "success",
        })

    if requirement_results is not None and requirements is not None:
        status_counts = {
            status: sum(
                1 for item in requirement_results or []
                if item.get("status") == status
            )
            for status in ("supported", "unsupported", "conflict")
        }
        detail = (
            f"发布 {len(requirement_results or [])} 个需求结果；已支持 {status_counts['supported']} 个；"
            f"未支持 {status_counts['unsupported']} 条；证据冲突 {status_counts['conflict']} 条"
        )
        if coverage is not None:
            detail += (
                "；覆盖审计通过"
                if coverage.get("complete")
                else "；覆盖审计未完全通过"
            )
        if requirement_warning:
            detail += f"；{requirement_warning}"
        steps.append({
            "name": "逐项结果生成、证据裁判与覆盖审计",
            "detail": detail,
            "status": "error" if requirement_warning else "success",
        })

    model_calls += max(0, requirement_model_calls)
    if requirement_token_usage:
        has_token_usage = True
        input_tokens += max(0, int(requirement_token_usage.get("input_tokens") or 0))
        output_tokens += max(0, int(requirement_token_usage.get("output_tokens") or 0))
    total_tokens = input_tokens + output_tokens if has_token_usage else None

    spans = [dict(item) for item in (trace_spans or []) if isinstance(item, dict)]
    if not spans:
        # Unit callers and legacy integrations may not install the runtime
        # callback handler.  Keep the trace useful by deriving coarse spans
        # from the messages that are already part of the public response.
        for index, message in enumerate(messages, start=1):
            if isinstance(message, AIMessage):
                metadata = getattr(message, "response_metadata", {}) or {}
                model = metadata.get("model_name") if isinstance(metadata, dict) else None
                usage = extract_token_usage(message)
                spans.append({
                    "span_id": f"message-llm-{index}",
                    "trace_id": trace_id or workflow_run_id,
                    "type": "llm",
                    "name": f"大模型：{model or 'Qwen'}",
                    "component": "qwen",
                    "status": "success",
                    "duration_ms": None,
                    "model": model,
                    "provider": "provider-metadata" if model else "DashScope/OpenAI-compatible",
                    "model_calls": 1,
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "token_source": "provider" if usage else "unavailable",
                    "attributes": {"derived_from": "AIMessage"},
                })
            elif isinstance(message, ToolMessage):
                payload = _parse_tool_payload(message.content)
                if message.name in {"search_knowledge_base", "unified_search", "list_knowledge_bases"}:
                    if message.name == "unified_search":
                        source_type = "unified_search"
                        name = "统一搜索编排器"
                        component = "unified_search"
                        count = len(payload.get("hits") or [])
                    elif message.name == "list_knowledge_bases":
                        source_type = "document"
                        name = "RAGFlow 知识库目录"
                        component = "ragflow"
                        count = len(payload.get("datasets") or [])
                    else:
                        source_type = "document"
                        name = "RAGFlow 知识库"
                        component = "ragflow"
                        count = len(payload.get("chunks") or [])
                    spans.append({
                        "span_id": f"message-tool-{index}",
                        "trace_id": trace_id or workflow_run_id,
                        "type": "retriever" if source_type == "document" else "api",
                        "name": name,
                        "component": component,
                        "status": "error" if payload.get("error") or payload.get("errors") else "success",
                        "duration_ms": payload.get("duration_ms"),
                        "model_calls": 0,
                        "tool_name": message.name,
                        "token_source": "unavailable",
                        "attributes": {"result_count": count, "derived_from": "ToolMessage"},
                        "error": payload.get("error") or "；".join(str(item) for item in payload.get("errors") or []) or None,
                    })
        for index, step in enumerate(steps, start=1):
            spans.append({
                "span_id": f"step-{index}",
                "trace_id": trace_id or workflow_run_id,
                "type": "stage",
                "name": f"阶段：{step.get('name') or '执行步骤'}",
                "component": "backend",
                "status": step.get("status", "success"),
                "duration_ms": step.get("duration_ms"),
                "model_calls": 0,
                "token_source": "unavailable",
                "attributes": {"detail": step.get("detail", ""), "derived_from": "TraceStep"},
            })

    # A test double or an older connector may return a ToolMessage without
    # going through the new recorder.  Fill only the missing tool occurrences
    # so the public trace still says which retrieval/tool entry ran.
    observed_tool_counts: dict[str, int] = {}
    for item in spans:
        tool_name = str(item.get("tool_name") or "")
        if tool_name:
            observed_tool_counts[tool_name] = observed_tool_counts.get(tool_name, 0) + 1
    message_tool_counts: dict[str, int] = {}
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, ToolMessage) or message.name not in {"search_knowledge_base", "unified_search", "list_knowledge_bases"}:
            continue
        tool_name = str(message.name)
        message_tool_counts[tool_name] = message_tool_counts.get(tool_name, 0) + 1
        if message_tool_counts[tool_name] <= observed_tool_counts.get(tool_name, 0):
            continue
        payload = _parse_tool_payload(message.content)
        if tool_name == "unified_search":
            component, name, span_type, count = "unified_search", "统一搜索编排器", "api", len(payload.get("hits") or [])
        elif tool_name == "list_knowledge_bases":
            component, name, span_type, count = "ragflow", "RAGFlow 知识库目录", "retriever", len(payload.get("datasets") or [])
        else:
            component, name, span_type, count = "document", "RAGFlow 知识库", "retriever", len(payload.get("chunks") or [])
        spans.append({
            "span_id": f"message-tool-fallback-{index}",
            "trace_id": trace_id or workflow_run_id,
            "type": span_type,
            "name": name,
            "component": component,
            "status": "error" if payload.get("error") or payload.get("errors") else "success",
            "duration_ms": payload.get("duration_ms"),
            "model_calls": 0,
            "tool_name": tool_name,
            "token_source": "unavailable",
            "attributes": {"result_count": count, "derived_from": "ToolMessage"},
            "error": payload.get("error") or "；".join(str(item) for item in payload.get("errors") or []) or None,
        })

    span_input = sum(
        int(item.get("input_tokens") or 0)
        for item in spans
        if item.get("type") == "llm" and item.get("input_tokens") is not None
    )
    span_output = sum(
        int(item.get("output_tokens") or 0)
        for item in spans
        if item.get("type") == "llm" and item.get("output_tokens") is not None
    )
    span_model_calls = sum(
        int(item.get("model_calls") or 0)
        for item in spans
        if item.get("type") == "llm"
        and (
            item.get("status") != "error"
            or item.get("input_tokens") is not None
            or item.get("output_tokens") is not None
        )
    )
    if not has_token_usage and (span_input or span_output):
        input_tokens, output_tokens = span_input, span_output
        total_tokens = span_input + span_output
        has_token_usage = True
    model_calls = max(model_calls, span_model_calls)
    provider_token_spans = [
        item for item in spans
        if item.get("type") == "llm" and item.get("token_source") == "provider"
    ]
    token_source = "provider" if has_token_usage and provider_token_spans else ("partial" if spans else "unavailable")
    return {
        "route": "unified_search" if any(
            isinstance(message, ToolMessage) and message.name == "unified_search"
            for message in messages
        ) else ("ragflow" if used_ragflow else "llm_only"),
        "used_ragflow": used_ragflow,
        "trace_id": trace_id or workflow_run_id,
        "model_calls": model_calls,
        "ragflow_calls": ragflow_calls,
        "input_tokens": input_tokens if has_token_usage else None,
        "output_tokens": output_tokens if has_token_usage else None,
        "total_tokens": total_tokens,
        "total_duration_ms": total_duration_ms,
        "token_source": token_source,
        "span_count": len(spans),
        "tool_span_count": sum(item.get("type") in {"tool", "retriever", "api"} for item in spans),
        "token_usage": {
            "input_tokens": input_tokens if has_token_usage else None,
            "output_tokens": output_tokens if has_token_usage else None,
            "total_tokens": total_tokens,
            "source": token_source,
        },
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "workflow_run_id": workflow_run_id,
        "spans": spans,
        "steps": steps,
    }


def _parse_tool_payload(content: Any) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _summarize_evidence(
    messages: list[Any],
    query: str,
    *,
    min_similarity: float = 0.0,
    generic_min_similarity: float = 0.0,
    evidence_mode: str | None = None,
) -> dict[str, Any]:
    """Merge multiple tool calls and re-check them against the original query."""
    chunks: list[dict[str, Any]] = []
    list_chunks: list[dict[str, Any]] = []
    ragflow_calls = 0
    search_calls = 0
    list_calls = 0
    candidate_count = 0
    errors: list[str] = []
    similar_entities: list[dict[str, Any]] = []
    seen_similar_entities: set[tuple[str, str]] = set()
    seen_chunks: set[tuple[str, str, str, str]] = set()

    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        payload = _parse_tool_payload(message.content)
        if message.name == "search_knowledge_base":
            if payload.get("guarded"):
                continue
            ragflow_calls += 1
            search_calls += 1
            if payload.get("error"):
                errors.append(str(payload["error"]))
            raw_chunks = payload.get("chunks") or []
            payload_chunks = [item for item in raw_chunks if isinstance(item, dict)]
            candidate_count += int(payload.get("candidate_count") or len(payload_chunks))
            for item in payload.get("similar_entities") or []:
                if not isinstance(item, dict):
                    continue
                key = (
                    canonical_identifier(str(item.get("requested") or "")),
                    canonical_identifier(str(item.get("candidate") or "")),
                )
                if all(key) and key not in seen_similar_entities:
                    seen_similar_entities.add(key)
                    similar_entities.append(dict(item))
            for chunk in payload_chunks:
                key = (
                    str(chunk.get("dataset_id") or ""),
                    str(chunk.get("document_id") or ""),
                    str(chunk.get("page") or ""),
                    str(chunk.get("version") or ""),
                    str(chunk.get("content") or ""),
                )
                if key not in seen_chunks:
                    seen_chunks.add(key)
                    chunks.append(chunk)
        elif message.name == "unified_search":
            # 统一搜索结果采用 SearchHit 契约；转换为现有证据管线理解的
            # chunk 形状，保留 source_type、版本和更新时间等多源元数据。
            # 旧的证据门控以 retrieval call 计数；把统一搜索视为一次
            # 有效检索，避免多源结果被误判为“模型跳过检索”。
            ragflow_calls += 1
            search_calls += 1
            if payload.get("errors"):
                errors.extend(str(item) for item in payload.get("errors") or [])
            raw_hits = payload.get("hits") or []
            hits = [item for item in raw_hits if isinstance(item, dict)]
            candidate_count += int(payload.get("total") or len(hits))
            for hit in hits:
                chunk = {
                    "content": hit.get("content", ""),
                    "document_name": hit.get("title") or hit.get("source_name") or "未知来源",
                    "document_id": hit.get("document_id"),
                    "dataset_id": hit.get("dataset_id"),
                    "page": hit.get("page"),
                    "score": hit.get("score", 0),
                    "evidence_id": hit.get("evidence_id"),
                    "source_type": hit.get("source_type"),
                    "source_name": hit.get("source_name"),
                    "version": hit.get("version"),
                    "effective_from": hit.get("effective_from"),
                    "effective_to": hit.get("effective_to"),
                    "updated_at": hit.get("updated_at"),
                    "authority": hit.get("authority"),
                    "freshness": hit.get("freshness"),
                    "metadata": hit.get("metadata") or {},
                }
                key = (
                    str(chunk.get("source_type") or ""),
                    str(chunk.get("document_id") or chunk.get("document_name") or ""),
                    str(chunk.get("page") or ""),
                    str(chunk.get("version") or ""),
                    str(chunk.get("content") or ""),
                )
                if key not in seen_chunks:
                    seen_chunks.add(key)
                    chunks.append(chunk)
        elif message.name == "list_knowledge_bases":
            if payload.get("guarded"):
                continue
            ragflow_calls += 1
            list_calls += 1
            if payload.get("error"):
                errors.append(str(payload["error"]))
                continue
            datasets = [
                item for item in (payload.get("datasets") or [])
                if isinstance(item, dict)
            ]
            candidate_count += len(datasets)
            for index, dataset in enumerate(datasets):
                dataset_id = str(dataset.get("id") or "")
                name = str(dataset.get("name") or "未命名知识库")
                list_chunks.append({
                    "content": f"知识库名称：{name}\n知识库 ID：{dataset_id}",
                    "document_name": "RAGFlow 知识库列表",
                    "document_id": f"dataset-list-{index}",
                    "dataset_id": dataset_id,
                    "page": None,
                    "score": 1.0,
                })

    all_chunks = chunks + list_chunks
    assessment = assess_evidence(
        query,
        all_chunks,
        # These chunks are retrieval candidates.  The second pass binds
        # multi-search results back to the original query; score is retained
        # for ordering and diagnostics, not used as an independent gate.
        min_similarity=min_similarity,
        generic_min_similarity=generic_min_similarity,
        mode=evidence_mode or normalize_evidence_mode(
            getattr(get_settings(), "evidence_mode", "generic")
        ),
    )
    assessment["candidate_count"] = max(candidate_count, len(all_chunks))
    normalized_evidence_mode = evidence_mode or normalize_evidence_mode(
        getattr(get_settings(), "evidence_mode", "generic")
    )
    if normalized_evidence_mode == "off":
        assessment["provided_count"] = len(assessment.get("chunks") or [])
        assessment["verified_count"] = 0
        assessment["rejected_count"] = 0
    else:
        assessment["verified_count"] = len(assessment.get("chunks") or [])
        assessment["rejected_count"] = max(
            assessment["candidate_count"] - assessment["verified_count"],
            0,
        )
    assessment["ragflow_calls"] = ragflow_calls
    assessment["search_calls"] = search_calls
    assessment["list_calls"] = list_calls
    missing_entity_keys = {
        canonical_identifier(str(item))
        for item in assessment.get("missing_entities") or []
    }
    assessment["similar_entities"] = [
        item
        for item in similar_entities
        if canonical_identifier(str(item.get("requested") or "")) in missing_entity_keys
    ][:3]
    assessment["has_error"] = bool(errors)
    if errors:
        assessment["errors"] = errors
        if not assessment["chunks"]:
            assessment["status"] = "retrieval_error"
            assessment["reason"] = "；".join(errors)
    return assessment


def _build_document_presence_answer(
    query: str,
    verified_chunks: list[dict[str, Any]],
) -> str | None:
    """Answer a single-model document-presence question from verified names."""
    if "document_presence" not in extract_aspects(query):
        return None

    requested = extract_identifiers(query)
    if len(requested) != 1:
        return None

    target = requested[0]
    target_key = canonical_identifier(target)
    names: list[str] = []
    seen: set[str] = set()
    for chunk in verified_chunks:
        name = str(chunk.get("document_name") or "").strip()
        name_identifiers = {
            canonical_identifier(item)
            for item in extract_identifiers(name)
        }
        if target_key not in name_identifiers or name in seen:
            continue
        seen.add(name)
        names.append(name)

    if not names:
        return None

    names.sort(key=lambda item: (not item.casefold().endswith(".pdf"), item.casefold()))
    files = "\n".join(f"- `{name}`" for name in names)
    return f"有。当前知识库已收录与 {target} 匹配的资料：\n{files}"


def _safe_missing_evidence_suggestions(evidence: dict[str, Any]) -> list[str]:
    similar = evidence.get("similar_entities") or []
    if similar:
        candidate = str(similar[0].get("candidate") or "").strip()
        if candidate:
            return [
                f"知识库中是否有 {candidate} 的说明书？",
                f"请介绍 {candidate} 的主要技术参数。",
            ]
    missing = evidence.get("missing_entities") or evidence.get("requested_entities") or []
    if missing:
        target = str(missing[0])
        return [
            f"知识库中是否有 {target} 的说明书？",
            f"需要补充什么资料才能确认 {target}？",
        ]
    return ["换一种更具体的方式检索", "查看当前知识库"]


def _context_ambiguity_result(
    query: str,
    context_resolution: dict[str, Any],
    total_duration_ms: int,
    *,
    trace_recorder=None,
) -> dict[str, Any]:
    """Return a clarification instead of retrieving evidence for a guess."""

    candidates = [
        str(item).strip()
        for item in context_resolution.get("candidates") or []
        if str(item).strip()
    ]
    candidate_text = "、".join(candidates)
    answer = (
        f"当前问题“{query}”没有明确设备或模块对象。历史对话中有多个候选：{candidate_text}。"
        "请明确要查询哪一个对象，我再继续检索。"
    )
    trace = trace_recorder.export(total_duration_ms=total_duration_ms) if trace_recorder else {}
    evidence = {
        "status": "context_ambiguous",
        "requires_evidence": query_requires_evidence(query),
        "reason": context_resolution.get("reason") or "上下文对象存在歧义",
        "candidate_count": 0,
        "verified_count": 0,
        "rejected_count": 0,
    }
    trace_payload = {
        "route": "context_clarification",
        "trace_id": trace.get("trace_id"),
        "used_ragflow": False,
        "model_calls": 0,
        "ragflow_calls": 0,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "total_duration_ms": total_duration_ms,
        "token_source": trace.get("token_source", "unavailable"),
        "span_count": trace.get("span_count", 0),
        "tool_span_count": trace.get("tool_span_count", 0),
        "token_usage": trace.get("token_usage", {}),
        "spans": trace.get("spans", []),
        "steps": [{
            "name": "上下文对象校验",
            "detail": evidence["reason"],
            "status": "waiting_user",
        }],
        "context_resolution": context_resolution,
    }
    return {
        "answer": answer,
        "claims": [],
        "requirements": [],
        "results": [],
        "coverage": None,
        "suggestions": build_clarification_suggestions(query, candidates),
        "citations": [],
        "retrieval_hits": [],
        "graph_relations": [],
        "evidence": evidence,
        "context_resolution": context_resolution,
        "trace": trace_payload,
    }


def _agent_error_result(
    query: str,
    exc: Exception,
    total_duration_ms: int,
    *,
    trace_recorder=None,
) -> dict[str, Any]:
    error_text = str(exc)
    lowered = error_text.casefold()
    if "allocationquota.freetieronly" in lowered or "free quota exhausted" in lowered:
        reason = "Qwen 模型免费额度已耗尽，或百炼控制台启用了“仅使用免费额度”"
        answer = (
            "当前 Qwen 模型额度不可用，暂时无法完成问答。请在阿里云百炼控制台开通可用额度，"
            "关闭“仅使用免费额度”，或切换到仍有额度且支持工具调用的模型后重试。"
        )
        suggestion = "检查 Qwen 模型额度"
    elif "apiconnectionerror" in lowered or "connection error" in lowered:
        reason = "无法连接 Qwen 模型服务，请检查网络、代理和 DASHSCOPE_BASE_URL"
        answer = "当前无法连接 Qwen 模型服务，暂时不能完成问答。请检查网络、代理和模型接口地址后重试。"
        suggestion = "检查 Qwen 连接配置"
    else:
        reason = f"Agent 调用失败：{error_text}"
        answer = "处理问题时发生错误，系统没有生成未经验证的答案。请展开调用链查看原因后重试。"
        suggestion = "重新提问"

    requires_evidence = query_requires_evidence(query)
    evidence = {
        "status": "agent_error",
        "requires_evidence": requires_evidence,
        "reason": reason,
        "candidate_count": 0,
        "verified_count": 0,
        "rejected_count": 0,
    }
    trace = trace_recorder.export(total_duration_ms=total_duration_ms) if trace_recorder else {}
    return {
        "answer": answer,
        "claims": [],
        "requirements": [],
        "results": [],
        "coverage": None,
        "suggestions": [{"text": suggestion, "query": suggestion}],
        "citations": [],
        "retrieval_hits": [],
        "graph_relations": [],
        "evidence": evidence,
        "trace": {
            "route": "error",
            "trace_id": trace.get("trace_id"),
            "used_ragflow": False,
            "model_calls": 0,
            "ragflow_calls": 0,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "total_duration_ms": total_duration_ms,
            "token_source": trace.get("token_source", "unavailable"),
            "span_count": trace.get("span_count", 0),
            "tool_span_count": trace.get("tool_span_count", 0),
            "token_usage": trace.get("token_usage", {}),
            "spans": trace.get("spans", []),
            "steps": [{"name": "Agent 调用失败", "detail": reason, "status": "error"}],
        },
    }
