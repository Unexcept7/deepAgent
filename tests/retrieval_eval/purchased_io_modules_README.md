# 外购设备-IO模块检索评测集 v1

## 适用数据集

- RAGFlow Dataset ID：`b19b045a956211f1ae737f9ffb05e7d0`
- 文档状态核查日期：2026-08-21
- 文档总数：35；其中 34 份为 `DONE`，`YC100模拟量输入输出模块使用说明书V1.2.doc` 为 `FAIL`。

失败文档不纳入 v1 金标，不能拿它的内容作为“应该被召回”的标准。修复并重新解析成功后，应另行新增用例，不要修改本版历史结果。

## 设计

共 25 条：22 条正例、3 条硬负例。

- `dev`：15 条，用于确定召回参数和 reranker 候选。
- `test`：10 条，用于固定参数后的最终对比。
- 单型号查询覆盖 M、FT、YC、K、XP 五类资料。
- 多来源查询覆盖同名 `M1401`、近似型号 M1303/M1304、M1503/M1507、
  多型号 XP 系列，以及 K75/K85 的硬件/协议文档职责区分。
- 金标以 RAGFlow `document_id` 为准，避免中文文件名乱码、同名文件和文件名变更造成误判。

本评测集有意包含容易被过度拟合的标题型问题，也包含改写、职责区分和多文档问题。
但 25 条仍只能作为工程基线，不足以证明模型的泛化能力。后续应从真实用户日志中脱敏抽样，
由业务人员标注至少一批未参与调参的查询。

## 运行基线

```powershell
Set-Location 'D:\百川实践\deepAgent'
& 'C:\Users\22069\.conda\envs\deepAgent-demo\python.exe' `
  scripts\evaluate_retrieval.py `
  --dataset tests\retrieval_eval\purchased_io_modules_retrieval_v1.jsonl `
  --ragflow-dataset-id b19b045a956211f1ae737f9ffb05e7d0 `
  --split all `
  --top-k 4 `
  --workers 4 `
  --output output\retrieval_eval\io_modules_baseline_top4.json
```

正式调参时先只运行 `dev`。确定候选数、检索权重或 reranker 后，将参数冻结，
再运行一次未参与调参的 `test`。基线和实验必须使用相同的 Dataset、文档版本、
切块参数、查询文本、K 和过滤条件。

`--workers` 默认为 1，适合观察单请求延迟。批量跑分可提高到 4，但并发模式下的
`mean_duration_ms` 会受到服务排队影响，只能用来发现明显回归，不能作为单用户延迟指标。

## 指标边界

脚本输出的是 document-level 代理指标。它能回答“正确来源文档是否被排到前 K”，
但不能证明返回片段就支持该结论。特别是同一正确文档内可能包含无关 chunk。

- `document_hit_precision_at_k` 先按 `document_id` 去重；同一手册命中多个切片只算一个来源文档。
- `chunk_hit_precision_at_k` 保留切片级精度，用于发现同一文件重复占据排名。
- `duplicate_chunk_count` 是 Top-K 中重复来源的切片数量。多型号比较题需要同时查看它和 `document_recall_at_k`。

下一版应在每条正例中补充 RAGFlow 返回的页码、chunk ID，并将前 K 片段标注为
`relevant`、`partially_relevant` 或 `irrelevant`，再比较 chunk-level Precision@K、
MRR 和 nDCG。硬负例不参加 Recall/MRR；它们用于检查 evidence 是否拒绝将相似片段
当作型号事实。
