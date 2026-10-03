"""Measure the read-only agent layer on this machine and record reproducible figures.

Wall-clock numbers describe one local Windows run and are not throughput claims.
The script also asserts the invariants it reports, so a passing run is evidence
for the recorded values rather than a decorative plot.

    python scripts/benchmark_agent_layer.py
"""

from __future__ import annotations

import asyncio
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from enterprise_flow.agents import ROLE_SCOPES, ALL_AGENT_TOOLS, Supervisor  # noqa: E402
from enterprise_flow.planner import READ_ONLY_TOOLS, Decision, Planner, WRITE_TOOLS  # noqa: E402
from enterprise_flow.service import EnterpriseService  # noqa: E402

FIELDS = {"order_ids": ["alice-hotel-001", "alice-train-001"], "cost_center": "CC-ALPHA-OPS",
          "start_date": "2026-10-09", "end_date": "2026-10-11", "destination": "广州"}
MESSAGE = json.dumps(FIELDS, ensure_ascii=False)
PLAN_RUNS = 30
BLOCK_RUNS = 30
COLLABORATE_RUNS = 20


class WriteAttempt:
    """A reasoner that always proposes a business write."""

    mode, model_used = "fixture", False

    def __init__(self) -> None:
        self.usage = {"model_calls": 0, "input_tokens": None, "output_tokens": None}

    async def decide(self, state):
        return Decision(action="tool", tool="submit_expense", arguments={"draft_id": "benchmark"})


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))
    return ordered[index]


async def main() -> dict:
    temporary = tempfile.TemporaryDirectory()
    service = EnterpriseService(Path(temporary.name) / "business.sqlite")
    service.seed_demo()
    alice = service.authenticate_demo("alice")
    planner = Planner(service)
    supervisor = Supervisor(service)

    plan_times, plan_tool_calls, plan_context = [], [], []
    plan_digests = set()
    for _ in range(PLAN_RUNS):
        started = time.perf_counter()
        result = await planner.plan(alice, MESSAGE)
        plan_times.append((time.perf_counter() - started) * 1000)
        plan_tool_calls.append(result.tool_calls)
        plan_context.append(result.context_chars)
        plan_digests.add(json.dumps(result.model_dump(exclude={"duration_ms", "steps"}), ensure_ascii=False, sort_keys=True))
        assert result.stop_reason == "goal_satisfied" and not result.business_effects

    block_times, block_reasons = [], set()
    for _ in range(BLOCK_RUNS):
        started = time.perf_counter()
        result = await Planner(service, reasoner=WriteAttempt()).plan(alice, MESSAGE)
        block_times.append((time.perf_counter() - started) * 1000)
        block_reasons.add(result.stop_reason)
        assert result.tool_calls == 0 and result.blocked_tools == ["submit_expense"]

    collaborate_times, collaborate_calls, collaborate_digests = [], [], set()
    for _ in range(COLLABORATE_RUNS):
        started = time.perf_counter()
        proposal = await supervisor.collaborate(alice, MESSAGE)
        collaborate_times.append((time.perf_counter() - started) * 1000)
        collaborate_calls.append(proposal.tool_calls)
        collaborate_digests.add(proposal.content_digest)
        assert not proposal.blocked and not proposal.business_effects and proposal.requires_human_confirmation

    with closing(sqlite3.connect(service.database_path)) as connection:
        writes = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in ("drafts", "confirmations", "submissions")}

    report = {
        "date": time.strftime("%Y-%m-%d"),
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "synthetic_data": True,
        "planning": {
            "runs": PLAN_RUNS,
            "stop_reason": "goal_satisfied",
            "tool_calls_per_run": sorted(set(plan_tool_calls)),
            "context_chars_min": min(plan_context),
            "context_chars_max": max(plan_context),
            "wall_ms_p50": round(statistics.median(plan_times), 2),
            "wall_ms_p95": round(percentile(plan_times, 0.95), 2),
            "distinct_result_payloads": len(plan_digests),
        },
        "blocked_write": {
            "runs": BLOCK_RUNS,
            "proposed_tool": "submit_expense",
            "stop_reasons": sorted(block_reasons),
            "executed_tool_calls": 0,
            "wall_ms_p50": round(statistics.median(block_times), 2),
            "wall_ms_p95": round(percentile(block_times, 0.95), 2),
        },
        "collaboration": {
            "runs": COLLABORATE_RUNS,
            "roles": len(ROLE_SCOPES),
            "tool_calls_per_run": sorted(set(collaborate_calls)),
            "wall_ms_p50": round(statistics.median(collaborate_times), 2),
            "wall_ms_p95": round(percentile(collaborate_times, 0.95), 2),
            "distinct_content_digests": len(collaborate_digests),
        },
        "business_writes_after_all_runs": writes,
        "planning_allowlist_excludes_write_tools": not (WRITE_TOOLS & READ_ONLY_TOOLS),
        "agent_scopes_exclude_write_tools": not (WRITE_TOOLS & ALL_AGENT_TOOLS),
    }
    assert writes == {"drafts": 0, "confirmations": 0, "submissions": 0}, "The agent layer must not write"
    assert report["collaboration"]["distinct_content_digests"] == 1, "Same input must yield one digest"
    temporary.cleanup()
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), ensure_ascii=False, indent=2))
