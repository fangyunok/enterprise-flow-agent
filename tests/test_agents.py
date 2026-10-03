"""Supervisor tests: least-privilege scopes, handoff, cross-checks and no writes."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from enterprise_flow.agents import (ALL_AGENT_TOOLS, ROLE_SCOPES, AgentTask, Blackboard, Conflict, CostEstimator,
                                    Finding, OrderReconciler, PolicyResearcher, Proposal, ScopedToolbox, Supervisor)
from enterprise_flow.planner import READ_ONLY_TOOLS, WRITE_TOOLS
from enterprise_flow.service import DomainError, EnterpriseService
from enterprise_flow.tools import ToolGateway

FIELDS = {"order_ids": ["alice-hotel-001", "alice-train-001"], "cost_center": "CC-ALPHA-OPS",
          "start_date": "2026-10-09", "end_date": "2026-10-11", "destination": "广州"}
MESSAGE = json.dumps(FIELDS, ensure_ascii=False)


class RogueSpecialist:
    """Pretends to be the estimator but tries a writing tool."""

    role = "cost_estimator"

    def __init__(self, tool: str = "submit_expense") -> None:
        self.tool = tool

    async def run(self, toolbox, task, board):
        await toolbox.call(self.tool, {})
        return Finding(agent=self.role, conclusion="unreachable"), {}


class StallingSpecialist:
    role = "cost_estimator"

    async def run(self, toolbox, task, board):
        await asyncio.sleep(5)
        return Finding(agent=self.role, conclusion="unreachable"), {}


class BrokenSpecialist:
    role = "cost_estimator"

    async def run(self, toolbox, task, board):
        raise RuntimeError("specialist exploded")


class SyntheticReconciler:
    role = "order_reconciler"

    def __init__(self, usable_count: int) -> None:
        self.usable_count = usable_count

    async def run(self, toolbox, task, board):
        views = [{"order_id": f"order-{index}", "usable_preview": True, "kind": "hotel", "city": "广州"}
                 for index in range(self.usable_count)]
        return Finding(agent=self.role, conclusion="synthetic scope"), {"views": views, "usable": views,
                                                                       "unavailable": [], "owned_count": self.usable_count}


class SyntheticEstimator:
    role = "cost_estimator"

    def __init__(self, item_count: int) -> None:
        self.item_count = item_count

    async def run(self, toolbox, task, board):
        items = [{"order_id": f"order-{index}", "eligible_cents": 10, "excess_cents": 0} for index in range(self.item_count)]
        calculation = {"items": items, "total_cents": 10 * self.item_count,
                       "eligible_cents": 10 * self.item_count, "excess_cents": 0}
        return Finding(agent=self.role, conclusion="synthetic estimate"), {"calculation": calculation, "error_code": None}


class AgentTests(unittest.IsolatedAsyncioTestCase):
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

    @staticmethod
    def codes(proposal: Proposal):
        return [conflict.code for conflict in proposal.conflicts]

    async def test_three_roles_produce_a_verified_calculation(self):
        proposal = await Supervisor(self.service).collaborate(self.alice, MESSAGE)
        self.assertEqual(proposal.roles, ["order_reconciler", "policy_researcher", "cost_estimator"])
        self.assertEqual(len(proposal.findings), 3)
        self.assertTrue(all(finding.ok for finding in proposal.findings))
        self.assertEqual(proposal.calculation["eligible_cents"], 123000)
        self.assertEqual(proposal.calculation["excess_cents"], 6000)
        self.assertEqual(proposal.conflicts, [])
        self.assertFalse(proposal.blocked)
        self.assertFalse(proposal.model_used)

    async def test_collaboration_creates_no_business_records(self):
        await Supervisor(self.service).collaborate(self.alice, MESSAGE)
        self.assertEqual(self.business_writes(), {"drafts": 0, "confirmations": 0, "submissions": 0, "events": 0})

    async def test_same_input_yields_the_same_content_digest(self):
        supervisor = Supervisor(self.service)
        first = await supervisor.collaborate(self.alice, MESSAGE)
        second = await supervisor.collaborate(self.alice, MESSAGE)
        self.assertEqual(first.content_digest, second.content_digest)
        self.assertEqual(len(first.content_digest), 64)
        changes = first.model_dump(exclude={"duration_ms", "content_digest"})
        changes["blocked"] = not changes["blocked"]
        self.assertNotEqual(self._digest_of(changes), first.content_digest)

    @staticmethod
    def _digest_of(payload):
        import hashlib
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                         default=str).encode("utf-8")).hexdigest()

    async def test_declares_human_confirmation_and_no_business_effect(self):
        proposal = await Supervisor(self.service).collaborate(self.alice, MESSAGE)
        self.assertTrue(proposal.requires_human_confirmation)
        self.assertTrue(proposal.read_only)
        self.assertFalse(proposal.business_effects)
        self.assertIn("人工确认", proposal.next_action)

    async def test_role_scopes_stay_read_only_and_never_include_writes(self):
        self.assertTrue(ALL_AGENT_TOOLS <= READ_ONLY_TOOLS)
        self.assertEqual(ALL_AGENT_TOOLS & WRITE_TOOLS, frozenset())
        for role, scope in ROLE_SCOPES.items():
            self.assertTrue(scope, role)
            self.assertFalse(scope & WRITE_TOOLS, role)

    async def test_scoped_toolbox_rejects_an_out_of_scope_tool(self):
        gateway = ToolGateway(self.service, self.alice)
        toolbox = ScopedToolbox(gateway, "policy_researcher", ROLE_SCOPES["policy_researcher"])
        with self.assertRaises(DomainError) as error:
            await toolbox.call("calculate_expense", {"fields": FIELDS})
        self.assertEqual(error.exception.code, "tool_scope_violation")
        self.assertEqual(toolbox.calls, [])

    async def test_a_specialist_cannot_reach_a_write_tool(self):
        for tool in sorted(WRITE_TOOLS):
            supervisor = Supervisor(self.service, agents={"order_reconciler": OrderReconciler(),
                                                          "policy_researcher": PolicyResearcher(),
                                                          "cost_estimator": RogueSpecialist(tool)})
            proposal = await supervisor.collaborate(self.alice, MESSAGE)
            failed = [finding for finding in proposal.findings if not finding.ok]
            self.assertEqual([finding.error_code for finding in failed], ["tool_scope_violation"])
            self.assertIn("tool_scope_violation", self.codes(proposal))
            self.assertTrue(proposal.blocked)
        self.assertEqual(self.business_writes(), {"drafts": 0, "confirmations": 0, "submissions": 0, "events": 0})

    async def test_ineligible_orders_block_the_proposal_once_per_root_cause(self):
        message = json.dumps({**FIELDS, "order_ids": ["alice-cancelled", "alice-no-receipt"],
                              "start_date": "2026-10-09", "end_date": "2026-10-09"}, ensure_ascii=False)
        proposal = await Supervisor(self.service).collaborate(self.alice, message)
        codes = self.codes(proposal)
        self.assertTrue(proposal.blocked)
        self.assertEqual(codes.count("order_preview_rejected"), 1)
        self.assertEqual(codes.count("order_ineligible"), 1)
        self.assertFalse(any(finding.ok for finding in proposal.findings if finding.agent == "cost_estimator"))

    async def test_another_employees_order_is_not_visible(self):
        message = json.dumps({**FIELDS, "order_ids": ["alice-hotel-001"], "cost_center": "CC-ALPHA-ENG"}, ensure_ascii=False)
        proposal = await Supervisor(self.service).collaborate(self.bob, message)
        self.assertEqual(proposal.orders, [])
        self.assertTrue(proposal.blocked)
        self.assertIn("order_preview_rejected", self.codes(proposal))

    async def test_missing_order_ids_route_only_the_reconciler(self):
        proposal = await Supervisor(self.service).collaborate(self.alice, json.dumps({"cost_center": "CC-ALPHA-OPS"}, ensure_ascii=False))
        self.assertEqual(proposal.roles, ["order_reconciler"])
        self.assertTrue(proposal.blocked)
        self.assertIn("missing_order_ids", self.codes(proposal))
        self.assertIsNone(proposal.calculation)

    async def test_policy_researcher_needs_the_reconciler_handoff(self):
        toolbox = ScopedToolbox(ToolGateway(self.service, self.alice), "policy_researcher", ROLE_SCOPES["policy_researcher"])
        finding, payload = await PolicyResearcher().run(toolbox, AgentTask(MESSAGE, FIELDS), Blackboard())
        self.assertEqual(finding.tool_calls, 0)
        self.assertEqual(toolbox.calls, [])
        self.assertEqual(payload["rows"], [])
        self.assertIn("订单范围", finding.conclusion)

    async def test_researcher_only_queries_the_reconciled_scope(self):
        proposal = await Supervisor(self.service).collaborate(self.alice, MESSAGE)
        kinds = {row["kind"] for row in proposal.candidate_policies}
        self.assertEqual(kinds, {"hotel", "train"})
        # A '*' city clause is tenant-wide; a clause for another city or tenant is not.
        self.assertTrue(all(row["city"] in {"广州", "*"} for row in proposal.candidate_policies))
        identifiers = {row["policy_id"] for row in proposal.candidate_policies}
        self.assertEqual(identifiers & {"beta-hotel-current", "beta-train", "alpha-hotel-shenzhen",
                                        "alpha-hotel-engineering"}, set())

    async def test_department_scope_changes_the_applicable_policy(self):
        message = json.dumps({**FIELDS, "order_ids": ["bob-hotel-001"], "start_date": "2026-10-09",
                              "end_date": "2026-10-11"}, ensure_ascii=False)
        bob = await Supervisor(self.service).collaborate(self.bob, json.dumps({**json.loads(message), "cost_center": "CC-ALPHA-ENG"}, ensure_ascii=False))
        alice = await Supervisor(self.service).collaborate(self.alice, MESSAGE)
        bob_policies = {row["policy_id"] for row in bob.candidate_policies}
        alice_policies = {row["policy_id"] for row in alice.candidate_policies}
        self.assertIn("alpha-hotel-engineering", bob_policies)
        self.assertNotIn("alpha-hotel-engineering", alice_policies)

    async def test_clause_version_change_between_days_is_not_a_conflict(self):
        message = json.dumps({**FIELDS, "order_ids": ["alice-hotel-old"], "start_date": "2026-09-29",
                              "end_date": "2026-10-02"}, ensure_ascii=False)
        proposal = await Supervisor(self.service).collaborate(self.alice, message)
        versions = {(row["policy_id"], row["version"]) for row in proposal.candidate_policies}
        self.assertEqual(versions, {("alpha-hotel-old", "2026-01"), ("alpha-hotel-current", "2026-10")})
        self.assertNotIn("policy_version_overlap", self.codes(proposal))
        # 1290.00 over three nights: two capped at the 2026-01 clause (350.00), one at the current clause (400.00).
        self.assertEqual(proposal.calculation["total_cents"], 129000)
        self.assertEqual(proposal.calculation["eligible_cents"], 110000)
        self.assertEqual(proposal.calculation["excess_cents"], 19000)

    async def test_same_day_multiple_versions_are_flagged_as_blocking(self):
        conflicts = Supervisor._conflicts([], {}, {"overlaps": [{"clause_id": "HOTEL-1", "trip_date": "2026-10-01",
                                                                 "versions": ["2026-01", "2026-10"]}]}, {}, {"order_ids": ["x"]})
        self.assertEqual([conflict.code for conflict in conflicts], ["policy_version_overlap"])
        self.assertEqual(conflicts[0].severity, "blocking")
        self.assertIn("HOTEL-1", conflicts[0].detail)

    async def test_policy_gap_is_flagged_as_blocking(self):
        conflicts = Supervisor._conflicts([], {}, {"gaps": [{"city": "火星", "kind": "hotel",
                                                             "trip_date": "2026-10-09"}]}, {}, {"order_ids": ["x"]})
        self.assertEqual([conflict.code for conflict in conflicts], ["policy_gap"])
        self.assertEqual(conflicts[0].severity, "blocking")

    async def test_cross_agent_disagreement_warns_without_blocking(self):
        supervisor = Supervisor(self.service, agents={"order_reconciler": SyntheticReconciler(2),
                                                      "policy_researcher": PolicyResearcher(),
                                                      "cost_estimator": SyntheticEstimator(1)})
        proposal = await supervisor.collaborate(self.alice, MESSAGE)
        self.assertIn("agent_disagreement", self.codes(proposal))
        disagreement = next(conflict for conflict in proposal.conflicts if conflict.code == "agent_disagreement")
        self.assertEqual(disagreement.severity, "warning")
        self.assertEqual(sorted(disagreement.agents), ["cost_estimator", "order_reconciler"])
        self.assertFalse(proposal.blocked)

    async def test_stalling_specialist_is_contained(self):
        supervisor = Supervisor(self.service, agents={"order_reconciler": OrderReconciler(),
                                                      "policy_researcher": PolicyResearcher(),
                                                      "cost_estimator": StallingSpecialist()}, agent_timeout=0.1)
        proposal = await supervisor.collaborate(self.alice, MESSAGE)
        finding = next(item for item in proposal.findings if item.agent == "cost_estimator")
        self.assertFalse(finding.ok)
        self.assertEqual(finding.error_code, "agent_unavailable")
        self.assertEqual(len(proposal.findings), 3)

    async def test_broken_specialist_is_contained(self):
        supervisor = Supervisor(self.service, agents={"order_reconciler": OrderReconciler(),
                                                      "policy_researcher": PolicyResearcher(),
                                                      "cost_estimator": BrokenSpecialist()})
        proposal = await supervisor.collaborate(self.alice, MESSAGE)
        finding = next(item for item in proposal.findings if item.agent == "cost_estimator")
        self.assertEqual(finding.error_code, "agent_failed")
        self.assertEqual(finding.tool_calls, 0)

    async def test_unregistered_role_is_rejected(self):
        with self.assertRaises(ValueError):
            Supervisor(self.service, agents={"finance_wizard": BrokenSpecialist()})

    async def test_mode_and_message_bounds_are_enforced(self):
        with self.assertRaises(ValueError):
            Supervisor(self.service, mode="bogus")
        supervisor = Supervisor(self.service)
        for message in ["", "   ", "x" * 4001]:
            with self.assertRaises(DomainError) as error:
                await supervisor.collaborate(self.alice, message)
            self.assertEqual(error.exception.code, "invalid_message")

    async def test_conflicts_are_deduplicated(self):
        duplicate = Conflict(code="policy_gap", detail="same", agents=["x"])
        self.assertEqual(len({(item.code, item.detail) for item in [duplicate, duplicate]}), 1)


if __name__ == "__main__":
    unittest.main()
