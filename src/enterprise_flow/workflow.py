"""Durable LangGraph business workflow with authorized human resume.

Graph checkpoints and business transactions use separate SQLite files. Each
write node is idempotent in the business service; replay after a process failure
does not create a second draft or submission. Local concurrency is bounded.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .model import Extractor, ModelError, TripFields, create_extractor
from .service import DomainError, EnterpriseService, Principal
from .tools import ToolGateway


class WorkflowState(TypedDict, total=False):
    run_id: str
    principal: dict[str, str]
    message: str
    fields: dict[str, Any]
    questions: list[str]
    status: str
    stage: str
    draft: dict[str, Any] | None
    calculation: dict[str, Any] | None
    submission: dict[str, Any] | None
    decision: dict[str, Any] | None
    model_used: bool
    model_usage: dict[str, Any]
    tool_events: list[dict[str, Any]]


class ResumeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: str
    fields: dict[str, Any] | None = None
    expected_version: int | None = Field(default=None, ge=1)
    expected_hash: str | None = Field(default=None, min_length=64, max_length=64)


class WorkflowEngine:
    def __init__(self, service: EnterpriseService, checkpoint_path: Path, mode: str, extractor: Extractor) -> None:
        self.service = service
        self.checkpoint_path = checkpoint_path
        self.mode = mode
        self.extractor = extractor
        self._locks: dict[str, asyncio.Lock] = {}
        self._capacity = asyncio.Semaphore(4)
        self._context: Any = None
        self.graph: Any = None
        self._closed = False

    @classmethod
    async def open(
        cls,
        service: EnterpriseService,
        checkpoint_path: str | Path,
        mode: str = "fixture",
        extractor: Extractor | None = None,
    ) -> "WorkflowEngine":
        path = Path(checkpoint_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = cls(service, path, mode, extractor or create_extractor(mode))
        engine._context = AsyncSqliteSaver.from_conn_string(str(path))
        saver = await engine._context.__aenter__()
        try:
            engine.graph = engine._build_graph().compile(checkpointer=saver)
        except BaseException:
            await engine._context.__aexit__(None, None, None)
            raise
        return engine

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._context.__aexit__(None, None, None)

    async def __aenter__(self) -> "WorkflowEngine":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    @staticmethod
    def _config(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 40}

    def _lock(self, run_id: str) -> asyncio.Lock:
        if self._closed:
            raise DomainError("engine_closed", "Workflow engine is closed", 503)
        if run_id not in self._locks:
            if len(self._locks) >= 4096:
                raise DomainError("engine_capacity", "Restart the local worker before adding more runs", 503)
            self._locks[run_id] = asyncio.Lock()
        return self._locks[run_id]

    @staticmethod
    def _principal(state: WorkflowState) -> Principal:
        return Principal(**state["principal"])

    async def _call(self, state: WorkflowState, name: str, args: dict[str, Any] | None = None) -> tuple[Any, list[dict[str, Any]]]:
        result, event = await ToolGateway(self.service, self._principal(state)).call(name, args)
        return result, state.get("tool_events", []) + [event]

    def _build_graph(self) -> StateGraph:
        builder = StateGraph(WorkflowState)
        builder.add_node("extract", self._extract)
        builder.add_node("collect", self._collect)
        builder.add_node("clarify", self._clarify)
        builder.add_node("evaluate", self._evaluate)
        builder.add_node("draft", self._draft)
        builder.add_node("approval", self._approval)
        builder.add_node("confirm", self._confirm)
        builder.add_node("submit", self._submit)
        builder.add_edge(START, "extract")
        builder.add_edge("extract", "collect")
        builder.add_conditional_edges("collect", lambda state: "clarify" if state["questions"] else "evaluate", {"clarify": "clarify", "evaluate": "evaluate"})
        builder.add_conditional_edges("clarify", lambda state: "cancel" if state["status"] == "cancelled" else "collect", {"cancel": END, "collect": "collect"})
        builder.add_edge("evaluate", "draft")
        builder.add_edge("draft", "approval")
        builder.add_conditional_edges("approval", lambda state: "cancel" if state["status"] == "cancelled" else "confirm", {"cancel": END, "confirm": "confirm"})
        builder.add_edge("confirm", "submit")
        builder.add_edge("submit", END)
        return builder

    async def _extract(self, state: WorkflowState) -> dict[str, Any]:
        extraction = await self.extractor.extract(state["message"])
        return {"fields": extraction.fields.model_dump(exclude_none=True), "model_used": extraction.model_used,
                "model_usage": {**extraction.usage, "mode": extraction.mode, "duration_ms": extraction.duration_ms},
                "stage": "collect", "status": "running"}

    async def _collect(self, state: WorkflowState) -> dict[str, Any]:
        fields = TripFields.model_validate(state.get("fields", {})).model_dump(exclude_none=True)
        events = state.get("tool_events", [])
        if not fields.get("cost_center"):
            preferences, events = await self._call(state, "get_preferences")
            centers = [item["value"] for item in preferences if item.get("key") == "cost_center"]
            if len(centers) == 1:
                fields["cost_center"] = centers[0]
        questions = []
        if not fields.get("order_ids"):
            questions.append("请选择本人的订单；订单金额和日期由业务数据库提供。")
        if not fields.get("cost_center"):
            questions.append("请选择本次申请的成本中心，或明确保存一个常用成本中心。")
        return {"fields": fields, "questions": questions, "tool_events": events,
                "status": "waiting_fields" if questions else "running", "stage": "clarify" if questions else "evaluate"}

    async def _clarify(self, state: WorkflowState) -> dict[str, Any]:
        answer = interrupt({"kind": "clarification", "questions": state["questions"], "fields": state["fields"]})
        if answer["action"] == "cancel":
            return {"status": "cancelled", "stage": "cancelled", "decision": answer}
        fields = {**state["fields"], **answer["fields"]}
        return {"fields": TripFields.model_validate(fields).model_dump(exclude_none=True), "stage": "collect", "status": "running"}

    async def _evaluate(self, state: WorkflowState) -> dict[str, Any]:
        # Retrieval is a read stage; calculation chooses authoritative applicable
        # snapshots itself and rejects ambiguity rather than trusting model text.
        orders, events = await self._call(state, "get_my_orders")
        selected = [order for order in orders if order.get("order_id") in state["fields"]["order_ids"]]
        trip_date = state["fields"].get("start_date")
        if not trip_date and selected:
            first = selected[0]
            trip_date = first.get("start_date") or first.get("date") or first.get("order_date")
        intermediate = {**state, "tool_events": events}
        if trip_date:
            _, events = await self._call(intermediate, "search_policy", {"query": state["fields"].get("destination", "差旅报销"), "trip_date": trip_date})
        calculation, events = await self._call({**state, "tool_events": events}, "calculate_expense", {"fields": state["fields"]})
        return {"calculation": calculation, "tool_events": events, "stage": "draft", "status": "running"}

    async def _draft(self, state: WorkflowState) -> dict[str, Any]:
        draft, events = await self._call(state, "create_expense_draft", {"fields": state["fields"], "request_key": state["run_id"]})
        return {"draft": draft, "tool_events": events, "stage": "approval", "status": "awaiting_confirmation"}

    async def _approval(self, state: WorkflowState) -> dict[str, Any]:
        draft = state["draft"]
        assert draft is not None
        answer = interrupt({"kind": "approval", "draft_id": draft["draft_id"], "expected_version": draft["version"],
                            "expected_hash": draft["content_hash"], "draft": draft})
        return {"decision": answer, "status": "cancelled" if answer["action"] == "cancel" else "approved",
                "stage": "cancelled" if answer["action"] == "cancel" else "confirm"}

    async def _confirm(self, state: WorkflowState) -> dict[str, Any]:
        draft = state["draft"]
        assert draft is not None
        result, events = await self._call(state, "confirm_expense_draft", {
            "draft_id": draft["draft_id"], "expected_version": state["decision"]["expected_version"],
            "expected_hash": state["decision"]["expected_hash"],
        })
        # Service confirmation may return a confirmation record or the draft.
        current = self.service.get_draft(self._principal(state), draft["draft_id"])
        return {"draft": current, "tool_events": events, "stage": "submit", "status": "running"}

    async def _submit(self, state: WorkflowState) -> dict[str, Any]:
        draft = state["draft"]
        assert draft is not None
        submission, events = await self._call(state, "submit_expense", {"draft_id": draft["draft_id"],
            "expected_version": draft["version"], "idempotency_key": "workflow-" + state["run_id"]})
        return {"submission": submission, "draft": self.service.get_draft(self._principal(state), draft["draft_id"]),
                "tool_events": events, "stage": "submitted", "status": "submitted"}

    async def start(self, principal: Principal, message: str, request_id: str | None = None) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise DomainError("invalid_message", "Message must contain 1 to 4000 characters")
        run = self.service.create_run(principal, "run-" + uuid4().hex, self.mode, message.strip(), request_id=request_id)
        run_id = run["run_id"]
        async with self._lock(run_id):
            snapshot = await self.graph.aget_state(self._config(run_id))
            if snapshot.values:
                return await self.get(principal, run_id)
            initial = WorkflowState(run_id=run_id, principal={"user_id": principal.user_id, "tenant_id": principal.tenant_id,
                "department_id": principal.department_id, "display_name": principal.display_name}, message=run["message"],
                fields={}, questions=[], draft=None, calculation=None, submission=None, decision=None,
                model_used=False, model_usage={"mode": self.mode, "model_calls": 0}, tool_events=[], status="running", stage="extract")
            return await self._invoke(principal, run_id, initial)

    async def resume(self, principal: Principal, run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        run = self.service.get_run(principal, run_id)  # Check ownership before checkpoint access.
        if run["mode"] != self.mode:
            raise DomainError("mode_mismatch", "Resume requires a worker configured for the run's original model mode", 409)
        try:
            parsed = ResumeDecision.model_validate(decision)
        except (ValidationError, TypeError) as exc:
            raise DomainError("invalid_decision", "Resume decision has an invalid schema") from exc
        async with self._lock(run_id):
            snapshot = await self.graph.aget_state(self._config(run_id))
            pending = self._pending(snapshot)
            if not snapshot.values:
                raise DomainError("run_not_started", "Run does not have a checkpoint", 409)
            if snapshot.values.get("status") in {"submitted", "cancelled"}:
                return await self.get(principal, run_id)
            if parsed.action == "retry":
                if pending or not snapshot.next:
                    raise DomainError("invalid_decision", "Only a failed or interrupted step can be retried", 409)
                return await self._invoke(principal, run_id, None)
            if parsed.action == "refresh":
                if not pending or pending["kind"] != "approval":
                    raise DomainError("invalid_decision", "Only an awaiting draft can be refreshed", 409)
                current = self.service.get_draft(principal, pending["draft_id"])
                await self.graph.aupdate_state(self._config(run_id), {"draft": current, "decision": None,
                    "status": "awaiting_confirmation", "stage": "approval"}, as_node="draft")
                return await self._invoke(principal, run_id, None)
            if not pending:
                raise DomainError("run_not_waiting", "Run is not awaiting a human decision", 409)
            if parsed.action == "cancel":
                value = {"action": "cancel"}
            elif parsed.action == "provide_fields" and pending["kind"] == "clarification":
                if parsed.fields is None:
                    raise DomainError("invalid_decision", "provide_fields requires fields")
                try:
                    # exclude_unset preserves existing explicit fields during partial updates.
                    value = {"action": "provide_fields", "fields": TripFields.model_validate(parsed.fields).model_dump(exclude_unset=True, exclude_none=True)}
                except ValidationError as exc:
                    raise DomainError("invalid_fields", "Only documented task fields may be supplied") from exc
            elif parsed.action == "approve" and pending["kind"] == "approval":
                if parsed.expected_version is None or parsed.expected_hash is None or not re.fullmatch(r"[0-9a-f]{64}", parsed.expected_hash):
                    raise DomainError("invalid_decision", "Approval requires a draft version and SHA256 hash")
                current = self.service.get_draft(principal, pending["draft_id"])
                if current["version"] != parsed.expected_version or current["content_hash"] != parsed.expected_hash:
                    raise DomainError("stale_confirmation", "Draft changed; refresh and confirm its current version", 409)
                if pending["expected_version"] != parsed.expected_version or pending["expected_hash"] != parsed.expected_hash:
                    raise DomainError("stale_confirmation", "Displayed workflow draft is stale; refresh it before confirmation", 409)
                value = {"action": "approve", "expected_version": parsed.expected_version, "expected_hash": parsed.expected_hash}
            else:
                raise DomainError("invalid_decision", "Decision does not match the waiting workflow stage", 409)
            return await self._invoke(principal, run_id, Command(resume=value))

    async def _invoke(self, principal: Principal, run_id: str, value: Any) -> dict[str, Any]:
        try:
            async with self._capacity:
                await asyncio.wait_for(self.graph.ainvoke(value, self._config(run_id)), timeout=120)
        except (DomainError, ModelError, TimeoutError) as exc:
            error = {"code": exc.code if isinstance(exc, DomainError) else "model_error" if isinstance(exc, ModelError) else "workflow_timeout",
                     "message": str(exc) if not isinstance(exc, TimeoutError) else "Workflow execution timed out"}
            self.service.update_run(principal, run_id, status="failed", error=error)
            return await self.get(principal, run_id, failed_error=error)
        except asyncio.CancelledError:
            self.service.update_run(principal, run_id, status="interrupted", error={"code": "execution_interrupted", "message": "Execution interrupted; retry the saved step"})
            raise
        except Exception:
            error = {"code": "execution_error", "message": "Workflow execution failed; retry the saved step"}
            self.service.update_run(principal, run_id, status="failed", error=error)
            return await self.get(principal, run_id, failed_error=error)
        return await self.get(principal, run_id, persist=True)

    @staticmethod
    def _pending(snapshot: Any) -> dict[str, Any] | None:
        for task in snapshot.tasks:
            for item in task.interrupts:
                if isinstance(item.value, dict):
                    return item.value
        return None

    async def get(self, principal: Principal, run_id: str, *, persist: bool = False, failed_error: dict[str, Any] | None = None) -> dict[str, Any]:
        run = self.service.get_run(principal, run_id)
        snapshot = await self.graph.aget_state(self._config(run_id))
        state = snapshot.values or {}
        stored_principal = state.get("principal", {})
        if state and any(stored_principal.get(key) != getattr(principal, key) for key in ("user_id", "tenant_id", "department_id")):
            raise DomainError("checkpoint_owner_mismatch", "Checkpoint does not belong to this employee", 403)
        pending = self._pending(snapshot)
        result = {"pending": pending, "draft": state.get("draft"), "calculation": state.get("calculation"),
                  "submission": state.get("submission"), "model_used": state.get("model_used", False),
                  "model_usage": state.get("model_usage", {"mode": self.mode, "model_calls": 0}), "tool_events": state.get("tool_events", [])}
        status = "failed" if failed_error else "waiting_fields" if pending and pending["kind"] == "clarification" else "awaiting_confirmation" if pending else state.get("status", run["status"])
        if not pending and run["status"] in {"failed", "interrupted"} and snapshot.next and not persist:
            status = run["status"]
        stage = state.get("stage", run.get("stage", "created"))
        if persist or failed_error:
            run = self.service.update_run(principal, run_id, status=status, stage=stage,
                draft_id=state.get("draft", {}).get("draft_id") if state.get("draft") else None,
                result=result, fields=state.get("fields", {}), questions=state.get("questions", []), error=failed_error)
        return {**run, "status": status, "stage": stage, "fields": state.get("fields", run.get("fields", {})), "result": result, **result}
