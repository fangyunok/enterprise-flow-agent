"""Thin HTTP client for the EnterpriseFlow console API.

Integration code should not have to hand-roll cookie sessions and error envelopes, so this client
wraps the documented endpoints and turns the server's ``{error, message}`` envelope into a typed
exception. It talks to the same handlers the browser uses, so nothing here can reach the business
layer without going through authorization first.

It is deliberately built on the standard library: the integration path then carries no third-party
dependency of its own, and the cookie handling matches what a browser session does.

    from enterprise_flow.client import EnterpriseFlowClient

    with EnterpriseFlowClient("http://127.0.0.1:7861") as client:
        client.login("alice")
        run = client.start_workflow("处理 alice-hotel-001，成本中心 CC-ALPHA-OPS。")
        if run.pending and run.pending["kind"] == "approval":
            run = client.approve(run.run_id, run.draft["version"], run.draft["content_hash"])
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any


class ClientError(Exception):
    """A domain or transport failure reported by the server."""

    def __init__(self, code: str, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


@dataclass
class Run:
    """A workflow run as the API returns it."""

    run_id: str
    status: str
    stage: str
    pending: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None
    submission: dict[str, Any] | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "Run":
        return cls(run_id=payload.get("run_id", ""), status=payload.get("status", ""), stage=payload.get("stage", ""),
                   pending=payload.get("pending"), draft=payload.get("draft"), submission=payload.get("submission"),
                   tool_events=payload.get("tool_events") or [], raw=payload)


class EnterpriseFlowClient:
    def __init__(self, base_url: str = "http://127.0.0.1:7861", *, timeout: float = 30) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty URL")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))

    def close(self) -> None:
        self._jar.clear()

    def __enter__(self) -> "EnterpriseFlowClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 params: dict[str, Any] | None = None, as_text: bool = False) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            # Mutations are checked for same-origin, so the client identifies its own origin.
            headers["Origin"] = self.base_url
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            status = exc.code
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ClientError("transport_error", str(exc.reason if isinstance(exc, urllib.error.URLError) else exc)) from exc
        else:
            status = response.status

        if "text/plain" in content_type:
            return raw.decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        if status >= 400:
            detail = parsed if isinstance(parsed, dict) else {}
            raise ClientError(str(detail.get("error", "http_error")), str(detail.get("message", "请求失败。")), status)
        return raw.decode("utf-8", "replace") if as_text else parsed

    # -- identity ---------------------------------------------------------
    def login(self, user_id: str) -> dict[str, Any]:
        return self._request("POST", "/api/login", {"user_id": user_id})

    def logout(self) -> dict[str, Any]:
        return self._request("POST", "/api/logout", {})

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/api/me")

    def demo_users(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/demo-users")

    # -- reads ------------------------------------------------------------
    def orders(self, start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
        return self._request("GET", "/api/orders", params={"start_date": start_date, "end_date": end_date})

    def cost_centers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/cost-centers")

    def policies(self, query: str = "", trip_date: str = "2026-10-09") -> list[dict[str, Any]]:
        return self._request("GET", "/api/policies", params={"query": query, "trip_date": trip_date})

    def policy_answer(self, question: str, trip_date: str = "2026-10-09") -> dict[str, Any]:
        return self._request("POST", "/api/policy-answers", {"question": question, "trip_date": trip_date})

    def drafts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/drafts")

    def draft(self, draft_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/drafts/{urllib.parse.quote(draft_id)}")

    def preferences(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/preferences")

    def set_preference(self, key: str, value: str) -> dict[str, Any]:
        return self._request("PATCH", "/api/preferences", {"key": key, "value": value, "confirmed": True})

    def delete_preference(self, key: str) -> dict[str, Any]:
        return self._request("DELETE", "/api/preferences", {"key": key})

    # -- writes -----------------------------------------------------------
    def create_draft(self, order_ids: list[str], cost_center: str | None = None, notes: str | None = None,
                     **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"order_ids": list(order_ids), **fields}
        if cost_center is not None:
            payload["cost_center"] = cost_center
        if notes is not None:
            payload["notes"] = notes
        return self._request("POST", "/api/drafts", payload)

    def confirm_draft(self, draft_id: str, expected_version: int, expected_hash: str) -> dict[str, Any]:
        return self._request("POST", f"/api/drafts/{urllib.parse.quote(draft_id)}/confirm",
                             {"expected_version": expected_version, "expected_hash": expected_hash})

    def submit_draft(self, draft_id: str, expected_version: int, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", f"/api/drafts/{urllib.parse.quote(draft_id)}/submit",
                             {"expected_version": expected_version, "idempotency_key": idempotency_key})

    # -- orchestration ----------------------------------------------------
    def start_workflow(self, message: str, request_id: str | None = None) -> Run:
        payload: dict[str, Any] = {"message": message}
        if request_id:
            payload["request_id"] = request_id
        return Run.parse(self._request("POST", "/api/workflows", payload))

    def get_workflow(self, run_id: str) -> Run:
        return Run.parse(self._request("GET", f"/api/workflows/{urllib.parse.quote(run_id)}"))

    def resume(self, run_id: str, decision: dict[str, Any]) -> Run:
        return Run.parse(self._request("POST", f"/api/workflows/{urllib.parse.quote(run_id)}/resume", {"decision": decision}))

    def provide_fields(self, run_id: str, fields: dict[str, Any]) -> Run:
        return self.resume(run_id, {"action": "provide_fields", "fields": fields})

    def approve(self, run_id: str, expected_version: int, expected_hash: str) -> Run:
        return self.resume(run_id, {"action": "approve", "expected_version": expected_version, "expected_hash": expected_hash})

    def cancel(self, run_id: str) -> Run:
        return self.resume(run_id, {"action": "cancel"})

    def runs(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/runs")

    # -- agent layer ------------------------------------------------------
    def plan(self, message: str, max_steps: int = 6) -> dict[str, Any]:
        return self._request("POST", "/api/plan", {"message": message, "max_steps": max_steps})

    def collaborate(self, message: str) -> dict[str, Any]:
        return self._request("POST", "/api/agent-proposals", {"message": message})

    # -- operations -------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def overview(self) -> dict[str, Any]:
        return self._request("GET", "/api/overview")

    def contract(self) -> dict[str, Any]:
        return self._request("GET", "/api/contract")

    def audit(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/audit")

    def trace(self) -> dict[str, Any]:
        return self._request("GET", "/api/trace")

    def metrics(self) -> str:
        return self._request("GET", "/api/metrics", as_text=True)
