"""Real ASGI boundary tests against a temporary seeded business database."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from enterprise_flow.webapp import create_app


async def _http(app, target, method="GET", data=None, *, cookie=None, headers=None, raw=None):
    parsed = urlsplit(target)
    request_headers = {k.lower(): v for k, v in (headers or {}).items()}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("content-type", "application/json")
    else:
        body = raw or b""
    request_headers.setdefault("host", "localhost:7861")
    if cookie:
        request_headers["cookie"] = cookie
    if method != "GET":
        request_headers.setdefault("content-length", str(len(body)))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "scheme": "http", "method": method,
        "path": parsed.path, "raw_path": parsed.path.encode(), "root_path": "",
        "query_string": parsed.query.encode(),
        "headers": [(k.encode("ascii"), v.encode("ascii")) for k, v in request_headers.items()],
        "server": ("localhost", 7861), "client": ("127.0.0.1", 54321),
    }
    sent, consumed = [], False
    async def receive():
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}
    async def send(message):
        sent.append(message)
    await app(scope, receive, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    payload = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    content_type = dict(start.get("headers", [])).get(b"content-type", b"")
    return start["status"], dict(start.get("headers", [])), json.loads(payload) if b"application/json" in content_type else payload.decode("utf-8")


class WebBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "enterprise.sqlite"
        self.app = create_app(self.db, model_mode="fixture")
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.cookie = None

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)
        self.temp.cleanup()

    async def login(self, user="alice"):
        status, headers, identity = await _http(self.app, "/api/login", "POST", {"user_id": user})
        self.assertEqual(status, 200, identity)
        self.cookie = headers[b"set-cookie"].decode().split(";", 1)[0]
        return headers, identity

    async def request(self, path, method="GET", data=None, **kwargs):
        return await _http(self.app, path, method, data, cookie=self.cookie, **kwargs)

    async def draft(self):
        await self.login()
        status, _, draft = await self.request("/api/drafts", "POST", {
            "order_ids": ["alice-hotel-001", "alice-train-001"], "cost_center": "CC-ALPHA-OPS",
        })
        self.assertEqual(status, 201, draft)
        self.assertEqual((draft["total_cents"], draft["eligible_cents"], draft["excess_cents"]), (129000, 123000, 6000))
        return draft

    async def test_public_home_labels_fixture_and_health_reports_mode(self):
        status, _, page = await self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("EnterpriseFlow", page)
        self.assertIn("固定业务工作流（不调用大模型）", page)
        self.assertIn("身份切换用于验证权限隔离", page)
        status, _, health = await self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["mode"], "fixture")
        self.assertFalse(health["model_validated"])

    async def test_authentication_required_and_cookie_tampering_rejected(self):
        self.assertEqual((await self.request("/api/orders"))[0], 401)
        headers, identity = await self.login()
        self.assertEqual(identity["user_id"], "alice")
        cookie_header = headers[b"set-cookie"].decode().lower()
        self.assertIn("httponly", cookie_header)
        self.assertIn("samesite=strict", cookie_header)
        name, value = self.cookie.split("=", 1)
        self.cookie = name + "=" + ("A" if value[0] != "A" else "B") + value[1:]
        self.assertEqual((await self.request("/api/orders"))[0], 401)

    async def test_login_identity_payload_cannot_override_tenant(self):
        status, _, response = await self.request("/api/login", "POST", {"user_id": "alice", "tenant_id": "beta"})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"], "unexpected_fields")

    async def test_cross_origin_mutations_rejected_before_route_and_same_origin_accepted(self):
        status, _, _ = await self.request("/api/login", "POST", {"user_id": "alice"}, headers={"origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        status, _, _ = await self.request("/api/login", "POST", {"user_id": "alice"}, headers={"origin": "http://localhost:7861"})
        self.assertEqual(status, 200)
        await self.login()
        for method, path, payload in [("PATCH", "/api/preferences", {"key": "cost_center", "value": "CC-ALPHA-OPS", "confirmed": True}), ("DELETE", "/api/preferences", {"key": "cost_center"})]:
            self.assertEqual((await self.request(path, method, payload, headers={"sec-fetch-site": "cross-site"}))[0], 403)

    async def test_bounded_json_and_content_type_validation(self):
        self.assertEqual((await self.request("/api/login", "POST", raw=b"x" * 33000, headers={"content-type": "application/json"}))[0], 413)
        self.assertEqual((await self.request("/api/login", "POST", raw=b"{broken", headers={"content-type": "application/json"}))[0], 400)
        self.assertEqual((await self.request("/api/login", "POST", raw=b"user_id=alice", headers={"content-type": "application/x-www-form-urlencoded"}))[0], 415)
        self.assertEqual((await self.request("/api/login", "POST", data=["alice"]))[0], 400)

    async def test_orders_policies_and_drafts_are_filtered_by_session_identity(self):
        draft = await self.draft()
        status, _, orders = await self.request("/api/orders")
        self.assertEqual(status, 200)
        self.assertTrue(all(order["owner_id"] == "alice" for order in orders))
        status, _, policies = await self.request("/api/policies?trip_date=2026-10-09")
        self.assertEqual(status, 200)
        self.assertTrue(all(policy["tenant_id"] == "alpha" and policy["department_scope"] in {"operations", "*"} for policy in policies))
        await self.login("bob")
        self.assertEqual((await self.request("/api/drafts/" + draft["draft_id"]))[0], 404)
        self.assertEqual((await self.request("/api/drafts"))[2], [])
        status, _, error = await self.request("/api/drafts", "POST", {"order_ids": ["alice-hotel-001"], "cost_center": "CC-ALPHA-ENG"})
        self.assertEqual(status, 404)
        self.assertEqual(error["error"], "order_not_found")

    async def test_confirm_requires_displayed_version_hash_and_edit_invalidates_it(self):
        draft = await self.draft()
        path = "/api/drafts/" + draft["draft_id"]
        self.assertEqual((await self.request(path + "/confirm", "POST", {"expected_version": 1}))[0], 400)
        self.assertEqual((await self.request(path + "/confirm", "POST", {"expected_version": True, "expected_hash": draft["content_hash"]}))[0], 400)
        self.assertEqual((await self.request(path + "/confirm", "POST", {"expected_version": 1, "expected_hash": "0" * 64}))[0], 409)
        status, _, confirmed = await self.request(path + "/confirm", "POST", {"expected_version": 1, "expected_hash": draft["content_hash"]})
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")
        status, _, edited = await self.request(path, "PATCH", {"expected_version": 1, **draft["input"], "notes": "修改后需要重新确认"})
        self.assertEqual(status, 200)
        self.assertEqual(edited["version"], 2)
        self.assertIsNone(edited["confirmation"])
        self.assertEqual((await self.request(path + "/submit", "POST", {"expected_version": 2, "idempotency_key": "web-edit-submit"}))[0], 409)

    async def test_duplicate_submit_returns_same_business_result(self):
        draft = await self.draft()
        path = "/api/drafts/" + draft["draft_id"]
        self.assertEqual((await self.request(path + "/submit", "POST", {"expected_version": 1, "idempotency_key": "web-submit"}))[0], 409)
        await self.request(path + "/confirm", "POST", {"expected_version": 1, "expected_hash": draft["content_hash"]})
        first = await self.request(path + "/submit", "POST", {"expected_version": 1, "idempotency_key": "web-submit"})
        second = await self.request(path + "/submit", "POST", {"expected_version": 1, "idempotency_key": "different-web-submit"})
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(first[2]["submission_id"], second[2]["submission_id"])
        self.assertEqual((await self.request(path))[2]["status"], "submitted")

    async def test_preferences_require_explicit_confirmation_and_do_not_leak_between_users(self):
        await self.login()
        data = {"key": "cost_center", "value": "CC-ALPHA-OPS", "confirmed": False}
        self.assertEqual((await self.request("/api/preferences", "PATCH", data))[0], 400)
        data["confirmed"] = True
        self.assertEqual((await self.request("/api/preferences", "PATCH", data))[0], 200)
        self.assertEqual((await self.request("/api/preferences"))[2][0]["value"], "CC-ALPHA-OPS")
        await self.login("bob")
        self.assertEqual((await self.request("/api/preferences"))[2], [])
        await self.login()
        self.assertEqual((await self.request("/api/preferences", "DELETE", {"key": "cost_center"}))[0], 200)
        self.assertEqual((await self.request("/api/preferences"))[2], [])

    async def test_draft_identity_injection_is_rejected_at_http_boundary(self):
        await self.login()
        status, _, response = await self.request("/api/drafts", "POST", {"order_ids": ["bob-hotel-001"], "cost_center": "CC-ALPHA-ENG", "owner_id": "bob"})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"], "unexpected_fields")

    async def test_invalid_order_date_returns_validation_response(self):
        await self.login()
        self.assertEqual((await self.request("/api/orders?start_date=invalid"))[0], 400)

    async def test_logout_removes_cookie(self):
        await self.login()
        status, headers, result = await self.request("/api/logout", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["logged_out"])
        self.assertIn("max-age=0", headers[b"set-cookie"].decode().lower())

    async def test_workflow_confirmation_survives_application_restart(self):
        await self.login()
        status, _, run = await self.request("/api/workflows", "POST", {
            "message": "请用订单 alice-hotel-001 和 alice-train-001，成本中心 CC-ALPHA-OPS 准备申请。",
            "request_id": "web-restart-case",
        })
        self.assertEqual(status, 201, run)
        self.assertEqual(run["status"], "awaiting_confirmation")
        self.assertFalse(run["model_used"])
        self.assertEqual(run["draft"]["eligible_cents"], 123000)
        await self.lifespan.__aexit__(None, None, None)
        self.app = create_app(self.db)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.assertEqual((await self.request("/api/me"))[0], 401)
        await self.login()
        status, _, restored = await self.request("/api/workflows/" + run["run_id"])
        self.assertEqual(status, 200)
        self.assertEqual(restored["pending"]["expected_hash"], run["pending"]["expected_hash"])
        status, _, submitted = await self.request("/api/workflows/" + run["run_id"] + "/resume", "POST", {"decision": {
            "action": "approve", "expected_version": restored["draft"]["version"], "expected_hash": restored["draft"]["content_hash"],
        }})
        self.assertEqual(status, 200, submitted)
        self.assertEqual(submitted["status"], "submitted")
        self.assertIsNotNone(submitted["submission"]["submission_id"])

    async def test_workflow_owner_check_blocks_read_and_resume_from_other_employee(self):
        await self.login()
        status, _, run = await self.request("/api/workflows", "POST", {"message": "帮我准备一份申请。"})
        self.assertEqual(status, 201)
        self.assertEqual(run["pending"]["kind"], "clarification")
        await self.login("bob")
        path = "/api/workflows/" + run["run_id"]
        self.assertEqual((await self.request(path))[0], 404)
        self.assertEqual((await self.request(path + "/resume", "POST", {"decision": {"action": "cancel"}}))[0], 404)

    async def test_workflow_clarification_uses_selected_own_orders_and_rejects_identity_fields(self):
        await self.login()
        status, _, run = await self.request("/api/workflows", "POST", {"message": "帮我准备一份申请。"})
        self.assertEqual(status, 201)
        path = "/api/workflows/" + run["run_id"] + "/resume"
        status, _, _ = await self.request(path, "POST", {"decision": {"action": "provide_fields", "fields": {"order_ids": ["alice-hotel-001"], "cost_center": "CC-ALPHA-OPS", "owner_id": "bob"}}})
        self.assertEqual(status, 400)
        status, _, resumed = await self.request(path, "POST", {"decision": {"action": "provide_fields", "fields": {"order_ids": ["alice-hotel-001", "alice-train-001"], "cost_center": "CC-ALPHA-OPS"}}})
        self.assertEqual(status, 200, resumed)
        self.assertEqual(resumed["status"], "awaiting_confirmation")
        self.assertEqual(resumed["draft"]["eligible_cents"], 123000)

    async def test_policy_answer_scope_and_no_evidence_abstention(self):
        await self.login()
        status, _, answer = await self.request("/api/policy-answers", "POST", {"question": "广州住宿限额", "trip_date": "2026-10-09"})
        self.assertEqual(status, 200, answer)
        self.assertFalse(answer["model_used"])
        self.assertFalse(answer["semantic_support_verified"])
        self.assertTrue(answer["sources"])
        self.assertTrue(all(source["tenant_id"] == "alpha" for source in answer["sources"]))
        status, _, missing = await self.request("/api/policy-answers", "POST", {"question": "quasar nebulahyperdrive", "trip_date": "2026-10-09"})
        self.assertEqual(status, 200)
        self.assertEqual(missing["citations"], [])
        self.assertEqual(missing["sources"], [])
    AGENT_MESSAGE = ("我于 2026-10-09 至 2026-10-11 去广州出差，请处理 alice-hotel-001 和 alice-train-001，"
                     "成本中心 CC-ALPHA-OPS。")

    async def test_workbench_exposes_the_read_only_agent_card(self):
        status, _, page = await self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("多角色只读分析", page)
        self.assertIn("不产生业务写入", page)
        self.assertIn("agent-run", page)
        self.assertIn("plan-run", page)

    async def test_agent_endpoints_require_session_and_same_origin(self):
        self.assertEqual((await self.request("/api/plan", "POST", {"message": self.AGENT_MESSAGE}))[0], 401)
        self.assertEqual((await self.request("/api/agent-proposals", "POST", {"message": self.AGENT_MESSAGE}))[0], 401)
        await self.login()
        for path in ("/api/plan", "/api/agent-proposals"):
            status, _, _ = await self.request(path, "POST", {"message": self.AGENT_MESSAGE},
                                              headers={"origin": "http://evil.example"})
            self.assertEqual(status, 403)

    async def test_read_only_plan_endpoint_returns_a_trace_and_writes_nothing(self):
        await self.login()
        status, _, plan = await self.request("/api/plan", "POST", {"message": self.AGENT_MESSAGE})
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["stop_reason"], "goal_satisfied")
        self.assertEqual([step["observation"]["tool"] for step in plan["steps"]],
                         ["get_my_orders", "search_policy", "calculate_expense"])
        self.assertEqual((plan["tool_calls"], plan["context_chars"] > 0), (3, True))
        self.assertTrue(plan["read_only"])
        self.assertFalse(plan["business_effects"])
        self.assertEqual(plan["blocked_tools"], [])
        self.assertEqual(plan["findings"]["calculate_expense"]["eligible_cents"], 123000)
        self.assertEqual((await self.request("/api/drafts"))[2], [])

    async def test_agent_proposal_endpoint_is_deterministic_and_writes_nothing(self):
        await self.login()
        status, _, proposal = await self.request("/api/agent-proposals", "POST", {"message": self.AGENT_MESSAGE})
        self.assertEqual(status, 200, proposal)
        second = (await self.request("/api/agent-proposals", "POST", {"message": self.AGENT_MESSAGE}))[2]
        self.assertEqual(proposal["content_digest"], second["content_digest"])
        self.assertEqual(proposal["roles"], ["order_reconciler", "policy_researcher", "cost_estimator"])
        self.assertTrue(proposal["requires_human_confirmation"])
        self.assertTrue(proposal["read_only"])
        self.assertFalse(proposal["business_effects"])
        self.assertFalse(proposal["blocked"])
        self.assertEqual(proposal["conflicts"], [])
        self.assertEqual(proposal["calculation"]["eligible_cents"], 123000)
        self.assertEqual((await self.request("/api/drafts"))[2], [])

    async def test_agent_endpoints_validate_input_without_writing(self):
        await self.login()
        status, _, error = await self.request("/api/plan", "POST", {"message": self.AGENT_MESSAGE, "max_steps": 99})
        self.assertEqual((status, error["error"]), (400, "invalid_field"))
        status, _, error = await self.request("/api/plan", "POST", {"message": self.AGENT_MESSAGE, "user_id": "bob"})
        self.assertEqual((status, error["error"]), (400, "unexpected_fields"))
        status, _, error = await self.request("/api/agent-proposals", "POST", {})
        self.assertEqual((status, error["error"]), (400, "missing_fields"))
        status, _, error = await self.request("/api/agent-proposals", "POST", {"message": "   "})
        self.assertEqual((status, error["error"]), (400, "invalid_field"))
        self.assertEqual((await self.request("/api/drafts"))[2], [])


if __name__ == "__main__":
    unittest.main()
