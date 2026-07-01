"""Orchestration layer for the split recursive gitlink merge core."""

from __future__ import annotations

from pathlib import Path

from .logic import GitlinkMergeLogic
from .model import (
    Ancestry,
    Conflict,
    GitlinkDecisionKind,
    GitlinkMergeError,
    GitlinkResolution,
    MergeCommitMessage,
    MergeEvent,
    RepositoryResolution,
)
from .planner import GitlinkMergePlanner
from .policy import MergePolicy
from .ports import GitExecutionPort


def _path_preview(conflicts: tuple[Conflict, ...], limit: int = 20) -> list[str]:
    paths = [conflict.path for conflict in conflicts]
    if len(paths) <= limit:
        return paths
    return [*paths[:limit], f"... {len(paths) - limit} more"]


class RecursiveGitlinkMergeEngine:
    """Bind planner, executor, and policy into a recursive gitlink resolver."""

    def __init__(
        self,
        actions: GitExecutionPort,
        *,
        planner: GitlinkMergePlanner | None = None,
        policy: MergePolicy | None = None,
        logic: GitlinkMergeLogic | None = None,
        max_depth: int = 16,
    ) -> None:
        self.actions = actions
        self.planner = planner or getattr(actions, "planner", GitlinkMergePlanner())
        self.policy = policy or MergePolicy()
        self.logic = logic or GitlinkMergeLogic()
        self.max_depth = max_depth

    def resolve_active_conflicts(self, repo_path: Path, *, depth: int = 0) -> RepositoryResolution:
        repo = repo_path.resolve(strict=False)
        self._ensure_depth(depth, repo)
        self.actions.ensure_git_worktree(repo)
        result = RepositoryResolution(repo_path=repo, depth=depth)
        self._event("repo.check", repo, "checking unresolved paths", {"depth": depth})

        while True:
            conflicts = self.actions.read_conflicts(repo)
            if not conflicts:
                self._event("repo.clean", repo, "no unresolved paths remain", {"depth": depth})
                self.policy.validate_after_conflicts(self.actions, repo, depth)
                return result

            groups = self.logic.group_conflicts(conflicts)
            self._event(
                "repo.conflicts_classified",
                repo,
                "classified unresolved paths",
                {
                    "depth": depth,
                    "total": len(conflicts),
                    "gitlinks": len(groups.gitlinks),
                    "non_gitlinks": len(groups.non_gitlinks),
                    "gitlink_paths": _path_preview(groups.gitlinks),
                    "non_gitlink_paths": _path_preview(groups.non_gitlinks),
                },
            )

            if groups.has_gitlinks:
                for conflict in groups.gitlinks:
                    result.gitlinks.append(self.resolve_gitlink_conflict(repo, conflict, depth))
                continue

            result.file_conflicts.extend(groups.non_gitlinks)
            self.policy.handle_file_conflicts(self.actions, repo, list(groups.non_gitlinks), depth)
            remaining = [conflict for conflict in self.actions.read_conflicts(repo) if conflict.kind != "gitlink"]
            if remaining:
                self.policy.handle_file_conflicts(self.actions, repo, remaining, depth)

    def merge_commits(
        self,
        repo_path: Path,
        *,
        ours_sha: str,
        theirs_sha: str,
        base_sha: str | None = None,
        depth: int = 0,
        message: MergeCommitMessage | None = None,
        checkout_start: bool = True,
    ) -> str:
        repo = repo_path.resolve(strict=False)
        self._ensure_depth(depth, repo)
        self.actions.ensure_git_worktree(repo)
        if checkout_start:
            self.actions.checkout_detached(repo, ours_sha)

        merge = self.actions.merge_no_commit(repo, theirs_sha)
        if merge.returncode == 0:
            self._event("repo.merge_clean", repo, "merge applied cleanly", {"depth": depth})
        elif merge.returncode == 1:
            self._event("repo.merge_conflicts", repo, "merge produced conflicts", {"depth": depth})
            self.resolve_active_conflicts(repo, depth=depth)
            remaining = self.actions.read_conflicts(repo)
            if remaining:
                paths = ", ".join(conflict.path for conflict in remaining)
                raise GitlinkMergeError(f"unresolved conflicts remain in {repo}: {paths}")
        else:
            detail = merge.stderr.strip() or merge.stdout.strip() or "no output"
            raise GitlinkMergeError(f"git merge failed in {repo}: {detail}")

        self.policy.validate_after_conflicts(self.actions, repo, depth)
        commit_message = message or MergeCommitMessage(
            title="Recursive gitlink merge",
            body="\n".join(
                [
                    f"Base: {base_sha or 'n/a'}",
                    f"Ours: {ours_sha}",
                    f"Theirs: {theirs_sha}",
                ]
            ),
        )
        return self.actions.commit_pending_merge(repo, commit_message)

    def resolve_gitlink_conflict(
        self,
        parent_repo: Path,
        conflict: Conflict,
        depth: int,
    ) -> GitlinkResolution:
        parent = parent_repo.resolve(strict=False)
        spec = self.logic.build_gitlink_spec(parent, conflict)
        path = spec.path
        sub_repo = spec.submodule_repo
        self._event(
            "gitlink.conflict",
            parent,
            f"detected gitlink conflict {path}",
            {
                "path": path,
                "base": spec.base_sha,
                "ours": spec.ours_sha,
                "theirs": spec.theirs_sha,
            },
        )
        if not self.actions.is_git_worktree(sub_repo):
            if self.policy.init_missing_submodules:
                self.actions.update_submodules(parent, path=path, check=False)
            if not self.actions.is_git_worktree(sub_repo):
                raise GitlinkMergeError(f"submodule is not initialized: {sub_repo}")

        for sha in [value for value in (spec.base_sha, spec.ours_sha, spec.theirs_sha) if value]:
            self.actions.ensure_commit_available(
                sub_repo,
                sha,
                fetch_missing=self.policy.fetch_missing_commits,
            )

        ancestry = Ancestry()
        needs_ancestry_lookup = self.logic.needs_ancestry_lookup(spec)
        if needs_ancestry_lookup:
            ancestry = self.actions.ancestry(sub_repo, spec.ours_sha, spec.theirs_sha)
        decision = self.planner.choose_gitlink_resolution(
            base_sha=spec.base_sha,
            ours_sha=spec.ours_sha,
            theirs_sha=spec.theirs_sha,
            ancestry=self.logic.ancestry_for_decision(spec, ancestry),
        )
        self._event(
            "gitlink.decision",
            parent,
            f"selected {decision.kind.value} for gitlink {path}",
            {
                "path": path,
                "base": spec.base_sha,
                "ours": spec.ours_sha,
                "theirs": spec.theirs_sha,
                "strategy": decision.kind.value,
                "result": decision.result_sha,
                "recursive_merge": decision.recursive_merge,
                "needs_ancestry_lookup": needs_ancestry_lookup,
                "ours_is_ancestor_of_theirs": ancestry.ours_is_ancestor_of_theirs,
                "theirs_is_ancestor_of_ours": ancestry.theirs_is_ancestor_of_ours,
            },
        )

        recursive_merge = decision.recursive_merge
        if decision.kind == GitlinkDecisionKind.RECURSIVE_MERGE:
            context = self.logic.build_recursive_context(spec, depth + 1)
            self._ensure_depth(context.depth, sub_repo)
            self.policy.prepare_recursive_merge(self.actions, context)
            result_sha = self.merge_commits(
                sub_repo,
                ours_sha=spec.ours_sha,
                theirs_sha=spec.theirs_sha,
                base_sha=spec.base_sha,
                depth=context.depth,
                message=self.policy.build_submodule_commit_message(context),
                checkout_start=False,
            )
        elif decision.result_sha:
            result_sha = decision.result_sha
        else:
            raise GitlinkMergeError(f"planner returned no result for {parent}:{path}")

        self.actions.checkout_detached(sub_repo, result_sha)
        self.actions.stage_path(parent, path)
        self._event(
            "gitlink.resolved",
            parent,
            f"resolved gitlink {path} via {decision.kind.value}",
            {
                "path": path,
                "base": spec.base_sha,
                "ours": spec.ours_sha,
                "theirs": spec.theirs_sha,
                "result": result_sha,
                "strategy": decision.kind.value,
                "recursive_merge": recursive_merge,
            },
        )
        return GitlinkResolution(
            path=path,
            repo_path=sub_repo,
            base_sha=spec.base_sha,
            ours_sha=spec.ours_sha,
            theirs_sha=spec.theirs_sha,
            result_sha=result_sha,
            strategy=decision.kind.value,
            recursive_merge=recursive_merge,
        )

    def _ensure_depth(self, depth: int, repo_path: Path) -> None:
        if depth > self.max_depth:
            raise GitlinkMergeError(f"recursive submodule depth exceeded at {repo_path}: {depth} > {self.max_depth}")

    def _event(self, name: str, repo_path: Path, message: str, data: dict[str, object] | None = None) -> None:
        self.policy.on_event(MergeEvent(name=name, repo_path=repo_path, message=message, data=data or {}))
