"""Score a field extractor against the labelled dataset from ``build_model_eval_dataset.py``.

The report separates three questions that are usually conflated:

* **Extraction quality** — field-level precision / recall on values the user actually stated.
  Precision is computed over *all* emitted values, so a hallucinated field is penalised twice:
  once as a false positive and once as a missed reference value.
* **Abstention quality** — cases listing ``forbidden`` fields must leave them empty. This is the
  behaviour that keeps a wrong answer from becoming a wrong business decision.
* **Operational cost** — wall-clock latency, reported token usage and per-case failures.

The script talks to any OpenAI-compatible endpoint through the project's own ``HttpExtractor``, so
whatever serves the model in production is exactly what gets measured here.

    python scripts/evaluate_model.py --mode fixture
    python scripts/evaluate_model.py --mode api --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
        --model qwen-plus --api-key "$KEY"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enterprise_flow.model import (  # noqa: E402
    Extraction,
    FixtureExtractor,
    HttpExtractor,
    ModelError,
    TripFields,
)

CASES_PATH = ROOT / "data" / "model_eval_cases.jsonl"
REPORT_PATH = ROOT / "results" / "model_eval.md"
RAW_PATH = ROOT / "results" / "model_eval.json"

FIELD_NAMES = ("order_ids", "cost_center", "start_date", "end_date", "destination", "notes")


def emitted_values(fields: TripFields) -> dict[str, object]:
    """Normalise an extraction into comparable values, dropping empty ones."""
    values: dict[str, object] = {}
    if fields.order_ids:
        values["order_ids"] = sorted(fields.order_ids)
    for name in FIELD_NAMES[1:]:
        value = getattr(fields, name)
        if value:
            values[name] = value.strip() if isinstance(value, str) else value
    return values


def normalise_expected(expected: dict) -> dict[str, object]:
    values: dict[str, object] = {}
    if "order_ids" in expected:
        values["order_ids"] = sorted(expected["order_ids"])
    for name, value in expected.items():
        if name != "order_ids" and value:
            values[name] = value.strip() if isinstance(value, str) else value
    return values


def score_case(case: dict, extraction: Extraction | None, error: str | None) -> dict:
    reference = normalise_expected(case["expected"])
    forbidden = set(case["forbidden"])
    if extraction is None:
        return {"case_id": case["case_id"], "group": case["case_id"].rsplit("-", 1)[0], "ok": False,
                "error": error, "expected": reference, "actual": {}, "missing": sorted(reference),
                "hallucinated": [], "forbidden_violations": sorted(forbidden), "abstained": False}
    actual = emitted_values(extraction.fields)
    missing = sorted(name for name in reference if actual.get(name) != reference[name])
    extra = sorted(name for name in actual if name not in reference)
    violations = sorted(name for name in forbidden if name in actual)
    return {
        "case_id": case["case_id"],
        "group": case["case_id"].rsplit("-", 1)[0],
        "ok": not missing and not extra and not violations,
        "error": error,
        "expected": reference,
        "actual": actual,
        "missing": missing,
        "hallucinated": extra,
        "forbidden_violations": violations,
        "abstained": not actual,
        "usage": extraction.usage,
        "duration_ms": extraction.duration_ms,
        "model_used": extraction.model_used,
    }


def aggregate(rows: list[dict], cases: list[dict]) -> dict:
    reference_total = sum(len(normalise_expected(case["expected"])) for case in cases)
    correct = sum(len(row["expected"]) - len(row["missing"]) for row in rows)
    emitted_total = sum(len(row["actual"]) for row in rows)
    correct_emitted = sum(1 for row in rows for name, value in row["actual"].items()
                          if row["expected"].get(name) == value)
    hallucinated = sum(len(row["hallucinated"]) for row in rows)
    violations = sum(len(row["forbidden_violations"]) for row in rows)
    # Abstention is scored on one consistent pool: cases where the user stated nothing extractable,
    # i.e. the reference is empty. For those, emitting any value at all is a failure, so the
    # numerator counts the rows of exactly that pool that came back empty.
    eligible_indexes = [index for index, case in enumerate(cases)
                        if not normalise_expected(case["expected"])]
    eligible = [cases[index] for index in eligible_indexes]
    abstained_in_pool = sum(1 for index in eligible_indexes
                            if index < len(rows) and rows[index]["abstained"])
    return {
        "cases": len(cases),
        "failed_cases": sum(1 for row in rows if row.get("error")),
        "exact_match_cases": sum(1 for row in rows if row["ok"]),
        "exact_match_rate": sum(1 for row in rows if row["ok"]) / len(cases) if cases else 0.0,
        "reference_fields": reference_total,
        "field_recall": correct / reference_total if reference_total else 0.0,
        "field_precision": correct_emitted / emitted_total if emitted_total else 0.0,
        "hallucinated_fields": hallucinated,
        "abstention_cases": abstained_in_pool,
        "abstention_eligible": len(eligible),
        "forbidden_violations": violations,
        "groups": sorted({row["group"] for row in rows}),
    }


async def run_case(extractor, case: dict) -> tuple[Extraction | None, str | None, int]:
    started = time.perf_counter()
    try:
        extraction = await extractor.extract(case["message"])
        return extraction, None, int((time.perf_counter() - started) * 1000)
    except ModelError as exc:
        return None, str(exc), int((time.perf_counter() - started) * 1000)
    except Exception as exc:  # transport, malformed payload
        return None, f"{type(exc).__name__}: {exc}", int((time.perf_counter() - started) * 1000)


async def evaluate(extractor, cases: list[dict], concurrency: int, repeat: int) -> tuple[list[dict], dict]:
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[int] = []
    usages: list[int] = []

    async def one(case: dict) -> dict:
        async with semaphore:
            extraction, error, elapsed = await run_case(extractor, case)
        latencies.append(elapsed)
        if extraction and extraction.usage:
            tokens = extraction.usage.get("output_tokens")
            if isinstance(tokens, int):
                usages.append(tokens)
        return score_case(case, extraction, error)

    rows: list[dict] = []
    for _ in range(repeat):
        rows.extend(await asyncio.gather(*(one(case) for case in cases)))
    summary = aggregate(rows, cases * repeat)
    summary["latency_p50_ms"] = round(statistics.median(latencies), 1) if latencies else 0.0
    summary["latency_max_ms"] = max(latencies) if latencies else 0
    summary["output_tokens_p50"] = statistics.median(usages) if usages else None
    summary["model_used"] = any(row.get("model_used") for row in rows)
    return rows, summary


def render(label: str, configuration: dict, summary: dict, rows: list[dict], show_failures: int) -> str:
    precision = summary["field_precision"]
    recall = summary["field_recall"]
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    lines = [f"### {label}", "",
             f"- 配置：{configuration}",
             f"- 用例：{summary['cases']} 条（{summary['exact_match_cases']} 条字段级完全匹配）"
             f" | 请求失败 {summary['failed_cases']} 条",
             f"- 模型调用：{'是' if summary['model_used'] else '否'}", "",
             "| 指标 | 值 |", "| --- | --- |",
             f"| 字段级 Precision | {precision:.3f} |",
             f"| 字段级 Recall | {recall:.3f} |",
             f"| 字段级 F1 | {f1:.3f} |",
             f"| 完全匹配用例率 | {summary['exact_match_rate']:.3f} |",
             f"| 幻觉字段（引用中不存在） | {summary['hallucinated_fields']} |",
             f"| 应留空却填了的字段 | {summary['forbidden_violations']} |",
             f"| 正确留空（弃答）用例 | {summary['abstention_cases']} / {summary['abstention_eligible']} |",
             f"| 单次 P50 / 最大延迟 | {summary['latency_p50_ms']} / {summary['latency_max_ms']} ms |",
             f"| 输出 token P50 | {summary['output_tokens_p50'] if summary['output_tokens_p50'] is not None else '未返回'} |", ""]
    if show_failures:
        failures = [row for row in rows if not row["ok"]][:show_failures]
        if failures:
            lines += [f"失分样例（前 {len(failures)} 条）", ""]
            for row in failures:
                lines.append(f"- `{row['case_id']}`"
                             + (f" 请求失败：{row['error']}" if row.get("error") else
                                f" 漏={row['missing'] or '无'} 多={row['hallucinated'] or '无'}"
                                f" 违规留空={row['forbidden_violations'] or '无'}"))
                lines.append(f"  - 期望：{json.dumps(row['expected'], ensure_ascii=False)}")
                lines.append(f"  - 实际：{json.dumps(row['actual'], ensure_ascii=False)}")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate field extraction against labelled cases")
    parser.add_argument("--mode", choices=["fixture", "qwen", "api"], default="fixture")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--repeat", type=int, default=1, help="repeat the whole set N times")
    parser.add_argument("--label", default=None)
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--show-failures", type=int, default=0, metavar="N")
    arguments = parser.parse_args()
    if arguments.repeat < 1 or arguments.concurrency < 1:
        parser.error("--repeat and --concurrency must be positive")

    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if arguments.mode == "fixture":
        extractor = FixtureExtractor()
        configuration = "extractor=FixtureExtractor（离线确定性解析）"
    else:
        extractor = HttpExtractor(mode=arguments.mode, base_url=arguments.base_url, model=arguments.model,
                                  api_key=arguments.api_key, timeout=arguments.timeout)
        configuration = f"extractor=HttpExtractor | model={extractor.model} | base={extractor.base_url}"

    rows, summary = asyncio.run(evaluate(extractor, cases, arguments.concurrency, arguments.repeat))
    summary["label"] = arguments.label or arguments.mode
    summary["mode"] = arguments.mode
    summary["model"] = getattr(extractor, "model", "fixture")
    summary["base_url"] = getattr(extractor, "base_url", "")
    summary["run_date"] = date.today().isoformat()

    label = arguments.label or f"{arguments.mode} · {summary['model']}"
    section = render(label, configuration, summary, rows, arguments.show_failures)

    header = ("# 字段提取评测记录\n\n标注集由 `scripts/build_model_eval_dataset.py` 生成，评测由 "
              "`scripts/evaluate_model.py` 运行。\nPrecision 以「模型实际吐出的字段」为分母，"
              "因此凭空生成的字段同时计入假阳性与漏检；`forbidden` 字段被填入记为弃答失败，\n"
              "对应「应当留空并请求人工补充」的场景。评测调用与线上相同的 `HttpExtractor`，"
              "不引入额外封装。\n\n")
    if arguments.append and REPORT_PATH.exists():
        REPORT_PATH.write_text(REPORT_PATH.read_text(encoding="utf-8") + "\n" + section, encoding="utf-8")
    else:
        REPORT_PATH.write_text(header + section, encoding="utf-8")

    existing = json.loads(RAW_PATH.read_text(encoding="utf-8")) if (arguments.append and RAW_PATH.exists()) else []
    if not isinstance(existing, list):
        existing = [existing]
    existing.append({**summary, "per_case": rows})
    RAW_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(section)
    print(f"report: {REPORT_PATH.relative_to(ROOT)}  raw: {RAW_PATH.relative_to(ROOT)}")
    if arguments.mode != "fixture" and summary["failed_cases"] == summary["cases"]:
        print("\n所有请求均失败：请检查模型服务地址、模型名与凭据。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
