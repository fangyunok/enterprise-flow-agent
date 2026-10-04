"""Tests for execution tracing.

Tracing is observability, not business logic, so the properties that matter are: a run always
produces a record, failures are captured instead of swallowed, model cost is only reported when a
rate exists, and an untraced code path behaves exactly as if no tracer existed.
"""

import unittest

from enterprise_flow.observability import (
    ExecutionTracer,
    PRICING_PER_MILLION_TOKENS,
    ModelUsage,
    current_tracer,
    estimate_cost,
    trace_run,
    traced,
)


class TracerScopeTest(unittest.TestCase):
    def test_no_tracer_is_attached_by_default(self):
        self.assertIsNone(current_tracer())

    def test_tracer_is_visible_inside_the_run_and_detached_after(self):
        with trace_run("run-1") as tracer:
            self.assertIs(current_tracer(), tracer)
        self.assertIsNone(current_tracer())

    def test_traced_without_a_tracer_yields_none_and_still_runs_the_block(self):
        reached = []
        with traced("step", "tool") as record:
            self.assertIsNone(record)
            reached.append(True)
        self.assertEqual(reached, [True])

    def test_nested_runs_restore_the_previous_tracer(self):
        with trace_run("outer") as outer:
            with trace_run("inner") as inner:
                self.assertIs(current_tracer(), inner)
            self.assertIs(current_tracer(), outer)


class SpanRecordingTest(unittest.TestCase):
    def test_span_records_duration_and_detail(self):
        tracer = ExecutionTracer("run-1")
        with tracer.span("search_policy", "tool", query="住宿"):
            pass
        self.assertEqual(len(tracer.spans), 1)
        record = tracer.spans[0]
        self.assertEqual((record.name, record.category, record.status), ("search_policy", "tool", "ok"))
        self.assertEqual(record.detail, {"query": "住宿"})
        self.assertGreaterEqual(record.duration_ms, 0)

    def test_failing_span_is_marked_and_re_raised(self):
        tracer = ExecutionTracer("run-1")
        with self.assertRaises(ValueError):
            with tracer.span("calculate_expense", "tool"):
                raise ValueError("bad input")
        self.assertEqual(tracer.spans[0].status, "error")
        self.assertIn("ValueError", tracer.spans[0].error)

    def test_span_is_recorded_even_when_the_block_fails(self):
        tracer = ExecutionTracer("run-1")
        try:
            with tracer.span("submit", "write"):
                raise RuntimeError("conflict")
        except RuntimeError:
            pass
        self.assertEqual(tracer.summary()["failed_spans"], 1)


class SummaryTest(unittest.TestCase):
    def build(self) -> ExecutionTracer:
        tracer = ExecutionTracer("run-2")
        for name, category in (("extract", "model"), ("plan", "agent"), ("search", "tool"), ("confirm", "write")):
            with tracer.span(name, category):
                pass
        tracer.set_attributes(model="qwen", run_id="run-2", skipped=None)
        return tracer

    def test_summary_groups_latency_by_category(self):
        stages = self.build().summary()["stage_ms"]
        self.assertEqual(set(stages), {"model", "agent", "tool", "write"})

    def test_summary_reports_span_and_failure_counts(self):
        summary = self.build().summary()
        self.assertEqual(summary["span_count"], 4)
        self.assertEqual(summary["failed_spans"], 0)
        self.assertEqual(summary["run_id"], "run-2")

    def test_attributes_drop_none_values(self):
        self.assertEqual(self.build().summary()["attributes"], {"model": "qwen", "run_id": "run-2"})

    def test_slowest_spans_are_capped(self):
        tracer = ExecutionTracer("run-3")
        for index in range(12):
            with tracer.span(f"step-{index}", "tool"):
                pass
        self.assertEqual(len(tracer.summary()["slowest"]), 5)


class UsageAccountingTest(unittest.TestCase):
    def test_unconfigured_model_records_calls_without_cost(self):
        usage = ModelUsage()
        usage.record("mystery-model", 1000, 500)
        self.assertEqual(usage.model_calls, 1)
        self.assertEqual(usage.cost_usd, 0.0)
        self.assertNotIn("uncounted_calls", usage.as_dict())

    def test_missing_provider_usage_is_flagged_instead_of_priced_as_zero(self):
        usage = ModelUsage()
        usage.record("mystery-model", None, None)
        self.assertEqual(usage.model_calls, 1)
        self.assertEqual(usage.as_dict()["uncounted_calls"], 1)

    def test_configured_rate_produces_a_cost(self):
        PRICING_PER_MILLION_TOKENS["priced-model"] = (0.5, 1.5)
        usage = ModelUsage()
        usage.record("priced-model", 2_000_000, 1_000_000)
        self.assertAlmostEqual(usage.cost_usd, 2.5, places=6)

    def test_estimate_cost_returns_none_for_an_unpriced_model(self):
        self.assertIsNone(estimate_cost("mystery-model", 100, 100))
        PRICING_PER_MILLION_TOKENS["priced-model"] = (0.5, 1.5)
        self.assertAlmostEqual(estimate_cost("priced-model", 1_000_000, 1_000_000), 2.0, places=6)

    def test_recording_an_empty_usage_dict_is_a_noop(self):
        tracer = ExecutionTracer("run-4")
        tracer.record_usage(None)
        tracer.record_usage({})
        self.assertEqual(tracer.usage.model_calls, 0)

    def test_tracer_aggregates_usage_across_spans(self):
        tracer = ExecutionTracer("run-5")
        PRICING_PER_MILLION_TOKENS["priced-model"] = (0.5, 1.5)
        with tracer.span("extract", "model"):
            tracer.record_usage({"input_tokens": 1_000_000, "output_tokens": 500_000}, "priced-model")
        with tracer.span("answer", "model"):
            tracer.record_usage({"input_tokens": 500_000, "output_tokens": 250_000}, "priced-model")
        usage = tracer.summary()["model_usage"]
        self.assertEqual(usage["model_calls"], 2)
        self.assertEqual(usage["input_tokens"], 1_500_000)
        # 1.5M input at $0.5/M plus 0.75M output at $1.5/M.
        self.assertAlmostEqual(usage["cost_usd"], 1.875, places=6)


if __name__ == "__main__":
    unittest.main()
