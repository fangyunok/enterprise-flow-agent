"""Command-line entry points for reproducible EnterpriseFlow demonstrations."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from . import __version__
from .resources import DEFAULT_DB_PATH, checkpoint_path, data_path


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _service(path: Path):
    from .service import EnterpriseService
    service = EnterpriseService(path)
    service.seed_demo()
    return service


async def _demo(path: Path, mode: str) -> dict:
    from .workflow import WorkflowEngine
    service = _service(path)
    principal = service.authenticate_demo("alice")
    fields = {
        "order_ids": ["alice-hotel-001", "alice-train-001"],
        "start_date": "2026-10-09",
        "end_date": "2026-10-11",
        "destination": "广州",
        "cost_center": "CC-ALPHA-OPS",
    }
    engine = await WorkflowEngine.open(service, checkpoint_path(path), mode=mode)
    try:
        waiting = await engine.start(principal, json.dumps(fields, ensure_ascii=False))
    finally:
        await engine.aclose()
    engine = await WorkflowEngine.open(service, checkpoint_path(path), mode=mode)
    try:
        restored = await engine.get(principal, waiting["run_id"])
        draft = restored.get("draft")
        if not isinstance(draft, dict):
            raise RuntimeError("Demo did not reach an approval checkpoint")
        decision = {
            "action": "approve",
            "expected_version": draft["version"],
            "expected_hash": draft["content_hash"],
        }
        submitted = await engine.resume(principal, waiting["run_id"], decision)
        repeated = await engine.resume(principal, waiting["run_id"], decision)
    finally:
        await engine.aclose()
    submission = submitted.get("submission")
    duplicate = repeated.get("submission")
    if submitted["status"] != "submitted" or not isinstance(submission, dict) or not submission.get("submission_id"):
        raise RuntimeError("Demo did not complete submission")
    restored_ok = restored.get("pending") == waiting.get("pending") and restored["run_id"] == waiting["run_id"]
    repeated_ok = isinstance(duplicate, dict) and duplicate.get("submission_id") == submission["submission_id"]
    if not restored_ok or not repeated_ok:
        raise RuntimeError("Demo checkpoint recovery or repeated submission failed")
    return {
        "notice": "Bundled enterprise dataset; fixture mode runs without a model service; submissions create application records.",
        "mode": mode,
        "model_used": submitted["model_used"],
        "run_id": waiting["run_id"],
        "checkpoint_restored": restored_ok,
        "waiting_status": waiting["status"],
        "final_status": submitted["status"],
        "total_cents": draft["total_cents"],
        "eligible_cents": draft["eligible_cents"],
        "excess_cents": draft["excess_cents"],
        "submission_id": submission["submission_id"],
        "repeated_resume_same_submission": repeated_ok,
        "policy_sources": [{"policy_id": row["policy_id"], "clause_id": row["clause_id"], "version": row["version"]} for row in draft["sources"]],
        "tool_events": submitted["tool_events"],
    }


DEMO_MESSAGE = ("我于 2026-10-09 至 2026-10-11 去广州出差，请处理 alice-hotel-001 和 alice-train-001，"
                "成本中心 CC-ALPHA-OPS。")


async def _plan(path: Path, mode: str, message: str, max_steps: int) -> dict:
    from .planner import Planner
    service = _service(path)
    principal = service.authenticate_demo("alice")
    result = await Planner(service, mode=mode, max_steps=max_steps).plan(principal, message)
    return {"notice": "只读规划：只调用查询与核算工具，不创建草稿、不确认、不提交。",
            "plan": result.model_dump()}


async def _collaborate(path: Path, mode: str, message: str, user: str) -> dict:
    from .agents import Supervisor
    service = _service(path)
    principal = service.authenticate_demo(user)
    proposal = await Supervisor(service, mode=mode).collaborate(principal, message)
    drafts = len(service.list_drafts(principal))
    return {"notice": "多角色只读分析：结论仅供核对，创建与提交仍在人工确认流程中完成。",
            "proposal": proposal.model_dump(), "draft_count_after_analysis": drafts}


async def _doctor(mode: str, timeout: int) -> dict:
    import httpx
    from .model import create_extractor
    seed = json.loads(data_path("demo_seed.json").read_text(encoding="utf-8"))
    report = {"version": __version__, "mode": mode, "sample_ready": bool(seed.get("users")), "model_used": False}
    if mode == "fixture":
        report.update(ready=report["sample_ready"], model_status="not_required")
        return report
    try:
        extractor = create_extractor(mode)
        headers = {"Authorization": f"Bearer {extractor.api_key}"} if extractor.api_key else {}
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            response = await client.get(extractor.base_url + "/models", headers=headers)
            response.raise_for_status()
            body = response.json()
        catalog = body.get("data") if isinstance(body, dict) else None
        if not isinstance(catalog, list):
            raise ValueError("Invalid model catalog")
        found = any(isinstance(item, dict) and item.get("id") == extractor.model for item in catalog)
        report.update(ready=report["sample_ready"] and found, model_status="catalog_contains_model" if found else "model_not_listed", catalog_check_only=True)
    except httpx.TimeoutException:
        report.update(ready=False, model_status="timeout", catalog_check_only=True)
    except httpx.HTTPStatusError as exc:
        report.update(ready=False, model_status=f"http_{exc.response.status_code}", catalog_check_only=True)
    except (httpx.HTTPError, ValueError):
        report.update(ready=False, model_status="unavailable_or_invalid_configuration", catalog_check_only=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="enterprise-flow", description="EnterpriseFlow 企业业务流程编排与执行平台")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="Initialize the bundled enterprise dataset")
    seed.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    demo = commands.add_parser("demo", help="Exercise checkpoint recovery and idempotent submission")
    demo.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    demo.add_argument("--mode", choices=["fixture", "qwen", "api"], default="fixture")
    serve = commands.add_parser("serve", help="Start the local demo web application")
    serve.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    serve.add_argument("--mode", choices=["fixture", "qwen", "api"], default="fixture")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, choices=range(1, 65536), metavar="PORT", default=7861)
    serve_api = commands.add_parser("serve-api", help="Start the FastAPI service with an OpenAPI contract")
    serve_api.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    serve_api.add_argument("--mode", choices=["fixture", "qwen", "api"], default="fixture")
    serve_api.add_argument("--host", default="127.0.0.1")
    serve_api.add_argument("--port", type=int, choices=range(1, 65536), metavar="PORT", default=8000)
    serve_api.add_argument("--no-seed", action="store_true", help="skip loading the bundled dataset")
    plan = commands.add_parser("plan", help="Run the bounded read-only ReAct planning loop")
    plan.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    plan.add_argument("--mode", choices=["fixture", "qwen", "api"], default="fixture")
    plan.add_argument("--message", default=DEMO_MESSAGE)
    plan.add_argument("--max-steps", type=int, choices=range(1, 21), metavar="STEPS", default=6)
    collaborate = commands.add_parser("collaborate", help="Run the supervisor multi-agent analysis")
    collaborate.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    collaborate.add_argument("--mode", choices=["fixture", "qwen", "api"], default="fixture")
    collaborate.add_argument("--message", default=DEMO_MESSAGE)
    collaborate.add_argument("--user", default="alice")
    doctor = commands.add_parser("doctor", help="Check bundled fixtures and optional model catalog")
    doctor.add_argument("--mode", choices=["fixture", "qwen", "api"], default="fixture")
    doctor.add_argument("--timeout", type=int, choices=range(1, 31), metavar="SECONDS", default=5)
    args = parser.parse_args(argv)
    try:
        if args.command == "seed":
            service = _service(args.db)
            _print({"database": str(args.db.resolve()), "demo_user_count": len(service.list_demo_users()), "synthetic_data": True})
        elif args.command == "demo":
            _print(asyncio.run(_demo(args.db, args.mode)))
        elif args.command == "plan":
            _print(asyncio.run(_plan(args.db, args.mode, args.message, args.max_steps)))
        elif args.command == "collaborate":
            _print(asyncio.run(_collaborate(args.db, args.mode, args.message, args.user)))
        elif args.command == "serve":
            import uvicorn
            from .webapp import create_app
            uvicorn.run(create_app(database_path=args.db, model_mode=args.mode), host=args.host, port=args.port)
        elif args.command == "serve-api":
            try:
                import uvicorn
                from .api import ServiceSettings, create_api
            except ImportError as exc:
                raise RuntimeError("serve-api needs the api extra: pip install '.[api]'") from exc
            uvicorn.run(
                create_api(ServiceSettings(database_path=args.db, model_mode=args.mode, seed=not args.no_seed)),
                host=args.host, port=args.port)
        elif args.command == "doctor":
            report = asyncio.run(_doctor(args.mode, args.timeout))
            _print(report)
            return 0 if report["ready"] else 2
    except (ValueError, RuntimeError, OSError) as exc:
        # Domain/model errors are intentionally safe; never print credentials or provider bodies.
        from .service import DomainError
        from .model import ModelError
        message = str(exc) if isinstance(exc, (DomainError, ModelError)) else "Operation failed; check local configuration and the workflow status."
        _print({"status": "error", "message": message})
        return 2
    return 0
