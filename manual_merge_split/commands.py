"""Manual merge and cleanup command orchestration."""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from merge_tool_adapters.manual_semiauto import ManualWorkflowGitlinkPolicy
from split_gitlink_merge import (
    Conflict,
    GitActionExecutor,
    GitCommandResult as CoreGitCommandResult,
    GitExecutionPort,
    GitlinkMergeContext,
    MergeCommitMessage,
)
from split_gitlink_merge import GitlinkMergeError, NonGitlinkConflictError, RecursiveGitlinkMergeEngine

from . import common
from . import execution
from . import messages as manual_messages
from .cross_submodule_moves import (
    CrossSubmoduleMoveService,
    describe_cross_submodule_move,
    get_unmerged_submodule_stage_commit,
)
from .file_conflicts import ManualFileConflictService
from .models import (
    CleanupResult,
    ConflictEntry,
    CrossSubmoduleMove,
    MergeBatchResult,
    MergeContext,
    REPORT_FILE_NAME,
    UnmergedConflict,
)
from .preflight import (
    ManualPreflightService,
    ensure_repo_ready,
)
from .publish import ManualPublishService
from .repository_ops import ManualRepositoryOps
from .session_workflow import ManualMergeSessionService


COMMAND_FUNCTIONS: tuple[str, ...] = ()


def _stringify_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class _ManualOpsGitBackend:
    def __init__(self, ops: ManualRepositoryOps) -> None:
        self.ops = ops

    def run(self, repo_path: Path, args: list[str], *, check: bool = True) -> CoreGitCommandResult:
        result = self.ops.run_git(repo_path, args, check=check)
        normalized = CoreGitCommandResult(
            args=["git", *args],
            cwd=Path(getattr(result, "cwd", repo_path)).resolve(strict=False),
            returncode=int(getattr(result, "returncode", 0)),
            stdout=_stringify_process_output(getattr(result, "stdout", "")),
            stderr=_stringify_process_output(getattr(result, "stderr", "")),
        )
        if check and normalized.returncode != 0:
            raise GitlinkMergeError(self.ops._format_git_failure(result))
        return normalized


def _resolve_gitlink_conflicts_with_common_core(
    repo_path: Path,
    ops: ManualRepositoryOps,
    *,
    context: MergeContext,
    display_path: str,
    target_ref: str,
    file_conflicts_service: ManualFileConflictService,
) -> tuple[list[str], bool]:
    repo = repo_path.resolve(strict=False)

    def prepare_submodule_branch(child_repo: Path, start_point: str) -> None:
        ops.create_submodule_branch(
            child_repo,
            context.work_branch,
            start_point=start_point,
        )

    def build_submodule_commit_message(recursive_context: GitlinkMergeContext) -> MergeCommitMessage:
        relative_submodule_path = recursive_context.submodule_repo.relative_to(repo).as_posix()
        manual_message = manual_messages.build_submodule_commit_message(
            repo_path=recursive_context.submodule_repo,
            parent_repo=recursive_context.parent_repo,
            display_path=manual_messages.join_display_path(display_path, relative_submodule_path),
            source_ref=context.merge_branch,
            target_ref=target_ref,
            source_sha=recursive_context.theirs_sha,
            target_sha=recursive_context.ours_sha,
            base_sha=recursive_context.base_sha,
            work_branch=context.work_branch,
        )
        return MergeCommitMessage(title=manual_message.title, body=manual_message.body)

    def handle_file_conflicts(
        actions: GitExecutionPort,
        conflict_repo: Path,
        conflicts: list[Conflict],
        depth: int,
    ) -> None:
        del actions, depth
        conflict_paths = [conflict.path for conflict in conflicts]
        unresolved, reason, rollback_requested = file_conflicts_service.resolve_file_conflicts(
            conflict_repo,
            conflict_paths,
            context,
        )
        context.rollback_requested = context.rollback_requested or rollback_requested
        file_conflicts_service.record_file_conflicts(
            conflict_repo,
            unresolved,
            context,
            reason=reason,
        )
        if unresolved:
            unresolved_paths = set(unresolved)
            raise NonGitlinkConflictError(
                conflict_repo,
                [conflict for conflict in conflicts if conflict.path in unresolved_paths],
            )

    policy = ManualWorkflowGitlinkPolicy(
        tracked_repo=repo,
        prepare_submodule_branch=prepare_submodule_branch,
        build_submodule_commit_message=build_submodule_commit_message,
        handle_file_conflicts=handle_file_conflicts,
    )
    engine = RecursiveGitlinkMergeEngine(
        GitActionExecutor(_ManualOpsGitBackend(ops)),
        policy=policy,
    )
    try:
        engine.resolve_active_conflicts(repo)
    except NonGitlinkConflictError:
        return list(policy.resolved_paths), False
    except GitlinkMergeError as exc:
        raise ops._automation_error(str(exc)) from exc
    return list(policy.resolved_paths), True


def set_verbose(manual_module: ModuleType | None, enabled: bool) -> None:
    execution.set_verbose(enabled)
    if manual_module is not None:
        setattr(manual_module, "VERBOSE", enabled)


def clone_repository(clone_branch: str, clone_url: str, clone_path: Path, ops: ManualRepositoryOps) -> Path:
    target = common.resolve_clone_target_path(clone_url, clone_path)
    print(f"Clone target resolved to: {target}", flush=True)
    ops.run_git(
        Path.cwd(),
        [
            "-c",
            "protocol.file.allow=always",
            "clone",
            "-v",
            "--recurse-submodules",
            "--progress",
            "--branch",
            clone_branch,
            clone_url,
            str(target),
        ],
        stream_output=True,
    )
    return target


def get_head_submodule_commit(repo_path: Path, relative_path: str, ops: ManualRepositoryOps) -> str | None:
    result = ops.run_git(repo_path, ["ls-tree", "HEAD", "--", relative_path], check=False)
    if result.returncode != 0 or not result.stdout:
        return None
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        mode, object_type, commit_sha = parts[0], parts[1], parts[2]
        if mode == "160000" and object_type == "commit":
            return commit_sha
    return None


def summarize_conflict_entries(entries: Iterable[ConflictEntry]) -> str:
    reasons = [entry.reason.strip() for entry in entries if entry.reason.strip()]
    if not reasons:
        return "no additional reason recorded"
    return "; ".join(dict.fromkeys(reasons))


def can_skip_failed_branch(context: MergeContext, completed: bool) -> bool:
    if completed or context.rollback_requested or not context.report_entries:
        return False
    return all(entry.category == "missing_submodule_commit" for entry in context.report_entries)


def can_auto_rollback_failed_branch(context: MergeContext) -> bool:
    if not context.report_entries:
        return False
    return all(entry.category == "missing_submodule_commit" for entry in context.report_entries)


def describe_incomplete_stage_conflict(conflict: UnmergedConflict) -> str:
    conflict_type = "gitlink" if conflict.is_gitlink else "file"
    stages = ",".join(str(stage) for stage in conflict.stages) or "none"
    missing = ",".join(str(stage) for stage in conflict.missing_stages) or "none"
    modes = ",".join(conflict.modes) or "unknown"
    return f"{conflict.path} ({conflict_type}, stages={stages}, missing={missing}, modes={modes})"


def record_incomplete_stage_conflicts(
    repo_path: Path,
    conflicts: list[UnmergedConflict],
    context: MergeContext,
) -> None:
    if not conflicts:
        return
    details = "; ".join(describe_incomplete_stage_conflict(conflict) for conflict in conflicts)
    context.report_entries.append(
        ConflictEntry(
            repo_path=repo_path,
            files=[conflict.path for conflict in conflicts],
            reason=(
                "Conflict has incomplete git index stages and requires manual resolution "
                f"outside this tool: {details}"
            ),
            category="incomplete_stage_conflict",
        )
    )


def refresh_submodules_after_root_checkout(
    repo_path: Path,
    ops: ManualRepositoryOps,
    preserved_dirty_paths_by_repo: dict[Path, set[str]],
) -> None:
    preserved_paths = preserved_dirty_paths_by_repo.get(repo_path.resolve(), set())
    if not preserved_paths:
        ops.update_submodules(repo_path)
        return

    update_paths = [
        submodule_path
        for submodule_path in ops.get_submodule_paths(repo_path)
        if submodule_path not in preserved_paths
    ]
    if update_paths:
        ops.update_submodules(repo_path, paths=update_paths)


def _safe_unlink_report(report_path: Path, repo_path: Path, ops: ManualRepositoryOps) -> bool:
    """Remove report file only if it is NOT tracked by git.

    Returns True if the file was removed, False if it was skipped
    because it is tracked (and thus managed by git reset --hard instead).
    """
    if not report_path.exists():
        return False
    if ops._is_path_tracked(repo_path, report_path.name):
        return False  # Tracked — let git manage it via reset --hard
    report_path.unlink()
    return True


def process_repository(
    repo_path: Path,
    context: MergeContext,
    *,
    root_base_remote_branch: str | None,
    submodule_start_point: str | None = None,
    is_root: bool,
    ops: ManualRepositoryOps,
    file_conflicts_service: ManualFileConflictService,
    cross_submodule_moves_service: CrossSubmoduleMoveService,
    prepare_branch: bool = True,
    preserved_dirty_paths_by_repo: dict[Path, set[str]] | None = None,
    ignored_root_untracked_paths: Iterable[str] = (),
    moved_away_paths: Iterable[str] = (),
    moved_here_moves: Iterable[CrossSubmoduleMove] = (),
    display_path: str = ".",
    parent_repo: Path | None = None,
    gitlink_base_sha: str | None = None,
    gitlink_ours_sha: str | None = None,
    gitlink_theirs_sha: str | None = None,
) -> bool:
    ops.ensure_git_repo(repo_path)
    ignored_dirty_paths = (
        preserved_dirty_paths_by_repo.get(repo_path.resolve(), set())
        if preserved_dirty_paths_by_repo is not None
        else ()
    )
    ensure_repo_ready(
        repo_path,
        ops,
        ignored_dirty_paths=ignored_dirty_paths,
        ignored_untracked_paths=ignored_root_untracked_paths if is_root else (),
    )

    if prepare_branch:
        if is_root:
            if root_base_remote_branch is None:
                raise ops._automation_error("Root base branch is required for the root repository")
            ops.create_root_branch(repo_path, root_base_remote_branch, context.work_branch)
        else:
            ops.create_submodule_branch(
                repo_path,
                context.work_branch,
                start_point=submodule_start_point,
            )

    if is_root:
        target_ref = root_base_remote_branch if prepare_branch and root_base_remote_branch else context.work_branch
    else:
        target_ref = gitlink_ours_sha or "HEAD"
    target_sha = gitlink_ours_sha or ops.try_resolve_commit(repo_path, "HEAD")
    merge_target_ref = ops.merge_branch(repo_path, context.merge_branch)
    source_sha = gitlink_theirs_sha or ops.try_resolve_commit(repo_path, merge_target_ref)

    submodule_conflicts, file_conflicts, incomplete_stage_conflicts = ops.split_conflicts_by_stage(repo_path)
    if incomplete_stage_conflicts:
        record_incomplete_stage_conflicts(repo_path, incomplete_stage_conflicts, context)
        return False

    resolved_by_common_core: list[str] = []
    if (
        submodule_conflicts
        and not context.enable_cross_submodule_moves
        and not moved_away_paths
        and not moved_here_moves
    ):
        resolved_by_common_core, common_core_completed = _resolve_gitlink_conflicts_with_common_core(
            repo_path,
            ops,
            context=context,
            display_path=display_path,
            target_ref=target_ref,
            file_conflicts_service=file_conflicts_service,
        )
        if not common_core_completed:
            return False
        submodule_conflicts, file_conflicts, incomplete_stage_conflicts = ops.split_conflicts_by_stage(repo_path)
        if incomplete_stage_conflicts:
            record_incomplete_stage_conflicts(repo_path, incomplete_stage_conflicts, context)
            return False

    moved_away_resolved = file_conflicts_service.resolve_moved_away_file_conflicts(
        repo_path,
        file_conflicts,
        set(moved_away_paths),
    )
    if moved_away_resolved:
        resolved_moved_away_set = set(moved_away_resolved)
        file_conflicts = [
            path for path in file_conflicts if path not in resolved_moved_away_set
        ]

    submodule_move_infos = []
    submodule_move_info_by_path = {}
    cross_submodule_moves = []
    if context.enable_cross_submodule_moves:
        submodule_move_infos = cross_submodule_moves_service.collect_submodule_move_candidate_info(repo_path, submodule_conflicts)
        submodule_move_info_by_path = {
            info.path: info
            for info in submodule_move_infos
        }
        cross_submodule_moves = cross_submodule_moves_service.collect_cross_submodule_moves(
            repo_path,
            submodule_conflicts,
            candidate_infos=submodule_move_infos,
        )
    moved_away_by_submodule: dict[str, set[str]] = {}
    moved_here_by_submodule: dict[str, list[CrossSubmoduleMove]] = {}
    for move in cross_submodule_moves:
        moved_away_by_submodule.setdefault(move.old_submodule_path, set()).add(move.old_file_path)
        moved_here_by_submodule.setdefault(move.new_submodule_path, []).append(move)

    unresolved_child_submodules: list[str] = []
    failed_child_submodules: list[str] = []
    submodules_to_process = common.unique_preserving_order(
        [
            *submodule_conflicts,
            *moved_away_by_submodule.keys(),
            *moved_here_by_submodule.keys(),
        ]
    )
    resolved_child_submodules: list[str] = [manual_messages.join_display_path(display_path, path) for path in resolved_by_common_core]
    for submodule_path in submodules_to_process:
        child_repo = repo_path / submodule_path
        child_info = submodule_move_info_by_path.get(submodule_path)
        child_base_commit = (
            child_info.base_commit
            if child_info is not None
            else get_unmerged_submodule_stage_commit(
                repo_path,
                submodule_path,
                1,
                run_git=ops.run_git,
            )
        )
        child_start_commit = (
            child_info.current_commit
            if child_info is not None
            else get_unmerged_submodule_stage_commit(
                repo_path,
                submodule_path,
                2,
                run_git=ops.run_git,
            )
        )
        child_merge_target = (
            child_info.merge_commit
            if child_info is not None
            else get_unmerged_submodule_stage_commit(
                repo_path,
                submodule_path,
                3,
                run_git=ops.run_git,
            )
        )
        if not child_start_commit:
            raise ops._automation_error(f"Could not determine current-side commit for submodule {child_repo}")
        if not child_merge_target:
            raise ops._automation_error(f"Could not determine merge target commit for submodule {child_repo}")
        if not ops.ensure_commit_available(child_repo, child_merge_target):
            remote_names = ops.get_remote_names(child_repo)
            if remote_names:
                reason = (
                    "merge target commit is unavailable after refreshing submodule remotes "
                    f"({', '.join(remote_names)}): {child_merge_target}"
                )
            else:
                reason = (
                    "submodule has no configured remotes, so the merge target commit cannot be "
                    f"fetched: {child_merge_target}"
                )
            context.report_entries.append(
                ConflictEntry(
                    repo_path=child_repo,
                    files=[],
                    reason=reason,
                    category="missing_submodule_commit",
                )
            )
            failed_child_submodules.append(submodule_path)
            if submodule_path in submodule_conflicts:
                unresolved_child_submodules.append(submodule_path)
            continue

        ops._verbose_print(
            "Submodule merge required: "
            f"{child_repo} -> start {child_start_commit}, merge target {child_merge_target}"
        )
        child_context = replace(
            context,
            merge_branch=child_merge_target,
            report_entries=[],
            rollback_requested=False,
        )
        child_display_path = manual_messages.join_display_path(display_path, submodule_path)
        child_completed = process_repository(
            child_repo,
            child_context,
            root_base_remote_branch=None,
            submodule_start_point=child_start_commit,
            is_root=False,
            prepare_branch=True,
            preserved_dirty_paths_by_repo=preserved_dirty_paths_by_repo,
            ignored_root_untracked_paths=ignored_root_untracked_paths,
            moved_away_paths=moved_away_by_submodule.get(submodule_path, set()),
            moved_here_moves=moved_here_by_submodule.get(submodule_path, []),
            display_path=child_display_path,
            parent_repo=repo_path,
            gitlink_base_sha=child_base_commit,
            gitlink_ours_sha=child_start_commit,
            gitlink_theirs_sha=child_merge_target,
            ops=ops,
            file_conflicts_service=file_conflicts_service,
            cross_submodule_moves_service=cross_submodule_moves_service,
        )
        context.report_entries.extend(child_context.report_entries)
        context.rollback_requested = context.rollback_requested or child_context.rollback_requested
        if child_completed:
            ops.stage_path(repo_path, submodule_path)
            resolved_child_submodules.append(child_display_path)
        else:
            failed_child_submodules.append(submodule_path)
            if submodule_path in submodule_conflicts:
                unresolved_child_submodules.append(submodule_path)

    unresolved_file_conflicts: list[str] = []
    if file_conflicts:
        unresolved_file_conflicts, unresolved_reason, rollback_requested = file_conflicts_service.resolve_file_conflicts(
            repo_path,
            file_conflicts,
            context,
        )
        context.rollback_requested = context.rollback_requested or rollback_requested
        file_conflicts_service.record_file_conflicts(
            repo_path,
            unresolved_file_conflicts,
            context,
            reason=unresolved_reason,
        )

    unresolved_moved_here: list[str] = []
    moved_here_moves_list = list(moved_here_moves)
    if moved_here_moves_list:
        unresolved_moved_here, moved_here_reason, rollback_requested = (
            cross_submodule_moves_service.apply_cross_submodule_moved_files(
                repo_path,
                moved_here_moves_list,
                context,
            )
        )
        context.rollback_requested = context.rollback_requested or rollback_requested
        if unresolved_moved_here:
            unresolved_moved_here_set = set(unresolved_moved_here)
            move_details = "; ".join(
                describe_cross_submodule_move(move)
                for move in moved_here_moves_list
                if move.new_file_path in unresolved_moved_here_set
            )
            file_conflicts_service.record_file_conflicts(
                repo_path,
                unresolved_moved_here,
                context,
                reason=(
                    (moved_here_reason or "cross-submodule move content requires manual resolution")
                    + (f": {move_details}" if move_details else "")
                ),
                category="cross_submodule_move_conflict",
            )

    remaining_submodule_conflicts, remaining_file_conflicts, remaining_incomplete_stage_conflicts = (
        ops.split_conflicts_by_stage(repo_path)
    )
    record_incomplete_stage_conflicts(repo_path, remaining_incomplete_stage_conflicts, context)
    unexpected_submodule_conflicts = [
        path for path in remaining_submodule_conflicts if path not in unresolved_child_submodules
    ]
    if unexpected_submodule_conflicts:
        raise ops._automation_error(f"Submodule conflicts still exist in {repo_path}: {unexpected_submodule_conflicts}")
    if (
        remaining_submodule_conflicts
        or remaining_file_conflicts
        or remaining_incomplete_stage_conflicts
        or unresolved_file_conflicts
        or unresolved_moved_here
        or failed_child_submodules
    ):
        return False

    ops.update_submodules(repo_path)
    if ops.working_tree_has_changes(repo_path) or ops.merge_in_progress(repo_path):
        commit_message = (
            manual_messages.build_root_commit_message(
                repo_path=repo_path,
                source_ref=context.merge_branch,
                target_ref=target_ref,
                source_sha=source_sha,
                target_sha=target_sha,
                work_branch=context.work_branch,
                resolved_submodules=resolved_child_submodules,
            )
            if is_root
            else manual_messages.build_submodule_commit_message(
                repo_path=repo_path,
                parent_repo=parent_repo,
                display_path=display_path,
                source_ref=context.merge_branch,
                target_ref=target_ref,
                source_sha=source_sha,
                target_sha=target_sha,
                base_sha=gitlink_base_sha,
                work_branch=context.work_branch,
                resolved_submodules=resolved_child_submodules,
            )
        )
        ops.commit_if_needed(
            repo_path,
            message=commit_message,
            fallback_message=(
                "Merge cross-submodule moved file changes"
                if moved_here_moves_list
                else None
            ),
        )
    return True


def write_report(report_path: Path, context: MergeContext, *, ops: ManualRepositoryOps | None = None, repo_path: Path | None = None) -> None:
    if not context.report_entries:
        if report_path.exists():
            if ops is not None and repo_path is not None and not _safe_unlink_report(report_path, repo_path, ops):
                pass  # Tracked file — let git manage it; do not create an unstaged deletion
        return

    lines = [
        "Manual conflict follow-up",
        "========================",
        "",
        "The script kept file conflicts in place for manual resolution.",
        "Suggested workflow for each repository:",
        "1. cd /d <repo path>",
        "2. git status",
        "3. resolve files",
        "4. git add <resolved files>",
        "5. git commit --no-edit",
        "",
    ]
    for entry in context.report_entries:
        lines.append(f"Repository: {entry.repo_path}")
        lines.append(f"Quick entry: cd /d {entry.repo_path}")
        if entry.reason:
            lines.append(f"Reason: {entry.reason}")
        lines.append("Conflicted files:")
        if entry.files:
            for relative_file in entry.files:
                lines.append(f"  - {relative_file}")
        else:
            lines.append("  - (no file path recorded)")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def rebuild_work_branches(
    repo_path: Path,
    result: CleanupResult,
    *,
    work_branch: str,
    root_base_remote_branch: str | None,
    submodule_start_point: str | None = None,
    is_root: bool,
    ops: ManualRepositoryOps,
) -> None:
    ops.ensure_git_repo(repo_path)
    if ops.working_tree_has_changes(repo_path) or ops.merge_in_progress(repo_path):
        raise ops._automation_error(f"Repository is not clean after cleanup, cannot rebuild work branch: {repo_path}")

    if is_root:
        if root_base_remote_branch is None:
            raise ops._automation_error("Root base branch is required when rebuilding the root repository")
        ops.create_root_branch(repo_path, root_base_remote_branch, work_branch)
    else:
        ops.create_submodule_branch(
            repo_path,
            work_branch,
            start_point=submodule_start_point,
        )

    result.rebuilt_branches.append(repo_path)

    for submodule_path in ops.get_submodule_paths(repo_path):
        child_repo = repo_path / submodule_path
        if not child_repo.exists():
            continue
        child_start_point = get_head_submodule_commit(repo_path, submodule_path, ops)
        try:
            rebuild_work_branches(
                child_repo,
                result,
                work_branch=work_branch,
                root_base_remote_branch=None,
                submodule_start_point=child_start_point,
                is_root=False,
                ops=ops,
            )
        except ops._automation_error_class() as exc:
            raise ops._automation_error(f"Failed to rebuild work branch for submodule repository {child_repo}: {exc}") from exc


def resolve_merge_work_branch(args: Any, repo_path: Path, ops: ManualRepositoryOps) -> str:
    if getattr(args, "use_current_branch", False):
        current_branch = ops.current_branch_name(repo_path)
        if current_branch is None:
            raise ops._automation_error(
                "Cannot continue on the current root branch because the repository is in a detached HEAD state."
            )
        return current_branch
    return common.compute_work_branch(args.work_branch)


def make_merge_context(args: Any, repo_path: Path, ops: ManualRepositoryOps) -> MergeContext:
    timeout_seconds = args.conflict_tool_timeout_seconds
    if args.file_conflict_mode != "tool":
        raise ops._automation_error(
            "File conflict preserve mode has been removed; merge this branch manually instead."
        )
    return MergeContext(
        work_branch=resolve_merge_work_branch(args, repo_path, ops),
        merge_branch=common.normalize_user_merge_ref(args.merge_branch),
        file_conflict_mode=args.file_conflict_mode,
        conflict_tool_command=args.conflict_tool_command,
        conflict_tool_timeout_seconds=None if timeout_seconds <= 0 else timeout_seconds,
        confirm_after_conflict_tool=not getattr(args, "no_confirm_after_conflict_tool", False),
    )


def run_merge(
    args: Any,
    *,
    manual_module: ModuleType | None,
    ops: ManualRepositoryOps,
    preflight_service: ManualPreflightService,
    file_conflicts_service: ManualFileConflictService,
    cross_submodule_moves_service: CrossSubmoduleMoveService,
) -> int:
    set_verbose(manual_module, getattr(args, "verbose", False))
    session_service = ManualMergeSessionService(
        ops=ops,
        preflight_service=preflight_service,
        file_conflicts_service=file_conflicts_service,
    )
    return session_service.start_merge(args)


def run_status(args: Any, *, manual_module: ModuleType | None, ops: ManualRepositoryOps) -> int:
    set_verbose(manual_module, getattr(args, "verbose", False))
    session_service = ManualMergeSessionService(
        ops=ops,
        preflight_service=ManualPreflightService(manual_module),
        file_conflicts_service=ManualFileConflictService(manual_module),
    )
    return session_service.status(args)


def run_resume(
    args: Any,
    *,
    manual_module: ModuleType | None,
    ops: ManualRepositoryOps,
    preflight_service: ManualPreflightService,
    file_conflicts_service: ManualFileConflictService,
) -> int:
    set_verbose(manual_module, getattr(args, "verbose", False))
    session_service = ManualMergeSessionService(
        ops=ops,
        preflight_service=preflight_service,
        file_conflicts_service=file_conflicts_service,
    )
    return session_service.resume(args)


def run_abort(args: Any, *, manual_module: ModuleType | None, ops: ManualRepositoryOps) -> int:
    set_verbose(manual_module, getattr(args, "verbose", False))
    session_service = ManualMergeSessionService(
        ops=ops,
        preflight_service=ManualPreflightService(manual_module),
        file_conflicts_service=ManualFileConflictService(manual_module),
    )
    return session_service.abort(args)


def run_cleanup(args: Any, *, manual_module: ModuleType | None, ops: ManualRepositoryOps) -> int:
    set_verbose(manual_module, getattr(args, "verbose", False))
    if args.rebuild_work_branch and not (args.base_remote_branch or "").strip():
        print("--base-remote-branch cannot be empty when rebuilding the work branch.", file=sys.stderr)
        return 1

    print(
        "WARNING: cleanup is destructive and is your responsibility. "
        "It may recursively abort merges/rebases and run git reset --hard HEAD plus git clean, "
        "discarding uncommitted, staged, and untracked local changes in the root repository and submodules.",
        file=sys.stderr,
    )
    if args.rebuild_work_branch:
        print(
            "WARNING: rebuild work branch may move an existing local branch with the same name; "
            "the tool will block if that would hide local committed work.",
            file=sys.stderr,
        )
    if not getattr(args, "yes", False):
        if sys.stdin.isatty():
            answer = input("Continue cleanup and accept possible data loss? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Cleanup aborted.", file=sys.stderr)
                return 1
        else:
            print(
                "Non-interactive cleanup requires --yes to confirm you accept possible data loss.",
                file=sys.stderr,
            )
            return 1

    repo_path = Path(args.repo).resolve()
    report_file_name = getattr(args, "report_file", REPORT_FILE_NAME)
    report_path = repo_path / report_file_name
    result = CleanupResult()

    try:
        ops.cleanup_repository(
            repo_path,
            result,
            skip_submodule_update=args.skip_submodule_update,
            allow_clean_x=bool(getattr(args, "allow_clean_x", False)),
            ignored_root_untracked_paths=(report_file_name,),
        )
        if _safe_unlink_report(report_path, repo_path, ops):
            result.removed_reports.append(report_path)
        if args.rebuild_work_branch:
            rebuild_work_branches(
                repo_path,
                result,
                work_branch=common.compute_work_branch(args.work_branch),
                root_base_remote_branch=args.base_remote_branch.strip(),
                is_root=True,
                ops=ops,
            )
    except ops._automation_error_class() as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("Cleanup completed successfully.")
    print(f"Visited repositories: {len(result.visited_repos)}")
    print(f"Merges aborted/reset: {len(result.aborted_merges)}")
    print(f"Hard resets: {len(result.hard_resets)}")
    if not args.skip_submodule_update:
        print(f"Submodule updates: {len(result.submodule_updates)}")
    if result.rebuilt_branches:
        print(f"Rebuilt work branches: {len(result.rebuilt_branches)}")
    if result.removed_reports:
        print(f"Removed report: {report_path}")
    if result.failed_steps:
        print(f"Failed steps: {len(result.failed_steps)}")
        for step_error in result.failed_steps:
            print(f"  - {step_error}", file=sys.stderr)
    return 0


@dataclass(frozen=True)
class ManualCommandService:
    """Facade for manual merge and cleanup command workflows."""

    manual_module: ModuleType | None = None

    def __post_init__(self) -> None:
        return None

    @property
    def ops(self) -> ManualRepositoryOps:
        return ManualRepositoryOps(self.manual_module)

    @property
    def preflight(self) -> ManualPreflightService:
        return ManualPreflightService(self.manual_module)

    @property
    def file_conflicts(self) -> ManualFileConflictService:
        return ManualFileConflictService(self.manual_module)

    @property
    def cross_submodule_moves(self) -> CrossSubmoduleMoveService:
        return CrossSubmoduleMoveService(self.manual_module)

    @property
    def publish(self) -> ManualPublishService:
        return ManualPublishService(self.manual_module)

    def set_verbose(self, enabled: bool) -> None:
        set_verbose(self.manual_module, enabled)

    def process_repository(self, repo_path: Path, context: MergeContext, **kwargs: Any) -> bool:
        return process_repository(
            repo_path,
            context,
            ops=self.ops,
            file_conflicts_service=self.file_conflicts,
            cross_submodule_moves_service=self.cross_submodule_moves,
            **kwargs,
        )

    def write_report(self, report_path: Path, context: MergeContext) -> None:
        write_report(report_path, context)

    def make_merge_context(self, args: Any, repo_path: Path) -> MergeContext:
        return make_merge_context(args, repo_path, self.ops)

    def run_merge(self, args: Any) -> int:
        return run_merge(
            args,
            manual_module=self.manual_module,
            ops=self.ops,
            preflight_service=self.preflight,
            file_conflicts_service=self.file_conflicts,
            cross_submodule_moves_service=self.cross_submodule_moves,
        )

    def run_status(self, args: Any) -> int:
        return run_status(args, manual_module=self.manual_module, ops=self.ops)

    def run_resume(self, args: Any) -> int:
        return run_resume(
            args,
            manual_module=self.manual_module,
            ops=self.ops,
            preflight_service=self.preflight,
            file_conflicts_service=self.file_conflicts,
        )

    def run_abort(self, args: Any) -> int:
        return run_abort(args, manual_module=self.manual_module, ops=self.ops)

    def run_cleanup(self, args: Any) -> int:
        return run_cleanup(args, manual_module=self.manual_module, ops=self.ops)

    def run_publish(self, args: Any) -> int:
        return self.publish.run_publish(args)
