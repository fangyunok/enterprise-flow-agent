"""Paths that work both in a source checkout and an installed wheel."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGED_DATA_DIR = PACKAGE_DIR / "data"
PROJECT_ROOT = PACKAGE_DIR.parents[1]
DATA_DIR = PROJECT_ROOT / "data" if (PROJECT_ROOT / "pyproject.toml").is_file() else PACKAGED_DATA_DIR
DEFAULT_DB_PATH = Path(os.environ.get("ENTERPRISE_DB_PATH", str(Path.cwd() / "runs" / "enterprise.sqlite"))).expanduser()


def data_path(name: str) -> Path:
    """Return a bundled public fixture, rejecting paths outside the fixture folder."""
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("Fixture name must be a single filename")
    path = DATA_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"Bundled fixture is missing: {name}")
    return path


def checkpoint_path(database_path: str | Path) -> Path:
    """Use a distinct checkpoint database for each business database."""
    path = Path(database_path)
    return path.with_name(f"{path.stem}-checkpoints.sqlite")
