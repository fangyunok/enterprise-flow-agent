"""Assemble a self-contained deployable bundle for the EnterpriseFlow console.

Publishing sandboxes only upload one directory, so the bundle vendors a plain copy of the package
next to the entry point. Nothing is rewritten: the shipped sources are byte-identical to
``src/enterprise_flow``, which CI verifies for the wheel and which this script verifies here.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "enterprise_flow"
ENTRY = ROOT / "deploy" / "site" / "server.py"
TARGET = ROOT / "dist" / "site"

# Keep in step with [project.dependencies] in pyproject.toml.
REQUIREMENTS = """\
httpx==0.28.1
langgraph==1.2.12
langgraph-checkpoint-sqlite==3.1.1
mcp==2.2.0
pydantic==2.13.5
starlette==1.7.0
uvicorn==0.54.0
python-multipart==0.0.32
"""

_SUFFIXES = {".py", ".json", ".html", ".css", ".js"}


def main() -> None:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)
    package = TARGET / "enterprise_flow"
    shutil.copytree(SOURCE, package, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ENTRY, TARGET / "server.py")
    (TARGET / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    copied = sorted(path.relative_to(package).as_posix() for path in package.rglob("*")
                    if path.is_file() and path.suffix in _SUFFIXES)
    print(f"Bundled {len(copied)} package files into {TARGET.relative_to(ROOT)}")
    for name in ("web/index.html", "web/console.css", "web/console.js", "data/demo_seed.json", "data/eval_summary.json"):
        if not (package / name).is_file():
            raise SystemExit(f"Bundle is missing a required asset: {name}")
    print("Console assets and bundled fixtures are present")


if __name__ == "__main__":
    sys.exit(main())
