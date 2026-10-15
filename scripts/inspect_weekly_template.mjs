import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "D:/百川实践/第七周工作内容_0911.xlsx";
const outputDir = "D:/百川实践/deepAgent/outputs/20260920-weekly-report";

await fs.mkdir(outputDir, { recursive: true });
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);

const summary = await workbook.inspect({
  kind: "workbook,sheet,table",
  include: "id,name,visibility",
  maxChars: 20000,
  tableMaxRows: 30,
  tableMaxCols: 12,
  tableMaxCellChars: 200,
});
const summaryText = typeof summary === "string" ? summary : JSON.stringify(summary, null, 2);
await fs.writeFile(`${outputDir}/template-inspect.txt`, summaryText, "utf8");

const sheetDetails = [];
for (const sheet of workbook.worksheets.items) {
  const used = sheet.getUsedRange();
  sheetDetails.push({
    name: sheet.name,
    usedAddress: used?.address ?? null,
    values: used?.values ?? null,
  });
}
await fs.writeFile(`${outputDir}/template-values.json`, JSON.stringify(sheetDetails, null, 2), "utf8");

for (const sheet of workbook.worksheets.items) {
  const safeName = sheet.name.replace(/[^\p{L}\p{N}_-]+/gu, "_");
  const preview = await workbook.render({
    sheetName: sheet.name,
    autoCrop: "all",
    scale: 1.5,
    format: "png",
  });
  await fs.writeFile(`${outputDir}/template-${safeName}.png`, new Uint8Array(await preview.arrayBuffer()));
}

console.log(`sheets=${workbook.worksheets.items.map((s) => s.name).join(",")}`);
console.log(summaryText);
console.log(JSON.stringify(sheetDetails, null, 2));
