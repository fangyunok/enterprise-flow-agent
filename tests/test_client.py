"""SDK client tests over a real socket.

The client is synchronous, so it cannot ride an ASGI transport. These tests boot the console the
same way a deployment does — a real port, a real cookie session — and then assert that the client
cannot reach anything the API itself would refuse.
"""
from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn

from enterprise_flow.client import ClientError, EnterpriseFlowClient
from enterprise_flow.webapp import create_app

# The client is built on the standard library on purpose: integration code should not need a
# third-party HTTP stack. These assertions double as that guarantee.
import enterprise_flow.client as client_module

for _name in ("httpx", "requests"):
    assert _name not in vars(client_module), f"client must stay dependency-free, found {_name}"

MESSAGE = ("我于 2026-10-09 至 2026-10-11 去广州出差，请处理 alice-hotel-001 和 alice-train-001，"
           "成本中心 CC-ALPHA-OPS。")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        app = create_app(Path(cls.temp.name) / "enterprise.sqlite", model_mode="fixture")
        config = uvicorn.Config(app, host="127.0.0.1", port=_free_port(), log_level="warning")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(120):
            if cls.server.started:
                break
            time.sleep(0.25)
        if not cls.server.started:
            raise RuntimeError("console did not start for the client tests")
        cls.base_url = f"http://127.0.0.1:{config.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.temp.cleanup()

    def setUp(self):
        self.client = EnterpriseFlowClient(self.base_url)

    def tearDown(self):
        self.client.close()

    def test_unauthenticated_read_is_rejected_with_typed_error(self):
        with self.assertRaises(ClientError) as caught:
            self.client.orders()
        self.assertEqual(caught.exception.code, "authentication_required")
        self.assertEqual(caught.exception.status_code, 401)

    def test_login_then_scoped_reads_and_draft_lifecycle(self):
        self.assertEqual(self.client.login("alice")["user_id"], "alice")

        orders = self.client.orders()
        self.assertTrue(orders)
        self.assertTrue(all(order["owner_id"] == "alice" for order in orders))

        draft = self.client.create_draft(["alice-hotel-001", "alice-train-001"], cost_center="CC-ALPHA-OPS")
        self.assertEqual((draft["total_cents"], draft["eligible_cents"], draft["excess_cents"]), (129000, 123000, 6000))

        confirmed = self.client.confirm_draft(draft["draft_id"], draft["version"], draft["content_hash"])
        self.assertEqual(confirmed["status"], "confirmed")

        submission = self.client.submit_draft(draft["draft_id"], confirmed["version"], "sdk-key-1")
        self.assertTrue(submission["submission_id"].startswith("EF-"))
        repeated = self.client.submit_draft(draft["draft_id"], confirmed["version"], "sdk-key-1")
        self.assertEqual(repeated["submission_id"], submission["submission_id"])

    def test_tenant_isolation_holds_through_the_client(self):
        self.client.login("alice")
        policies = self.client.policies("住宿")
        self.assertTrue(policies)
        self.assertEqual({row["tenant_id"] for row in policies}, {"alpha"})

    def test_agent_layer_stays_read_only(self):
        self.client.login("alice")
        plan = self.client.plan(MESSAGE)
        self.assertTrue(plan["read_only"])
        self.assertFalse(plan["business_effects"])
        self.assertEqual(plan["stop_reason"], "goal_satisfied")

        proposal = self.client.collaborate(MESSAGE)
        self.assertEqual(len(proposal["roles"]), 3)
        self.assertTrue(proposal["requires_human_confirmation"])
        self.assertEqual(self.client.drafts(), [])

    def test_start_approve_and_idempotent_submit(self):
        self.client.login("alice")
        run = self.client.start_workflow(MESSAGE, request_id="client-test")
        self.assertEqual(run.status, "awaiting_confirmation")
        self.assertIsNotNone(run.draft)

        approved = self.client.approve(run.run_id, run.draft["version"], run.draft["content_hash"])
        self.assertEqual(approved.status, "submitted")
        submission = approved.submission["submission_id"]

        again = self.client.approve(run.run_id, run.draft["version"], run.draft["content_hash"])
        self.assertEqual(again.submission["submission_id"], submission)
        self.assertGreaterEqual(len(approved.tool_events), 5)

        # Counters are only incremented by real execution, so they appear after a run.
        self.assertIn("enterpriseflow_tool_calls_total", self.client.metrics())
        self.assertGreaterEqual(self.client.trace()["summary"]["span_count"], 5)

    def test_operations_endpoints(self):
        self.assertEqual(self.client.health()["service"], "EnterpriseFlow")
        self.assertGreaterEqual(len(self.client.overview()["capability"]), 10)
        self.assertIsInstance(self.client.metrics(), str)
        self.assertIn("/api/metrics", [item["path"] for item in self.client.contract()["endpoints"]])

        self.client.login("alice")
        self.client.create_draft(["alice-hotel-001"], cost_center="CC-ALPHA-OPS")
        self.assertIn("draft_created", {event["action"] for event in self.client.audit()})

    def test_preference_round_trip(self):
        self.client.login("alice")
        self.client.set_preference("cost_center", "CC-ALPHA-OPS")
        self.assertIn("CC-ALPHA-OPS", [row["value"] for row in self.client.preferences()])
        self.client.delete_preference("cost_center")
        self.assertEqual(self.client.preferences(), [])


if __name__ == "__main__":
    unittest.main()
