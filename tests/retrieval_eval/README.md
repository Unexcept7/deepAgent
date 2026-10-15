# 北辰-47 检索评测集 v1

这是面向当前 RAGFlow 检索链路的第一版文档级金标评测集。它只使用
`output/pdf/rag_private_test_corpus` 中的三份虚构 PDF；运行前必须将这三份
PDF 上传到同一个 RAGFlow 知识库，并确认状态为 `Success`。

## 范围与边界

- 共 16 条独立查询：14 条可检索正例、2 条硬负例。
- 正例包含精确定位、改写、约束查询、跨文档关联、版本冲突和条件推理。
- `dev` 用于调参，`test` 仅用于最终对比。相同 `group_id` 是同一事实的改写，
  不应把它们当作独立样本来宣称统计显著性。
- 这份 v1 是**文档级**金标：以是否返回正确来源文档衡量。`required_concepts`
  用于人工确认片段是否真的支持问题，不是脚本自动判定的真值。

## 为什么硬负例不计入 Recall

`AuroraMax-Z99` 和 `INC-999` 在语料中没有资料。原始召回器通常仍会返回语义
相近片段，因此不能把“返回了片段”直接判为检索错误，也不能计算 Recall。
它们应在端到端测试中验证 `generic`/`industrial_strict` evidence 是否拒答。
脚本会保留其候选和分数，供人工检查，但不会混入 Retrieval Recall@K 或 MRR。

## 指标解释

脚本输出的 `document_hit_precision_at_k`、`document_recall_at_k`、`mrr` 是
文档名匹配的代理指标。它们可用于同一语料、同一查询、同一 K 下比较“基线召回”
和“加入 reranker 后”的相对变化，不能替代严格的 chunk-level Precision。

后续要做正式 reranker 决策，应在 RAGFlow 导入后补充每条查询的
`document_id`、页码和 chunk ID，并由领域人员给片段标注 `relevant`、
`partially_relevant`、`irrelevant`。届时再计算 chunk-level Precision@K、
Recall@K、MRR 和 nDCG。

## 运行

先确保 `.env` 中的 `RAGFLOW_API_KEY` 和该评测知识库的 Dataset ID 已配置。
评测时建议暂时把 `RAGFLOW_TOP_K` 调整到不小于待评估 K，否则客户端会截断结果。

```powershell
Set-Location 'D:\百川实践\deepAgent'
& 'C:\Users\22069\.conda\envs\deepAgent-demo\python.exe' `
  scripts\evaluate_retrieval.py `
  --dataset tests\retrieval_eval\beichen47_retrieval_v1.jsonl `
  --split test `
  --top-k 4 `
  --output output\retrieval_eval\beichen47_baseline_top4.json
```

对比 reranker 时，除了 reranker 配置外，Dataset、chunk 参数、查询文本、K、
检索请求参数都必须保持不变。不要只选表现好的问题报告结果；应同时报告 `dev`
与 `test` 的结果、逐条命中情况、总耗时和失败请求数。
