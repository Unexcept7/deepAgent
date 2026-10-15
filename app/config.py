from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    dashscope_api_key: str | None = None
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.8-max"
    qwen_max_retries: int = 1
    # DashScope 直连通常比读取本机代理更稳定；企业网络需要代理时可改为 true。
    qwen_trust_env: bool = False
    # 模型中心保存非敏感模型档案和阶段路由；密钥仍通过环境变量引用。
    model_registry_store_path: str | None = None
    model_routing_enabled: bool = True
    max_agent_steps: int = 30
    max_ragflow_calls: int = 1
    # 统一搜索配置。默认启用多源抽象，但只有同时满足配置条件的连接器
    # 才会真正发起请求；未配置的来源会在响应 warnings 中明确提示。
    unified_search_enabled: bool = True
    unified_search_top_k: int = 12
    # 证据检索采用渐进式 Top-K：先取较小候选集，证据不足、覆盖不完整
    # 或出现冲突时再扩大到 unified_search_top_k，避免普通问题携带过多片段。
    unified_search_initial_top_k: int = 6
    unified_search_timeout_seconds: int = 8
    unified_search_prefetch_enabled: bool = False
    search_sources: str = "document,qa,graph,sql,api"
    search_max_parallel: int = 4
    qa_data_path: str | None = None
    business_api_base_url: str | None = None
    business_api_token: str | None = None
    business_api_timeout_seconds: int = 8
    sql_search_url: str | None = None
    sql_search_token: str | None = None
    sql_search_timeout_seconds: int = 8
    sql_search_max_rows: int = 100
    mcp_search_url: str | None = None
    mcp_search_token: str | None = None
    graph_provider: str = "none"
    neo4j_uri: str | None = None
    neo4j_username: str = "neo4j"
    neo4j_password: str | None = None
    # 图谱导入是高风险写操作。留空时只允许查询，任何导入请求都会拒绝。
    graph_write_token: str | None = None
    # 业务写入默认需要用户确认，高风险动作默认进入审批。
    write_action_default: str = "confirm"
    high_risk_action_default: str = "approval"
    audit_log_enabled: bool = True

    ragflow_base_url: str = "http://localhost:9380"
    ragflow_api_key: str | None = None
    ragflow_dataset_id: str | None = None
    ragflow_dataset_ids: str | None = None
    # 默认关闭全库自动发现，避免检索结果跨知识库串库。
    ragflow_auto_discover_datasets: bool = False
    # off 不执行后端证据底线和完整性约束，RAGFlow 候选原样交给 Qwen。
    # generic 执行通用证据校验；industrial_strict 执行型号、属性和参数级强校验。
    evidence_mode: str = "generic"
    # 证据门控阈值。相似度只用于候选排序和诊断；实体/内容/属性底线
    # 决定证据是否保留，不把相似度单独当成事实证明或阻断条件。
    evidence_min_similarity: float = 0.2
    evidence_generic_min_similarity: float = 0.3
    # 留空时不在 API 请求中指定重排模型；设置 qwen3-rerank 后由 RAGFlow
    # 先对较大的候选集重排，再返回 RAGFLOW_TOP_K 个片段。
    ragflow_rerank_id: str = ""
    ragflow_top_k: int = 4
    # RAGFlow 在调用重排模型前使用的内部召回候选数。它不是最终返回数：
    # 最终返回数由 RAGFLOW_TOP_K 控制。RAGFlow 会按 page_size 向上取整
    # 重排窗口，因此这里保留有限的过召回即可，不应无界增大。
    ragflow_retrieval_top_k: int = 24
    ragflow_chunk_max_chars: int = 2200
    # 知识库目录页使用较短的超时，避免 RAGFlow 未启动时阻塞管理界面。
    ragflow_catalog_timeout_seconds: int = 5

    # 为事实型知识问答生成结构化声明，并逐条进行证据裁判。
    claim_validation_enabled: bool = True
    claim_max_items: int = 12
    requirement_max_items: int = 12
    requirement_max_parallel_searches: int = 4
    requirement_plan_audit_enabled: bool = True
    # Fact-bound answer assembly.  The requirement judge creates the factual
    # ledger; the answer model only selects fact IDs and connective wording.
    # The server renders values from that ledger and performs only binding
    # contract checks, so no free-form model answer is soft-bound afterward.
    answer_assembly_mode: str = "fact_binding"
    answer_repair_max_attempts: int = 1

    # 工作流编排定义默认落在 JSON 文件；部署到数据库时可由环境变量覆盖。
    workflow_store_path: str | None = None
    # 路由策略定义默认落在 JSON 文件；部署到数据库时可由环境变量覆盖。
    routing_policy_store_path: str | None = None
    # 语义路由 Prompt 定义默认落在 JSON 文件；部署到数据库时可由环境变量覆盖。
    semantic_prompt_store_path: str | None = None
    # 业务词典定义默认落在 JSON 文件；部署到数据库时可由环境变量覆盖。
    dictionary_store_path: str | None = None
    # 应用中心定义默认落在 JSON 文件；部署到数据库时可由环境变量覆盖。
    application_store_path: str | None = None
    # 多智能体评测结果默认落在 JSON 文件；部署到数据库时可由环境变量覆盖。
    evaluation_store_path: str | None = None
    # 统一搜索连接器的短路保护，避免单个上游连续故障拖慢每轮 Agent 请求。
    search_circuit_failure_threshold: int = 2
    search_circuit_cooldown_seconds: int = 15
    # 工具中心使用 SQLite 保存定义、版本、授权确认和审计记录。
    tool_center_db_path: str | None = None
    tool_center_encryption_keys: str | None = None
    tool_center_auth_tokens: str | None = None
    tool_center_local_admin: bool = True
    tool_center_allowed_hosts: str = "localhost,127.0.0.1,::1,host.docker.internal"
    tool_center_session_hours: int = 8
    tool_center_confirmation_minutes: int = 15

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=("settings_",),
    )

    def configured_dataset_ids(self) -> list[str]:
        values: list[str] = []
        if self.ragflow_dataset_id:
            # 兼容误将多个 ID 写入单数变量的情况，例如 ID1,ID2。
            values.extend(item.strip() for item in self.ragflow_dataset_id.split(","))
        if self.ragflow_dataset_ids:
            values.extend(item.strip() for item in self.ragflow_dataset_ids.split(","))
        return list(dict.fromkeys(item for item in values if item))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
