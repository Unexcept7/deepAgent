# 五项核心缺陷修复方案

基准：master，d68132cd40e68a8f7d819df4ecb47d9784926540。只修改核心问答、评测及对应测试；保留 API、权限、前端和部署配置。

| 顺序 | 修复 | 验证标准 |
|---|---|---|
| 1 | 完整数值和单位匹配，复用原有数值提取；禁止数值子串、负号和小数尾部误命中 | 124 V、-24 V、0.24 V、24 mV 均不能支持24 V；24.0 V与24 V允许等值 |
| 2 | 复用同一事实分句和对象范围，限定词与值必须在同一事实范围 | 额定124 V、最大48 V不得支持额定48 V；跨型号不得串值；正确值仍正常输出 |
| 3 | 首轮检索后先逐项计算需求缺失，再决定补检 | 两条电压证据不能阻止协议和功能码补检；已满足需求不重复补检，仍最多一轮 |
| 4 | 结构化评测在同一对象的事实分句中检查属性、值、单位和条件 | M1401=124 V、M1401-AO=24 V不能通过M1401=24 V的金标 |
| 5 | 否定检查比较金标与答案的极性 | 正确否定答案通过；肯定/否定互换失败；保留原正向用例 |

执行：先新增失败回归，确认复现；再在共享证据函数修复，避免平行校验；定向运行新增与相邻测试；最后运行禁用真实模型和外部 HTTP 的完整 pytest及git diff --check。测试临时目录留在项目内，验证结束清理。无需新增模型调用或依赖。

边界：同单位数值等值使用 Decimal；不隐式换算单位。分句与极性检查是确定性底线，复杂表格、跨句推断和双重否定仍需现有语义裁判及真实业务集验证。

## 执行结果

五项修复已完成。

- app/agents/evidence.py：完整数值与单位用 Decimal 比较；共享事实分句按精确对象及限定词划分范围，保留后续参数项的明确对象。
- app/agents/claims.py：关键值必须在同一对象且满足限定词的事实范围中得到支持；自动绑定也复用完整数值检查。
- app/agents/graph.py：先计算缺失需求再判断补检，仍只执行一轮定向扩大检索。
- app/evaluation_service.py：结构化金标按对象范围逐条匹配，数值复用证据校验；答案值检查正否极性，属性标签和条件按内容匹配。
- tests/test_review_fixes.py：新增22个回归实例，包含完整run_agent路径的补检验证；已有测试未放宽。
- CORE_OPTIMIZATION_REPORT.md：链接本次修复，保留首轮报告作为历史记录。

验证记录：

| 阶段 | 真实结果 |
|---|---|
| 首批新用例，修复前 | 10 failed、4 passed |
| 首次定向回归（新用例及相邻模块） | 65 passed |
| 修复前原完整基线 | 262 passed、1 skipped |
| 最终完整回归 | 284 passed、1 skipped，14.91秒 |
| 最终git diff --check | 通过 |

跳过的是原live Qwen用例，3个warning是已有pytest导入与Starlette弃用提示。真实模型及外部HTTP在测试进程内禁用，未改本机配置。Windows TestClient需要本地通信，因此完整回归在沙箱外执行。

最终测试命令：

```powershell
& 'C:\Users\22069\.conda\envs\deepAgent-demo\python.exe' -u -c "import pytest, requests; from langchain_openai import ChatOpenAI; deny = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('offline regression: live provider disabled')); ChatOpenAI.invoke = deny; ChatOpenAI.stream = deny; requests.Session.request = deny; raise SystemExit(pytest.main(['tests', '-q', '--basetemp=.test-tmp-five-proof', '--tb=short']))"
```

没有新增依赖或模型调用。本次测试证明五项可复现缺陷得到修复，不是实际业务准确率、Token或生产延迟收益的测量。未提交或推送，未修改权限、前端及部署。临时测试目录已清理，原有数据库和密钥文件保留。
