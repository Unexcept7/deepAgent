# DeepAgent 核心优化交付记录

本文件记录首轮优化。随后复审发现的五项数值绑定、补检及评测问题，其修复方案和最新验证结果见 [CORE_FIX_PLAN.md](CORE_FIX_PLAN.md)。首轮测试全绿不代表这些反例已经覆盖。

## 范围与基准

日期：2026-10-15。分支：master。基准提交：cb9b9e2baa2a770a4f3aed996d05c833147b254e。

修改仅涉及核心问答、检索、证据、上下文、执行预算、评测及必要 schema、配置、测试和说明文档。没有改登录权限、会话所有权、前端、管理界面、部署、CI、数据库选型；没有新增数据库、多 Agent 架构或依赖，没有提交或推送。

实际链路：`run_agent → resolve_history_aware_query/build_semantic_context → semantic router/build_safe_retrieval_plan → plan_requirements → 后端预检索 → assess_evidence → evaluate_requirements（生成与独立裁判）→ compose_answer → validate_answer_plan → 引用筛选/API 最终结果`。普通事实问答已有跳过自由回答模型的路径，继续复用；业务工具任务保留原 Agent。

## 修复与证明

以下新增用例均为合成逻辑回归，不能当作真实制造业业务金标。测试主体为 `tests/test_core_regressions.py`。

| 问题及原因 | 修改 | 回归证明 | 调用与耗时影响 |
|---|---|---|---|
| required_concepts 拼接检索正文，导致错误或空答案通过 | 答案、召回、上下文、证据、引用分组；答案指标仅依据答案；结构化金标检查型号、属性、值、单位、条件 | retrieval_cannot_supply_missing_or_negated_answer、structured_evaluation_checks_unit_exact_model_and_attribute、citation_metric_requires_value_support_not_only_existing_id | 不新增模型调用 |
| 引用 ID 合法但关键值错误；同文档多型号值互串 | 绑定证据的正文核对关键单位、型号、协议与限定词；多型号按相关语句检查；否定协议不作支持 | legal_evidence_id_cannot_support_wrong_unit_model_or_protocol、object_values_from_one_document_cannot_be_swapped、rated_value_cannot_prove_maximum_and_negated_protocol_is_not_support | 确定性检查，无新增模型 |
| 事实计划的 text/prefix/suffix 可以写任意技术结论 | 对象、属性、值由服务器渲染；连接文字限定为安全说明；校验渲染一致性及已支持需求遗漏 | unbound_text_and_fact_prefix_cannot_publish_injected_technical_claim；原 answer_composer 测试 | 失败时仍最多一次答案修复；表达灵活性收窄 |
| 自由回答自动挂全部证据 | 未声明绑定的旧自由回答拒绝发布；继续保留结构化及事实账本路径 | test_run_agent_does_not_attach_all_evidence_to_unbound_plain_answer | 不增加模型调用；旧自由回答可能更多安全回退 |
| 流式输出先发未经校验的技术文字 | 事实路径缓冲 token；普通对话继续流式输出 | fact_stream_buffers_unvalidated_tokens；原 streaming 测试 | 事实内容首 token 推迟至校验后 |
| 历史候选累计导致切换后持续歧义；模型推测固化摘要 | 最近明确用户轮次优先；同轮多对象仍澄清；单对象直接补全；摘要不吸收无用户文字依据的别名推测 | explicit_switch_updates_followup_and_comparison_stays_ambiguous；原 context/clarification 测试 | 明确单对象追问减少改写模型调用 |
| 时间上下文受宿主时区影响且难测试 | 使用配置业务时区，允许 build_semantic_context 注入时钟，向语义规划传当前时间 | clock_is_injected_and_business_timezone_controls_date | 无模型调用增加 |
| source_type 聚合丢失独立连接和查询排名；异构分数打破并列 | 按连接+查询路径做 RRF；并列用稳定 ID；合并查询命中信息，保留页和版本 | independent_connections_keep_rank_and_merge_query_provenance、page_and_version_are_separate_evidence；原 unified_search 测试 | 不新增重排模型 |
| 补检重复全计划，且覆盖首轮成功结果 | 定向检索缺失需求，合并首轮与补检结果；请求内相同 RAGFlow 调用缓存 | progressive_search_only_targets_missing_attribute、request_cache_and_budgets_are_shared_and_bounded | 减少重复检索；仍只有一轮扩大检索 |
| 人工固定 authority/freshness，看起来像真实元数据 | 缺失值保留 null；有实际字段时保留，包括 0；排序仅使用中性缺省 | 原统一搜索、SQL、工具中心测试 | 无模型调用增加 |
| 版本与生效元数据未送入裁判；证据输入缺少独立预算 | 传递版本、生效期、更新时间；保留冲突；按完整片段和文档覆盖限制证据输入 | conflict_keeps_sources_and_does_not_choose_a_version、evidence_context_budget_preserves_cross_document_coverage | 限制输入字符；不是实际 Token 收益测量 |
| 搜索 timeout_ms 未控制等待，线程池退出继续等待；来源错误变无答案 | 固定容量共享池、剩余预算与连接等待超时；旧 RAGFlow 工具也使用有界任务；保留已完成连接证据；公开来源错误 | slow_source_keeps_completed_evidence_and_returns_before_worker_finishes、legacy_ragflow_timeout_keeps_completed_connection_results | 慢来源提前返回；运行线程仍占容量直至真正结束 |
| 管理连接每次新服务重置熔断，同类连接互相污染 | 受管熔断状态跨请求复用，按工作区/类型/连接隔离 | connection_failure_does_not_open_sibling_circuit、managed_circuit_survives_service_recreation | 连续故障避免重复上游调用 |
| 条件只展示跳过，仍执行搜索；未知条件默认通过 | 执行搜索分支前检查已有条件和可达路径；不支持表达式拒绝执行 | false_or_unknown_workflow_condition_cannot_call_search | 条件不满足时零对应检索调用 |
| 部分字段缺失或冲突阻断其余值 | 继续输出 supported 事实，说明缺失和冲突来源；来源故障标注 source_error 原因 | partial_results_publish_supported_values_and_missing_attributes、conflict_keeps_sources_and_does_not_choose_a_version | 无新增模型调用 |

四个旧测试的契约明确修正：事实计划与旧通用校验都不能将 XP3018 的值仅凭文件名套用为 XP3018-AO；不能用异构原始分数打破 RRF 并列；不能为未绑定的自由回答自动挂全部证据。没有关闭校验或把错误结果改成通过。

## 验证命令与结果

基准代码由指定提交的 app/tests/scripts/services 导出到临时目录，以同样离线方法测试。临时基准和测试目录已清理。

为防止既有测试读取本机模型配置后连接真实服务，测试进程禁用真实 ChatOpenAI invoke/stream 和 requests HTTP；现有 FakeModel/FakeConnector 保留。Windows 沙箱限制会阻碍 TestClient 本地通信，完整测试在沙箱外运行，仍维持上述离线限制。

实际完整命令（在对应代码根目录运行）：

```powershell
& 'C:\Users\22069\.conda\envs\deepAgent-demo\python.exe' -u -c "import pytest, requests; from langchain_openai import ChatOpenAI; deny = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('offline regression: live provider disabled')); ChatOpenAI.invoke = deny; ChatOpenAI.stream = deny; requests.Session.request = deny; raise SystemExit(pytest.main(['tests', '-q', '--basetemp=.test-tmp-final-proof', '--tb=short']))"
```

| 指标 | 基准 | 修改后 |
|---|---:|---:|
| 完整 pytest | 234 passed, 1 skipped | 262 passed, 1 skipped |
| 完整 pytest 本次墙钟 | 10.33 s | 10.87 s |
| 合成错误回答/否定回答/空回答被误判通过 | 3 / 3 | 0 / 3 |
| 相同合成正确回答被判通过 | 1 / 1 | 1 / 1 |
| 300 ms 慢连接器，100 ms 搜索预算，本次单次观测返回耗时 | 297 ms | 109 ms |
| 该超时是否报告来源故障 | 否 | 是 |

测试数量增加 28 个参数化回归实例。测试总耗时包含用例集合变化，不是生产性能提升比例；单次慢来源对照是受控延时，不能代表真实 RAGFlow p95。跳过的是原 live Qwen 用例；3 个 warning 来自 pytest 已导入模块的断言重写及已有 Starlette/httpx 弃用提示。`git diff --check` 成功。

## 修改文件

- 编排与语义：app/agents/graph.py、app/agents/conversation_context.py、app/agents/history_aware.py、app/agents/semantic_router.py。
- 证据与答案：app/agents/claims.py、app/agents/evidence.py、app/agents/answer_composer.py。
- 检索与预算：app/search/service.py、app/search/budget.py（新增）、app/search/models.py、app/search/connectors.py、app/search/structured_query.py、app/tools/ragflow.py、app/tool_center/service.py（仅受管搜索相关）。
- 评测及必需契约：app/evaluation_service.py、app/schemas/evaluation.py、app/schemas/response.py、app/config.py。
- 测试：tests/test_core_regressions.py（新增）、tests/test_answer_composer.py、tests/test_evidence.py、tests/test_unified_search.py。
- 文档：README.md、CORE_OPTIMIZATION_REPORT.md（新增）。

## 配置与兼容性

新增环境配置：AGENT_SEARCH_BUDGET_SECONDS=120、AGENT_MAX_SEARCH_CALLS=24、AGENT_MAX_MODEL_CALLS=16、AGENT_MAX_PLANNING_CALLS=4、RAGFLOW_SEARCH_TIMEOUT_SECONDS=8、BUSINESS_TIMEZONE=Asia/Shanghai、EVIDENCE_CONTEXT_MAX_CHARS=24000。没有修改用户 .env。

现有 API/SSE 事件和引用字段保留；新增 EvidenceSummary.has_error/errors、评测 expected_facts/expected_citation_support/history、指标分组与 trace.request_budget。authority/freshness 未知时返回 null，现有评分字段仍返回数值。时间预算覆盖进入 run_agent 后的规划、搜索及逐项答案模型调用；搜索调用按 RAGFlow 请求和非文档连接器执行计数，不能解读成数据库驱动内所有网络请求的计数。

## 仍需真实验证的边界

- 没有调用真实模型或 RAGFlow，没有真实业务集准确率、召回率、Token、并发 p95 或成本数据。应使用已有检索评测脚本和已审核业务金标补测。
- 非数值事实、复杂条件、表格跨列关系、工厂范围及版本生效冲突仍依赖现有需求裁判的语义能力。已把实际元数据送入裁判；没有实现可凭未知元数据自动选“最新版本”的逻辑。
- 正文字面型号缺失、只有文件名标型号的资料会保守回退；QA 对象可从实际 question 字段关联。需用真实资料验证此约束的召回/拒答代价。
- 连接文字白名单和服务器生成对象/属性文本会减少自然措辞变化。复杂自然叙述、推断应由结构化、可追溯事实承载，不能绕过绑定。
- 关键词和 expected_facts 检查是辅助指标，不能替代专家语义评判；段落改写、双重否定、复杂单位换算及任意条件等价没有宣称全面解决。
- Future.cancel 只取消未开始任务。运行中的阻塞线程不能强制终止；固定容量池避免无限创建后台任务，饱和时返回来源故障。仍需真实客户端和负载验证。
- 条件执行只支持已有流程所需的常量与 requires_confirmation 比较；其他表达式拒绝执行。没有扩展可视化编排或构建通用 DAG。

原有未跟踪 app/data/tool_center.key 和 app/data/tool_center.sqlite3 未删除、未纳入修改文件列表。
