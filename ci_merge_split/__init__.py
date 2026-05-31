"""Modular boundaries for the remaining CI merge workflow.

These modules are intentionally thin compatibility facades for the first
split pass.  They give the large original ``ci_merge.py`` responsibilities
stable homes while preserving the original behavior during migration.
"""

from .legacy import load_ci_merge_module
from .commands import CiCommandService
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
from .runtime import ExecutionContext, GitRunner, JsonReport
from .snapshot_restore import CiSnapshotRestoreService
from .workflow import CiWorkflowServices

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
    "load_ci_merge_module",
]
