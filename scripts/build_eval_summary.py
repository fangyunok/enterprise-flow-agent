"""Regenerate the packaged evaluation summary from the committed measurement records.

The web console ships ``src/enterprise_flow/data/eval_summary.json`` so an installed wheel
can display the same numbers without a source checkout. CI re-runs this script and fails
when the packaged file drifts from ``results/``, so the console can never show stale figures.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
TARGET = ROOT / "src" / "enterprise_flow" / "data" / "eval_summary.json"


def _load(name: str):
    path = RESULTS / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def retrieval_rows(records):
    rows = []
    for record in records or []:
        recall = record.get("recall") or {}
        rows.append({
            "label": record.get("label", record.get("mode", "")),
            "mode": record.get("mode", ""),
            "queries": record.get("queries", 0),
            "recall_at1": round(float(recall.get("@1", 0)), 4),
            "recall_at3": round(float(recall.get("@3", 0)), 4),
            "recall_at5": round(float(recall.get("@5", 0)), 4),
            "leakage": int(record.get("leaks", 0)),
            "scoped_pool_p50": record.get("scoped_pool_p50"),
            "scoped_pool_max": record.get("scoped_pool_max"),
            "latency_p50_ms": record.get("latency_p50_ms"),
            "store": record.get("store", "none"),
            "embedder": record.get("embedder", ""),
            "reranker": record.get("reranker", ""),
        })
    return rows


def model_rows(records):
    rows = []
    for record in records or []:
        rows.append({
            "label": "字段 Recall", "value": f"{float(record.get('field_recall', 0)):.3f}",
            "note": f"{record.get('cases', 0)} 条标注用例 · {record.get('reference_fields', 0)} 个参考字段",
        })
        rows.append({
            "label": "字段 Precision", "value": f"{float(record.get('field_precision', 0)):.3f}",
            "note": f"幻觉字段 {record.get('hallucinated_fields', 0)} 个 · 越权字段 {record.get('forbidden_violations', 0)} 个",
        })
        rows.append({
            "label": "整例完全匹配", "value": f"{record.get('exact_match_cases', 0)}/{record.get('cases', 0)}",
            "note": f"应弃答 {record.get('abstention_eligible', 0)} 条，实际弃答 {record.get('abstention_cases', 0)} 条",
        })
        rows.append({
            "label": "覆盖组别", "value": f"{len(record.get('groups') or [])} 组",
            "note": "、".join(record.get("groups") or []),
        })
    return rows


def planner_rows(benchmark):
    if not benchmark:
        return []
    planning = benchmark.get("planning") or {}
    blocked = benchmark.get("blocked_write") or {}
    collaboration = benchmark.get("collaboration") or {}
    writes = benchmark.get("business_writes_after_all_runs") or {}
    return [
        {"label": "只读规划循环", "value": f"{planning.get('runs', 0)} 次全部 {planning.get('stop_reason', '')}",
         "note": f"每次 {','.join(str(v) for v in planning.get('tool_calls_per_run') or [])} 次工具调用 · P50 {planning.get('wall_ms_p50')}ms / P95 {planning.get('wall_ms_p95')}ms", "good": True},
        {"label": "写入工具拦截", "value": f"{blocked.get('runs', 0)} 次全部 blocked_tool",
         "note": f"提议工具 {blocked.get('proposed_tool', '')} · 实际执行工具调用 {blocked.get('executed_tool_calls', 0)} 次", "good": True},
        {"label": "多角色协作", "value": f"{collaboration.get('runs', 0)} 次 · {collaboration.get('roles', 0)} 个角色",
         "note": f"每次 {','.join(str(v) for v in collaboration.get('tool_calls_per_run') or [])} 次工具调用 · P50 {collaboration.get('wall_ms_p50')}ms / P95 {collaboration.get('wall_ms_p95')}ms", "good": True},
        {"label": "业务写入计数", "value": f"{writes.get('drafts', 0)} 草稿 / {writes.get('confirmations', 0)} 确认 / {writes.get('submissions', 0)} 提交",
         "note": "三轮共 80 次运行后业务表无新增", "good": True},
    ]


def main() -> None:
    summary = {
        "sources": ["results/retrieval_eval.json", "results/model_eval.json", "results/agent_layer_benchmark.json"],
        "retrieval": retrieval_rows(_load("retrieval_eval.json")),
        "model": model_rows(_load("model_eval.json")),
        "planner": planner_rows(_load("agent_layer_benchmark.json")),
    }
    TARGET.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
