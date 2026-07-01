"""Loader for the original CI merge module during incremental splitting."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


DEFAULT_CI_PROJECT = Path(r"D:\Git\MergeTool_for_CICD")


def load_ci_merge_module(project_path: Path | str = DEFAULT_CI_PROJECT) -> ModuleType:
    project = Path(project_path).resolve(strict=False)
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    return importlib.import_module("ci_merge")


def require_functions(module: ModuleType, names: tuple[str, ...]) -> None:
    missing = [name for name in names if not callable(getattr(module, name, None))]
    if missing:
        raise AttributeError(f"ci_merge module is missing required functions: {', '.join(missing)}")

