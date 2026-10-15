# MySQL MCP 服务

这是一个独立的、真实连接 MySQL 的只读小服务。它不在内存中保存设备数据，也不接受自然语言 SQL；业务数据由 `devices` 和 `alarms` 表提供，DeepAgent 通过 REST 或 MCP 读取。

## 1. 初始化数据库

在 MySQL 客户端执行：

```sql
SOURCE services/mysql_mcp/schema.sql;
```

如果需要为接口联调准备 20 条设备记录和 20 条告警记录，再执行：

```sql
SOURCE services/mysql_mcp/seed.sql;
```

`seed.sql` 是幂等的，重复执行会更新同编号记录，不会不断追加重复数据。里面的记录明确标注为联调数据，不能替代产品说明书或现场台账。

然后为服务创建只读账号（账号密码不要写入仓库）：

```sql
CREATE USER 'mcp_reader'@'%' IDENTIFIED BY '请替换为强密码';
GRANT SELECT ON enterprise_mcp.* TO 'mcp_reader'@'%';
FLUSH PRIVILEGES;
```

服务不会自动造数据。执行 `seed.sql` 后可用于接口联调；正式使用时，应将真实设备台账和告警同步到这两张表，接口返回的就是数据库中的真实记录。

## 2. 配置并启动

复制 `.env.example` 为同目录的 `.env`，填写 MySQL 连接信息；共享环境建议同时设置 `MCP_AUTH_TOKEN`。

在项目根目录安装依赖并启动：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn services.mysql_mcp.main:app --host 127.0.0.1 --port 8100
```

健康检查：

```text
http://127.0.0.1:8100/health
```

如果设置了 `MCP_AUTH_TOKEN`，调用 `/api/*` 和 `/mcp` 时要带：

```text
Authorization: Bearer <MCP_AUTH_TOKEN>
```

## 3. REST API

```text
GET /api/devices?query=M1503&limit=20
GET /api/devices/M1503
GET /api/devices/M1503/alarms?days=30&limit=50
GET /api/search?query=M1503%20DI&top_k=10
```

搜索只允许服务内置的设备字段、告警字段和有限过滤条件，所有值通过参数化 SQL 传入，不把用户输入拼成 SQL。

## 4. 接入 DeepAgent 工具中心

在“工具中心 → 连接管理”新增：

```json
{
  "name": "MySQL 设备查询",
  "type": "mcp",
  "endpoint": "http://localhost:8100/mcp",
  "environment": "production",
  "config": {
    "transport": "streamable_http",
    "protocol_version": "2025-11-25"
  }
}
```

如果设置了服务令牌，在连接的认证配置中选择 Bearer，并绑定同一个令牌。保存后使用“从 MCP 导入”，服务会返回 `equipment_search`、`get_device_status` 和 `get_alarm_history` 三个工具。将它们的风险级别确认成 `read_only`，发布连接和工具。

随后在应用中心给目标 Agent 勾选这个 MCP 数据源，并保存/发布版本。这样：

- 统一搜索可以调用 `equipment_search`；
- Agent 可以按设备编号调用状态和告警工具；
- 数据库账号只存在服务端环境变量，主知识库不保存数据库密码；
- 没有发布的 MCP 工具不会被应用自动调用。
