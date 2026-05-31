"""CI-style adapter around split_gitlink_merge.

This file does not modify or import-patch ci_merge.py.  It provides a migration
surface that can be called by a future CI script wrapper while preserving the
original CI semantics: pure gitlink conflicts are handled recursively, mixed or
file conflicts fail, and CI-specific reporting/guards remain policy hooks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from ci_merge_split import CiWorkflowServices
from ci_merge_split.repository_ops import display_path_for as ci_display_path_for
from split_gitlink_merge import (
    Conflict,
    GitActionExecutor,
    GitCommandResult,
    GitlinkMergeContext,
    GitlinkMergeError,
    MergeCommitMessage,
    MergeEvent,
    MergePolicy,
    NonGitlinkConflictError,
    RecursiveGitlinkMergeEngine,
)
from split_gitlink_merge.executor import GitBackend, format_git_failure
from split_gitlink_merge.ports import GitExecutionPort


@dataclass
class CiMergeResult:
    repo_path: Path
    display_path: str
    base_sha: str | None
    source_sha: str
    target_sha: str
    result_sha: str
    strategy: str


class CiGitBackend:
    """Adapt ci_merge.GitRunner to split_gitlink_merge.GitBackend."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def run(
        self,
        repo_path: Path,
        args: list[str],
        *,
        check: bool = True,
    ) -> GitCommandResult:
        result = self.ctx.runner.run(repo_path, args, allow_failure=not check)
        return GitCommandResult(
            args=result.cmd,
            cwd=Path(result.cwd),
            returncode=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
        )


class CiAutoMergePolicy(MergePolicy):
    """CI policy hooks for the split gitlink core."""

    def __init__(self, ctx: Any | None = None, ci_module: ModuleType | None = None) -> None:
        super().__init__(
            fetch_missing_commits=True,
            init_missing_submodules=True,
            update_submodules_before_recursive_merge=False,
        )
        self.ctx = ctx
        self.ci_module = ci_module
        self.services = (
            CiWorkflowServices.from_module(ci_module)
            if ci_module is not None
            else (CiWorkflowServices.from_local() if ctx is not None else None)
        )
        self.resolved_gitlink_records: list[dict[str, Any]] = []

    def on_event(self, event: MergeEvent) -> None:
        if event.name == "gitlink.conflict":
            self._record_gitlink_conflict(event)
            return
        if event.name != "gitlink.resolved":
            return
        data = dict(event.data)
        record = {
            "path": data.get("path"),
            "repo_path": str(event.repo_path / str(data.get("path") or "")),
            "base": data.get("base"),
            "ours": data.get("ours"),
            "theirs": data.get("theirs"),
            "result": data.get("result"),
            "strategy": data.get("strategy"),
            "recursive_merge": data.get("recursive_merge"),
        }
        self.resolved_gitlink_records.append(record)

        if self.ctx is not None and hasattr(self.ctx, "report"):
            report = self.ctx.report
            increment = getattr(report, "increment_summary", None)
            if callable(increment):
                increment("submodules_processed")
                if data.get("strategy") in {"use_theirs", "fast_forward_to_theirs"}:
                    increment("fast_forward_submodules")
                if data.get("recursive_merge"):
                    increment("submodule_recursive_merges")

        if self.ctx is not None and self.services is not None:
            sub_repo = event.repo_path / str(data.get("path") or "")
            self.services.report_artifacts.collect_repository_details(
                self.ctx,
                sub_repo,
                self._display_path(sub_repo),
                data.get("base"),
                data.get("ours"),
                data.get("theirs"),
                data.get("result"),
                data.get("strategy"),
            )

    def _record_gitlink_conflict(self, event: MergeEvent) -> None:
        if self.ctx is None or not hasattr(self.ctx, "report"):
            return
        data = dict(event.data)
        path = str(data.get("path") or "")
        stages = []
        for stage_number, key in (("1", "base"), ("2", "ours"), ("3", "theirs")):
            sha = data.get(key)
            if not sha:
                continue
            stages.append(
                {
                    "mode": "160000",
                    "sha": sha,
                    "stage": stage_number,
                    "raw": f"160000 {sha} {stage_number}\t{path}",
                }
            )
        self.ctx.report.add_conflicts(
            [
                {
                    "repo_path": str(event.repo_path),
                    "path": path,
                    "stages": stages,
                    "action": "recursive_merge",
                    "type": "gitlink_conflict",
                }
            ]
        )

    def prepare_recursive_merge(
        self,
        actions: GitExecutionPort,
        context: GitlinkMergeContext,
    ) -> None:
        if self.ctx is not None and self.services is not None:
            self.services.guards.clean_and_prepare_repo(self.ctx, context.submodule_repo)

        branch = self._branch_for_depth(context.depth)
        actions.run(context.submodule_repo, ["checkout", "-B", branch, context.ours_sha])
        actions.run(context.submodule_repo, ["submodule", "update", "--init", "--recursive", "--force"], check=False)

    def handle_file_conflicts(
        self,
        actions: GitExecutionPort,
        repo_path: Path,
        conflicts: list[Conflict],
        depth: int,
    ) -> None:
        del actions, depth
        raise NonGitlinkConflictError(repo_path, conflicts)

    def validate_after_conflicts(
        self,
        actions: GitExecutionPort,
        repo_path: Path,
        depth: int,
    ) -> None:
        del actions, depth
        if self.ctx is None or self.services is None:
            return
        self.services.guards.ensure_clean_after_merge(self.ctx, repo_path)

    def build_submodule_commit_message(
        self,
        context: GitlinkMergeContext,
    ) -> MergeCommitMessage:
        if self.ctx is not None and self.services is not None:
            gitlink_context_cls = getattr(self.ci_module, "GitlinkMergeContext", None)
            ci_gitlink_context = None
            if gitlink_context_cls is not None:
                ci_gitlink_context = gitlink_context_cls(
                    parent_display_path=self._display_path(context.parent_repo),
                    conflict_path=context.submodule_path,
                    base_sha=context.base_sha or "",
                    ours_sha=context.ours_sha,
                    theirs_sha=context.theirs_sha,
                )
            ci_message = self.services.messages.build_merge_commit_message(
                self.ctx,
                repo_path=context.submodule_repo,
                display_path=self._display_path(context.submodule_repo),
                source_sha=context.theirs_sha,
                target_sha=context.ours_sha,
                base_sha=context.base_sha,
                depth=context.depth,
                strategy="recursive_gitlink_merge",
                gitlink_context=ci_gitlink_context,
            )
            return MergeCommitMessage(ci_message.title, ci_message.body)
        return super().build_submodule_commit_message(context)

    def _branch_for_depth(self, depth: int) -> str:
        if self.ctx is not None and self.services is not None:
            return self.services.messages.branch_for_depth(self.ctx, depth)
        job = getattr(self.ctx, "job_id", "manual") if self.ctx is not None else "manual"
        return f"ci-submodule-merge/{job}-d{depth}"

    def _display_path(self, repo_path: Path) -> str:
        if self.ctx is not None:
            return ci_display_path_for(self.ctx, repo_path)
        return str(repo_path)


class CiAutoGitlinkMergeService:
    """CI-style merge workflow using split_gitlink_merge for gitlink recursion."""

    def __init__(
        self,
        backend: GitBackend,
        *,
        policy: CiAutoMergePolicy | None = None,
        max_depth: int = 16,
        ci_module: ModuleType | None = None,
        ctx: Any | None = None,
    ) -> None:
        self.actions = GitActionExecutor(backend)
        self.policy = policy or CiAutoMergePolicy(ctx=ctx, ci_module=ci_module)
        self.engine = RecursiveGitlinkMergeEngine(self.actions, policy=self.policy, max_depth=max_depth)
        self.ci_module = ci_module
        self.ctx = ctx
        self.services = (
            CiWorkflowServices.from_module(ci_module)
            if ci_module is not None
            else (CiWorkflowServices.from_local() if ctx is not None else None)
        )
        self.max_depth = max_depth

    @classmethod
    def from_ci_context(
        cls,
        ctx: Any,
        ci_module: ModuleType | None = None,
    ) -> "CiAutoGitlinkMergeService":
        return cls(
            CiGitBackend(ctx),
            policy=CiAutoMergePolicy(ctx=ctx, ci_module=ci_module),
            max_depth=getattr(ctx, "max_depth", 16),
            ci_module=ci_module,
            ctx=ctx,
        )

    def merge_repo(
        self,
        repo_path: Path,
        *,
        source_sha: str,
        target_sha: str,
        base_sha: str | None,
        depth: int,
        display_path: str | None = None,
    ) -> CiMergeResult:
        repo = repo_path.resolve(strict=False)
        if depth > self.max_depth:
            raise GitlinkMergeError(f"recursive merge depth exceeded at {repo}: {depth} > {self.max_depth}")

        self._record_snapshot(repo, source_sha, target_sha, depth)
        branch = self._branch_for_depth(depth)
        self.actions.run(repo, ["checkout", "-B", branch, target_sha])
        self.actions.run(repo, ["submodule", "update", "--init", "--recursive", "--force"], check=False)

        merge = self.actions.merge_no_commit(repo, source_sha)
        strategy = "clean_merge"
        if merge.returncode == 0:
            self.policy.validate_after_conflicts(self.actions, repo, depth)
        elif merge.returncode == 1:
            conflicts = self.actions.read_conflicts(repo)
            gitlinks = [conflict for conflict in conflicts if conflict.kind == "gitlink"]
            non_gitlinks = [conflict for conflict in conflicts if conflict.kind != "gitlink"]
            if not gitlinks or non_gitlinks:
                self.policy.handle_file_conflicts(self.actions, repo, conflicts, depth)
            self.engine.resolve_active_conflicts(repo, depth=depth)
            self.policy.validate_after_conflicts(self.actions, repo, depth)
            strategy = "recursive_gitlink_merge"
        else:
            raise GitlinkMergeError(format_git_failure(merge))

        message = self._build_commit_message(
            repo,
            display_path or self._display_path(repo),
            source_sha,
            target_sha,
            base_sha,
            depth,
            strategy,
        )
        result_sha = self.actions.commit_pending_merge(repo, message)
        self._collect_repository_details(repo, display_path or self._display_path(repo), base_sha, target_sha, source_sha, result_sha, strategy)
        return CiMergeResult(repo, display_path or self._display_path(repo), base_sha, source_sha, target_sha, result_sha, strategy)

    def _build_commit_message(
        self,
        repo: Path,
        display_path: str,
        source_sha: str,
        target_sha: str,
        base_sha: str | None,
        depth: int,
        strategy: str,
    ) -> MergeCommitMessage:
        if self.ctx is not None and self.services is not None:
            resolved = [
                {
                    "path": item.get("path"),
                    "result": item,
                }
                for item in self.policy.resolved_gitlink_records
            ]
            ci_message = self.services.messages.build_merge_commit_message(
                self.ctx,
                repo_path=repo,
                display_path=display_path,
                source_sha=source_sha,
                target_sha=target_sha,
                base_sha=base_sha,
                depth=depth,
                strategy=strategy,
                resolved_gitlinks=resolved or None,
            )
            return MergeCommitMessage(ci_message.title, ci_message.body)
        title = "CI recursive gitlink merge" if strategy == "recursive_gitlink_merge" else "CI merge"
        body = "\n".join(
            [
                f"Repository: {display_path}",
                f"Source SHA: {source_sha}",
                f"Target SHA: {target_sha}",
                f"Base SHA: {base_sha or 'n/a'}",
                f"Strategy: {strategy}",
            ]
        )
        return MergeCommitMessage(title, body)

    def _record_snapshot(self, repo: Path, source_sha: str, target_sha: str, depth: int) -> None:
        if self.ctx is not None and self.services is not None:
            self.services.snapshots.record_snapshot(self.ctx, repo, source_sha, target_sha, depth)

    def _collect_repository_details(
        self,
        repo: Path,
        display_path: str,
        base_sha: str | None,
        target_sha: str,
        source_sha: str,
        result_sha: str,
        strategy: str,
    ) -> None:
        if self.ctx is not None and self.services is not None:
            self.services.report_artifacts.collect_repository_details(
                self.ctx,
                repo,
                display_path,
                base_sha,
                target_sha,
                source_sha,
                result_sha,
                strategy,
            )

    def _branch_for_depth(self, depth: int) -> str:
        if self.ctx is not None and self.services is not None:
            return self.services.messages.branch_for_depth(self.ctx, depth)
        return f"ci-submodule-merge/adapter-d{depth}"

    def _display_path(self, repo: Path) -> str:
        if self.ctx is not None:
            return ci_display_path_for(self.ctx, repo)
        return str(repo)
