"""Loader for the original manual recursive submodule merge module."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


DEFAULT_MANUAL_PROJECT = Path(r"D:\Git\PythonTool")


def load_manual_merge_module(project_path: Path | str = DEFAULT_MANUAL_PROJECT) -> ModuleType:
    project = Path(project_path).resolve(strict=False)
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    return importlib.import_module("recursive_submodule_merge")


def require_functions(module: ModuleType, names: tuple[str, ...]) -> None:
    missing = [name for name in names if not callable(getattr(module, name, None))]
    if missing:
        raise AttributeError(f"recursive_submodule_merge module is missing required functions: {', '.join(missing)}")

