# Knowledge Base DeepAgent

一个由 Qwen、DeepAgents、LangGraph、RAGFlow 和 Redis 驱动的制造业企业知识库助手。

当前代码包含多数据源统一搜索层：RAGFlow 文档、可选 QA JSON、可选 Neo4j 图谱、企业业务 API、受控 SQL 搜索和只读 MCP 搜索代理均实现了统一的 `SearchHit` 契约。DeepAgent 可以通过 `unified_search` 工具自主选择和汇总这些来源。

## 工作方式

用户可以自然提问。DeepAgent 根据问题和历史上下文进行规划，优先使用统一搜索工具检索多种企业来源；后端仍负责权限、来源配置、证据校验和风险边界。RAGFlow 新增知识库后，系统可以自动发现并检索。

事实型知识问答采用证据优先的答案链路：RAGFlow、QA、图谱和业务接口只返回统一证据；需求裁判判断证据是否支持用户问题；Qwen 通过 `AnswerDraft` 组装自然回答；最后只校验证据 ID、声明级绑定和来源完整性。型号简称、系列名和子型号的语义归一化由模型结合证据完成，不再用整段答案与问题做字符串相等校验。若答案草稿绑定失败，系统最多执行一次模型修复；在 `ANSWER_GATE_MODE=soft` 下，只要需求裁判已经确认有可用证据，就保留自然回答并由后端补齐来源绑定；只有没有可支持证据或模型没有返回答案时才发布安全结果。设置为 `hard` 才会在绑定结构异常时直接回退。

运行详情中的“答案组装与证据绑定”会显示组装模式、校验状态、是否尝试修复、修复是否成功和失败原因；结构化草稿通过 `answer_draft` 字段随 API 返回，原始来源仍通过 `citations` 和 `retrieval_hits` 返回。

## 启动

```powershell
conda activate deepAgent-demo
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

浏览器打开：<http://127.0.0.1:8000>

根路径现在使用 `app/static/index_production.html`，页面只消费真实后端接口，不包含演示答案或服务异常时的虚构降级数据。旧的 `index_demo.html` 仅作为历史参考文件保留。

## 生产容器

```powershell
docker compose -f docker-compose.production.yml up -d --build
```

生产部署前应复制并填写 `.env.example` 中的模型、RAGFlow、SSO/网关身份、图谱和企业业务 API 配置。MES、ERP、CMMS、SCADA 的接口字段、认证方式和权限范围必须按企业实际 API 契约实现，不能使用默认示例字段直接连接生产系统。

## 配置

复制 `.env.example` 为 `.env`，配置 Qwen、RAGFlow 和 Redis。源码模式的 RAGFlow 后端默认地址为 `http://localhost:9380`。

若 `RAGFLOW_AUTO_DISCOVER_DATASETS=true`，助手会通过 RAGFlow API 自动获取全部知识库，无需为每个新知识库修改 Python 代码。

## 企业知识图谱（Neo4j）

RAGFlow 保留为文档解析、Chunk、页码证据和原文检索底座；Neo4j 只保存
设备、部件、故障码、故障、SOP、维修案例和工单等长期业务关系。未配置
Neo4j 时，图谱连接器自动跳过，不会影响文档问答。

在 `.env` 中设置以下配置以启用图谱查询：

```env
GRAPH_PROVIDER=neo4j
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=实际密码
# 仅供受控 ETL/审核任务调用，绝不能暴露给浏览器或 Agent。
GRAPH_WRITE_TOKEN=随机高强度令牌
```

首次部署 Neo4j 时，只启动图谱服务即可：

```powershell
docker compose -f docker-compose.production.yml up -d neo4j
```

图数据库浏览器地址为 <http://127.0.0.1:7474>。用户名为 `neo4j`，密码以
本机 `.env` 中的 `NEO4J_PASSWORD` 为准；`.env` 包含真实凭据，不能提交到
版本库或发给无权限人员。

图谱健康状态可通过 `GET /api/graph/status` 检查。图谱查询使用服务端固定、
参数化的 Cypher 模板，模型和客户端都不能提交任意 Cypher。受控导入接口为
`POST /api/graph/upsert`，必须携带 `X-Graph-Write-Token` 请求头；实体和关系
均以幂等方式写入，并可在关系 `properties` 中保存 `source_document`、`page`、
`chunk_id`、`version`、`confidence` 等证据元数据。

导入链路应是：RAGFlow 解析文档 → 抽取实体/关系 JSON → 实体标准化和审核 →
调用受保护导入接口。普通问答 Agent 只读检索 RAGFlow 和 Neo4j，不能写图谱。

前端“图谱导入”页面对应以下接口：

```text
GET  /api/graph/pipeline/documents?dataset_id=...
POST /api/graph/pipeline/extract
POST /api/graph/upsert
```

`/api/graph/pipeline/extract` 会读取指定文档的 Chunk 并调用 Qwen，返回待审核
实体、关系及来源页码；它不会写入 Neo4j。审核页面将选中项标记为 `approved`
后，才调用带 `X-Graph-Write-Token` 的 `/api/graph/upsert`。

## 统一搜索调试 API

启动后可通过以下接口检查多数据源状态和检索结果：

```text
GET  /api/search/sources
POST /api/search
```

请求示例：

```json
{
  "query": "北辰47 E-203 最近三个月维修记录",
  "source_types": ["document", "qa", "graph", "sql", "api"],
  "top_k": 12,
  "filters": {"factory": "一号工厂", "device_model": "北辰47"}
}
```

接口会返回来源类型、标题、证据 ID、页码/版本、更新时间、排序分数、告警和每个连接器的执行 trace。未配置的来源会被跳过并放入 `warnings`，不会阻断 RAGFlow 问答。

## 工作流编排

“工作流”页面现在是可编辑编排器：工作流定义持久化在
`app/data/workflows.json`，支持 `workflow_id`、版本、节点/边、节点参数、启停状态、
数据源、Top-K、证据阈值和人工确认点。可以拖动节点、添加条件分支和并行节点、调试
单节点、保存草稿、发布、回滚、版本对比并从模板创建副本。

工作台通过 `/chat/stream` 传入 `workflow_id` 和 `workflow_version`，运行时返回
`workflow_started`、`workflow_node` 事件；节点状态来自真实的规划、统一搜索、证据门控、
裁判和答案生成阶段，而不是页面模拟。详细契约见
[`docs/workflow-orchestration.md`](docs/workflow-orchestration.md)。

## 应用中心

应用中心是面向用户和治理的 Agent 控制面：应用定义保存工作流绑定、版本、数据源、工具、
Top-K、证据阈值、人工确认、权限和渠道；工作流仍负责节点编排和真实执行。应用中心支持创建、
保存草稿、发布、回滚、版本对比，并通过 `app_id` 将 `/chat/stream` 的真实运行 ID、耗时、错误和
版本回写到运行历史。工作台顶部提供 Agent 选择器；内置的企业知识助手和证据校验 Agent 分别
绑定 `knowledge-research` 与 `evidence-review` 两个独立工作流。详细契约见
[`docs/application-center.md`](docs/application-center.md)。

## 测试

```powershell
python -m pytest tests
```
