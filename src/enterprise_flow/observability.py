"""Execution tracing: per-step decision records, latency and model cost accounting.

An enterprise agent has to answer "why did it do that, and what did that cost" for every run, so
this module records one span per decision point and aggregates token usage per run. Spans are
persisted through the existing ``events`` table, which keeps the audit trail in one place and
means tracing adds no new storage dependency.

The design assumes a tracer is optional. Nothing in the business path requires one; when a tracer
is not attached, :func:`current_tracer` returns a no-op recorder so call sites stay branch-free.
"""

from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

# A run-scoped tracer, so concurrent runs never mix their spans.
_ACTIVE: contextvars.ContextVar["ExecutionTracer | None"] = contextvars.ContextVar(
    "enterprise_flow_tracer", default=None)

# Pricing is configuration, not a constant: deployments change rates and models without a code edit.
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {}


@dataclass
class Span:
    """One recorded decision point: what ran, how long it took and what it produced."""

    name: str
    category: str
    started_at: float
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "category": self.category,
                                   "duration_ms": self.duration_ms, "status": self.status}
        if self.detail:
            payload["detail"] = self.detail
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass
class ModelUsage:
    """Token counts and the derived cost, accumulated across every model call in a run."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    uncounted_calls: int = 0
    cost_usd: float = 0.0

    def record(self, model: str, input_tokens: int | None, output_tokens: int | None) -> None:
        self.model_calls += 1
        if input_tokens is None or output_tokens is None:
            # A provider that does not return usage must not be silently priced at zero.
            self.uncounted_calls += 1
            return
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        rate = PRICING_PER_MILLION_TOKENS.get(model)
        if rate:
            self.cost_usd += (input_tokens * rate[0] + output_tokens * rate[1]) / 1_000_000

    def as_dict(self) -> dict[str, Any]:
        payload = {"model_calls": self.model_calls, "input_tokens": self.input_tokens,
                   "output_tokens": self.output_tokens, "cost_usd": round(self.cost_usd, 6)}
        if self.uncounted_calls:
            payload["uncounted_calls"] = self.uncounted_calls
        return payload


class ExecutionTracer:
    """Collects spans for one run and totals the model usage it produced."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.spans: list[Span] = []
        self.usage = ModelUsage()
        self.attributes: dict[str, Any] = {}

    @contextmanager
    def span(self, name: str, category: str = "step", **detail: Any) -> Iterator[Span]:
        record = Span(name=name, category=category, started_at=time.perf_counter(), detail=dict(detail))
        try:
            yield record
        except Exception as exc:
            record.status = "error"
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.duration_ms = int((time.perf_counter() - record.started_at) * 1000)
            self.spans.append(record)

    def record_usage(self, usage: dict[str, int | None] | None, model: str = "") -> None:
        if not usage:
            return
        self.usage.record(model, usage.get("input_tokens"), usage.get("output_tokens"))

    def set_attributes(self, **values: Any) -> None:
        self.attributes.update({key: value for key, value in values.items() if value is not None})

    def summary(self) -> dict[str, Any]:
        """The run's record: stages, decision points, totals and the slowest steps."""
        by_category: dict[str, int] = {}
        for record in self.spans:
            by_category[record.category] = by_category.get(record.category, 0) + record.duration_ms
        slowest = sorted(self.spans, key=lambda item: -item.duration_ms)[:5]
        return {
            "run_id": self.run_id,
            "span_count": len(self.spans),
            "failed_spans": sum(1 for record in self.spans if record.status == "error"),
            "stage_ms": dict(sorted(by_category.items(), key=lambda item: -item[1])),
            "slowest": [{"name": record.name, "duration_ms": record.duration_ms} for record in slowest],
            "model_usage": self.usage.as_dict(),
            "attributes": self.attributes,
            "spans": [record.as_dict() for record in self.spans],
        }


def current_tracer() -> ExecutionTracer | None:
    return _ACTIVE.get()


@contextmanager
def bind_tracer(tracer: ExecutionTracer) -> Iterator[ExecutionTracer]:
    """Attach an existing tracer to the current context.

    A workflow run resumes several times; binding the same tracer keeps one continuous
    span list for the whole run instead of one list per resume.
    """
    token = _ACTIVE.set(tracer)
    try:
        yield tracer
    finally:
        _ACTIVE.reset(token)


@contextmanager
def trace_run(run_id: str, tracer: ExecutionTracer | None = None) -> Iterator[ExecutionTracer]:
    """Attach a tracer to the current context for the duration of the block."""
    tracer = tracer or ExecutionTracer(run_id)
    with bind_tracer(tracer):
        yield tracer


@contextmanager
def traced(name: str, category: str = "step", **detail: Any) -> Iterator[Span | None]:
    """Record a step when a tracer is attached; otherwise run the block untouched."""
    tracer = current_tracer()
    if tracer is None:
        yield None
        return
    with tracer.span(name, category, **detail) as record:
        yield record


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Cost in USD for a call, or None when the model has no configured rate."""
    rate = PRICING_PER_MILLION_TOKENS.get(model)
    if not rate:
        return None
    return round((input_tokens * rate[0] + output_tokens * rate[1]) / 1_000_000, 6)


class MetricsRegistry:
    """Process-local counters and histograms exposed as Prometheus text.

    Deliberately dependency-free: the scrape endpoint has to work in the base install,
    so the registry keeps bounded buckets in memory instead of pulling a client library.
    """

    def __init__(self, max_samples: int = 1000) -> None:
        self.max_samples = max_samples
        self._counters: dict[tuple[str, str], float] = {}
        self._samples: dict[tuple[str, str], list[float]] = {}

    def inc(self, name: str, labels: dict[str, Any] | None = None, value: float = 1) -> None:
        key = (name, self._fmt(labels))
        self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        key = (name, self._fmt(labels))
        bucket = self._samples.setdefault(key, [])
        bucket.append(float(value))
        if len(bucket) > self.max_samples:
            del bucket[0]

    @staticmethod
    def _fmt(labels: dict[str, Any] | None) -> str:
        if not labels:
            return ""
        return ",".join(f'{key}="{str(value).replace(chr(34), chr(39))}"' for key, value in sorted(labels.items()))

    @staticmethod
    def _quantile(values: list[float], q: float) -> float:
        ordered = sorted(values)
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return round(ordered[index], 3)

    def render(self, prefix: str = "enterpriseflow") -> str:
        """Prometheus text exposition format, one family per metric name."""
        lines: list[str] = []
        for (name, labelset) in sorted(self._counters):
            key = f"{prefix}_{name}"
            lines.append(f"# TYPE {key} counter")
            lines.append(f"{key}{{{labelset}}} {self._counters[(name, labelset)]:g}" if labelset else f"{key} {self._counters[(name, labelset)]:g}")
        for (name, labelset) in sorted(self._samples):
            samples = self._samples[(name, labelset)]
            key = f"{prefix}_{name}"
            lines.append(f"# TYPE {key} summary")
            for quantile in (0.5, 0.95):
                suffix = f",quantile=\"{quantile}\"" if labelset else f"quantile=\"{quantile}\""
                body = f"{labelset}{suffix}" if labelset else suffix
                lines.append(f"{key}{{{body}}} {self._quantile(samples, quantile):g}")
            total = round(sum(samples), 3)
            count_label = f"{labelset},count=\"{len(samples)}\"" if labelset else f"count=\"{len(samples)}\""
            lines.append(f"{key}_sum {total:g}")
            lines.append(f"{key}_count{{{count_label}}} {len(samples)}")
        return "\n".join(lines) + ("\n" if lines else "")


METRICS = MetricsRegistry()
