"""Tests for the FastAPI service surface.

The API must not become a second implementation of the business rules, so these tests check
contract shape and, more importantly, that the authorization the domain layer enforces still holds
when reached over HTTP.
"""

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from fastapi.testclient import TestClient

from enterprise_flow.api import APIService, ServiceSettings, create_api


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.app = create_api(ServiceSettings(database_path=Path(self.folder.name) / "api.sqlite"))
        # The context manager keeps the lifespan open, so the workflow engine keeps its checkpoint
        # connection for every request in the test — the same lifetime a real server has.
        client = TestClient(self.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.client = client

    def principal_headers(self) -> dict[str, str]:
        return {"user_id": "bob"}


class ContractTest(ApiTestCase):
    def test_openapi_document_lists_every_endpoint(self):
        schema = self.app.openapi()
        self.assertEqual(schema["info"]["title"], "EnterpriseFlow API")
        self.assertEqual(sorted(schema["paths"]), [
            "/health", "/orders", "/policies", "/traces/{run_id}", "/workflows",
            "/workflows/{run_id}", "/workflows/{run_id}/resume"])

    def test_health_reports_dependency_readiness(self):
        payload = self.client.get("/health").json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["database"], "ok")
        self.assertEqual(payload["retrieval_mode"], "keyword")

    def test_unknown_principal_is_rejected_with_a_domain_error(self):
        response = self.client.get("/orders", params={"user_id": "nobody"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("error", response.json())


class RetrievalEndpointTest(ApiTestCase):
    def test_policies_returns_scoped_rows(self):
        rows = self.client.get("/policies", params={"user_id": "bob", "query": "住宿"}).json()
        self.assertTrue(rows["items"])
        self.assertTrue(all(item["tenant_id"] == "alpha" for item in rows["items"]))
        self.assertIn(rows["retrieval_path"], {"keyword", "hybrid", "hybrid+rerank"})

    def test_policies_never_cross_the_tenant_boundary(self):
        for user_id in ("bob", "diana"):
            rows = self.client.get("/policies", params={"user_id": user_id, "query": "住宿"}).json()
            expected = "alpha" if user_id == "bob" else "beta"
            self.assertTrue(all(item["tenant_id"] == expected for item in rows["items"]))

    def test_blank_query_stays_inside_the_authorized_scope(self):
        rows = self.client.get("/policies", params={"user_id": "bob", "query": ""}).json()
        self.assertTrue(rows["items"])
        self.assertTrue(all(item["tenant_id"] == "alpha" for item in rows["items"]))

    def test_invalid_trip_date_is_rejected(self):
        response = self.client.get("/policies", params={"user_id": "bob", "query": "住宿", "trip_date": "10/09/2026"})
        self.assertEqual(response.status_code, 400)


class WorkflowEndpointTest(ApiTestCase):
    def test_start_returns_a_run_handle(self):
        response = self.client.post("/workflows", json={
            "user_id": "bob", "message": "把 ORD-1001 和 ORD-1002 合并成一个差旅申请"})
        self.assertEqual(response.status_code, 202)
        handle = response.json()
        self.assertTrue(handle["run_id"].startswith("run-"))
        self.assertIn(handle["status"],
                      {"running", "awaiting_confirmation", "clarification", "waiting_fields"})

    def test_run_is_readable_after_start(self):
        run_id = self.client.post("/workflows", json={"user_id": "bob", "message": "订单 ORD-1001"}).json()["run_id"]
        payload = self.client.get(f"/workflows/{run_id}", params={"user_id": "bob"}).json()
        self.assertEqual(payload["run_id"], run_id)

    def test_another_principal_cannot_read_the_run(self):
        run_id = self.client.post("/workflows", json={"user_id": "bob", "message": "订单 ORD-1001"}).json()["run_id"]
        response = self.client.get(f"/workflows/{run_id}", params={"user_id": "diana"})
        self.assertGreaterEqual(response.status_code, 400)

    def test_oversized_message_is_rejected(self):
        response = self.client.post("/workflows", json={"user_id": "bob", "message": "x" * 5000})
        self.assertEqual(response.status_code, 422)

    def test_health_reports_the_engine_after_a_run(self):
        self.client.post("/workflows", json={"user_id": "bob", "message": "订单 ORD-1001"})
        self.assertEqual(self.client.get("/health").json()["workflow_engine"], "ready")


class ResumeEndpointTest(ApiTestCase):
    def clarification_fields(self) -> dict:
        """Read the pending clarification so the submitted fields match what the stage expects."""
        for row in self.client.get("/orders", params={"user_id": "bob"}).json()["items"][:1]:
            return {"order_ids": [row["order_id"]], "cost_center": "CC-ALPHA-ENG"}
        return {"order_ids": [], "cost_center": "CC-ALPHA-ENG"}

    def start_clarification(self) -> str:
        return self.client.post("/workflows", json={
            "user_id": "bob", "message": "帮我申请一下费用"}).json()["run_id"]

    def test_resume_answers_a_clarification_with_fields(self):
        run_id = self.start_clarification()
        response = self.client.post(f"/workflows/{run_id}/resume", json={
            "user_id": "bob", "action": "provide_fields", "fields": self.clarification_fields()})
        self.assertEqual(response.status_code, 200)
        # Supplying the fields moves the run past clarification to the approval pause.
        self.assertEqual(response.json()["status"], "awaiting_confirmation")

    def test_resume_reaches_the_approval_pause_and_then_approves(self):
        run_id = self.start_clarification()
        self.client.post(f"/workflows/{run_id}/resume", json={
            "user_id": "bob", "action": "provide_fields", "fields": self.clarification_fields()})
        pending = self.client.get(f"/workflows/{run_id}", params={"user_id": "bob"}).json()["pending"]
        self.assertEqual(pending["kind"], "approval")
        response = self.client.post(f"/workflows/{run_id}/resume", json={
            "user_id": "bob", "action": "approve",
            "expected_version": pending["expected_version"], "expected_hash": pending["expected_hash"]})
        self.assertEqual(response.status_code, 200)

    def test_approval_without_version_and_hash_is_rejected(self):
        run_id = self.start_clarification()
        self.client.post(f"/workflows/{run_id}/resume", json={
            "user_id": "bob", "action": "provide_fields", "fields": self.clarification_fields()})
        response = self.client.post(f"/workflows/{run_id}/resume", json={"user_id": "bob", "action": "approve"})
        # The decision schema rejects the missing version and hash before the engine evaluates it,
        # and the run stays at the approval pause either way.
        self.assertIn(response.status_code, {400, 409})
        self.assertEqual(self.client.get(f"/workflows/{run_id}",
                                         params={"user_id": "bob"}).json()["status"], "awaiting_confirmation")

    def test_approval_with_a_stale_hash_is_rejected(self):
        run_id = self.start_clarification()
        self.client.post(f"/workflows/{run_id}/resume", json={
            "user_id": "bob", "action": "provide_fields", "fields": self.clarification_fields()})
        stale = "0" * 64
        response = self.client.post(f"/workflows/{run_id}/resume", json={
            "user_id": "bob", "action": "approve", "expected_version": 1, "expected_hash": stale})
        self.assertEqual(response.status_code, 409)

    def test_unknown_action_is_rejected(self):
        run_id = self.start_clarification()
        response = self.client.post(f"/workflows/{run_id}/resume", json={
            "user_id": "bob", "action": "teleport", "fields": self.clarification_fields()})
        self.assertGreaterEqual(response.status_code, 400)

    def test_resume_rejects_another_principal(self):
        run_id = self.start_clarification()
        response = self.client.post(f"/workflows/{run_id}/resume", json={
            "user_id": "diana", "action": "provide_fields", "fields": self.clarification_fields()})
        self.assertGreaterEqual(response.status_code, 400)

    def test_short_content_hash_is_rejected_by_the_contract(self):
        run_id = self.start_clarification()
        response = self.client.post(f"/workflows/{run_id}/resume", json={
            "user_id": "bob", "action": "approve", "expected_hash": "abc"})
        self.assertEqual(response.status_code, 422)


class ServiceFactoryTest(unittest.TestCase):
    def test_seed_can_be_disabled(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = APIService(ServiceSettings(database_path=Path(folder.name) / "x.sqlite", seed=False))
        with self.assertRaises(Exception):
            state.service.authenticate_demo("bob")

    def test_extractor_follows_the_configured_mode(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        fixture = APIService(ServiceSettings(database_path=Path(folder.name) / "a.sqlite"))
        self.assertEqual(type(fixture.extractor()).__name__, "FixtureExtractor")
        # The API mode extractor refuses an empty base URL at construction, which is the intended
        # guard against silently degrading to a non-model path.
        with self.assertRaises(ValueError):
            APIService(ServiceSettings(database_path=Path(folder.name) / "b.sqlite", model_mode="api")).extractor()
        with unittest.mock.patch.dict("os.environ", {"ENTERPRISE_API_BASE": "http://127.0.0.1:8001/v1",
                                                    "ENTERPRISE_API_MODEL": "qwen-plus"}):
            configured = APIService(ServiceSettings(database_path=Path(folder.name) / "c.sqlite", model_mode="api"))
            self.assertEqual(type(configured.extractor()).__name__, "HttpExtractor")


if __name__ == "__main__":
    unittest.main()
