"""Adapters that use split_gitlink_merge for CI and manual merge workflows."""

from .ci_auto import CiAutoGitlinkMergeService, CiGitBackend, CiMergeResult
from .manual_semiauto import ManualMergeSummary, ManualSemiAutoMergeService

__all__ = [
    "CiAutoGitlinkMergeService",
    "CiGitBackend",
    "CiMergeResult",
    "ManualMergeSummary",
    "ManualSemiAutoMergeService",
]
