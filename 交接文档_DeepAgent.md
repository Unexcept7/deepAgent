# DeepAgent 项目交接文档

更新时间：2026-10-15  
项目目录：`D:\百川实践\deepAgent`  
用途：交给新的聊天继续维护本项目。

## 1. 项目定位

这是一个面向制造业企业知识库的 DeepAgent 系统，主要技术链路是：

```
浏览器
  -> FastAPI
  -> DeepAgents / LangGraph
  -> Qwen 兼容 OpenAI 协议
  -> RAGFlow、Neo4j、QA、业务 API、受控 SQL、MCP
```

生产页面入口：

- 前端：`app/static/index_production.html`
- 后端入口：`app/main.py`
- 服务地址：`http://127.0.0.1:8000`
- 健康检查：`http://127.0.0.1:8000/health`
- API 文档：`http://127.0.0.1:8000/docs`

项目不是 React/Vue 项目，前端主要是原生 HTML、CSS 和 JavaScript。修改页面时优先检查生产 HTML 及对应的独立 JS/CSS 文件。

## 2. 当前主要能力

### 2.1 多数据源统一搜索

统一搜索层使用统一的 `SearchHit` 证据契约，当前支持或预留：

- RAGFlow 文档知识库
- QA JSON
- Neo4j 企业知识图谱
- 企业业务 API
- 受控 SQL 搜索
- 只读 MCP 搜索代理

相关代码：

- `app/search/`
- `app/tools/ragflow.py`
- `app/graph/`
- `app/tools/mcp.py`
- `app/main.py`

接口：

```
GET  /api/search/sources
POST /api/search
```

统一搜索请求可以带 `app_id`、`source_types`、`top_k` 和过滤条件。未配置或不可用的数据源会进入 `warnings`，不应伪造答案。

### 2.2 证据优先问答

事实型问题的主要流程：

```
用户问题
  -> 语义路由 / 需求规划
  -> 统一搜索
  -> 证据裁判
  -> AnswerDraft 组装
  -> 证据 ID、声明和来源校验
  -> 返回答案与引用
```

关键约束：

- 不允许把一个型号的证据套到另一个型号。
- 不支持的参数、通道数、端子号和关系不能凭空补齐。
- 不存在的型号应拒答。
- 图谱结果和文档结果要分别保留来源。
- `ANSWER_GATE_MODE=soft` 会在有可靠证据但答案结构有问题时尽量保留自然回答；`hard` 更严格回退。

### 2.3 图谱导入

图谱导入流程：

```
RAGFlow 文档
  -> 读取 Chunk
  -> Qwen 抽取实体和关系
  -> 页面审核
  -> /api/graph/upsert
  -> Neo4j
```

接口：

```
GET  /api/graph/status
GET  /api/graph/pipeline/documents?dataset_id=...
POST /api/graph/pipeline/extract
POST /api/graph/upsert
```

`/api/graph/pipeline/extract` 只生成待审核数据，不直接写 Neo4j。写入必须带 `X-Graph-Write-Token`。普通问答 Agent 只读图谱。

### 2.4 工作流和应用中心

工作流定义保存在：

`app/data/workflows.json`

支持工作流节点、边、条件、并行、单节点调试、版本、发布、回滚和模板。

应用中心管理：

- 应用版本
- 工作流绑定
- 知识库和数据源范围
- 工具授权
- Top-K 和证据阈值
- 人工确认
- 权限和渠道
- 运行历史

工作台通过 `/chat/stream` 传递 `workflow_id`、`workflow_version` 和 `app_id`。

## 3. 最近完成的页面改动

### 3.1 工具与数据中心

相关文件：

- `app/static/tool-center.js`
- `app/static/tool-center.css`
- `app/static/index_production.html`

已完成：

- 工具、数据连接、凭据、调用记录共用同一页面框架。
- 详情和编辑器改成弹出框，主页面不再保留右侧编辑面板。
- 工具列表改为全宽布局，减少右侧空白和页面滚动。
- 点击详情、保存、发布、测试时不再整页闪烁刷新。
- 编辑器高级区域展开状态和弹窗滚动位置会保留。
- 搜索、环境筛选、调用状态筛选都会回到第 1 页。
- 四个标签页均使用每页 5 条的分页。
- 当前资源版本：`tool-center.js?v=20261015-6`。

工具中心分页规则：

- 工具、数据连接、凭据、调用记录四个标签页都会显示页码和总数。
- 只有一页时，上一页和下一页按钮置灰。
- 超过 5 条时可以翻页。

核心常量：

```javascript
const PAGE_SIZE = 5;
```

### 3.2 全局列表分页

新增文件：

- `app/static/list-pagination.js`
- `app/static/list-pagination.css`
- `tests/frontend/test_list_pagination.cjs`

后端静态资源白名单已在 `app/main.py` 注册。

公共分页特点：

- 默认每页 5 条。
- 对实际记录列表和表格生效。
- 搜索或筛选后回到第 1 页。
- 跨页 checkbox 状态保留。
- 数据少于等于 5 条时隐藏公共分页条。
- 记录仍保留在 DOM 中，原有提交逻辑不会丢失跨页选择。
- 工具中心使用自己的分页，被公共分页器排除。
- 答案正文和普通 Markdown 表格不分页。

覆盖的主要页面：

- 工作台对话和来源列表
- 应用中心应用、版本记录、知识库选择项
- 知识库连接和数据集
- 路由策略
- 提示词中心
- 模型中心
- 词典中心
- 统一搜索结果
- 评测历史、详情和排名
- 图谱审核实体/关系表
- 工作流运行记录

应用历史中原来的“最近 12 条”限制已经移除；评测详情中原来的“前 100 条”限制已经移除。评测后端历史保留上限仍由业务存储逻辑控制，当前历史接口使用 `limit=50`。

## 4. 最新验证结果

使用 bundled Node 运行时和 Microsoft Edge 无头浏览器执行了生产前端资源测试。API 使用测试夹具，不调用真实模型或生产服务。

命令：

```powershell
& "C:\Users\22069\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" --test tests/frontend/test_tool_center.cjs tests/frontend/test_list_pagination.cjs
```

结果：

```
tests 3
pass 3
fail 0
```

已验证：

- 工具中心四个标签页的分页和页码。
- 工具中心详情不触发整页刷新。
- 保存失败时表单内容不丢失。
- 详情弹窗滚动位置和高级区域状态保留。
- 应用、知识库、统一搜索、评测、图谱审核、工作流和对话来源列表分页。
- 搜索和筛选后页码重置。
- 跨页选择状态保留。
- 常见桌面窗口尺寸下工具中心没有额外页面滚动条。

语法和 Python 检查：

```powershell
& "C:\Users\22069\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" --check app/static/tool-center.js
& "C:\Users\22069\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" --check app/static/list-pagination.js
& "C:\Users\22069\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" --check app/static/evaluation-center.js
& "C:\Users\22069\.conda\envs\deepAgent-demo\python.exe" -m py_compile app/main.py
```

## 5. 当前已知限制

### 5.1 分页是前端分页

公共分页器会先加载接口返回的记录，再在浏览器中分页。它不是数据库或 API 的 `offset/limit` 服务端分页。

如果未来某列表可能达到几百条以上，应改成后端分页，至少增加：

```
page
page_size
total
items
```

不要继续把大量数据全部塞入 DOM。

### 5.2 部分页面没有真实业务列表

设备、任务、运行日志、系统设置等页面目前主要是状态卡片或配置区域，没有真实的多条业务记录，因此没有凭空增加分页数据。

### 5.3 测试边界

最近的浏览器回归测试使用 mock API，验证的是页面真实代码和交互，不代表 RAGFlow、Neo4j、DashScope、MCP 或企业 API 当前在线。

需要真实联调时，单独检查：

- DeepAgent `8000` 端口
- RAGFlow `9380` 端口
- Redis/Valkey
- Neo4j `7687`
- MySQL MCP 服务
- DashScope/OpenAI 兼容模型配置

## 6. RAGFlow 和模型问题的历史结论

### 6.1 DashScope `Arrearage`

错误形式：

```
code: Arrearage
Access denied, please make sure your account is in good standing
```

这是模型服务账户欠费或账户状态导致的上游拒绝，通常不是文档切分代码造成的。需要检查 DashScope 账户余额、欠费状态、API Key 所属账号和模型权限。

### 6.2 `qwen3-rerank` 的 document index 超限

错误形式：

```
document index: 35 is invalid, it should not be larger than 50000: input.documents
```

排查入口：

```
RAGFlow 向量/关键词召回
  -> 组装 input.documents
  -> 调用 qwen3-rerank
```

单个 Chunk 设置为 512 tokens，并不能证明发送给重排模型的每个 `input.documents` 项都满足模型接口限制。需要记录实际发送给 rerank 的第 N 个候选的字符串长度、字节长度和来源 `chunk_id`，再对照 Qwen 接口限制。问题发生在 rerank 请求组装/发送阶段，不是在解析器切分界面本身。

### 6.3 OpenAI 协议模型的 system message

历史错误：

```
System message must be at the beginning.
```

排查时检查最终发给 OpenAI 兼容网关的完整 `messages` 顺序，确保所有 `system` 消息在第一条，不能在 user/tool/assistant 消息之后动态插入 system。

## 7. 启动方式

### 7.1 启动 DeepAgent

```powershell
Set-Location "D:\百川实践\deepAgent"
conda activate deepAgent-demo
python -m uvicorn app.main:app --reload
```

打开：

`http://127.0.0.1:8000`

如果当前终端的 `python` 不确定，直接使用：

```powershell
& "C:\Users\22069\.conda\envs\deepAgent-demo\python.exe" -m uvicorn app.main:app --reload
```

### 7.2 启动 RAGFlow

在 WSL Ubuntu：

```bash
cd ~/ragflow
source .venv/bin/activate
docker compose -f docker/docker-compose-base.yml up -d
bash docker/launch_backend_service.sh
```

RAGFlow 默认地址：

`http://localhost:9380`

### 7.3 环境变量

主要配置在项目根目录 `.env`，常见项包括：

```env
DASHSCOPE_API_KEY=...
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=...
RAGFLOW_BASE_URL=http://localhost:9380
RAGFLOW_API_KEY=...
REDIS_URL=redis://localhost:6379/2
```

不要把 `.env` 内容复制到聊天，也不要在交接文档中填写实际 Token、密码或 API Key。

## 8. 下一次继续工作的建议顺序

新聊天打开本文件后：

1. 先读取本文件、`README.md` 和 `启动流程.md`，确认当前需求是 UI、检索还是 MCP。
2. 用 `rg` 检查目标模块现状，不要假设当前运行服务已加载最新静态资源。
3. 如果修改了 `app/main.py` 或静态资源白名单，重启后端；如果只是页面资源更新，浏览器执行 Ctrl+F5。
4. 修改前写出成功标准，修改后至少运行对应的语法检查和前端回归测试。
5. 真实 RAGFlow/模型测试前，先检查服务健康、模型账户状态和当前 Agent/工作流版本。
6. 不要把 mock 测试结果表述为真实生产服务验证结果。

## 9. 给新聊天的直接启动提示

可以把下面这段直接发给新的聊天：

```text
请先阅读 D:\百川实践\deepAgent\交接文档_DeepAgent.md、README.md 和启动流程.md，再继续处理我的项目。

这是一个 Python FastAPI + DeepAgents/LangGraph + Qwen + RAGFlow + Neo4j 的企业知识库项目，生产前端入口是 app/static/index_production.html，后端入口是 app/main.py。请先检查当前代码和实际调用链，不要假设服务已经重启，也不要泄露 .env 中的凭据。

当前最近完成的是全站列表分页和工具与数据中心四个标签页分页，规则是每页 5 条；工具中心分页在 app/static/tool-center.js，公共分页在 app/static/list-pagination.js。修改时遵守最小改动原则，完成后运行对应的前端回归测试并说明验证边界。

我的要求是：
```

## 10. 维护约束

- 先检查代码和调用链，再修改。
- 只修改和当前需求直接相关的文件。
- 不新增没有必要的依赖或抽象。
- 不伪造 RAGFlow、图谱、模型或业务 API 的返回结果。
- 不把 Token、密码、`.env` 内容写入聊天或文档。
- 修改后说明改动文件、原因、验证命令和未验证部分。
- 用户要求“直接修改”时，完成可验证的代码修改后再汇报，不停在设计建议阶段。



