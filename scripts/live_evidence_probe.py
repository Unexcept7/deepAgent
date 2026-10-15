"""Run read-only online probes against the currently configured RAGFlow.

The script never prints API keys or dataset IDs.  It is intentionally separate
from pytest because it depends on the local RAGFlow service and its data.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.tools.ragflow import RAGFlowClient


QUESTIONS = [
    "FT-RS-9999 支持哪些 Modbus 功能码？",
    "FT-RS-0808 支持哪些 Modbus 功能码？",
    "FT-RS-0808 有多少路输入和输出？",
    "FT-RS-0808 的默认设备地址、波特率、数据位、停止位和校验方式是什么？",
    "比较 FT-RS-0808、M1501 和 M1516 的数字量输入输出能力。",
    "FT-RS-0808 使用的 Modbus-RTU 和 OPC UA 有什么区别？",
    "知识库中是否有 FT-RS-0808 的说明书？请列出文件名。",
    "请返回系统中所有设备的信息。",
]


def main() -> None:
    client = RAGFlowClient()
    for index, question in enumerate(QUESTIONS, 1):
        started = time.perf_counter()
        try:
            result = client.search(question, top_k=8)
            record = {
                "index": index,
                "question": question,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "status": result.get("evidence_status"),
                "candidate_count": result.get("candidate_count"),
                "verified_count": result.get("verified_count"),
                "rejected_count": result.get("rejected_count"),
                "missing_entities": result.get("missing_entities"),
                "missing_subjects": result.get("missing_subjects"),
                "missing_aspects": result.get("missing_aspects"),
                "coverage_limited": result.get("coverage_limited"),
                "documents": list(dict.fromkeys(
                    item.get("document_name")
                    for item in result.get("chunks", [])
                    if item.get("document_name")
                )),
            }
        except Exception as exc:  # pragma: no cover - online diagnostics
            record = {
                "index": index,
                "question": question,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            }
        # ASCII JSON keeps output readable even in Windows PowerShell 5.1.
        print(json.dumps(record, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
