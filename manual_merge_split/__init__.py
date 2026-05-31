"""Modular boundaries for the remaining manual merge workflow."""

from .commands import ManualCommandService
from .cross_submodule_moves import CrossSubmoduleMoveService
from .execution import format_git_failure, run_git, run_git_binary, set_verbose, verbose_print
from .file_conflicts import ManualFileConflictService
from .guided_commands import ManualGuidedCommandService
from .legacy import load_manual_merge_module
from .models import (
    CleanupResult,
    ConflictEntry,
    CrossSubmoduleAddedFile,
    CrossSubmoduleDeletedFile,
    CrossSubmoduleMove,
    DirtyRepoAssessment,
    FileConflictResolution,
    GitBinaryResult,
    GitResult,
    ManualSplitError,
    MergeAutomationError,
    MergeBatchResult,
    MergeContext,
    MergePreflightResult,
    RepoSnapshot,
    SubmoduleConflictInfo,
    TextMergeNormalization,
)
from .preflight import ManualPreflightService
from .repository_ops import ManualRepositoryOps
from .runtime import build_parser, main, parse_args
from .workflow import ManualWorkflowServices

__all__ = [
    "CrossSubmoduleMoveService",
    "CleanupResult",
    "ConflictEntry",
    "CrossSubmoduleAddedFile",
    "CrossSubmoduleDeletedFile",
    "CrossSubmoduleMove",
    "DirtyRepoAssessment",
    "FileConflictResolution",
    "GitBinaryResult",
    "GitResult",
    "MergeAutomationError",
    "ManualSplitError",
    "ManualFileConflictService",
    "ManualGuidedCommandService",
    "ManualCommandService",
    "ManualPreflightService",
    "ManualRepositoryOps",
    "ManualWorkflowServices",
    "MergeBatchResult",
    "MergeContext",
    "MergePreflightResult",
    "RepoSnapshot",
    "SubmoduleConflictInfo",
    "TextMergeNormalization",
    "load_manual_merge_module",
    "build_parser",
    "format_git_failure",
    "main",
    "parse_args",
    "run_git",
    "run_git_binary",
    "set_verbose",
    "verbose_print",
]
