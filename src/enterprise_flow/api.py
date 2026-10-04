"""FastAPI service surface over the existing business layer.

The domain rules, authorization and workflow engine stay exactly where they were — this module only
adds a service contract around them. That means the HTTP API, the CLI and the web UI all drive the
same ``EnterpriseService`` and the same ``WorkflowEngine``.

What this layer adds on top of the web UI:

* an OpenAPI document, so the contract is reviewable instead of implied;
* async run submission, so a caller can start a workflow and poll it instead of holding a socket;
* a health endpoint that reports dependency readiness rather than process liveness;
* per-request tracing through the same tracer the workflow uses.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .model import FixtureExtractor, HttpExtractor, ModelError
from .observability import ExecutionTracer, current_tracer, traced
from .schemas import Principal
from .service import DomainError, EnterpriseService


class ServiceSettings(BaseModel):
    """Runtime configuration, resolved once at startup."""

    database_path: Path
    model_mode: str = "fixture"
    seed: bool = True
    trace_enabled: bool = True


def settings_from_env() -> ServiceSettings:
    return ServiceSettings(
        database_path=Path(os.environ.get("ENTERPRISEFLOW_DB", "runs/enterprise.db")),
        model_mode=os.environ.get("ENTERPRISEFLOW_MODE", "fixture"),
        seed=os.environ.get("ENTERPRISEFLOW_SEED", "1") == "1",
        trace_enabled=os.environ.get("ENTERPRISEFLOW_TRACE", "1") == "1",
    )


class StartRunRequest(BaseModel):
    """Start a workflow run for one of the built-in principals."""

    user_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class RunHandle(BaseModel):
    run_id: str
    status: str
    stage: str


class SubmitDraftRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    fields: dict[str, Any]


class ResumeRunRequest(BaseModel):
    """Continue a paused run: the caller's identity plus the decision the engine expects.

    Field names mirror the engine's ``ResumeDecision`` (``action``/``fields``/``expected_version``/
    ``expected_hash``) so the API surface cannot drift from what the workflow validates.
    """

    user_id: str = Field(min_length=1, max_length=64)
    action: str = Field(default="confirm", min_length=1, max_length=32)
    fields: dict[str, Any] | None = None
    expected_version: int | None = Field(default=None, ge=1)
    expected_hash: str | None = Field(default=None, min_length=64, max_length=64)


class HealthReport(BaseModel):
    status: str
    database: str
    model_mode: str
    retrieval_mode: str
    workflow_engine: str
    trace: str


class APIService:
    """Holds the process-wide singletons the endpoints share."""

    def __init__(self, settings: ServiceSettings) -> None:
        self.settings = settings
        self.service = EnterpriseService(settings.database_path)
        if settings.seed:
            self.service.seed_demo()
        self._engine: Any = None
        self._lock = asyncio.Lock()

    def extractor(self) -> Any:
        if self.settings.model_mode == "fixture":
            return FixtureExtractor()
        return HttpExtractor(mode=self.settings.model_mode)

    async def engine(self) -> Any:
        async with self._lock:
            if self._engine is None:
                from .workflow import WorkflowEngine
                checkpoint = self.settings.database_path.with_name(
                    self.settings.database_path.stem + "-checkpoints.sqlite")
                self._engine = await WorkflowEngine.open(
                    self.service, checkpoint, mode=self.settings.model_mode,
                    extractor=self.extractor())
            return self._engine

    @property
    def engine_ready(self) -> bool:
        return self._engine is not None

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.aclose()
            self._engine = None


def create_api(settings: ServiceSettings | None = None) -> FastAPI:
    settings = settings or settings_from_env()
    state = APIService(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # The service object is created up front and closed on shutdown; routes close over it
        # directly, so they never depend on request-scoped state being populated.
        application.state.api = state
        try:
            yield
        finally:
            await state.aclose()

    application = FastAPI(
        title="EnterpriseFlow API",
        version="0.2.0",
        summary="Scoped retrieval, human-in-the-loop workflow and auditable submission.",
        lifespan=lifespan,
    )

    def api(request: Request) -> APIService:
        # Fall back to the closure value: dependency resolution can run before the lifespan startup.
        try:
            return request.app.state.api
        except AttributeError:
            return state

    def resolve(user_id: str) -> Principal:
        # No try/except: the DomainError handler below turns domain failures into the same envelope
        # for HTTP and engine paths alike, so there is a single error shape for clients.
        return state.service.authenticate_demo(user_id)

    @application.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "message": exc.message})

    @application.exception_handler(ModelError)
    async def model_error(_: Request, exc: ModelError) -> JSONResponse:
        # The message is already sanitized upstream: no URL, no payload, no credentials.
        return JSONResponse(status_code=503, content={"error": "model_unavailable", "message": str(exc)})

    @application.get("/health", response_model=HealthReport, tags=["ops"])
    async def health(service: APIService = Depends(api)) -> HealthReport:
        database = "ok"
        try:
            with service.service.database.transaction() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception:
            database = "unavailable"
        engine = "ready" if service.engine_ready else "lazy"
        return HealthReport(
            status="ok" if database == "ok" else "degraded",
            database=database,
            model_mode=settings.model_mode,
            retrieval_mode=getattr(service.service.retriever, "mode", "keyword"),
            workflow_engine=engine,
            trace="on" if settings.trace_enabled else "off",
        )

    @application.get("/policies", tags=["retrieval"])
    async def policies(user_id: str, query: str = "", trip_date: str = "2026-10-09",
                       service: APIService = Depends(api)) -> dict[str, Any]:
        principal = resolve(user_id)
        with traced("api.policies", "tool", user_id=user_id):
            rows = service.service.search_policies(principal, query, trip_date)
        return {"items": rows, "count": len(rows),
                "retrieval_path": rows[0].get("retrieval_path", "keyword") if rows else "keyword"}

    @application.get("/orders", tags=["business"])
    async def orders(user_id: str, service: APIService = Depends(api)) -> dict[str, Any]:
        return {"items": service.service.list_orders(resolve(user_id))}

    @application.post("/workflows", response_model=RunHandle, status_code=202, tags=["workflow"])
    async def start_run(payload: StartRunRequest, service: APIService = Depends(api)) -> RunHandle:
        principal = resolve(payload.user_id)
        engine = await service.engine()
        with traced("api.workflow.start", "workflow", user_id=payload.user_id):
            outcome = await engine.start(principal, payload.message)
        return RunHandle(run_id=outcome["run_id"], status=outcome["status"], stage=outcome.get("stage", "collect"))

    @application.get("/workflows/{run_id}", tags=["workflow"])
    async def read_run(run_id: str, user_id: str, service: APIService = Depends(api)) -> dict[str, Any]:
        principal = resolve(user_id)
        engine = await service.engine()
        with traced("api.workflow.read", "workflow", run_id=run_id):
            return await engine.get(principal, run_id)

    @application.post("/workflows/{run_id}/resume", response_model=RunHandle, tags=["workflow"])
    async def resume_run(run_id: str, payload: ResumeRunRequest, service: APIService = Depends(api)) -> RunHandle:
        principal = resolve(payload.user_id)
        engine = await service.engine()
        # Only the keys the caller actually supplied reach the engine's decision model, so an
        # omitted optional field stays absent instead of arriving as an explicit null.
        decision = {key: value for key, value in payload.model_dump().items()
                    if key != "user_id" and value is not None}
        with traced("api.workflow.resume", "workflow", run_id=run_id):
            outcome = await engine.resume(principal, run_id, decision)
        return RunHandle(run_id=run_id, status=outcome.get("status", "running"),
                         stage=outcome.get("stage", "collect"))

    @application.get("/traces/{run_id}", tags=["ops"])
    async def trace_of(run_id: str) -> dict[str, Any]:
        tracer: ExecutionTracer | None = current_tracer()
        if tracer is None or tracer.run_id != run_id:
            return {"run_id": run_id, "spans": [], "note": "tracing is per-request; query the events table for a stored run"}
        return tracer.summary()

    return application


app = None


def get_app() -> FastAPI:
    """Lazily build the application so importing this module does not touch the database."""
    global app
    if app is None:
        app = create_api()
    return app
