"""Policy hooks for CI/manual behavior around the split gitlink core."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .model import (
    Conflict,
    GitlinkMergeContext,
    MergeCommitMessage,
    MergeEvent,
    NonGitlinkConflictError,
)

if TYPE_CHECKING:
    from .ports import GitExecutionPort


class MergePolicy:
    """Default strict policy.

    CI code can subclass this to export artifacts and reject mixed conflicts.
    Manual tools can subclass it to open mergetool for file conflicts.
    """

    def __init__(
        self,
        *,
        fetch_missing_commits: bool = False,
        init_missing_submodules: bool = True,
        update_submodules_before_recursive_merge: bool = True,
    ) -> None:
        self.fetch_missing_commits = fetch_missing_commits
        self.init_missing_submodules = init_missing_submodules
        self.update_submodules_before_recursive_merge = update_submodules_before_recursive_merge

    def on_event(self, event: MergeEvent) -> None:
        return None

    def prepare_recursive_merge(
        self,
        actions: "GitExecutionPort",
        context: GitlinkMergeContext,
    ) -> None:
        actions.checkout_detached(context.submodule_repo, context.ours_sha)
        if self.update_submodules_before_recursive_merge:
            actions.update_submodules(context.submodule_repo, check=False)

    def handle_file_conflicts(
        self,
        actions: "GitExecutionPort",
        repo_path: Path,
        conflicts: list[Conflict],
        depth: int,
    ) -> None:
        del actions
        raise NonGitlinkConflictError(repo_path, conflicts)

    def validate_after_conflicts(
        self,
        actions: "GitExecutionPort",
        repo_path: Path,
        depth: int,
    ) -> None:
        del actions, repo_path, depth
        return None

    def build_submodule_commit_message(
        self,
        context: GitlinkMergeContext,
    ) -> MergeCommitMessage:
        body_lines = [
            f"Parent repository: {context.parent_repo}",
            f"Submodule path: {context.submodule_path}",
            f"Base: {context.base_sha or 'n/a'}",
            f"Ours: {context.ours_sha}",
            f"Theirs: {context.theirs_sha}",
        ]
        return MergeCommitMessage(
            title=f"Recursive submodule merge: {context.submodule_path}",
            body="\n".join(body_lines),
        )
