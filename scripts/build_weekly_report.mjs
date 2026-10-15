import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "D:/百川实践/第七周工作内容_0911.xlsx";
const outputDir = "D:/百川实践/deepAgent/outputs/20260920-weekly-report";
const outputPath = `${outputDir}/第八周工作内容_0920.xlsx`;

await fs.mkdir(outputDir, { recursive: true });
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItem("Sheet1");

// Keep the template's four columns and formatting, replacing only the report rows.
const rows = [
  [
    "企业知识助手检索与证据链路改造",
    "围绕企业设备知识问答，完善意图与实体识别、上下文整理、检索计划和后端安全裁决；改造统一搜索，按检索计划查询 RAGFlow 文档库和 Neo4j 图谱，增加实体优先检索、来源追踪、去重和 RRF 融合；完善证据校验、逐项裁判、覆盖审计和最终回答组织。",
    "完成语义路由与 RetrievalPlan 数据结构，支持多意图/实体以及 RAGFlow、Neo4j、业务 API、人工确认等安全路由；实测 M1401 图谱返回 3 条关系、RAGFlow 返回 3 个文档片段，相关测试 160 passed、1 skipped。",
    "9.14-9.18",
  ],
  [
    "Agent 工作流可观测性与管理配置完善",
    "完善工作流编排与 TraceRecorder，记录真实节点状态、模型/工具/API 调用、耗时、输入输出 Token、检索返回条数和连接信息；同时完善应用中心、工作流、路由策略中心和词典中心，支持版本、发布状态、关键词/同义词、模型 Prompt 和数据源参数配置。",
    "执行详情可展示需求规划、统一搜索、Neo4j、RAGFlow、证据校验和回答生成等阶段，便于定位调用失败与性能瓶颈；形成可维护的路由与词典配置入口，降低规则硬编码依赖。",
    "9.14-9.18",
  ],
];

// Use the first existing data row as the visual template for all report rows.
for (let row = 3; row <= 6; row += 1) {
  sheet.getRange("A2:D2").copyTo(sheet.getRange(`A${row}:D${row}`), "all");
}

const blankRows = Array.from({ length: 9 }, () => [null, null, null, null]);
sheet.getRange("A2:D10").values = blankRows;
sheet.getRange("A2:D3").values = rows;

// Keep long Chinese text readable without changing the template's column structure.
sheet.getRange("A2:D10").format.wrapText = true;
sheet.getRange("A2:D3").format.rowHeight = 126;

workbook.recalculate();

const outputValues = sheet.getRange("A1:D10").values;
const inspect = await workbook.inspect({
  kind: "table",
  range: "Sheet1!A1:D10",
  maxChars: 12000,
  tableMaxRows: 12,
  tableMaxCols: 6,
  tableMaxCellChars: 240,
});
const inspectText = typeof inspect === "string" ? inspect : JSON.stringify(inspect, null, 2);
await fs.writeFile(`${outputDir}/report-values.json`, JSON.stringify(outputValues, null, 2), "utf8");
await fs.writeFile(`${outputDir}/report-inspect.txt`, inspectText, "utf8");

const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
const formulaErrorsText = typeof formulaErrors === "string" ? formulaErrors : JSON.stringify(formulaErrors, null, 2);
await fs.writeFile(`${outputDir}/formula-error-scan.txt`, formulaErrorsText, "utf8");

const preview = await workbook.render({
  sheetName: "Sheet1",
  range: "A1:D10",
  autoCrop: "all",
  scale: 1.5,
  format: "png",
});
await fs.writeFile(`${outputDir}/第八周工作内容_0920.png`, new Uint8Array(await preview.arrayBuffer()));

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);

console.log(JSON.stringify({ outputPath, values: outputValues, formulaErrors: formulaErrorsText }, null, 2));
