from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from enterprise_flow.model import FixtureExtractor, HttpExtractor, ModelError, TripFields
from enterprise_flow.service import DomainError, EnterpriseService
from enterprise_flow.tools import ToolGateway
from enterprise_flow.workflow import WorkflowEngine


FIELDS = {"order_ids": ["alice-hotel-001", "alice-train-001"], "cost_center": "CC-ALPHA-OPS",
          "start_date": "2026-10-09", "end_date": "2026-10-11", "destination": "广州"}


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = EnterpriseService(self.root / "business.sqlite")
        self.service.seed_demo()
        self.alice = self.service.authenticate_demo("alice")
        self.bob = self.service.authenticate_demo("bob")
        self.engine = await WorkflowEngine.open(self.service, self.root / "checkpoints.sqlite")

    async def asyncTearDown(self):
        await self.engine.aclose()
        self.temporary.cleanup()

    def count(self, table):
        with closing(sqlite3.connect(self.service.database_path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    @staticmethod
    def approval(run):
        return {"action": "approve", "expected_version": run["draft"]["version"], "expected_hash": run["draft"]["content_hash"]}

    async def complete_start(self, request_id=None):
        return await self.engine.start(self.alice, json.dumps(FIELDS, ensure_ascii=False), request_id=request_id)

    async def test_real_graph_interrupt_precedes_confirmation_and_submission(self):
        run = await self.complete_start()
        self.assertEqual(run["status"], "awaiting_confirmation")
        self.assertEqual(run["pending"]["kind"], "approval")
        self.assertEqual(run["draft"]["eligible_cents"], 123000)
        self.assertEqual(run["draft"]["excess_cents"], 6000)
        self.assertFalse(run["model_used"])
        self.assertEqual(run["model_usage"]["model_calls"], 0)
        self.assertEqual(self.count("drafts"), 1)
        self.assertEqual(self.count("confirmations"), 0)
        self.assertEqual(self.count("submissions"), 0)
        self.assertIn("search_policy", [event["tool"] for event in run["tool_events"]])
        submitted = await self.engine.resume(self.alice, run["run_id"], self.approval(run))
        self.assertEqual(submitted["status"], "submitted")
        self.assertTrue(submitted["submission"]["submission_id"].startswith("EF-"))
        self.assertEqual(self.count("confirmations"), 1)
        self.assertEqual(self.count("submissions"), 1)

    async def test_file_checkpoint_survives_engine_and_connection_restart(self):
        run = await self.complete_start()
        await self.engine.aclose()
        service = EnterpriseService(self.root / "business.sqlite")
        self.engine = await WorkflowEngine.open(service, self.root / "checkpoints.sqlite")
        restored = await self.engine.get(self.alice, run["run_id"])
        self.assertEqual(restored["pending"]["expected_hash"], run["pending"]["expected_hash"])
        self.assertEqual(restored["draft"]["draft_id"], run["draft"]["draft_id"])
        submitted = await self.engine.resume(self.alice, run["run_id"], self.approval(restored))
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(self.count("drafts"), 1)

    async def test_missing_fields_persist_and_resume_with_typed_inputs(self):
        run = await self.engine.start(self.alice, "帮我准备报销")
        self.assertEqual(run["status"], "waiting_fields")
        self.assertEqual(run["pending"]["kind"], "clarification")
        self.assertEqual(self.count("drafts"), 0)
        resumed = await self.engine.resume(self.alice, run["run_id"], {"action": "provide_fields", "fields": FIELDS})
        self.assertEqual(resumed["status"], "awaiting_confirmation")
        self.assertEqual(resumed["draft"]["eligible_cents"], 123000)

    async def test_unauthorized_resume_and_read_never_access_checkpoint(self):
        run = await self.complete_start()
        with patch.object(self.engine.graph, "aget_state", new_callable=AsyncMock) as read:
            for action in (
                self.engine.get(self.bob, run["run_id"]),
                self.engine.resume(self.bob, run["run_id"], self.approval(run)),
            ):
                with self.assertRaises(DomainError) as error:
                    await action
                self.assertEqual(error.exception.code, "run_not_found")
            read.assert_not_awaited()
        self.assertEqual(self.count("submissions"), 0)

    async def test_model_cannot_inject_identity_or_amounts_via_resume(self):
        run = await self.engine.start(self.alice, "请准备")
        for injected in ({**FIELDS, "tenant_id": "beta"}, {**FIELDS, "amount_cents": 1}, {**FIELDS, "owner_id": "bob"}):
            with self.assertRaises(DomainError) as error:
                await self.engine.resume(self.alice, run["run_id"], {"action": "provide_fields", "fields": injected})
            self.assertEqual(error.exception.code, "invalid_fields")
        self.assertEqual(self.count("drafts"), 0)

    async def test_request_id_and_duplicate_approval_are_idempotent(self):
        first, second = await asyncio.gather(self.complete_start("same-request"), self.complete_start("same-request"))
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.count("drafts"), 1)
        approved = self.approval(first)
        a, b = await asyncio.gather(
            self.engine.resume(self.alice, first["run_id"], approved),
            self.engine.resume(self.alice, first["run_id"], approved),
        )
        self.assertEqual(a["submission"]["submission_id"], b["submission"]["submission_id"])
        self.assertEqual(self.count("submissions"), 1)

    async def test_stale_confirmation_rejected_then_refreshed_to_new_version(self):
        run = await self.complete_start()
        updated = self.service.edit_draft(self.alice, run["draft"]["draft_id"], {"notes": "补充说明"}, 1)
        with self.assertRaises(DomainError) as error:
            await self.engine.resume(self.alice, run["run_id"], self.approval(run))
        self.assertEqual(error.exception.code, "stale_confirmation")
        self.assertEqual(self.count("confirmations"), 0)
        refreshed = await self.engine.resume(self.alice, run["run_id"], {"action": "refresh"})
        self.assertEqual(refreshed["draft"]["version"], 2)
        self.assertEqual(refreshed["pending"]["expected_hash"], updated["content_hash"])
        submitted = await self.engine.resume(self.alice, run["run_id"], self.approval(refreshed))
        self.assertEqual(submitted["submission"]["approved_version"], 2)

    async def test_replay_after_draft_commit_failure_creates_only_one_draft(self):
        original = self.service.create_draft
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise DomainError("worker_interrupted", "Worker stopped after committing draft", 503)
        with patch.object(self.service, "create_draft", side_effect=interrupted):
            run = await self.complete_start()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.count("drafts"), 1)
        recovered = await self.engine.resume(self.alice, run["run_id"], {"action": "retry"})
        self.assertEqual(recovered["status"], "awaiting_confirmation")
        self.assertEqual(self.count("drafts"), 1)

    async def test_replay_after_submission_commit_and_restart_has_one_side_effect(self):
        run = await self.complete_start()
        original = self.service.submit_draft
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise DomainError("worker_interrupted", "Worker stopped after committing submission", 503)
        with patch.object(self.service, "submit_draft", side_effect=interrupted):
            failed = await self.engine.resume(self.alice, run["run_id"], self.approval(run))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.count("submissions"), 1)
        await self.engine.aclose()
        self.engine = await WorkflowEngine.open(self.service, self.root / "checkpoints.sqlite")
        recovered = await self.engine.resume(self.alice, run["run_id"], {"action": "retry"})
        self.assertEqual(recovered["status"], "submitted")
        repeated = await self.engine.resume(self.alice, run["run_id"], self.approval(run))
        self.assertEqual(recovered["submission"]["submission_id"], repeated["submission"]["submission_id"])
        self.assertEqual(self.count("submissions"), 1)
        self.assertEqual(self.count("confirmations"), 1)

    async def test_explicit_cost_center_overrides_confirmed_memory(self):
        self.service.set_preference(self.alice, "cost_center", "CC-ALPHA-GENERAL")
        explicit = await self.complete_start()
        self.assertEqual(explicit["draft"]["input"]["cost_center"], "CC-ALPHA-OPS")
        without = {key: value for key, value in FIELDS.items() if key != "cost_center"}
        remembered = await self.engine.start(self.alice, json.dumps(without, ensure_ascii=False))
        self.assertEqual(remembered["draft"]["input"]["cost_center"], "CC-ALPHA-GENERAL")

    async def test_cancel_never_confirms_or_submits(self):
        run = await self.complete_start()
        cancelled = await self.engine.resume(self.alice, run["run_id"], {"action": "cancel"})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.count("confirmations"), 0)
        self.assertEqual(self.count("submissions"), 0)

    async def test_mcp_uses_authorized_principal_and_rejects_identity_payload(self):
        gateway = ToolGateway(self.service, self.alice)
        orders, _ = await gateway.call("get_my_orders")
        self.assertTrue(all(order["owner_id"] == "alice" for order in orders))
        with self.assertRaises(DomainError) as error:
            await gateway.call("calculate_expense", {"fields": {**FIELDS, "order_ids": ["bob-hotel-001"]}})
        self.assertEqual(error.exception.code, "order_not_found")
        with self.assertRaises(DomainError) as error:
            await gateway.call("calculate_expense", {"fields": {**FIELDS, "user_id": "bob"}})
        self.assertEqual(error.exception.code, "invalid_tool_input")

    async def test_checkpoint_created_in_another_process_can_be_resumed(self):
        code = textwrap.dedent("""
            import asyncio, json, sys
            from enterprise_flow.service import EnterpriseService
            from enterprise_flow.workflow import WorkflowEngine
            async def main():
                service = EnterpriseService(sys.argv[1])
                engine = await WorkflowEngine.open(service, sys.argv[2])
                try:
                    run = await engine.start(service.authenticate_demo('alice'), sys.argv[3])
                    print(json.dumps({'run_id': run['run_id'], 'status': run['status']}))
                finally:
                    await engine.aclose()
            asyncio.run(main())
        """)
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(self.service.database_path),
            str(self.root / "checkpoints.sqlite"), json.dumps(FIELDS, ensure_ascii=False),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        written = json.loads(stdout)
        restored = await self.engine.get(self.alice, written["run_id"])
        self.assertEqual(restored["status"], "awaiting_confirmation")
        submitted = await self.engine.resume(self.alice, written["run_id"], self.approval(restored))
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(self.count("drafts"), 1)
        self.assertEqual(self.count("submissions"), 1)

    async def test_resume_cannot_switch_saved_model_mode(self):
        run = await self.complete_start()
        other = await WorkflowEngine.open(self.service, self.root / "checkpoints.sqlite", mode="qwen")
        try:
            read = await other.get(self.alice, run["run_id"])
            self.assertEqual(read["mode"], "fixture")
            with self.assertRaises(DomainError) as error:
                await other.resume(self.alice, run["run_id"], self.approval(run))
            self.assertEqual(error.exception.code, "mode_mismatch")
        finally:
            await other.aclose()
        self.assertEqual(self.count("submissions"), 0)

    async def test_model_failure_is_persisted_and_retry_uses_saved_step(self):
        class TemporarilyUnavailable:
            calls = 0
            async def extract(self, message):
                self.calls += 1
                if self.calls == 1:
                    raise ModelError("Model service is unavailable")
                return await FixtureExtractor().extract(message)
        extractor = TemporarilyUnavailable()
        self.engine.extractor = extractor
        failed = await self.complete_start()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "model_error")
        self.assertEqual(self.count("drafts"), 0)
        resumed = await self.engine.resume(self.alice, failed["run_id"], {"action": "retry"})
        self.assertEqual(resumed["status"], "awaiting_confirmation")
        self.assertEqual(extractor.calls, 2)
        self.assertIsNone(resumed["error"])

    async def test_partial_clarifications_pause_again_without_losing_fields(self):
        run = await self.engine.start(self.alice, "请准备报销")
        partial = await self.engine.resume(self.alice, run["run_id"], {"action": "provide_fields", "fields": {"order_ids": FIELDS["order_ids"]}})
        self.assertEqual(partial["status"], "waiting_fields")
        completed = await self.engine.resume(self.alice, run["run_id"], {"action": "provide_fields", "fields": {"cost_center": FIELDS["cost_center"]}})
        self.assertEqual(completed["status"], "awaiting_confirmation")
        self.assertEqual(completed["fields"]["order_ids"], FIELDS["order_ids"])


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_label_and_seeded_identifiers(self):
        result = await FixtureExtractor().extract("广州 2026-10-09 到 2026-10-11，alice-hotel-001 和 alice-train-001，CC-ALPHA-OPS")
        self.assertEqual(result.fields.order_ids, FIELDS["order_ids"])
        self.assertEqual(result.fields.cost_center, "CC-ALPHA-OPS")
        self.assertEqual(result.fields.start_date, "2026-10-09")
        self.assertFalse(result.model_used)

    async def test_http_structured_extraction_and_returned_usage(self):
        observed = []
        def respond(request):
            observed.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(FIELDS, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20}})
        result = await HttpExtractor(transport=httpx.MockTransport(respond)).extract("帮我准备报销")
        self.assertTrue(result.model_used)
        self.assertEqual(result.usage["input_tokens"], 40)
        self.assertEqual(result.fields.order_ids, FIELDS["order_ids"])
        self.assertEqual(observed[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", observed[0])

    async def test_provider_identity_and_invalid_usage_are_rejected(self):
        for fields, usage in (({**FIELDS, "owner_id": "bob"}, {}), (FIELDS, {"prompt_tokens": True}), (FIELDS, {"completion_tokens": -1})):
            def respond(request):
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(fields)}}], "usage": usage})
            with self.assertRaises(ModelError):
                await HttpExtractor(transport=httpx.MockTransport(respond)).extract("request")

    async def test_malformed_provider_payloads_never_fall_back_to_fixture(self):
        for body in ({"choices": []}, [], {"choices": [{"message": {"content": None}}]}, {"choices": [{"message": {"content": "not-json"}}]}):
            def respond(request):
                return httpx.Response(200, json=body)
            with self.assertRaises(ModelError):
                await HttpExtractor(transport=httpx.MockTransport(respond)).extract("alice-hotel-001 CC-ALPHA-OPS")

    async def test_http_request_can_be_cancelled(self):
        cancelled = asyncio.Event()
        async def respond(request):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        extractor = HttpExtractor(transport=httpx.MockTransport(respond))
        task = asyncio.create_task(extractor.extract("request"))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_field_schema_rejects_non_iso_date_and_duplicate_orders(self):
        for fields in ({"start_date": "20261009"}, {"order_ids": ["one", "one"]}, {"amount_cents": 3}):
            with self.assertRaises(ValueError):
                TripFields.model_validate(fields)


if __name__ == "__main__":
    unittest.main()
