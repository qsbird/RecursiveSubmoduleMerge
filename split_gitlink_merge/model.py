"""Data contracts for the split recursive gitlink merge core."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class GitlinkMergeError(RuntimeError):
    """Base error for recursive gitlink merge failures."""


class NonGitlinkConflictError(GitlinkMergeError):
    """Raised when strict policy sees conflicts outside submodule gitlinks."""

    def __init__(self, repo_path: Path, conflicts: list["Conflict"]) -> None:
        self.repo_path = repo_path
        self.conflicts = conflicts
        paths = ", ".join(conflict.path for conflict in conflicts)
        super().__init__(f"non-gitlink conflicts remain in {repo_path}: {paths}")


@dataclass(frozen=True)
class GitCommandResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


@dataclass(frozen=True)
class ConflictStage:
    mode: str
    sha: str
    stage: str
    path: str
    raw: str


@dataclass(frozen=True)
class Conflict:
    repo_path: Path
    path: str
    stages: tuple[ConflictStage, ...]
    kind: str

    @property
    def modes(self) -> set[str]:
        return {stage.mode for stage in self.stages}

    @property
    def stage_numbers(self) -> set[str]:
        return {stage.stage for stage in self.stages}

    def stage_sha(self, stage_number: str) -> str | None:
        matches = sorted({stage.sha for stage in self.stages if stage.stage == stage_number and stage.sha})
        if len(matches) != 1:
            return None
        return matches[0]


@dataclass(frozen=True)
class Ancestry:
    ours_is_ancestor_of_theirs: bool = False
    theirs_is_ancestor_of_ours: bool = False


class GitlinkDecisionKind(str, Enum):
    SAME_SHA = "same_sha"
    USE_THEIRS = "use_theirs"
    USE_OURS = "use_ours"
    FAST_FORWARD_TO_THEIRS = "fast_forward_to_theirs"
    ALREADY_CONTAINS_THEIRS = "already_contains_theirs"
    RECURSIVE_MERGE = "recursive_merge"


@dataclass(frozen=True)
class GitlinkDecision:
    kind: GitlinkDecisionKind
    result_sha: str | None
    recursive_merge: bool = False


@dataclass(frozen=True)
class MergeCommitMessage:
    title: str
    body: str = ""

    def git_args(self) -> list[str]:
        args = ["-m", self.title]
        if self.body:
            args.extend(["-m", self.body])
        return args


@dataclass(frozen=True)
class GitlinkMergeContext:
    parent_repo: Path
    submodule_path: str
    submodule_repo: Path
    base_sha: str | None
    ours_sha: str
    theirs_sha: str
    depth: int


@dataclass
class GitlinkResolution:
    path: str
    repo_path: Path
    base_sha: str | None
    ours_sha: str
    theirs_sha: str
    result_sha: str
    strategy: str
    recursive_merge: bool


@dataclass
class RepositoryResolution:
    repo_path: Path
    depth: int
    gitlinks: list[GitlinkResolution] = field(default_factory=list)
    file_conflicts: list[Conflict] = field(default_factory=list)

    @property
    def resolved_gitlinks(self) -> int:
        return len(self.gitlinks)


@dataclass(frozen=True)
class MergeEvent:
    name: str
    repo_path: Path
    message: str
    data: dict[str, Any] = field(default_factory=dict)
