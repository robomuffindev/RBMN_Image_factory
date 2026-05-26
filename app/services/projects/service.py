"""Project folder + sidecar JSON management."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger

_log = get_logger("factory.projects")


def project_root_dir(project_id: str) -> Path:
    return get_settings().projects_dir / project_id


def project_subdirs(project_id: str) -> dict[str, Path]:
    root = project_root_dir(project_id)
    return {
        "root": root,
        "references": root / "references",
        "outputs": root / "outputs",
        "logs": root / "logs",
    }


def ensure_project_dirs(project_id: str) -> dict[str, Path]:
    dirs = project_subdirs(project_id)
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def delete_project_dir(project_id: str) -> None:
    root = project_root_dir(project_id)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
        _log.info("projects.dir.deleted", project_id=project_id, path=str(root))


def write_sidecar(project_id: str, data: dict[str, Any]) -> Path:
    dirs = ensure_project_dirs(project_id)
    p = dirs["root"] / "project.json"
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return p
