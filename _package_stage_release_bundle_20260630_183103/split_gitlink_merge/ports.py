"""Execution ports for recursive gitlink merge orchestration.

The core engine depends on these protocols instead of a concrete subprocess
implementation.  This keeps the domain logic testable while still allowing CI
and manual tools to plug in their own Git runners.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .model import Ancestry, Conflict, GitCommandResult, MergeCommitMessage


class GitBackend(Protocol):
    def run(
        self,
        repo_path: Path,
        args: list[str],
        *,
        check: bool = True,
    ) -> GitCommandResult:
        """Run a Git command in repo_path."""


class GitQueryPort(Protocol):
    def run(self, repo_path: Path, args: list[str], *, check: bool = True) -> GitCommandResult:
        """Run a raw Git command."""

    def read_conflicts(self, repo_path: Path) -> list[Conflict]:
        """Read and classify unresolved index entries."""

    def ensure_git_worktree(self, repo_path: Path) -> None:
        """Raise if repo_path is not a Git worktree."""

    def is_git_worktree(self, repo_path: Path) -> bool:
        """Return whether repo_path is a Git worktree."""

    def ensure_commit_available(self, repo_path: Path, sha: str, *, fetch_missing: bool) -> None:
        """Ensure a commit object is available in repo_path."""

    def ancestry(self, repo_path: Path, ours_sha: str, theirs_sha: str) -> Ancestry:
        """Return ancestry facts for two commits."""

    def current_head(self, repo_path: Path) -> str:
        """Return HEAD for repo_path."""


class GitMutationPort(Protocol):
    def checkout_detached(self, repo_path: Path, sha: str) -> None:
        """Checkout sha detached in repo_path."""

    def update_submodules(self, repo_path: Path, *, path: str | None = None, check: bool = True) -> GitCommandResult:
        """Run submodule update."""

    def merge_no_commit(self, repo_path: Path, sha: str) -> GitCommandResult:
        """Run a no-commit merge."""

    def stage_path(self, repo_path: Path, path: str) -> None:
        """Stage path in repo_path."""

    def commit_pending_merge(self, repo_path: Path, message: MergeCommitMessage) -> str:
        """Commit pending merge or staged changes and return HEAD."""


class GitExecutionPort(GitQueryPort, GitMutationPort, Protocol):
    """Combined query/mutation port used by the orchestration layer."""

