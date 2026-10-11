"""Local EnterpriseFlow console with signed demo identity and guarded JSON APIs.

The identity picker switches between the built-in principals used for permission-isolation checks.
All resource access still passes a Principal reconstructed by the server.

The console markup, stylesheet and behaviour live in ``enterprise_flow/web/`` and are served as
package assets rather than being embedded in this module, so the same files ship in the wheel and
in the deployed demo bundle.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .resources import web_path
from .service import DomainError, EnterpriseService

_COOKIE = "enterprise_flow_demo"
_SESSION_SECONDS = 8 * 60 * 60
_MAX_BODY = 32768
_DRAFT_FIELDS = {"order_ids", "cost_center", "notes", "start_date", "end_date", "destination"}

_MODE_LABELS = {"fixture": "固定业务工作流（不调用大模型）", "qwen": "Qwen 模型字段提取", "api": "模型 API 字段提取"}

# Orchestration stages in execution order; the console maps a run's stage onto this list.
STAGE_ORDER = ["created", "extract", "collect", "evaluate", "draft", "approval", "confirm", "submit", "submitted"]
STAGE_MESSAGES = {
    "created": "任务已创建，等待执行。",
    "extract": "提取用户明确提供的受限字段。",
    "collect": "补全成本中心等字段，缺失则持久化暂停。",
    "evaluate": "按租户、部门与生效日检索制度并确定性核算。",
    "draft": "生成版本化草稿并绑定内容摘要。",
    "approval": "等待人工确认：任务已持久化暂停。",
    "confirm": "把人工决定绑定到版本与内容摘要。",
    "submit": "事务提交，唯一约束与幂等键生效。",
    "submitted": "已生成申请编号，重复提交返回同一编号。",
    "cancelled": "任务已取消，没有产生业务写入。",
}

_ASSET_TYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}


class _HTTPProblem(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        self.code, self.message, self.status_code = code, message, status_code


class SameOriginMiddleware:
    """Reject cross-origin browser mutations; allow local CLI requests."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] in {"POST", "PATCH", "PUT", "DELETE"}:
            request = Request(scope)
            allowed = request.headers.get("sec-fetch-site", "").lower() not in {"cross-site", "same-site"}
            origin = request.headers.get("origin")
            if origin is not None:
                try:
                    source, target = urlsplit(origin), urlsplit(str(request.url))
                    def port(value):
                        return value.port or (443 if value.scheme == "https" else 80)
                    allowed = allowed and source.scheme in {"http", "https"}
                    allowed = allowed and not source.username and not source.password
                    allowed = allowed and not source.path and not source.query and not source.fragment
                    allowed = allowed and (source.scheme, source.hostname, port(source)) == (target.scheme, target.hostname, port(target))
                except ValueError:
                    allowed = False
            if not allowed:
                await JSONResponse({"error": "origin_rejected", "message": "请从本机工作台提交请求。"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _SessionSigner:
    def __init__(self, secret: bytes) -> None:
        self.secret = secret

    def sign(self, user_id: str) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"user_id": user_id, "expires": int(time.time()) + _SESSION_SECONDS}, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
        return payload + "." + signature

    def read(self, token: str | None) -> str:
        if not token or len(token) > 2048:
            raise _HTTPProblem("authentication_required", "请先选择员工身份。", 401)
        try:
            payload, supplied = token.split(".", 1)
            expected = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("signature")
            decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not isinstance(decoded, dict) or not isinstance(decoded.get("user_id"), str):
                raise ValueError("payload")
            expires = decoded.get("expires")
            if type(expires) is not int or expires <= time.time():
                raise ValueError("expiry")
            return decoded["user_id"]
        except (ValueError, UnicodeError, TypeError, json.JSONDecodeError) as exc:
            raise _HTTPProblem("authentication_required", "登录会话已失效，请重新选择员工。", 401) from exc


async def _json_body(request: Request, *, optional: bool = False) -> dict[str, Any]:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) < 0 or int(length) > _MAX_BODY:
                raise _HTTPProblem("body_too_large", "请求内容过大。", 413)
        except ValueError as exc:
            raise _HTTPProblem("invalid_body", "请求长度不合法。") from exc
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > _MAX_BODY:
            raise _HTTPProblem("body_too_large", "请求内容过大。", 413)
    if optional and not received:
        return {}
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise _HTTPProblem("unsupported_media_type", "请使用 JSON 请求。", 415)
    try:
        result = json.loads(received)
    except (ValueError, UnicodeError) as exc:
        raise _HTTPProblem("invalid_json", "JSON 格式不正确。") from exc
    if not isinstance(result, dict):
        raise _HTTPProblem("invalid_body", "请求必须为 JSON 对象。")
    return result


def _fields(payload: dict, allowed: set[str], required: set[str] | None = None) -> None:
    if set(payload) - allowed:
        raise _HTTPProblem("unexpected_fields", "请求包含不支持的字段；身份由服务端会话确定。")
    if required and required - set(payload):
        raise _HTTPProblem("missing_fields", "请求缺少必要字段。")


def _version(payload: dict) -> int:
    value = payload.get("expected_version")
    if type(value) is not int or value < 1:
        raise _HTTPProblem("invalid_version", "请提供当前草稿版本。")
    return value


def _string(payload: dict, name: str, maximum: int = 4000) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _HTTPProblem("invalid_field", f"字段 {name} 不能为空或超过长度限制。")
    return value.strip()


def _encode(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _response(value, status_code: int = 200) -> JSONResponse:
    normalized = json.loads(json.dumps(value, ensure_ascii=False, default=_encode))
    return JSONResponse(normalized, status_code=status_code, headers={"cache-control": "no-store", "x-content-type-options": "nosniff"})


_NO_CACHE = {"cache-control": "no-store", "x-content-type-options": "nosniff"}

_CAPABILITY = [
    ("流程编排", "真实 LangGraph 状态图，字段补充与审批使用 interrupt / Command 暂停恢复"),
    ("持久化恢复", "AsyncSqliteSaver 文件检查点，业务库与检查点库分离"),
    ("MCP 集成", "官方 SDK 内存传输；工具服务器绑定服务端身份，不接受模型传入身份"),
    ("制度检索", "SQL 先按租户、部门与生效日过滤再排序；关键词 / 向量召回 / 交叉编码器精排，融合用 RRF"),
    ("制度问答", "MCP 检索后读取模型 JSON 答复，逐条校验来源 ID 与原文片段"),
    ("费用核算", "金额使用整数分；住宿按晚拆分并选择当日制度；证据缺失时阻止办理"),
    ("版本确认", "草稿版本与 SHA256 内容摘要绑定；编辑草稿或来源变化后旧确认失效"),
    ("事务提交", "写事务、唯一约束与幂等键；重复提交返回同一申请编号"),
    ("只读规划", "有界 ReAct：步数 / 上下文字符 / 循环签名三重限制；写入工具执行前被拦下"),
    ("多角色协作", "Supervisor + 三个最小权限角色，黑板交接，结论交叉检查并按 blocking / warning 分级"),
    ("可观测性", "运行级 trace、阶段耗时、token 成本核算，指标以 Prometheus 文本暴露"),
    ("流式进度", "运行阶段以 SSE 推送，页面不轮询"),
    ("质量门禁", "检索与抽取评测与线上同源，可作为 CI 阈值拦住质量退化"),
]

_BOUNDARY = [
    ("身份与租户", "服务端", "MCP 工具服务器绑定服务端身份，参数中不含员工或租户字段"),
    ("金额与制度适用", "确定性业务层", "整数分核算，逐日选择条款；冲突或证据缺失时阻止办理"),
    ("提交许可", "人工确认", "只有绑定到版本与内容摘要的人工确认才能进入提交"),
    ("模型可输出字段", "受限白名单", "订单 ID、成本中心、日期、目的地、备注；越权字段被 Schema 拒绝"),
    ("规划层工具", "只读白名单", "创建、确认与提交工具不在规划层可见集合内"),
]


def create_app(database_path: Path | str | None = None, model_mode: str = "fixture") -> Starlette:
    if model_mode not in {"fixture", "qwen", "api"}:
        raise ValueError("model_mode must be fixture, qwen, or api")
    database_path = Path(database_path or os.environ.get("ENTERPRISEFLOW_DB", "runs/enterprise.db"))
    service = EnterpriseService(database_path)
    service.seed_demo()
    signer = _SessionSigner(secrets.token_bytes(32))
    engine_lock = asyncio.Lock()

    async def engine():
        async with engine_lock:
            if app.state.workflow_engine is None:
                from .workflow import WorkflowEngine
                app.state.workflow_engine = await WorkflowEngine.open(service, database_path.with_name(database_path.stem + "-checkpoints.sqlite"), mode=model_mode)
            return app.state.workflow_engine

    def principal(request: Request):
        return service.authenticate_demo(signer.read(request.cookies.get(_COOKIE)))

    def optional_principal(request: Request):
        """Console bootstraps before a login; anonymous views must not fail."""
        try:
            return service.authenticate_demo(signer.read(request.cookies.get(_COOKIE)))
        except (DomainError, _HTTPProblem):
            return None

    @asynccontextmanager
    async def lifespan(application):
        yield
        if application.state.workflow_engine is not None:
            await application.state.workflow_engine.aclose()

    def _asset(name: str) -> Response:
        path = web_path(name)
        return Response(path.read_bytes(), media_type=_ASSET_TYPES.get(path.suffix, "application/octet-stream"), headers={**_NO_CACHE, "referrer-policy": "same-origin"})

    async def home(request):
        page = web_path("index.html").read_text(encoding="utf-8")
        return HTMLResponse(page.replace("__MODE_LABEL__", html.escape(_MODE_LABELS[model_mode])),
                            headers={**_NO_CACHE, "referrer-policy": "same-origin"})

    async def css(request):
        return _asset("console.css")

    async def js(request):
        return _asset("console.js")

    async def health(request):
        database = "ok"
        try:
            with service.database.transaction() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception:
            database = "unavailable"
        return _response({"status": "ok" if database == "ok" else "degraded", "service": "EnterpriseFlow",
                          "mode": model_mode, "model_validated": False, "demo": True, "database": database,
                          "retrieval_mode": service.retrieval_mode,
                          "workflow_engine": "ready" if app.state.workflow_engine is not None else "lazy"})

    async def overview(request):
        identity = optional_principal(request)
        kpi = service.dashboard(identity) if identity else {"runs": None, "drafts": None, "submissions": None, "pending_human": None}
        return _response({
            "mode": model_mode,
            "mode_label": _MODE_LABELS[model_mode],
            "kpi": kpi,
            "pipeline": [{"name": name, "state": "done" if index < 4 else "", "note": note}
                         for index, (name, note) in enumerate([
                             ("受限字段提取", "模型只输出白名单字段"),
                             ("制度检索", "权限过滤后排序"),
                             ("确定核算", "整数分与按晚拆分"),
                             ("人工确认", "interrupt 持久化暂停"),
                             ("事务提交", "唯一约束与幂等键"),
                         ])],
            "boundary": [{"title": title, "owner": owner, "detail": detail} for title, owner, detail in _BOUNDARY],
            "capability": [{"name": name, "detail": detail} for name, detail in _CAPABILITY],
        })

    async def contract(request):
        return _response({
            "endpoints": [
                {"method": "GET", "path": "/health", "tag": "ops", "summary": "进程与依赖就绪检查"},
                {"method": "GET", "path": "/api/overview", "tag": "ops", "summary": "控制台总览计数与能力矩阵"},
                {"method": "GET", "path": "/api/orders", "tag": "business", "summary": "按服务端身份过滤的本人订单"},
                {"method": "GET", "path": "/api/policies", "tag": "retrieval", "summary": "权限过滤后的制度检索"},
                {"method": "POST", "path": "/api/policy-answers", "tag": "retrieval", "summary": "带引用核验的只读制度问答"},
                {"method": "GET/POST", "path": "/api/drafts", "tag": "business", "summary": "版本化草稿的列表与幂等创建"},
                {"method": "PATCH", "path": "/api/drafts/{draft_id}", "tag": "business", "summary": "按期望版本编辑草稿"},
                {"method": "POST", "path": "/api/drafts/{draft_id}/confirm", "tag": "approval", "summary": "绑定版本与内容摘要的确认"},
                {"method": "POST", "path": "/api/drafts/{draft_id}/submit", "tag": "approval", "summary": "幂等事务提交"},
                {"method": "POST", "path": "/api/workflows", "tag": "workflow", "summary": "启动 LangGraph 运行"},
                {"method": "GET", "path": "/api/runs/{run_id}/events", "tag": "workflow", "summary": "运行阶段的 SSE 事件流"},
                {"method": "POST", "path": "/api/plan", "tag": "agent", "summary": "有界只读 ReAct 规划轨迹"},
                {"method": "POST", "path": "/api/agent-proposals", "tag": "agent", "summary": "多角色只读分析与交叉检查"},
                {"method": "GET", "path": "/api/audit", "tag": "ops", "summary": "当前身份的审计事件流"},
                {"method": "GET", "path": "/api/metrics", "tag": "ops", "summary": "Prometheus 文本指标"},
            ],
            "health": {"status": "ok", "mode": model_mode, "database": "ok", "retrieval_mode": service.retrieval_mode,
                       "workflow_engine": "ready" if app.state.workflow_engine is not None else "lazy"},
        })

    async def metrics(request):
        from .observability import METRICS
        return Response(METRICS.render(), media_type="text/plain; version=0.0.4; charset=utf-8", headers=_NO_CACHE)

    async def evaluation(request):
        from .resources import data_path
        try:
            summary = json.loads(data_path("eval_summary.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            summary = {"sources": [], "retrieval": [], "model": [], "planner": []}
        return _response(summary)

    async def audit(request):
        return _response(service.list_events(principal(request)))

    async def runs(request):
        return _response(service.list_runs(principal(request)))

    async def trace(request):
        identity = principal(request)
        records = service.list_runs(identity)
        for record in records:
            if record.get("trace"):
                return _response({"run_id": record["run_id"], "summary": record["trace"]})
        return _response({"run_id": records[0]["run_id"] if records else None, "summary": {"spans": [], "stage_ms": {}, "model_usage": {}}})

    async def run_events(request):
        """Server-sent run progress. The browser subscribes instead of polling."""
        identity = principal(request)
        run_id = request.path_params["run_id"]
        manager = await engine()

        async def stream():
            previous: dict[str, Any] | None = None
            for _ in range(600):
                try:
                    record = await manager.get(identity, run_id)
                except DomainError as exc:
                    yield "data: " + json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False) + "\n\n"
                    return
                stage = record.get("stage") or "created"
                status = record.get("status") or ""
                terminal = status in {"submitted", "cancelled", "failed"} or not record.get("pending")
                payload = {"run_id": run_id, "stage": stage, "status": status,
                           "stage_index": STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0,
                           "message": STAGE_MESSAGES.get(stage, stage), "terminal": terminal}
                if payload != previous:
                    yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                    previous = payload
                if terminal:
                    return
                await asyncio.sleep(1.0)
            yield "data: " + json.dumps({"run_id": run_id, "message": "订阅超时，请刷新任务状态。", "terminal": True}, ensure_ascii=False) + "\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                headers={**_NO_CACHE, "x-accel-buffering": "no", "connection": "keep-alive"})

    async def demo_users(request):
        return _response(service.list_demo_users())

    async def login(request):
        payload = await _json_body(request)
        _fields(payload, {"user_id"}, {"user_id"})
        identity = service.authenticate_demo(_string(payload, "user_id", 128))
        response = _response(identity)
        response.set_cookie(_COOKIE, signer.sign(identity.user_id), max_age=_SESSION_SECONDS, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
        return response

    async def logout(request):
        principal(request)
        await _json_body(request, optional=True)
        response = _response({"logged_out": True})
        response.delete_cookie(_COOKIE, path="/", httponly=True, samesite="strict")
        return response

    async def me(request):
        return _response(principal(request))

    async def policies(request):
        return _response(service.search_policies(principal(request), query=request.query_params.get("query", ""), trip_date=request.query_params.get("trip_date", "2026-10-09")))

    async def my_orders(request):
        return _response(service.list_orders(principal(request), start_date=request.query_params.get("start_date"), end_date=request.query_params.get("end_date")))

    async def policy_answer(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"question", "trip_date"}, {"question", "trip_date"})
        from .policy_qa import PolicyQA
        result = await PolicyQA(service, mode=model_mode).answer(identity, _string(payload, "question", 500), _string(payload, "trip_date", 10))
        return _response(result)

    async def cost_centers(request):
        return _response(service.list_cost_centers(principal(request)))

    async def plan(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"message", "max_steps"}, {"message"})
        steps = payload.get("max_steps", 6)
        if type(steps) is not int or not 1 <= steps <= 20:
            raise _HTTPProblem("invalid_field", "max_steps 需要 1–20 的整数。")
        from .planner import Planner
        result = await Planner(service, mode=model_mode, max_steps=steps).plan(identity, _string(payload, "message"))
        return _response(result)

    async def agent_proposals(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"message"}, {"message"})
        from .agents import Supervisor
        proposal = await Supervisor(service, mode=model_mode).collaborate(identity, _string(payload, "message"))
        return _response(proposal)

    async def drafts(request):
        identity = principal(request)
        if request.method == "GET":
            return _response(service.list_drafts(identity))
        payload = await _json_body(request)
        _fields(payload, _DRAFT_FIELDS, {"order_ids"})
        return _response(service.create_draft(identity, payload), 201)

    async def draft(request):
        identity, draft_id = principal(request), request.path_params["draft_id"]
        if request.method == "GET":
            return _response(service.get_draft(identity, draft_id))
        payload = await _json_body(request)
        _fields(payload, _DRAFT_FIELDS | {"expected_version"}, {"expected_version", "order_ids"})
        version = _version(payload)
        return _response(service.edit_draft(identity, draft_id, {k: v for k, v in payload.items() if k != "expected_version"}, version))

    async def confirm(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"expected_version", "expected_hash"}, {"expected_version", "expected_hash"})
        digest = _string(payload, "expected_hash", 64)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise _HTTPProblem("invalid_hash", "请提供页面显示草稿的内容摘要。")
        return _response(service.confirm_draft(identity, request.path_params["draft_id"], _version(payload), expected_hash=digest))

    async def submit(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"expected_version", "idempotency_key"}, {"expected_version", "idempotency_key"})
        return _response(service.submit_draft(identity, request.path_params["draft_id"], _version(payload), _string(payload, "idempotency_key", 128)))

    async def preferences(request):
        identity = principal(request)
        if request.method == "GET":
            return _response(service.get_preferences(identity))
        payload = await _json_body(request)
        if request.method == "DELETE":
            _fields(payload, {"key"}, {"key"})
            result = service.delete_preference(identity, _string(payload, "key", 64))
            return _response(result if result is not None else {"deleted": True})
        _fields(payload, {"key", "value", "confirmed"}, {"key", "value", "confirmed"})
        if payload["confirmed"] is not True:
            raise _HTTPProblem("confirmation_required", "保存长期偏好需要用户明确确认。")
        return _response(service.set_preference(identity, _string(payload, "key", 64), _string(payload, "value", 128), confirmed=True))

    async def workflow_start(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"message", "request_id"}, {"message"})
        request_id = _string(payload, "request_id", 128) if payload.get("request_id") is not None else None
        manager = await engine()
        return _response(await manager.start(identity, _string(payload, "message"), request_id=request_id), 201)

    async def workflow_get(request):
        identity = principal(request)
        manager = await engine()
        return _response(await manager.get(identity, request.path_params["run_id"]))

    async def workflow_resume(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"decision"}, {"decision"})
        if not isinstance(payload["decision"], dict):
            raise _HTTPProblem("invalid_decision", "继续任务的 decision 必须是对象。")
        manager = await engine()
        return _response(await manager.resume(identity, request.path_params["run_id"], payload["decision"]))

    async def problem(request, exc):
        return _response({"error": exc.code, "message": exc.message}, exc.status_code)

    routes = [
        Route("/", home), Route("/health", health), Route("/console.css", css), Route("/console.js", js),
        Route("/api/demo-users", demo_users),
        Route("/api/login", login, methods=["POST"]), Route("/api/logout", logout, methods=["POST"]),
        Route("/api/me", me), Route("/api/policies", policies), Route("/api/policy-answers", policy_answer, methods=["POST"]), Route("/api/orders", my_orders), Route("/api/cost-centers", cost_centers),
        Route("/api/plan", plan, methods=["POST"]), Route("/api/agent-proposals", agent_proposals, methods=["POST"]),
        Route("/api/drafts", drafts, methods=["GET", "POST"]), Route("/api/drafts/{draft_id}", draft, methods=["GET", "PATCH"]),
        Route("/api/drafts/{draft_id}/confirm", confirm, methods=["POST"]), Route("/api/drafts/{draft_id}/submit", submit, methods=["POST"]),
        Route("/api/preferences", preferences, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/workflows", workflow_start, methods=["POST"]), Route("/api/workflows/{run_id}", workflow_get),
        Route("/api/workflows/{run_id}/resume", workflow_resume, methods=["POST"]),
        Route("/api/runs", runs), Route("/api/runs/{run_id}/events", run_events),
        Route("/api/overview", overview), Route("/api/contract", contract), Route("/api/evaluation", evaluation),
        Route("/api/audit", audit), Route("/api/trace", trace), Route("/api/metrics", metrics),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={_HTTPProblem: problem, DomainError: problem})
    app.state.service, app.state.workflow_engine, app.state.model_mode = service, None, model_mode
    app.add_middleware(SameOriginMiddleware)
    return app
