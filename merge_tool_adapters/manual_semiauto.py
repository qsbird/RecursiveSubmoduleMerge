"""Manual/semi-automatic adapter around split_gitlink_merge."""

from __future__ import annotations

import ctypes
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from split_gitlink_merge import (
    Conflict,
    GitActionExecutor,
    GitlinkMergeError,
    GitlinkMergeContext,
    MergeEvent,
    MergeCommitMessage,
    MergePolicy,
    RecursiveGitlinkMergeEngine,
    SubprocessGitBackend,
)
from split_gitlink_merge.executor import GitBackend, format_git_failure
from split_gitlink_merge.ports import GitExecutionPort


@dataclass
class ManualMergeSummary:
    repo_path: Path
    resolved_gitlinks: int = 0
    file_conflicts: int = 0
    mergetool_runs: int = 0
    root_committed: bool = False
    root_commit_sha: str | None = None
    final_submodule_update: bool = False
    events: list[str] | None = None


class ManualWorkflowGitlinkPolicy(MergePolicy):
    """Manual workflow hooks for using the split gitlink engine inside commands.py."""

    def __init__(
        self,
        *,
        tracked_repo: Path,
        prepare_submodule_branch: Callable[[Path, str], None],
        build_submodule_commit_message: Callable[[GitlinkMergeContext], MergeCommitMessage],
        handle_file_conflicts: Callable[[GitExecutionPort, Path, list[Conflict], int], None],
        on_prepare_recursive_merge: Callable[[GitlinkMergeContext], None] | None = None,
        on_gitlink_conflict: Callable[[MergeEvent], None] | None = None,
        on_gitlink_resolved: Callable[[MergeEvent], None] | None = None,
        fetch_missing_commits: bool = False,
    ) -> None:
        super().__init__(
            fetch_missing_commits=fetch_missing_commits,
            init_missing_submodules=True,
            update_submodules_before_recursive_merge=True,
        )
        self.tracked_repo = tracked_repo.resolve(strict=False)
        self._prepare_submodule_branch = prepare_submodule_branch
        self._build_submodule_commit_message = build_submodule_commit_message
        self._handle_file_conflicts = handle_file_conflicts
        self._on_prepare_recursive_merge = on_prepare_recursive_merge
        self._on_gitlink_conflict = on_gitlink_conflict
        self._on_gitlink_resolved = on_gitlink_resolved
        self.events: list[str] = []
        self.resolved_paths: list[str] = []

    def on_event(self, event: MergeEvent) -> None:
        self.events.append(f"[{event.repo_path}] {event.message}")
        if event.name == "gitlink.conflict":
            # Fires before the engine touches the submodule, so this is the only
            # place where its pre-merge state can still be recorded.  Every
            # gitlink resolution passes through here, not just recursive ones.
            if self._on_gitlink_conflict is not None:
                self._on_gitlink_conflict(event)
            return
        if event.name != "gitlink.resolved":
            return
        path = str(event.data.get("path", "")).strip()
        result_sha = str(event.data.get("result", "")).strip()
        if path and result_sha:
            # The common engine checks out the resolved gitlink commit detached
            # before staging it in the parent.  The manual workflow promises a
            # reusable work branch in every touched submodule, so restore that
            # branch after the engine has finalized the resolution.
            self._prepare_submodule_branch(event.repo_path / path, result_sha)
        if self._on_gitlink_resolved is not None:
            self._on_gitlink_resolved(event)
        if event.repo_path == self.tracked_repo and path and path not in self.resolved_paths:
            self.resolved_paths.append(path)

    def prepare_recursive_merge(
        self,
        actions: GitExecutionPort,
        context: GitlinkMergeContext,
    ) -> None:
        if self._on_prepare_recursive_merge is not None:
            self._on_prepare_recursive_merge(context)
        self._prepare_submodule_branch(context.submodule_repo, context.ours_sha)
        if self.update_submodules_before_recursive_merge:
            result = actions.update_submodules(context.submodule_repo, check=False)
            if int(getattr(result, "returncode", 0)) != 0:
                # Deliberately non-fatal: the engine re-checks initialization
                # before descending and can still fetch a missing nested commit.
                # Record it so a later "submodule is not initialized" is traceable.
                self.events.append(
                    f"[{context.submodule_repo}] submodule update before recursive merge failed: "
                    f"{format_git_failure(result)}"
                )

    def build_submodule_commit_message(
        self,
        context: GitlinkMergeContext,
    ) -> MergeCommitMessage:
        return self._build_submodule_commit_message(context)

    def handle_file_conflicts(
        self,
        actions: GitExecutionPort,
        repo_path: Path,
        conflicts: list[Conflict],
        depth: int,
    ) -> None:
        self._handle_file_conflicts(actions, repo_path, conflicts, depth)


class ManualMergePolicy(MergePolicy):
    def __init__(
        self,
        *,
        confirm: str = "none",
        gui_mergetool: bool = True,
        tool: str | None = None,
        fetch_missing_commits: bool = False,
    ) -> None:
        super().__init__(
            fetch_missing_commits=fetch_missing_commits,
            init_missing_submodules=True,
            update_submodules_before_recursive_merge=True,
        )
        self.confirm = confirm
        self.gui_mergetool = gui_mergetool
        self.tool = tool
        self.events: list[str] = []
        self.mergetool_runs = 0
        self.file_conflicts = 0

    def on_event(self, event: MergeEvent) -> None:
        self.events.append(f"[{event.repo_path}] {event.message}")

    def handle_file_conflicts(
        self,
        actions: GitExecutionPort,
        repo_path: Path,
        conflicts: list[Conflict],
        depth: int,
    ) -> None:
        del depth
        for conflict in conflicts:
            self.file_conflicts += 1
            self._resolve_one_file_conflict(actions, repo_path, conflict.path)

    def _resolve_one_file_conflict(
        self,
        actions: GitExecutionPort,
        repo_path: Path,
        path: str,
    ) -> None:
        while True:
            cmd = ["mergetool", "--no-prompt"]
            if self.gui_mergetool:
                cmd.append("-g")
            if self.tool:
                cmd.append(f"--tool={self.tool}")
            cmd.extend(["--", path])
            self.mergetool_runs += 1
            result = actions.run(repo_path, cmd, check=False)
            if result.returncode != 0:
                raise GitlinkMergeError(format_git_failure(result))

            still_unmerged = self._path_unmerged(actions, repo_path, path)
            action = self._confirm_after_tool(repo_path, path, still_unmerged)
            if action == "retry":
                continue
            if action == "abort":
                raise GitlinkMergeError(f"user aborted after mergetool for {repo_path}:{path}")
            if still_unmerged:
                add = actions.run(repo_path, ["add", "--", path], check=False)
                if add.returncode != 0:
                    rm = actions.run(repo_path, ["rm", "-q", "--", path], check=False)
                    if rm.returncode != 0:
                        raise GitlinkMergeError(format_git_failure(add))
                if self._path_unmerged(actions, repo_path, path):
                    continue
            return

    @staticmethod
    def _path_unmerged(
        actions: GitExecutionPort,
        repo_path: Path,
        path: str,
    ) -> bool:
        result = actions.run(repo_path, ["ls-files", "-u", "--", path], check=False)
        if result.returncode != 0:
            raise GitlinkMergeError(format_git_failure(result))
        return bool(result.stdout.strip())

    def _confirm_after_tool(self, repo_path: Path, path: str, still_unmerged: bool) -> str:
        if self.confirm == "none":
            return "continue"
        unresolved_text = "Git still reports this path as unmerged." if still_unmerged else "Git no longer reports this path as unmerged."
        message = (
            f"Merge tool has closed for:\n\n{repo_path}\\{path}\n\n"
            f"{unresolved_text}\n\n"
            "Yes: accept current worktree/index and continue\n"
            "No: reopen merge tool\n"
            "Cancel: abort"
        )
        if self.confirm == "gui" and platform.system().lower() == "windows":
            result = ctypes.windll.user32.MessageBoxW(None, message, "Recursive Submodule Merge", 0x00000003 | 0x00000020)  # type: ignore[attr-defined]
            if result == 6:
                return "continue"
            if result == 7:
                return "retry"
            return "abort"
        while True:
            answer = input(f"\n{message}\nChoose [y] accept, [r] reopen, [a] abort: ").strip().lower()
            if answer in {"", "y", "yes"}:
                return "continue"
            if answer in {"r", "retry", "reopen", "n", "no"}:
                return "retry"
            if answer in {"a", "abort", "c", "cancel"}:
                return "abort"


class ManualSemiAutoMergeService:
    """Manual workflow using split_gitlink_merge for recursive gitlinks."""

    def __init__(
        self,
        backend: GitBackend | None = None,
        *,
        policy: ManualMergePolicy | None = None,
        max_depth: int = 16,
    ) -> None:
        self.policy = policy or ManualMergePolicy()
        self.actions = GitActionExecutor(backend or SubprocessGitBackend(auto_safe_directory=True))
        self.engine = RecursiveGitlinkMergeEngine(self.actions, policy=self.policy, max_depth=max_depth)

    def resolve_current_or_merge(
        self,
        repo_path: Path,
        *,
        merge_ref: str | None = None,
    ) -> ManualMergeSummary:
        repo = repo_path.resolve(strict=False)
        self.actions.ensure_git_worktree(repo)
        if merge_ref:
            merge = self.actions.run(repo, ["merge", "--no-ff", "--no-commit", merge_ref], check=False)
            if merge.returncode not in (0, 1):
                raise GitlinkMergeError(format_git_failure(merge))

        resolution = self.engine.resolve_active_conflicts(repo)
        root_commit_sha = self._commit_root_merge_if_pending(repo)
        final_update = False
        if root_commit_sha:
            self.actions.update_submodules(repo)
            final_update = True
        return ManualMergeSummary(
            repo_path=repo,
            resolved_gitlinks=resolution.resolved_gitlinks,
            file_conflicts=self.policy.file_conflicts,
            mergetool_runs=self.policy.mergetool_runs,
            root_committed=bool(root_commit_sha),
            root_commit_sha=root_commit_sha,
            final_submodule_update=final_update,
            events=list(self.policy.events),
        )

    def _commit_root_merge_if_pending(self, repo_path: Path) -> str | None:
        merge_head = self.actions.run(repo_path, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False)
        if merge_head.returncode != 0:
            return None
        self.actions.run(repo_path, ["commit", "--no-edit"])
        return self.actions.current_head(repo_path)
