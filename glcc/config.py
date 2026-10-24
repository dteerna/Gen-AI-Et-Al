"""Environment-driven settings. Everything has a safe local default."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(os.getenv("GLCC_DATA", ROOT / "data"))


def db_path() -> str:
    return os.getenv("GLCC_DB", "glcc.db")


def load_directory(path: str | Path | None = None) -> dict[str, list[str]]:
    p = Path(path or os.getenv("GLCC_ROLES_FILE", ROOT / "config" / "roles.json"))
    return {k: list(v) for k, v in json.loads(p.read_text(encoding="utf-8"))["directory"].items()}
