"""Persistent session contracts for resumable single-branch manual merges."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


SESSION_SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _StrEnum(str, Enum):
    def __str__(self) -> str:
        return str(self.value)


class SessionStatus(_StrEnum):
    RUNNING = "running"
    PAUSED_MANUAL = "paused_manual"
    SUCCEEDED = "succeeded"
    ABORTED = "aborted"
    FAILED_FATAL = "failed_fatal"


class SessionPhase(_StrEnum):
    MERGE_PENDING = "merge_pending"
    MERGE_ACTIVE = "merge_active"
    MANUAL_RESOLUTION = "manual_resolution"
    RESUMING = "resuming"
    READY_TO_COMMIT = "ready_to_commit"
    CHILD_COMMITTED_PENDING_STAGE = "child_committed_pending_stage"
    FINALIZING = "finalizing"
    COMMITTED = "committed"
    ABORTING = "aborting"


class SessionOutcome(_StrEnum):
    SUCCESS = "success"
    PAUSED_MANUAL = "paused_manual"
    FAILED_FATAL = "failed_fatal"
    ABORTED = "aborted"


@dataclass(frozen=True)
class PausedConflictRecord:
    repo_path: str
    path: str
    disposition: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "path": self.path,
            "disposition": self.disposition,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PausedConflictRecord":
        return cls(
            repo_path=str(payload["repo_path"]),
            path=str(payload["path"]),
            disposition=str(payload["disposition"]),
            detail=str(payload.get("detail", "")),
        )


@dataclass(frozen=True)
class RepositoryFrame:
    repo_path: str
    display_path: str
    depth: int
    parent_repo_path: str | None = None
    parent_gitlink_path: str | None = None
    original_head: str | None = None
    original_branch: str | None = None
    ours_sha: str | None = None
    theirs_sha: str | None = None
    base_sha: str | None = None
    expected_merge_head: str | None = None
    phase: SessionPhase = SessionPhase.MERGE_PENDING
    commit_title: str = ""
    commit_body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "display_path": self.display_path,
            "depth": self.depth,
            "parent_repo_path": self.parent_repo_path,
            "parent_gitlink_path": self.parent_gitlink_path,
            "original_head": self.original_head,
            "original_branch": self.original_branch,
            "ours_sha": self.ours_sha,
            "theirs_sha": self.theirs_sha,
            "base_sha": self.base_sha,
            "expected_merge_head": self.expected_merge_head,
            "phase": self.phase.value,
            "commit_title": self.commit_title,
            "commit_body": self.commit_body,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RepositoryFrame":
        return cls(
            repo_path=str(payload["repo_path"]),
            display_path=str(payload.get("display_path", ".")),
            depth=int(payload["depth"]),
            parent_repo_path=_optional_string(payload.get("parent_repo_path")),
            parent_gitlink_path=_optional_string(payload.get("parent_gitlink_path")),
            original_head=_optional_string(payload.get("original_head", payload.get("head_commit"))),
            original_branch=_optional_string(payload.get("original_branch", payload.get("branch_name"))),
            ours_sha=_optional_string(payload.get("ours_sha")),
            theirs_sha=_optional_string(payload.get("theirs_sha", payload.get("target_ref"))),
            base_sha=_optional_string(payload.get("base_sha")),
            expected_merge_head=_optional_string(
                payload.get("expected_merge_head", payload.get("merge_head_commit"))
            ),
            phase=SessionPhase(payload.get("phase", SessionPhase.MERGE_PENDING.value)),
            commit_title=str(payload.get("commit_title", "")),
            commit_body=str(payload.get("commit_body", "")),
        )


@dataclass(frozen=True)
class MergeSession:
    session_id: str
    root_repo_path: str
    work_branch: str
    target_ref: str
    base_ref: str = ""
    merge_sha: str | None = None
    revision: int = 0
    schema_version: int = SESSION_SCHEMA_VERSION
    status: SessionStatus = SessionStatus.RUNNING
    phase: SessionPhase = SessionPhase.MERGE_PENDING
    outcome: SessionOutcome | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    repo_stack: tuple[RepositoryFrame, ...] = ()
    touched_frames: tuple[RepositoryFrame, ...] = ()
    current_repo_path: str | None = None
    paused_conflicts: tuple[PausedConflictRecord, ...] = ()
    paused_reason: str | None = None
    last_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        *,
        root_repo_path: str,
        work_branch: str,
        target_ref: str,
        base_ref: str = "",
        merge_sha: str | None = None,
        repo_stack: list[RepositoryFrame] | tuple[RepositoryFrame, ...] = (),
        touched_frames: list[RepositoryFrame] | tuple[RepositoryFrame, ...] | None = None,
        current_repo_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "MergeSession":
        now = utc_now_iso()
        normalized_stack = tuple(repo_stack)
        normalized_touched = normalized_stack if touched_frames is None else tuple(touched_frames)
        return cls(
            session_id=uuid4().hex,
            root_repo_path=root_repo_path,
            work_branch=work_branch,
            target_ref=target_ref,
            base_ref=base_ref,
            merge_sha=merge_sha,
            created_at=now,
            updated_at=now,
            repo_stack=normalized_stack,
            touched_frames=normalized_touched,
            current_repo_path=current_repo_path or (normalized_stack[-1].repo_path if normalized_stack else None),
            metadata=dict(metadata or {}),
        )

    @property
    def current_frame(self) -> RepositoryFrame | None:
        return self.repo_stack[-1] if self.repo_stack else None

    def with_revision(self, revision: int, *, updated_at: str | None = None) -> "MergeSession":
        return replace(self, revision=revision, updated_at=updated_at or utc_now_iso())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "revision": self.revision,
            "root_repo_path": self.root_repo_path,
            "work_branch": self.work_branch,
            "target_ref": self.target_ref,
            "base_ref": self.base_ref,
            "merge_sha": self.merge_sha,
            "status": self.status.value,
            "phase": self.phase.value,
            "outcome": None if self.outcome is None else self.outcome.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "repo_stack": [frame.to_dict() for frame in self.repo_stack],
            "touched_frames": [frame.to_dict() for frame in self.touched_frames],
            "current_repo_path": self.current_repo_path,
            "paused_conflicts": [conflict.to_dict() for conflict in self.paused_conflicts],
            "paused_reason": self.paused_reason,
            "last_error": self.last_error,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MergeSession":
        return cls(
            schema_version=int(payload["schema_version"]),
            session_id=str(payload["session_id"]),
            revision=int(payload["revision"]),
            root_repo_path=str(payload["root_repo_path"]),
            work_branch=str(payload["work_branch"]),
            target_ref=str(payload["target_ref"]),
            base_ref=str(payload.get("base_ref", "")),
            merge_sha=_optional_string(payload.get("merge_sha")),
            status=SessionStatus(payload["status"]),
            phase=SessionPhase(payload["phase"]),
            outcome=None if payload.get("outcome") is None else SessionOutcome(payload["outcome"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            repo_stack=tuple(RepositoryFrame.from_dict(item) for item in _require_list(payload, "repo_stack")),
            touched_frames=tuple(
                RepositoryFrame.from_dict(item)
                for item in payload.get("touched_frames", payload.get("repo_stack", []))
            ),
            current_repo_path=_optional_string(payload.get("current_repo_path")),
            paused_conflicts=tuple(
                PausedConflictRecord.from_dict(item) for item in _require_list(payload, "paused_conflicts")
            ),
            paused_reason=_optional_string(payload.get("paused_reason")),
            last_error=_optional_string(payload.get("last_error")),
            metadata=_require_dict(payload, "metadata"),
        )


@dataclass(frozen=True)
class SessionStatusSnapshot:
    session_path: str
    exists: bool
    locked: bool
    session: MergeSession | None = None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _require_list(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key, [])
    if isinstance(value, list):
        return value
    raise ValueError(f"Session field '{key}' must be a list")


def _require_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    if isinstance(value, dict):
        return dict(value)
    raise ValueError(f"Session field '{key}' must be a mapping")
