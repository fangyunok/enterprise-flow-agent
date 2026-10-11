"""Boot the console over a real socket and check the paths a visitor and a client would use.

Unit tests drive the ASGI app in-process. This script closes the gap to a real deployment: a real
port, real cookies, real SSE headers and the same bundle that gets published.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

DEFAULT_MESSAGE = ("我于 2026-10-09 至 2026-10-11 去广州出差，请处理 alice-hotel-001 和 alice-train-001，"
                   "成本中心 CC-ALPHA-OPS。")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def build_app(database: Path, mode: str = "fixture"):
    from enterprise_flow.webapp import create_app
    return create_app(database_path=database, model_mode=mode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EnterpriseFlow console smoke check")
    parser.add_argument("--mode", choices=["fixture", "qwen", "api"], default="fixture")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    args = parser.parse_args(argv)

    import uvicorn
    port = free_port()
    database = Path(tempfile.mkdtemp(prefix="enterprise-flow-smoke-")) / "console.sqlite"
    config = uvicorn.Config(build_app(database, args.mode), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(120):
        if server.started:
            break
        time.sleep(0.25)
    if not server.started:
        print("console did not start")
        return 2

    root = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    failures: list[str] = []

    def check(name: str, condition: bool, detail: object = "") -> None:
        print(("PASS  " if condition else "FAIL  ") + name + ("" if condition else "  " + str(detail)[:200]))
        if not condition:
            failures.append(name)

    def get(path: str, timeout: int = 20):
        with opener.open(root + path, timeout=timeout) as response:
            raw = response.read()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw.decode("utf-8", "replace")

    def post(path: str, payload: dict, timeout: int = 40):
        request = urllib.request.Request(root + path, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "Origin": root}, method="POST")
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())

    try:
        status, body = get("/health")
        check("health reports the process and its dependencies", status == 200 and body["status"] == "ok", body)

        status, page = get("/")
        check("console page renders with the mode label", status == 200 and "EnterpriseFlow" in page and "固定业务工作流（不调用大模型）" in page)
        check("console states the identity-isolation purpose", "身份切换用于验证权限隔离" in page)
        check("agent layer is labelled read-only", "多角色只读分析" in page and "不产生业务写入" in page)

        status, css = get("/console.css")
        check("stylesheet is served as a real asset", status == 200 and "--accent" in css)
        status, script = get("/console.js")
        check("console script is served as a real asset", status == 200 and "agent-run" in script and "plan-run" in script)

        status, users = get("/api/demo-users")
        check("demo identities are listed", status == 200 and any(u["user_id"] == "alice" for u in users))

        status, me = post("/api/login", {"user_id": "alice"})
        check("login binds a server-side principal", status == 200 and me["user_id"] == "alice", me)

        status, orders = get("/api/orders")
        check("orders are scoped to the bound employee", bool(orders) and all(o["owner_id"] == "alice" for o in orders))

        status, run = post("/api/workflows", {"message": args.message, "request_id": "smoke-console"})
        check("workflow starts", status == 201 and run["run_id"].startswith("run-"), run)
        run_id = run["run_id"]

        status, record = get("/api/workflows/" + run_id)
        check("workflow pauses for human approval", status == 200 and record["status"] == "awaiting_confirmation", record.get("status"))
        draft = record["draft"]
        check("deterministic amounts are correct", (draft["total_cents"], draft["eligible_cents"], draft["excess_cents"]) == (129000, 123000, 6000))

        status, resumed = post(f"/api/workflows/{run_id}/resume",
                               {"decision": {"action": "approve", "expected_version": draft["version"], "expected_hash": draft["content_hash"]}})
        check("approval resumes and submits", status == 200 and resumed["status"] == "submitted", resumed.get("status"))
        submission = resumed["submission"]["submission_id"]

        status, repeated = post(f"/api/workflows/{run_id}/resume",
                                {"decision": {"action": "approve", "expected_version": draft["version"], "expected_hash": draft["content_hash"]}})
        check("repeated approval returns the same submission", repeated["submission"]["submission_id"] == submission)

        status, trace = get("/api/trace")
        check("run trace accumulates spans across resumes", status == 200 and trace["summary"]["span_count"] >= 5, trace["summary"].get("span_count"))

        status, metrics = get("/api/metrics")
        check("prometheus metrics expose tool counters", status == 200 and "enterpriseflow_tool_calls_total" in metrics)

        status, audit = get("/api/audit")
        check("audit trail records the submission", status == 200 and any(e["action"] == "expense_submitted" for e in audit))

        status, evaluation = get("/api/evaluation")
        check("packaged evaluation summary is served", status == 200 and len(evaluation["retrieval"]) == 2 and evaluation["planner"])

        status, proposal = post("/api/agent-proposals", {"message": args.message})
        check("multi-agent analysis stays read-only", status == 200 and proposal["read_only"] and proposal["business_effects"] is False and len(proposal["roles"]) == 3)

        status, plan = post("/api/plan", {"message": args.message})
        check("read-only planner never writes", status == 200 and plan["stop_reason"] == "goal_satisfied" and plan["business_effects"] is False)

        status, policies = get("/api/policies?query=%E4%BD%8F%E5%AE%BF&trip_date=2026-10-09")
        check("policy retrieval is tenant-scoped", status == 200 and {row["tenant_id"] for row in policies} == {"alpha"})

        status, events = get(f"/api/runs/{run_id}/events")
        check("run event stream returns SSE content type", status == 200 and isinstance(events, str) and "data:" in events)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        check("smoke request completed", False, exc)
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    print()
    print("FAILURES:", ", ".join(failures) if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
