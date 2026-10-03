"""Planner boundary tests: read-only enforcement, budgets, loops and model contract."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import httpx

from enterprise_flow.planner import (READ_ONLY_TOOLS, WRITE_TOOLS, Decision, HttpReasoner, Planner,
                                     RuleReasoner, create_reasoner)
from enterprise_flow.service import DomainError, EnterpriseService
from enterprise_flow.tools import ToolGateway

FIELDS = {"order_ids": ["alice-hotel-001", "alice-train-001"], "cost_center": "CC-ALPHA-OPS",
          "start_date": "2026-10-09", "end_date": "2026-10-11", "destination": "广州"}
MESSAGE = json.dumps(FIELDS, ensure_ascii=False)


class ScriptedReasoner:
    """A reasoner that returns whatever the test wants, in order."""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.mode, self.model_used = "fixture", False
        self.usage = {"model_calls": 0, "input_tokens": None, "output_tokens": None}

    async def decide(self, state):
        if self.decisions:
            return self.decisions.pop(0)
        return Decision(action="finish", finish_reason="goal_satisfied", thought="done")


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = EnterpriseService(Path(self.temporary.name) / "business.sqlite")
        self.service.seed_demo()
        self.alice = self.service.authenticate_demo("alice")
        self.bob = self.service.authenticate_demo("bob")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def count(self, table):
        with closing(sqlite3.connect(self.service.database_path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def business_writes(self):
        return {table: self.count(table) for table in ("drafts", "confirmations", "submissions", "events")}

    async def plan(self, message=MESSAGE, **kwargs):
        return await Planner(self.service, **kwargs).plan(self.alice, message)

    async def test_fixture_loop_reads_then_computes_and_stops_on_goal(self):
        result = await self.plan()
        self.assertEqual(result.stop_reason, "goal_satisfied")
        self.assertEqual([step.observation.tool for step in result.steps],
                         ["get_my_orders", "search_policy", "calculate_expense"])
        self.assertEqual(result.findings["calculate_expense"]["eligible_cents"], 123000)
        self.assertEqual(result.findings["calculate_expense"]["excess_cents"], 6000)
        self.assertFalse(result.model_used)
        self.assertTrue(result.read_only)
        self.assertFalse(result.business_effects)
        self.assertEqual(result.blocked_tools, [])
        self.assertEqual(result.usage["planning"]["model_calls"], 0)

    async def test_planning_creates_no_business_records(self):
        await self.plan()
        self.assertEqual(self.business_writes(), {"drafts": 0, "confirmations": 0, "submissions": 0, "events": 0})

    async def test_reasoner_proposed_write_tool_is_blocked_before_execution(self):
        for tool in sorted(WRITE_TOOLS):
            reasoner = ScriptedReasoner([Decision(action="tool", tool=tool, arguments={"draft_id": "draft-x", "expected_version": 1})])
            result = await self.plan(reasoner=reasoner)
            self.assertEqual(result.stop_reason, "blocked_tool")
            self.assertEqual(result.blocked_tools, [tool])
            self.assertEqual(result.tool_calls, 0)
            self.assertEqual(result.steps, [])
        self.assertEqual(self.business_writes(), {"drafts": 0, "confirmations": 0, "submissions": 0, "events": 0})

    async def test_unknown_and_self_referential_tools_are_blocked(self):
        for tool in ["unknown_tool", "plan_readonly_analysis", "sql: drop table drafts"]:
            result = await self.plan(reasoner=ScriptedReasoner([Decision(action="tool", tool=tool)]))
            self.assertEqual(result.stop_reason, "blocked_tool")
            self.assertEqual(result.blocked_tools, [tool])
            self.assertEqual(result.tool_calls, 0)

    async def test_missing_tool_name_is_reported_as_an_empty_block(self):
        result = await self.plan(reasoner=ScriptedReasoner([Decision(action="tool")]))
        self.assertEqual(result.stop_reason, "blocked_tool")
        self.assertEqual(result.blocked_tools, [""])

    async def test_repeated_identical_action_is_detected_as_a_loop(self):
        reasoner = ScriptedReasoner([Decision(action="tool", tool="get_my_orders", arguments={})] * 8)
        result = await self.plan(reasoner=reasoner, max_steps=6)
        self.assertEqual(result.stop_reason, "loop_detected")
        self.assertEqual(result.tool_calls, 1)

    async def test_same_tool_with_different_arguments_is_not_a_loop(self):
        class Varying:
            mode, model_used = "fixture", False
            def __init__(self):
                self.usage = {"model_calls": 0, "input_tokens": None, "output_tokens": None}
            async def decide(self, state):
                return Decision(action="tool", tool="get_my_orders",
                                arguments={"start_date": f"2026-0{state.remaining_steps}-01"})

        result = await self.plan(reasoner=Varying(), max_steps=4)
        self.assertEqual(result.stop_reason, "max_steps")
        self.assertEqual(result.tool_calls, 4)
        self.assertEqual(len({json.dumps(step.observation.arguments, sort_keys=True) for step in result.steps}), 4)

    async def test_context_budget_exhaustion_stops_the_loop(self):
        class Varying:
            mode, model_used = "fixture", False
            def __init__(self):
                self.usage = {"model_calls": 0, "input_tokens": None, "output_tokens": None}
            async def decide(self, state):
                return Decision(action="tool", tool="get_my_orders",
                                arguments={"start_date": f"2026-0{state.remaining_steps}-01"})

        result = await self.plan(reasoner=Varying(), max_steps=6, max_context_chars=1000)
        self.assertEqual(result.stop_reason, "budget_exhausted")
        self.assertLess(result.tool_calls, 6)
        self.assertGreater(result.context_chars, 1000)

    async def test_missing_order_ids_stop_as_clarification(self):
        result = await self.plan(json.dumps({"cost_center": "CC-ALPHA-OPS"}, ensure_ascii=False))
        self.assertEqual(result.stop_reason, "needs_clarification")
        self.assertIn("order_ids", result.missing_fields)
        self.assertEqual(result.tool_calls, 1)

    async def test_missing_cost_center_stops_as_clarification(self):
        message = json.dumps({**FIELDS, "cost_center": None}, ensure_ascii=False)
        result = await self.plan(message)
        self.assertEqual(result.stop_reason, "needs_clarification")
        self.assertIn("cost_center", result.missing_fields)
        self.assertEqual([step.observation.tool for step in result.steps],
                         ["get_my_orders", "search_policy", "get_preferences", "calculate_expense"])
        self.assertEqual(result.steps[-1].observation.error_code, "missing_cost_center")

    async def test_another_employees_orders_are_not_reachable(self):
        result = await Planner(self.service).plan(self.bob, json.dumps({**FIELDS, "cost_center": "CC-ALPHA-ENG"}, ensure_ascii=False))
        self.assertIn(result.stop_reason, {"tool_error", "needs_clarification"})
        self.assertNotIn("calculate_expense", result.findings)
        self.assertEqual(result.steps[-1].observation.error_code, "order_not_found")

    async def test_message_bounds_are_enforced(self):
        for message in ["", "   ", "x" * 4001]:
            with self.assertRaises(DomainError) as error:
                await Planner(self.service).plan(self.alice, message)
            self.assertEqual(error.exception.code, "invalid_message")

    async def test_construction_bounds_are_enforced(self):
        for kwargs in [{"max_steps": 0}, {"max_steps": 21}, {"max_context_chars": 10}, {"mode": "bogus"}]:
            with self.assertRaises(ValueError):
                Planner(self.service, **kwargs)

    async def test_observations_are_digested_and_bounded(self):
        result = await self.plan()
        for step in result.steps:
            self.assertEqual(len(step.observation.digest), 64)
            self.assertGreaterEqual(step.observation.duration_ms, 0)
            self.assertLessEqual(len(step.observation.summary.get("order_ids", [])), 10)

    async def test_mcp_tool_exposes_the_read_only_plan_and_validates_bounds(self):
        gateway = ToolGateway(self.service, self.alice)
        result, event = await gateway.call("plan_readonly_analysis", {"message": MESSAGE})
        self.assertTrue(event["ok"])
        self.assertEqual(result["stop_reason"], "goal_satisfied")
        self.assertFalse(result["business_effects"])
        with self.assertRaises(DomainError) as error:
            await gateway.call("plan_readonly_analysis", {"message": MESSAGE, "max_steps": 99})
        self.assertEqual(error.exception.code, "invalid_tool_input")

    async def test_create_reasoner_validates_mode(self):
        self.assertIsInstance(create_reasoner("fixture"), RuleReasoner)
        for mode in ["bogus", ""]:
            with self.assertRaises(ValueError):
                create_reasoner(mode)


class HttpReasonerTests(unittest.IsolatedAsyncioTestCase):
    """Protocol-level checks with a mock transport; no real model is contacted."""

    EXTRACTION_MARKER = "Extract only explicitly stated reimbursement task fields"

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = EnterpriseService(Path(self.temporary.name) / "business.sqlite")
        self.service.seed_demo()
        self.alice = self.service.authenticate_demo("alice")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def transport(self, decisions=None, usage=None, planning_error=None, extraction_error=None):
        """Route field extraction and planning separately and record what was sent."""
        seen = {"planning": [], "extraction": 0}
        queue = list(decisions or [])

        def respond(request):
            payload = json.loads(request.content.decode("utf-8"))
            if payload["messages"][0]["content"].startswith(self.EXTRACTION_MARKER):
                seen["extraction"] += 1
                if extraction_error is not None:
                    return extraction_error
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(FIELDS, ensure_ascii=False)}}]})
            seen["planning"].append(payload)
            if planning_error is not None:
                return planning_error
            content = queue.pop(0) if queue else {"thought": "done", "action": "finish", "finish_reason": "goal_satisfied"}
            body = {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}
            if usage:
                body["usage"] = usage
            return httpx.Response(200, json=body)

        return httpx.MockTransport(respond), seen

    async def test_prompt_offers_only_read_only_tools(self):
        transport, seen = self.transport()
        result = await Planner(self.service, mode="qwen", transport=transport).plan(self.alice, MESSAGE)
        self.assertEqual(result.stop_reason, "goal_satisfied")
        self.assertTrue(result.model_used)
        self.assertEqual(seen["extraction"], 1)
        system = seen["planning"][0]["messages"][0]["content"]
        for name in READ_ONLY_TOOLS:
            self.assertIn(name, system)
        for name in WRITE_TOOLS:
            self.assertNotIn(name, system)
        self.assertEqual(seen["planning"][0]["temperature"], 0)
        self.assertFalse(seen["planning"][0]["stream"])

    async def test_model_proposed_write_is_blocked_by_the_planner(self):
        transport, _ = self.transport([{"thought": "submit it", "action": "tool",
                                        "tool": "submit_expense", "arguments": {"draft_id": "draft-1"}}])
        result = await Planner(self.service, mode="qwen", transport=transport).plan(self.alice, MESSAGE)
        self.assertEqual(result.stop_reason, "blocked_tool")
        self.assertEqual(result.blocked_tools, ["submit_expense"])
        self.assertEqual(result.tool_calls, 0)

    async def test_model_plan_is_executed_and_observations_are_returned(self):
        transport, seen = self.transport([
            {"thought": "read orders", "action": "tool", "tool": "get_my_orders", "arguments": {}},
            {"thought": "enough", "action": "finish", "finish_reason": "goal_satisfied"},
        ])
        result = await Planner(self.service, mode="qwen", transport=transport).plan(self.alice, MESSAGE)
        self.assertEqual(result.stop_reason, "goal_satisfied")
        self.assertEqual([step.observation.tool for step in result.steps], ["get_my_orders"])
        follow_up = json.loads(seen["planning"][1]["messages"][1]["content"])
        self.assertEqual(len(follow_up["observations"]), 1)
        self.assertEqual(follow_up["observations"][0]["tool"], "get_my_orders")
        self.assertNotIn("tenant_id", follow_up["observations"][0]["summary"])

    async def test_usage_is_recorded_from_the_provider(self):
        transport, _ = self.transport(usage={"prompt_tokens": 120, "completion_tokens": 24})
        result = await Planner(self.service, mode="qwen", transport=transport).plan(self.alice, MESSAGE)
        self.assertEqual(result.usage["planning"], {"model_calls": 1, "input_tokens": 120, "output_tokens": 24})

    async def test_planning_provider_error_stops_the_loop_without_leaking_details(self):
        transport, _ = self.transport(planning_error=httpx.Response(401, text="private-provider-detail"))
        result = await Planner(self.service, mode="qwen", transport=transport).plan(self.alice, MESSAGE)
        self.assertEqual(result.stop_reason, "reasoner_error")
        self.assertEqual(result.tool_calls, 0)
        self.assertNotIn("private-provider-detail", json.dumps(result.model_dump(), ensure_ascii=False))

    async def test_extraction_model_failure_becomes_a_bounded_service_error(self):
        transport, _ = self.transport(extraction_error=httpx.Response(503, text="private-upstream-detail"))
        with self.assertRaises(DomainError) as error:
            await Planner(self.service, mode="qwen", transport=transport).plan(self.alice, MESSAGE)
        self.assertEqual(error.exception.code, "model_error")
        self.assertNotIn("private-upstream-detail", str(error.exception))

    async def test_invalid_action_schema_stops_the_loop(self):
        for content in [{"action": "explode"}, {"thought": "x"}, {"action": "tool", "tool": 7}]:
            transport, _ = self.transport([content])
            result = await Planner(self.service, mode="qwen", transport=transport).plan(self.alice, MESSAGE)
            self.assertEqual(result.stop_reason, "reasoner_error")
            self.assertEqual(result.tool_calls, 0)

    async def test_reasoner_requires_a_model_mode(self):
        for mode in ["fixture", "bogus"]:
            with self.assertRaises(ValueError):
                HttpReasoner(mode=mode)


if __name__ == "__main__":
    unittest.main()
