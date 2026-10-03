"""Supervisor multi-agent analysis with per-role least-privilege tool scopes.

Specialists only ever receive read-only tools, and every tool call is checked
against the calling role's scope before it reaches MCP. The layer therefore
produces analysis and a proposal, never a business effect: creating, confirming
and submitting a draft remain behind the human-approved LangGraph path.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .model import ModelError, create_extractor
from .planner import READ_ONLY_TOOLS, WRITE_TOOLS, canonical
from .service import DomainError, EnterpriseService, Principal
from .tools import ToolGateway

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "order_reconciler": frozenset({"get_my_orders", "get_preferences"}),
    "policy_researcher": frozenset({"search_policy"}),
    "cost_estimator": frozenset({"get_my_orders", "search_policy", "calculate_expense"}),
}
ALL_AGENT_TOOLS: frozenset[str] = frozenset().union(*ROLE_SCOPES.values())

# Enforced at import time: no specialist may ever be granted a writing tool.
assert ALL_AGENT_TOOLS <= READ_ONLY_TOOLS, "Agent scopes must stay read-only"
assert not ALL_AGENT_TOOLS & WRITE_TOOLS, "Agent scopes must never include business writes"

_BLOCKING = {"order_preview_rejected", "order_ineligible", "order_not_found", "policy_gap",
             "policy_version_overlap", "policy_conflict", "insufficient_evidence", "missing_cost_center",
             "cost_center_denied", "scoped_denial", "tool_scope_violation", "missing_order_ids"}
_MAX_QUERY_DAYS = 8
_MAX_EVIDENCE = 10


@dataclass(frozen=True)
class AgentTask:
    """The only context a specialist receives: text and already-extracted fields."""

    message: str
    fields: dict[str, Any]


@dataclass
class ScopedToolbox:
    """Per-agent tool access; the scope check happens before any MCP call."""

    gateway: ToolGateway
    role: str
    allowed: frozenset[str]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in self.allowed:
            raise DomainError("tool_scope_violation", f"{self.role} 无权调用 {name}。", 403)
        result, event = await self.gateway.call(name, arguments)
        self.calls.append({**event, "agent": self.role})
        return result


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str
    conclusion: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    ok: bool = True
    error_code: str | None = None
    tool_calls: int = 0
    duration_ms: int = 0


class Conflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    detail: str
    agents: list[str] = Field(default_factory=list)
    severity: str = "warning"


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str
    model_used: bool
    task_fields: dict[str, Any] = Field(default_factory=dict)
    roles: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    orders: list[dict[str, Any]] = Field(default_factory=list)
    candidate_policies: list[dict[str, Any]] = Field(default_factory=list)
    calculation: dict[str, Any] | None = None
    next_action: str = ""
    blocked: bool = False
    requires_human_confirmation: bool = True
    read_only: bool = True
    business_effects: bool = False
    tool_calls: int = 0
    duration_ms: int = 0
    content_digest: str = ""


class Blackboard:
    """Typed handoff between specialists; later roles read earlier conclusions."""

    def __init__(self) -> None:
        self.findings: dict[str, Finding] = {}
        self.payload: dict[str, Any] = {}

    def record(self, finding: Finding, payload: dict[str, Any] | None = None) -> None:
        self.findings[finding.agent] = finding
        if payload:
            self.payload[finding.agent] = payload

    def require(self, agent: str) -> dict[str, Any]:
        return self.payload.get(agent) or {}


class Specialist(Protocol):
    role: str

    async def run(self, toolbox: ScopedToolbox, task: AgentTask, board: Blackboard) -> tuple[Finding, dict[str, Any]]: ...


def _order_view(order: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """Preview classification only; the business service stays authoritative."""
    reasons: list[str] = []
    if order.get("currency") != "CNY":
        reasons.append("currency")
    if order.get("status") != "completed":
        reasons.append("status")
    if order.get("receipt_valid") != 1:
        reasons.append("receipt")
    if fields.get("destination") and order.get("city") != fields["destination"]:
        reasons.append("city")
    if fields.get("start_date") and str(order.get("start_date", "")) < fields["start_date"]:
        reasons.append("start_before_scope")
    if fields.get("end_date") and str(order.get("end_date", "")) > fields["end_date"]:
        reasons.append("end_after_scope")
    return {"order_id": order.get("order_id"), "kind": order.get("kind"), "city": order.get("city"),
            "amount_cents": order.get("amount_cents"), "start_date": order.get("start_date"),
            "end_date": order.get("end_date"), "usable_preview": not reasons, "reasons": reasons}


def _scope_days(start: Any, end: Any) -> list[str]:
    try:
        first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    except ValueError:
        return [str(start)] if start else []
    if last < first:
        return [first.isoformat()]
    days = min((last - first).days + 1, _MAX_QUERY_DAYS)
    return [(first + timedelta(days=offset)).isoformat() for offset in range(days)]


class OrderReconciler:
    """Maps the requested order IDs onto the bound employee's own orders."""

    role = "order_reconciler"

    async def run(self, toolbox: ScopedToolbox, task: AgentTask, board: Blackboard) -> tuple[Finding, dict[str, Any]]:
        started = time.perf_counter()
        orders = await toolbox.call("get_my_orders")
        preferences = await toolbox.call("get_preferences")
        owned = {row["order_id"]: row for row in orders}
        requested = list(task.fields.get("order_ids") or [])
        views = [_order_view(owned[order_id], task.fields) for order_id in requested if order_id in owned]
        usable = [view for view in views if view["usable_preview"]]
        rejected = [view["order_id"] for view in views if not view["usable_preview"]]
        unavailable = [order_id for order_id in requested if order_id not in owned] + rejected
        saved_center = next((row["value"] for row in preferences if row.get("key") == "cost_center"), None)
        payload = {"owned_count": len(owned), "views": views, "usable": usable,
                   "unavailable": unavailable, "saved_cost_center": saved_center}
        if not requested:
            conclusion = f"本人共有 {len(owned)} 笔授权订单，但输入未指定订单 ID；金额必须来自业务数据库。"
        else:
            conclusion = (f"本人共有 {len(owned)} 笔授权订单，本次引用 {len(views)} 笔，"
                          f"其中 {len(usable)} 笔通过预检、{len(unavailable)} 笔需人工确认。")
        finding = Finding(agent=self.role, conclusion=conclusion,
                          evidence=[{"ref": view["order_id"], "kind": "order", "detail": view}
                                    for view in views[:_MAX_EVIDENCE]],
                          tool_calls=len(toolbox.calls), duration_ms=round((time.perf_counter() - started) * 1000))
        return finding, payload


class PolicyResearcher:
    """Retrieves candidate clauses for the reconciled order scope only."""

    role = "policy_researcher"

    async def run(self, toolbox: ScopedToolbox, task: AgentTask, board: Blackboard) -> tuple[Finding, dict[str, Any]]:
        started = time.perf_counter()
        views = board.require("order_reconciler").get("usable") or []
        if not views:
            conclusion = "订单范围尚未确定，无法确定需要检索的城市、费用类型与业务日期。"
            finding = Finding(agent=self.role, conclusion=conclusion,
                              duration_ms=round((time.perf_counter() - started) * 1000))
            return finding, {"searched": [], "rows": [], "gaps": [], "overlaps": []}
        queries: list[tuple[str, str, str]] = []
        for view in views:
            city, kind = str(view.get("city") or ""), str(view.get("kind") or "")
            for day in _scope_days(view.get("start_date"), view.get("end_date")):
                if (city, kind, day) not in queries:
                    queries.append((city, kind, day))
        searched: list[dict[str, Any]] = []
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        gaps: list[dict[str, Any]] = []
        clauses: set[str] = set()
        versions_per_day: dict[tuple[str, str], set[str]] = {}
        for city, kind, day in queries:
            result = await toolbox.call("search_policy", {"query": f"{city} {kind}".strip(), "trip_date": day})
            matching = [row for row in result if row.get("city") in {city, "*"}]
            searched.append({"city": city, "kind": kind, "trip_date": day, "returned": len(result)})
            if not any(row.get("kind") == kind for row in matching):
                gaps.append({"city": city, "kind": kind, "trip_date": day})
            for row in matching:
                rows.setdefault((row["policy_id"], day), {
                    "policy_id": row["policy_id"], "clause_id": row["clause_id"], "version": row["version"],
                    "kind": row["kind"], "city": row["city"], "title": row["title"],
                    "cap_cents": row.get("cap_cents"), "trip_date": day})
                clauses.add(row["clause_id"])
                versions_per_day.setdefault((row["clause_id"], day), set()).add(row["version"])
        # Two versions of one clause on the same business day is a real ambiguity;
        # a version change between days is normal and must not be reported.
        overlaps = [{"clause_id": clause_id, "trip_date": day, "versions": sorted(versions)}
                    for (clause_id, day), versions in sorted(versions_per_day.items()) if len(versions) > 1]
        records = list(rows.values())
        conclusion = (f"在本人租户、部门与生效区间内按 {len(queries)} 个业务日期检索，"
                      f"命中 {len(records)} 条适用条款记录，覆盖 {len(clauses)} 个条款。")
        if gaps:
            conclusion += f" {len(gaps)} 个范围没有可用条款，需要人工核查。"
        if overlaps:
            conclusion += f" {len(overlaps)} 个条款在同一生效期内存在多版本，需要人工裁决。"
        finding = Finding(agent=self.role, conclusion=conclusion,
                          evidence=[{"ref": row["policy_id"], "kind": "policy", "detail": row}
                                    for row in records[:_MAX_EVIDENCE]],
                          tool_calls=len(toolbox.calls), duration_ms=round((time.perf_counter() - started) * 1000))
        return finding, {"searched": searched, "rows": records, "gaps": gaps, "overlaps": overlaps}


class CostEstimator:
    """Calls the deterministic calculation service; the model never computes money."""

    role = "cost_estimator"

    async def run(self, toolbox: ScopedToolbox, task: AgentTask, board: Blackboard) -> tuple[Finding, dict[str, Any]]:
        started = time.perf_counter()
        try:
            calculation = await toolbox.call("calculate_expense", {"fields": dict(task.fields)})
        except DomainError as exc:
            finding = Finding(agent=self.role, conclusion=f"确定性核算被业务服务拒绝：{exc.message}",
                              ok=False, error_code=exc.code, tool_calls=len(toolbox.calls),
                              duration_ms=round((time.perf_counter() - started) * 1000))
            return finding, {"calculation": None, "error_code": exc.code}
        items = calculation.get("items") or []
        conclusion = (f"业务服务按 {len(items)} 笔订单完成核算：票据合计 {calculation['total_cents']} 分，"
                      f"可申请 {calculation['eligible_cents']} 分，超额 {calculation['excess_cents']} 分。")
        evidence = [{"ref": item["order_id"], "kind": "calculation",
                     "detail": {"eligible_cents": item["eligible_cents"], "excess_cents": item["excess_cents"]}}
                    for item in items]
        finding = Finding(agent=self.role, conclusion=conclusion, evidence=evidence,
                          tool_calls=len(toolbox.calls), duration_ms=round((time.perf_counter() - started) * 1000))
        return finding, {"calculation": calculation, "error_code": None}


class Supervisor:
    """Deterministic dispatch with a blackboard handoff and cross-agent checks."""

    def __init__(self, service: EnterpriseService, mode: str = "fixture", *, extractor: Any | None = None,
                 agents: dict[str, Specialist] | None = None, tool_timeout: float = 10,
                 agent_timeout: float = 30) -> None:
        if mode not in {"fixture", "qwen", "api"}:
            raise ValueError("Mode must be fixture, qwen or api")
        self.service = service
        self.mode = mode
        self.extractor = extractor or create_extractor(mode)
        self.agents: dict[str, Specialist] = agents or {
            "order_reconciler": OrderReconciler(),
            "policy_researcher": PolicyResearcher(),
            "cost_estimator": CostEstimator(),
        }
        if not set(self.agents) <= set(ROLE_SCOPES):
            raise ValueError("Only registered specialist roles may be dispatched")
        self.tool_timeout = tool_timeout
        self.agent_timeout = agent_timeout

    @staticmethod
    def _route(fields: dict[str, Any]) -> list[str]:
        """Route on extracted state, not on model opinion."""
        roles = ["order_reconciler"]
        if fields.get("order_ids"):
            roles.extend(["policy_researcher", "cost_estimator"])
        return roles

    async def collaborate(self, principal: Principal, message: str) -> Proposal:
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise DomainError("invalid_message", "任务描述需要 1–4000 个字符。")
        started = time.perf_counter()
        extraction = await self.extractor.extract(message.strip())
        fields = extraction.fields.model_dump(exclude_none=True)
        roles = self._route(fields)
        gateway = ToolGateway(self.service, principal, timeout=self.tool_timeout)
        boxes = {role: ScopedToolbox(gateway, role, ROLE_SCOPES[role]) for role in roles}
        board = Blackboard()
        task = AgentTask(message=message.strip(), fields=fields)

        async def dispatch(role: str) -> None:
            try:
                finding, payload = await asyncio.wait_for(self.agents[role].run(boxes[role], task, board),
                                                          timeout=self.agent_timeout)
            except DomainError as exc:
                finding, payload = Finding(agent=role, conclusion=f"角色未能完成：{exc.message}", ok=False,
                                           error_code=exc.code, tool_calls=len(boxes[role].calls)), {"error_code": exc.code}
            except (ModelError, TimeoutError, asyncio.TimeoutError):
                finding, payload = Finding(agent=role, conclusion="角色执行超时或模型不可用，本次不产出结论。",
                                           ok=False, error_code="agent_unavailable",
                                           tool_calls=len(boxes[role].calls)), {}
            except Exception:  # A specialist must never break the supervisor contract.
                finding, payload = Finding(agent=role, conclusion="角色执行失败，本次不产出结论。", ok=False,
                                           error_code="agent_failed", tool_calls=len(boxes[role].calls)), {}
            board.record(finding, payload)

        # Stage one establishes the order scope; stage two consumes it in parallel.
        await dispatch(roles[0])
        if len(roles) > 1:
            await asyncio.gather(*(dispatch(role) for role in roles[1:]))

        findings = [board.findings[role] for role in roles]
        handoff = board.require("order_reconciler")
        researcher = board.require("policy_researcher")
        estimator = board.require("cost_estimator")
        conflicts = self._conflicts(findings, handoff, researcher, estimator, fields)
        blocked = any(conflict.severity == "blocking" for conflict in conflicts)
        calculation = estimator.get("calculation")
        proposal = Proposal(
            mode=extraction.mode if self.mode == "fixture" else self.mode,
            model_used=extraction.model_used,
            task_fields=fields,
            roles=roles,
            findings=findings,
            conflicts=conflicts,
            orders=handoff.get("views") or [],
            candidate_policies=researcher.get("rows") or [],
            calculation=calculation,
            next_action=("请补充或修正上面标注的问题后重新分析；本层不会创建草稿。" if blocked else
                         "分析结论仅供核对；创建、确认与提交仍在工作台的人工确认流程中完成。"),
            blocked=blocked,
            tool_calls=sum(len(box.calls) for box in boxes.values()),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        proposal.content_digest = hashlib.sha256(canonical({
            "roles": roles,
            "task_fields": fields,
            "findings": [{"agent": item.agent, "ok": item.ok, "conclusion": item.conclusion,
                          "evidence": item.evidence, "error_code": item.error_code} for item in findings],
            "conflicts": [conflict.model_dump() for conflict in conflicts],
            "orders": proposal.orders,
            "candidate_policies": proposal.candidate_policies,
            "calculation": calculation,
            "blocked": blocked,
        }).encode("utf-8")).hexdigest()
        return proposal

    @staticmethod
    def _conflicts(findings: list[Finding], handoff: dict[str, Any], researcher: dict[str, Any],
                   estimator: dict[str, Any], fields: dict[str, Any]) -> list[Conflict]:
        conflicts: list[Conflict] = []
        if not fields.get("order_ids"):
            conflicts.append(Conflict(code="missing_order_ids", severity="blocking", agents=["order_reconciler"],
                                      detail="输入没有给出订单 ID；金额只能来自业务数据库，需要用户补充。"))
        for finding in findings:
            if not finding.ok:
                code = finding.error_code or "agent_failed"
                conflicts.append(Conflict(code=code, severity="blocking" if code in _BLOCKING else "warning",
                                          agents=[finding.agent], detail=finding.conclusion))
        unavailable = handoff.get("unavailable") or []
        if unavailable:
            conflicts.append(Conflict(code="order_preview_rejected", severity="blocking", agents=["order_reconciler"],
                                      detail="以下订单未通过预检或不属于当前员工：" + "、".join(unavailable[:10])))
        for gap in researcher.get("gaps") or []:
            conflicts.append(Conflict(code="policy_gap", severity="blocking", agents=["policy_researcher"],
                                      detail=f"{gap['trip_date']} 的 {gap['kind']} 没有适用条款。"))
        for overlap in researcher.get("overlaps") or []:
            conflicts.append(Conflict(code="policy_version_overlap", severity="blocking", agents=["policy_researcher"],
                                      detail=f"条款 {overlap['clause_id']} 在 {overlap['trip_date']} 同时存在多个生效版本：{'、'.join(overlap['versions'])}。"))
        calculation = estimator.get("calculation")
        if calculation is not None:
            reconciled = len(handoff.get("usable") or [])
            calculated = len(calculation.get("items") or [])
            if reconciled and reconciled != calculated:
                conflicts.append(Conflict(code="agent_disagreement", agents=["order_reconciler", "cost_estimator"],
                                          detail=f"订单预检通过 {reconciled} 笔，业务核算采用 {calculated} 笔，以业务服务结果为准。"))
        # Only add a generic explanation when the specialist did not already report
        # the same root cause; two conflicts for one cause is noise, not evidence.
        reported = {conflict.code for conflict in conflicts}
        if calculation is None and estimator.get("error_code") in {"missing_cost_center", "insufficient_evidence", "order_ineligible"}:
            detail = "确定成本中心或补充有效订单后，才能给出可核对金额。"
            if estimator.get("error_code") == "insufficient_evidence":
                detail = "所选订单的日期和城市没有可适用制度，无法给出可核对金额。"
            if estimator["error_code"] not in reported:
                conflicts.append(Conflict(code=estimator["error_code"], severity="blocking",
                                          agents=["cost_estimator"], detail=detail))
        unique: list[Conflict] = []
        seen: set[tuple[str, str]] = set()
        for conflict in conflicts:
            key = (conflict.code, conflict.detail)
            if key not in seen:
                seen.add(key)
                unique.append(conflict)
        return unique
