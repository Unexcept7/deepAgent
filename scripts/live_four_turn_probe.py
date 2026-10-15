"""Run the four-turn regression scenario against the live Qwen/RAGFlow stack."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.graph import run_agent


QUESTIONS = [
    "FT-RS-9999 支持哪些 Modbus 功能码？",
    "FT-RS-0808 支持哪些 Modbus 功能码？",
    "比较 FT-RS-0808、M1501 和 M1516 的数字量输入输出能力。",
    "请返回系统中所有设备的信息。",
]


def main() -> int:
    history: list[dict[str, str]] = []
    records: list[dict] = []
    for question in QUESTIONS:
        result = run_agent(history, question)
        evidence = result.get("evidence") or {}
        trace = result.get("trace") or {}
        records.append({
            "query": question,
            "status": evidence.get("status"),
            "missing_entities": evidence.get("missing_entities"),
            "model_calls": trace.get("model_calls"),
            "ragflow_calls": trace.get("ragflow_calls"),
            "used_ragflow": trace.get("used_ragflow"),
            "answer": result.get("answer", "")[:500],
        })
        history.extend([
            {"role": "user", "content": question},
            {"role": "assistant", "content": result.get("answer", "")},
        ])

    print(json.dumps(records, ensure_ascii=False, indent=2))
    expected = ["not_found", "supported", "supported", "partial"]
    actual = [record["status"] for record in records]
    print(f"SUMMARY | expected={expected} | actual={actual}")
    return 0 if actual == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
