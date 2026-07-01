"""Adapters that use split_gitlink_merge for CI and manual merge workflows."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .ci_auto import CiAutoGitlinkMergeService, CiGitBackend, CiMergeResult
    from .manual_semiauto import ManualMergeSummary, ManualSemiAutoMergeService


_EXPORTS = {
    "CiAutoGitlinkMergeService": "ci_auto",
    "CiGitBackend": "ci_auto",
    "CiMergeResult": "ci_auto",
    "ManualMergeSummary": "manual_semiauto",
    "ManualSemiAutoMergeService": "manual_semiauto",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value

__all__ = [
    "CiAutoGitlinkMergeService",
    "CiGitBackend",
    "CiMergeResult",
    "ManualMergeSummary",
    "ManualSemiAutoMergeService",
]
