"""Single-port entry point for the published EnterpriseFlow console.

The bundle produced by ``scripts/build_site_bundle.py`` places this file next to a plain copy of
the ``enterprise_flow`` package, so the deployed process drives exactly the same business service,
workflow engine and console assets as a local install — only the storage path differs.

    python server.py            # listens on $PORT (default 8000)
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("ENTERPRISEFLOW_DB", str(BASE_DIR / "data" / "enterprise.sqlite")))
MODE = os.environ.get("ENTERPRISEFLOW_MODE", "fixture")
PORT = int(os.environ.get("PORT", "8000"))


def build():
    from enterprise_flow.webapp import create_app
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    return create_app(database_path=DATABASE, model_mode=MODE)


app = None


def main() -> None:
    import uvicorn
    uvicorn.run(build(), host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
else:  # Some hosts import an ASGI object instead of running a command.
    app = build()
