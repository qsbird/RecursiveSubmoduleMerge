"""Split recursive gitlink merge core.

The package separates the recursive submodule gitlink merge problem into:

* model: stable data contracts;
* planner: pure parsing and decision logic;
* logic: pure domain logic that converts conflict data into merge intent;
* ports: execution interfaces consumed by the orchestration layer;
* executor: concrete Git command execution;
* policy: CI/manual behavior hooks over execution ports;
* engine: orchestration that binds those pieces together.
"""

from .engine import RecursiveGitlinkMergeEngine
from .executor import GitActionExecutor, SubprocessGitBackend
from .logic import ConflictGroups, GitlinkConflictSpec, GitlinkMergeLogic
from .model import (
    Ancestry,
    Conflict,
    ConflictStage,
    GitCommandResult,
    GitlinkDecision,
    GitlinkDecisionKind,
    GitlinkMergeContext,
    GitlinkMergeError,
    GitlinkResolution,
    MergeCommitMessage,
    MergeEvent,
    NonGitlinkConflictError,
    RepositoryResolution,
)
from .planner import GitlinkMergePlanner
from .policy import MergePolicy
from .ports import GitBackend, GitExecutionPort, GitMutationPort, GitQueryPort

__all__ = [
    "Ancestry",
    "Conflict",
    "ConflictGroups",
    "ConflictStage",
    "GitActionExecutor",
    "GitBackend",
    "GitCommandResult",
    "GitExecutionPort",
    "GitMutationPort",
    "GitQueryPort",
    "GitlinkConflictSpec",
    "GitlinkDecision",
    "GitlinkDecisionKind",
    "GitlinkMergeContext",
    "GitlinkMergeError",
    "GitlinkMergeLogic",
    "GitlinkMergePlanner",
    "GitlinkResolution",
    "MergeCommitMessage",
    "MergeEvent",
    "MergePolicy",
    "NonGitlinkConflictError",
    "RecursiveGitlinkMergeEngine",
    "RepositoryResolution",
    "SubprocessGitBackend",
]
