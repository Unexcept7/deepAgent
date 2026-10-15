import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const outputPath = "D:/百川实践/deepAgent/outputs/20260920-weekly-report/第八周工作内容_0920.xlsx";
const outputDir = "D:/百川实践/deepAgent/outputs/20260920-weekly-report";
const input = await FileBlob.load(outputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItem("Sheet1");
const values = sheet.getRange("A1:D10").values;
const report = {
  sheets: workbook.worksheets.items.map((item) => item.name),
  values,
  nonEmptyRows: values.filter((row) => row.some((cell) => cell !== null && cell !== "")).length,
};
await fs.writeFile(`${outputDir}/saved-file-verification.json`, JSON.stringify(report, null, 2), "utf8");
console.log(JSON.stringify(report, null, 2));
