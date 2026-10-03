"""Principal-bound MCP tools; models cannot supply or change identity."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from mcp import Client
from mcp.server import MCPServer
from pydantic import ValidationError

from .model import TripFields
from .service import DomainError, EnterpriseService, Principal


def create_tool_server(service: EnterpriseService, principal: Principal) -> MCPServer:
    """Create an in-process MCP server bound to one trusted server principal.

    This is an SDK protocol transport, not a publicly exposed unauthenticated
    MCP endpoint. Domain failures preserve safe codes in structured envelopes.
    """
    server = MCPServer("EnterpriseFlow business tools", version="0.1.0")

    def invoke(function: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"ok": True, "result": function(principal, *args, **kwargs)}
        except DomainError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc), "status_code": exc.status_code}}
        except (ValidationError, ValueError, TypeError):
            return {"ok": False, "error": {"code": "invalid_tool_input", "message": "Tool input is invalid", "status_code": 400}}

    def payload(value: dict[str, Any]) -> dict[str, Any]:
        return TripFields.model_validate(value).model_dump(exclude_none=True)

    @server.tool()
    def get_my_orders(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        """List only the bound employee's authorized orders."""
        return invoke(service.list_orders, start_date=start_date, end_date=end_date)

    @server.tool()
    def search_policy(query: str = "", trip_date: str = "2026-10-09") -> dict[str, Any]:
        """Retrieve clauses filtered by bound tenant, department and date."""
        return invoke(service.search_policies, query=query, trip_date=trip_date)

    @server.tool()
    def get_preferences() -> dict[str, Any]:
        """Read explicitly saved preferences for the bound employee."""
        return invoke(service.get_preferences)

    @server.tool()
    def calculate_expense(fields: dict[str, Any]) -> dict[str, Any]:
        """Read authoritative orders and compute cents using structured policies."""
        try:
            parsed = payload(fields)
        except (ValidationError, ValueError, TypeError):
            return {"ok": False, "error": {"code": "invalid_tool_input", "message": "Invalid task fields", "status_code": 400}}
        return invoke(service.evaluate_expense, parsed)

    @server.tool()
    def create_expense_draft(fields: dict[str, Any], request_key: str) -> dict[str, Any]:
        """Create an idempotent versioned draft; this does not submit it."""
        try:
            parsed = payload(fields)
        except (ValidationError, ValueError, TypeError):
            return {"ok": False, "error": {"code": "invalid_tool_input", "message": "Invalid task fields", "status_code": 400}}
        return invoke(service.create_draft, parsed, request_key=request_key)

    @server.tool()
    def confirm_expense_draft(draft_id: str, expected_version: int, expected_hash: str) -> dict[str, Any]:
        """Bind a human decision to the exact authorized draft version and hash."""
        return invoke(service.confirm_draft, draft_id, expected_version, expected_hash=expected_hash)

    @server.tool()
    def submit_expense(draft_id: str, expected_version: int, idempotency_key: str) -> dict[str, Any]:
        """Submit only a current confirmed draft using a transactional service."""
        return invoke(service.submit_draft, draft_id, expected_version, idempotency_key)

    @server.tool()
    async def plan_readonly_analysis(message: str, max_steps: int = 6, mode: str = "fixture") -> dict[str, Any]:
        """Run a bounded read-only planning loop; it cannot create, confirm or submit."""
        if mode not in {"fixture", "qwen", "api"} or type(max_steps) is not int or not 1 <= max_steps <= 20:
            return {"ok": False, "error": {"code": "invalid_tool_input", "message": "Invalid planning bounds", "status_code": 400}}
        from .planner import Planner
        try:
            result = await Planner(service, mode=mode, max_steps=max_steps).plan(principal, message)
        except DomainError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc), "status_code": exc.status_code}}
        except (ValueError, TypeError):
            return {"ok": False, "error": {"code": "invalid_tool_input", "message": "Invalid planning input", "status_code": 400}}
        return {"ok": True, "result": result.model_dump()}

    return server


class ToolGateway:
    """Execute actual MCP calls with bounded await and structured validation."""

    ALLOWED = frozenset({"get_my_orders", "search_policy", "get_preferences", "calculate_expense",
                         "create_expense_draft", "confirm_expense_draft", "submit_expense",
                         "plan_readonly_analysis"})

    def __init__(self, service: EnterpriseService, principal: Principal, timeout: float = 15) -> None:
        self.server = create_tool_server(service, principal)
        self.timeout = timeout

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
        if name not in self.ALLOWED:
            raise DomainError("unknown_tool", "Tool is not in the business allowlist")
        started = time.perf_counter()

        async def perform() -> Any:
            async with Client(self.server) as client:
                response = await client.call_tool(name, arguments or {})
                envelope = response.structured_content
                if envelope is None:
                    for block in response.content:
                        if getattr(block, "type", None) == "text":
                            try:
                                envelope = json.loads(block.text)
                            except (ValueError, TypeError):
                                continue
                            break
            # Raise domain errors after the SDK task groups have exited. Raising
            # within Client.__aexit__ would wrap them in an ExceptionGroup.
            if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
                raise DomainError("tool_protocol_error", "MCP tool returned an invalid response", 502)
            if not envelope["ok"]:
                error = envelope.get("error")
                if not isinstance(error, dict):
                    raise DomainError("tool_protocol_error", "MCP tool returned an invalid error", 502)
                raise DomainError(error.get("code", "tool_error"), error.get("message", "Business tool failed"), error.get("status_code", 400))
            return envelope.get("result")

        try:
            result = await asyncio.wait_for(perform(), timeout=self.timeout)
        except TimeoutError as exc:
            raise DomainError("tool_timeout", "Business tool timed out", 504) from exc
        except DomainError:
            raise
        except Exception as exc:
            # SDK transport failures may be ExceptionGroups. Never copy their
            # nested provider or database payloads into a user-visible record.
            raise DomainError("tool_transport_error", "MCP business tool transport failed", 502) from exc
        return result, {"tool": name, "duration_ms": round((time.perf_counter() - started) * 1000), "ok": True}
