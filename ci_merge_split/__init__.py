"""Modular boundaries for the remaining CI merge workflow.

These modules are intentionally thin compatibility facades for the first
split pass.  They give the large original ``ci_merge.py`` responsibilities
stable homes while preserving the original behavior during migration.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .commands import CiCommandService
    from .legacy import load_ci_merge_module
    from .message_builder import CommitMessage
    from .messages import CiMessageService
    from .models import (
        CiMergeError,
        CommandResult,
        ConflictAnalysis,
        ExitCode,
        GitCommandError,
        GitlinkMergeContext,
        MergeResult,
        MergeStepContext,
        Phase,
        PreflightResult,
        RefResolution,
    )
    from .publish import CiPublishService
    from .report_artifacts import CiReportArtifactsService
    from .repository_guards import CiRepositoryGuards
    from .review import export_review_record
    from .review_html import generate_html_file
    from .runtime import ExecutionContext, GitRunner, JsonReport
    from .snapshot_restore import CiSnapshotRestoreService
    from .workflow import CiWorkflowServices


_EXPORTS = {
    "CiCommandService": "commands",
    "CiMessageService": "messages",
    "CiMergeError": "models",
    "CommitMessage": "message_builder",
    "CommandResult": "models",
    "ConflictAnalysis": "models",
    "CiPublishService": "publish",
    "CiReportArtifactsService": "report_artifacts",
    "CiRepositoryGuards": "repository_guards",
    "CiSnapshotRestoreService": "snapshot_restore",
    "CiWorkflowServices": "workflow",
    "ExitCode": "models",
    "ExecutionContext": "runtime",
    "GitRunner": "runtime",
    "GitCommandError": "models",
    "GitlinkMergeContext": "models",
    "JsonReport": "runtime",
    "MergeResult": "models",
    "MergeStepContext": "models",
    "Phase": "models",
    "PreflightResult": "models",
    "RefResolution": "models",
    "export_review_record": "review",
    "generate_html_file": "review_html",
    "load_ci_merge_module": "legacy",
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
    "CiMessageService",
    "CiCommandService",
    "CiMergeError",
    "CommitMessage",
    "CommandResult",
    "ConflictAnalysis",
    "CiPublishService",
    "CiReportArtifactsService",
    "CiRepositoryGuards",
    "CiSnapshotRestoreService",
    "CiWorkflowServices",
    "ExitCode",
    "ExecutionContext",
    "GitRunner",
    "GitCommandError",
    "GitlinkMergeContext",
    "JsonReport",
    "MergeResult",
    "MergeStepContext",
    "Phase",
    "PreflightResult",
    "RefResolution",
    "export_review_record",
    "generate_html_file",
    "load_ci_merge_module",
]
