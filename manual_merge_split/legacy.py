"""Opt-in loader for the original manual recursive submodule merge module."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType


MANUAL_LEGACY_PROJECT_ENVVAR = "MERGETOOL_MANUAL_LEGACY_PROJECT"


def resolve_manual_project_path(project_path: Path | str | None = None) -> Path | None:
    if project_path is not None:
        return Path(project_path).resolve(strict=False)
    configured_path = os.environ.get(MANUAL_LEGACY_PROJECT_ENVVAR)
    if not configured_path:
        return None
    return Path(configured_path).resolve(strict=False)


def load_manual_merge_module(project_path: Path | str | None = None) -> ModuleType:
    project = resolve_manual_project_path(project_path)
    if project is None:
        raise RuntimeError(
            "external recursive_submodule_merge project is not configured; "
            f"set {MANUAL_LEGACY_PROJECT_ENVVAR} or pass project_path explicitly"
        )
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    return importlib.import_module("recursive_submodule_merge")


def require_functions(module: ModuleType, names: tuple[str, ...]) -> None:
    missing = [name for name in names if not callable(getattr(module, name, None))]
    if missing:
        raise AttributeError(f"recursive_submodule_merge module is missing required functions: {', '.join(missing)}")
