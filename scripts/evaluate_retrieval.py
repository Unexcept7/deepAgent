"""Run the document-level retrieval benchmark against the configured RAGFlow dataset."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import fmean
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.tools.ragflow import RAGFlowClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "tests/retrieval_eval/beichen47_retrieval_v1.jsonl",
    )
    parser.add_argument("--split", choices=("all", "dev", "test"), default="all")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument(
        "--ragflow-dataset-id",
        help="只检索指定 RAGFlow Dataset，忽略 .env 中其他绑定 Dataset。",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并发检索数；默认 1 用于测量单请求延迟。",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load_cases(path: Path, split: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            case = json.loads(line)
            if split == "all" or case.get("split") == split:
                cases.append(case)
    if not cases:
        raise ValueError(f"评测集 {path} 中没有 split={split} 的用例")
    return cases


def normalize_document_name(value: str) -> str:
    stem = Path(str(value or "")).stem.casefold()
    return re.sub(r"[\s_\-()（）]", "", stem)


def gold_document_ids(case: dict[str, Any]) -> set[str]:
    return {
        str(item["document_id"])
        for item in case.get("gold_documents") or []
        if isinstance(item, dict) and item.get("document_id")
    }


def gold_document_names(case: dict[str, Any]) -> set[str]:
    names = set()
    for item in case.get("gold_documents") or []:
        name = item.get("document_name") if isinstance(item, dict) else item
        if name:
            names.add(normalize_document_name(str(name)))
    return names


def evaluate_case(client: RAGFlowClient, case: dict[str, Any], top_k: int) -> dict[str, Any]:
    started_at = time.perf_counter()
    result = client.search(case["query"], top_k=top_k)
    duration_ms = round((time.perf_counter() - started_at) * 1000)
    chunks = list(result.get("chunks") or [])[:top_k]
    retrieved = [
        {
            "document_id": chunk.get("document_id"),
            "document_name": str(chunk.get("document_name") or ""),
            "page": chunk.get("page"),
            "score": chunk.get("score"),
        }
        for chunk in chunks
    ]
    expected_ids = gold_document_ids(case)
    expected_names = gold_document_names(case)

    def is_gold(chunk: dict[str, Any]) -> bool:
        if expected_ids:
            return str(chunk.get("document_id") or "") in expected_ids
        return normalize_document_name(chunk["document_name"]) in expected_names

    hit_positions = [
        index + 1
        for index, chunk in enumerate(retrieved)
        if is_gold(chunk)
    ]
    unique_retrieved: list[dict[str, Any]] = []
    seen_document_keys: set[str] = set()
    for chunk in retrieved:
        document_key = str(
            chunk.get("document_id")
            or normalize_document_name(chunk["document_name"])
        )
        if document_key in seen_document_keys:
            continue
        seen_document_keys.add(document_key)
        unique_retrieved.append(chunk)
    hit_documents = {
        str(chunk.get("document_id") or normalize_document_name(chunk["document_name"]))
        for chunk in unique_retrieved
        if is_gold(chunk)
    }
    expected = expected_ids or expected_names
    record: dict[str, Any] = {
        "id": case["id"],
        "group_id": case["group_id"],
        "split": case["split"],
        "query": case["query"],
        "expected_outcome": case["expected_outcome"],
        "gold_documents": case.get("gold_documents") or [],
        "required_concepts": case.get("required_concepts") or [],
        "difficulty": case["difficulty"],
        "retrieved": retrieved,
        "unique_retrieved_documents": [
            {
                "document_id": chunk.get("document_id"),
                "document_name": chunk.get("document_name"),
                "page": chunk.get("page"),
                "score": chunk.get("score"),
            }
            for chunk in unique_retrieved
        ],
        "returned_chunk_count": len(retrieved),
        "unique_document_count": len(unique_retrieved),
        "duplicate_chunk_count": len(retrieved) - len(unique_retrieved),
        "candidate_count": result.get("candidate_count"),
        "total": result.get("total"),
        "duration_ms": duration_ms,
    }
    if case["expected_outcome"] == "retrieve":
        record.update({
            # Chunk precision is kept for diagnosing repeated chunks. It is
            # not a document-level metric when one file occupies many ranks.
            "chunk_hit_precision_at_k": len(hit_positions) / top_k,
            "document_hit_precision_at_k": (
                len(hit_documents) / len(unique_retrieved)
                if unique_retrieved else 0
            ),
            "document_recall_at_k": len(hit_documents) / len(expected),
            "reciprocal_rank": 1 / min(hit_positions) if hit_positions else 0,
            "all_gold_documents_retrieved": hit_documents == expected,
        })
    return record


def summarize(records: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    positive = [item for item in records if item["expected_outcome"] == "retrieve"]
    abstain = [item for item in records if item["expected_outcome"] == "abstain"]
    return {
        "top_k": top_k,
        "case_count": len(records),
        "retrieval_case_count": len(positive),
        "abstention_case_count": len(abstain),
        "mean_document_hit_precision_at_k": fmean(
            item["document_hit_precision_at_k"] for item in positive
        ),
        "mean_chunk_hit_precision_at_k": fmean(
            item["chunk_hit_precision_at_k"] for item in positive
        ),
        "mean_document_recall_at_k": fmean(
            item["document_recall_at_k"] for item in positive
        ),
        "mrr": fmean(item["reciprocal_rank"] for item in positive),
        "all_gold_documents_retrieved_rate": fmean(
            float(item["all_gold_documents_retrieved"]) for item in positive
        ),
        "mean_duration_ms": round(fmean(item["duration_ms"] for item in records), 2),
        "abstention_candidates": [
            {"id": item["id"], "retrieved": item["retrieved"]}
            for item in abstain
        ],
    }


def main() -> None:
    args = parse_args()
    if args.top_k < 1:
        raise ValueError("--top-k 必须大于 0")
    if args.workers < 1:
        raise ValueError("--workers 必须大于 0")
    cases = load_cases(args.dataset, args.split)
    client = RAGFlowClient()
    if args.ragflow_dataset_id:
        client.configured_ids = [args.ragflow_dataset_id]
        client.auto_discover = False
    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    def run_case(case: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        try:
            return evaluate_case(client, case, args.top_k), None
        except Exception as exc:
            return None, {"id": case["id"], "error": str(exc)}

    if args.workers == 1:
        results = map(run_case, cases)
    else:
        executor = ThreadPoolExecutor(max_workers=args.workers)
        results = executor.map(run_case, cases)

    try:
        for index, (record, failure) in enumerate(results, start=1):
            print(f"[{index}/{len(cases)}] {cases[index - 1]['id']}", flush=True)
            if record:
                records.append(record)
            if failure:
                failures.append(failure)
    finally:
        if args.workers > 1:
            executor.shutdown(wait=True)

    payload = {
        "dataset": str(args.dataset),
        "split": args.split,
        "summary": summarize(records, args.top_k) if records else None,
        "records": records,
        "failures": failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    if failures:
        print(json.dumps({"failures": failures}, ensure_ascii=False, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
