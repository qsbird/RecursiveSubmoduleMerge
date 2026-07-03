"""Shared CI merge models split out of the original CI merge script."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class ExitCode:
    SUCCESS = 0
    ROOT_CONFLICT = 1
    SUBMODULE_CONFLICT = 2
    CLEAN_OR_RESTORE_FAILED = 3
    PREFLIGHT_FAILED = 4
    GIT_COMMAND_FAILED = 5
    ARGUMENT_ERROR = 6
    UNDEFINED_STATE = 7
    SYSTEM_EXCEPTION = 8
    PUBLISH_FAILED = 9
    PARTIAL_SUCCESS = 10


class Phase:
    INIT = "INIT"
    CLEANING = "CLEANING"
    SYNCING_REFS = "SYNCING_REFS"
    PREFLIGHT = "PREFLIGHT"
    RESOLVING_REFS = "RESOLVING_REFS"
    MERGING_ROOT = "MERGING_ROOT"
    ANALYZING_CONFLICTS = "ANALYZING_CONFLICTS"
    MERGING_SUBMODULE = "MERGING_SUBMODULE"
    COMMITTING = "COMMITTING"
    COLLECTING_REPORT = "COLLECTING_REPORT"
    PUBLISHING = "PUBLISHING"
    RESTORING = "RESTORING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class CiMergeError(Exception):
    """Structured failure that maps directly to a non-zero process exit code."""

    def __init__(
        self,
        exit_code: int,
        phase: str,
        reason: str,
        message: str,
        details: dict[str, Any] | None = None,
        restore_needed: bool = False,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.phase = phase
        self.reason = reason
        self.message = message
        self.details = details or {}
        self.restore_needed = restore_needed

    def to_report_error(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "phase": self.phase,
            "reason": self.reason,
            "message": self.message,
            "details": self.details,
            "timestamp": now_iso(),
        }


@dataclass
class CommandResult:
    cmd: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    ended_at: str
    duration_ms: int
    allow_failure: bool
    timed_out: bool = False
    attempt: int = 1
    max_attempts: int = 1
    network_failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cmd": self.cmd,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "allow_failure": self.allow_failure,
            "timed_out": self.timed_out,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "network_failure_reason": self.network_failure_reason,
        }


class GitCommandError(CiMergeError):
    def __init__(self, phase: str, command: CommandResult) -> None:
        if command.timed_out:
            reason = "GIT_COMMAND_TIMEOUT"
            message = "git command timed out"
        elif command.network_failure_reason:
            reason = "GIT_NETWORK_FAILED"
            message = "git command failed because the remote network operation failed"
        else:
            reason = "GIT_COMMAND_FAILED"
            message = f"git command failed with exit code {command.exit_code}"
        super().__init__(
            ExitCode.GIT_COMMAND_FAILED,
            phase,
            reason,
            message,
            details={"command": command.to_dict()},
        )


@dataclass
class MergeStepContext:
    index: int
    total: int
    source: str
    source_ref: str | None
    source_sha: str
    target: str
    target_ref: str | None
    target_sha: str
    merge_base_sha: str | None
    artifact_prefix: str | None = None


@dataclass
class RefResolution:
    input_ref: str
    resolved_ref: str
    strategy: str
    sha: str | None
    command: CommandResult


@dataclass
class PreflightResult:
    passed: bool
    checks: list[dict[str, Any]]
    failed_checks: list[dict[str, Any]]
    source_ref: str | None = None
    source_sha: str | None = None
    target_ref: str | None = None
    target_sha: str | None = None
    merge_base_sha: str | None = None


@dataclass
class ConflictAnalysis:
    conflicts: list[dict[str, Any]]
    gitlink_conflicts: list[dict[str, Any]]

    @property
    def gitlink_only(self) -> bool:
        return bool(self.gitlink_conflicts) and len(self.gitlink_conflicts) == len(self.conflicts)


@dataclass
class MergeResult:
    repo_path: Path
    display_path: str
    base_sha: str | None
    source_sha: str
    target_sha: str
    result_sha: str
    strategy: str


@dataclass
class GitlinkMergeContext:
    parent_display_path: str
    conflict_path: str
    base_sha: str
    ours_sha: str
    theirs_sha: str
