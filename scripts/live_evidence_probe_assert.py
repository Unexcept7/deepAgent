"""Assert critical evidence-gate behavior against the live RAGFlow service."""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.tools.ragflow import RAGFlowClient


CASES = [
    {
        "name": "虚构型号拒答",
        "question": "FT-RS-9999 支持哪些 Modbus 功能码？",
        "status": "not_found",
        "verified": 0,
        "missing_entity": "FT-RS-9999",
    },
    {
        "name": "真实型号功能码",
        "question": "FT-RS-0808 支持哪些 Modbus 功能码？",
        "status": "supported",
        "document_contains": "FT-RS-0808",
    },
    {
        "name": "真实型号通道数",
        "question": "FT-RS-0808 有多少路输入和输出？",
        "status": "supported",
        "document_contains": "FT-RS-0808",
    },
    {
        "name": "多型号分别覆盖",
        "question": "比较 FT-RS-0808、M1501 和 M1516 的数字量输入输出能力。",
        "status": "supported",
        "documents_contain_all": ["FT-RS-0808", "M1501", "M1516"],
    },
    {
        "name": "缺失协议资料降级",
        "question": "FT-RS-0808 使用的 Modbus-RTU 和 OPC UA 有什么区别？",
        "status": "partial",
        "missing_subject": "OPC UA",
    },
    {
        "name": "Top-K 不冒充全集",
        "question": "请返回系统中所有设备的信息。",
        "status": "partial",
        "coverage_limited": True,
    },
]


def main() -> int:
    client = RAGFlowClient()
    passed = 0
    failures: list[str] = []
    for case in CASES:
        started = time.perf_counter()
        result = client.search(case["question"], top_k=8)
        documents = list(dict.fromkeys(
            str(item.get("document_name") or "")
            for item in result.get("chunks", [])
        ))
        checks = [result.get("evidence_status") == case["status"]]
        if "verified" in case:
            checks.append(result.get("verified_count") == case["verified"])
        if "missing_entity" in case:
            checks.append(case["missing_entity"] in (result.get("missing_entities") or []))
        if "missing_subject" in case:
            checks.append(case["missing_subject"] in (result.get("missing_subjects") or []))
        if "coverage_limited" in case:
            checks.append(result.get("coverage_limited") is case["coverage_limited"])
        if "document_contains" in case:
            checks.append(any(case["document_contains"] in item for item in documents))
        if "documents_contain_all" in case:
            checks.extend(
                any(expected in item for item in documents)
                for expected in case["documents_contain_all"]
            )

        duration_ms = round((time.perf_counter() - started) * 1000)
        if all(checks):
            passed += 1
            print(
                f"PASS | {case['name']} | status={result.get('evidence_status')} | "
                f"candidate={result.get('candidate_count')} | verified={result.get('verified_count')} | "
                f"duration_ms={duration_ms}"
            )
        else:
            failures.append(case["name"])
            print(
                f"FAIL | {case['name']} | expected={case} | "
                f"actual_status={result.get('evidence_status')} | documents={documents}"
            )

    print(f"SUMMARY | passed={passed} | failed={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
