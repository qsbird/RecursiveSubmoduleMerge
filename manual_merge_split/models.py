"""Shared manual merge models split out of the original manual script."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


ROOT_BASE_REMOTE_BRANCH = "origin/main"
WORK_BRANCH_TEMPLATE = "liyuheng/2026/temp{date}/bugfixUI"
REPORT_FILE_NAME = "merge_manual_todo.txt"
DEFAULT_FILE_CONFLICT_MODE = "tool"
DEFAULT_CONFLICT_TOOL_COMMAND = 'code --wait "{file}"'
DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS = 1800
CROSS_SUBMODULE_MOVE_SIMILARITY_THRESHOLD = 0.50
CROSS_SUBMODULE_MOVE_CONTENT_SAMPLE_BYTES = 200_000
KNOWN_MISSING_REVISION_MARKERS = (
    "Needed a single revision",
    "unknown revision",
    "bad revision",
    "ambiguous argument",
)


class MergeAutomationError(RuntimeError):
    """Raised when the merge automation cannot continue safely."""


ManualSplitError = MergeAutomationError


class ManualConflictDisposition(str, Enum):
    SUPPORTED_FILE = "supported_file"
    SUPPORTED_GITLINK = "supported_gitlink"
    MANUAL_TAKEOVER = "manual_takeover"
    FATAL = "fatal"


@dataclass
class GitResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str


@dataclass
class GitBinaryResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass
class ConflictEntry:
    repo_path: Path
    files: list[str] = field(default_factory=list)
    reason: str = ""
    category: str = "manual_conflict"


@dataclass(frozen=True)
class UnmergedConflict:
    path: str
    modes: tuple[str, ...]
    stages: tuple[int, ...]

    @property
    def is_gitlink(self) -> bool:
        return "160000" in self.modes

    @property
    def is_complete(self) -> bool:
        return set(self.stages) == {1, 2, 3}

    @property
    def missing_stages(self) -> tuple[int, ...]:
        return tuple(stage for stage in (1, 2, 3) if stage not in set(self.stages))


@dataclass
class DirtyRepoAssessment:
    repo_path: Path
    lineage: tuple[str, ...]
    reason: str
    risk_confirmed: bool = False

    @property
    def root_relative_path(self) -> str:
        return "/".join(self.lineage)


@dataclass
class MergeContext:
    work_branch: str
    merge_branch: str
    file_conflict_mode: str
    conflict_tool_command: str
    conflict_tool_timeout_seconds: int | None
    confirm_after_conflict_tool: bool = True
    enable_cross_submodule_moves: bool = False
    report_entries: list[ConflictEntry] = field(default_factory=list)
    rollback_requested: bool = False


@dataclass(frozen=True)
class ManualConflictRecord:
    path: str
    mode: str
    sha: str
    stage: int


@dataclass(frozen=True)
class ClassifiedManualConflict:
    repo_path: Path
    path: str
    disposition: ManualConflictDisposition
    records: tuple[ManualConflictRecord, ...]
    status_code: str = ""
    reason: str = ""


@dataclass
class CleanupResult:
    visited_repos: list[Path] = field(default_factory=list)
    aborted_merges: list[Path] = field(default_factory=list)
    hard_resets: list[Path] = field(default_factory=list)
    submodule_updates: list[Path] = field(default_factory=list)
    removed_reports: list[Path] = field(default_factory=list)
    rebuilt_branches: list[Path] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)
    not_clean_repos: list[Path] = field(default_factory=list)


@dataclass
class RepoSnapshot:
    repo_path: Path
    head_commit: str
    branch_name: str | None


@dataclass
class MergePreflightResult:
    allowed_dirty_submodules: list[DirtyRepoAssessment] = field(default_factory=list)


@dataclass
class MergeBatchResult:
    completed_branches: list[str] = field(default_factory=list)
    skipped_branches: list[str] = field(default_factory=list)
    skipped_branch_reasons: dict[str, str] = field(default_factory=dict)
    stopped_branch: str | None = None
    rolled_back_to_branch: str | None = None
    rolled_back_to_checkpoint: bool = False


@dataclass
class FileConflictResolution:
    reason: str | None
    rollback_requested: bool = False


@dataclass(frozen=True)
class TextMergeNormalization:
    had_utf8_bom: bool
    newline: bytes


@dataclass(frozen=True)
class SubmoduleConflictInfo:
    path: str
    repo_path: Path
    base_commit: str | None
    current_commit: str | None
    merge_commit: str | None

    def commit_for_stage(self, stage: int) -> str | None:
        if stage == 1:
            return self.base_commit
        if stage == 2:
            return self.current_commit
        if stage == 3:
            return self.merge_commit
        raise ValueError(f"Unsupported git index stage: {stage}")


@dataclass(frozen=True)
class CrossSubmoduleDeletedFile:
    info: SubmoduleConflictInfo
    file_path: str
    move_side: int


@dataclass(frozen=True)
class CrossSubmoduleAddedFile:
    info: SubmoduleConflictInfo
    file_path: str
    move_side: int


@dataclass(frozen=True)
class CrossSubmoduleMove:
    old_submodule_path: str
    old_repo_path: Path
    old_file_path: str
    old_base_commit: str
    old_other_commit: str
    new_submodule_path: str
    new_repo_path: Path
    new_file_path: str
    new_added_commit: str
    move_side: int
    score: float


@dataclass
class PublishCandidate:
    """A single repository that may need to be pushed during publish."""
    repo_path: Path
    display_path: str
    commit_sha: str
    remote_before_sha: str | None = None
    remote_state: str = "unknown"
    depth: int = 0
    selected: bool = True
