from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from enterprise_flow.policy_qa import PolicyQA
from enterprise_flow.service import DomainError, EnterpriseService


class PolicyQATests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = EnterpriseService(Path(self.temporary.name) / "business.sqlite")
        self.service.seed_demo()
        self.alice = self.service.authenticate_demo("alice")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_fixture_is_direct_source_preview_scoped_before_return(self):
        answer = await PolicyQA(self.service).answer(self.alice, "广州住宿")
        self.assertEqual(answer["status"], "source_preview")
        self.assertFalse(answer["model_used"])
        self.assertTrue(answer["sources"])
        self.assertTrue(all(s["tenant_id"] == "alpha" and s["department_scope"] != "engineering" for s in answer["sources"]))
        self.assertFalse(answer["business_effects"])

    async def test_missing_evidence_does_not_contact_model(self):
        def forbidden(request):
            self.fail("Missing evidence must not call model")
        answer = await PolicyQA(self.service, mode="qwen", transport=httpx.MockTransport(forbidden)).answer(self.alice, "火星住宿", "2025-01-01")
        self.assertEqual(answer["status"], "insufficient_evidence")
        self.assertEqual(answer["citations"], [])

    async def test_real_protocol_receives_only_authorized_sources(self):
        def respond(request):
            payload = json.loads(request.content)
            supplied = json.loads(payload["messages"][1]["content"])
            self.assertTrue(all(s["tenant_id"] == "alpha" for s in supplied["sources"]))
            self.assertTrue(all(s["department_scope"] != "engineering" for s in supplied["sources"]))
            source = next(s for s in supplied["sources"] if s["policy_id"] == "alpha-hotel-current")
            proposal = {"answer": "广州住宿每晚限额400元。", "citations": [{"policy_id": source["policy_id"], "answer_quote": "每晚限额400元", "source_quote": "每晚限额400元"}]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(proposal)}}], "usage": {"prompt_tokens": 80, "completion_tokens": 30}})
        answer = await PolicyQA(self.service, mode="qwen", transport=httpx.MockTransport(respond)).answer(self.alice, "广州住宿")
        self.assertEqual(answer["status"], "pending_review")
        self.assertEqual(answer["usage"]["output_tokens"], 30)
        self.assertFalse(answer["semantic_support_verified"])

    async def test_unknown_or_fabricated_source_quote_rejected(self):
        for identifier, quote in [("beta-hotel-current", "每晚限额400元"), ("alpha-hotel-current", "住宿不限额")]:
            def respond(request):
                proposal = {"answer": "每晚限额400元。", "citations": [{"policy_id": identifier, "answer_quote": "每晚限额400元", "source_quote": quote}]}
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(proposal)}}]})
            with self.assertRaises(DomainError) as error:
                await PolicyQA(self.service, mode="qwen", transport=httpx.MockTransport(respond)).answer(self.alice, "住宿")
            self.assertEqual(error.exception.code, "invalid_model_answer")

    async def test_provider_error_hides_body_and_does_not_submit(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401, text="private-provider-detail"))
        with self.assertRaises(DomainError) as error:
            await PolicyQA(self.service, mode="qwen", transport=transport).answer(self.alice, "住宿")
        self.assertNotIn("private-provider-detail", str(error.exception))
        with self.service.database.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
