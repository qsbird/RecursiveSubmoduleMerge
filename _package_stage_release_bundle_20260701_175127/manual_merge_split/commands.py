"""Manual merge and cleanup command orchestration."""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from . import common
from . import execution
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
    DirtyRepoAssessment,
    MergeBatchResult,
    MergeContext,
    REPORT_FILE_NAME,
    UnmergedConflict,
)
from .preflight import (
    ManualPreflightService,
    ToolAvailabilityResult,
    build_preserved_dirty_path_map,
    check_tool_availability,
    ensure_repo_ready,
    format_dirty_submodule_list,
)
from .publish import ManualPublishService
from .repository_ops import ManualRepositoryOps


COMMAND_FUNCTIONS: tuple[str, ...] = ()


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


def prompt_yes_no(prompt: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def prompt_to_continue_with_dirty_submodules(entries: list[DirtyRepoAssessment]) -> bool:
    print("Detected dirty submodules that preflight believes are not touched by the planned merge:")
    print(format_dirty_submodule_list(entries))
    return prompt_yes_no("Continue the merge without cleaning those submodules", False)


def print_dirty_submodule_warning(entries: list[DirtyRepoAssessment]) -> None:
    if not entries:
        return
    print("Continuing with preserved dirty submodules:")
    print(format_dirty_submodule_list(entries))


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

    ops.merge_branch(repo_path, context.merge_branch)

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
    for submodule_path in submodules_to_process:
        child_repo = repo_path / submodule_path
        child_info = submodule_move_info_by_path.get(submodule_path)
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
            ops=ops,
            file_conflicts_service=file_conflicts_service,
            cross_submodule_moves_service=cross_submodule_moves_service,
        )
        context.report_entries.extend(child_context.report_entries)
        context.rollback_requested = context.rollback_requested or child_context.rollback_requested
        if child_completed:
            ops.stage_path(repo_path, submodule_path)
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
        ops.commit_if_needed(
            repo_path,
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
    if args.continue_current_branch:
        current_branch = ops.current_branch_name(repo_path)
        if current_branch is None:
            raise ops._automation_error(
                "Cannot continue on the current root branch because the repository is in a detached HEAD state."
            )
        return current_branch
    return common.compute_work_branch(args.work_branch)


def make_merge_context(args: Any, repo_path: Path, ops: ManualRepositoryOps) -> MergeContext:
    timeout_seconds = args.conflict_tool_timeout_seconds
    normalized_merge_branches = [common.normalize_user_merge_ref(branch) for branch in args.merge_branches]
    if args.file_conflict_mode != "tool":
        raise ops._automation_error(
            "File conflict preserve mode has been removed; merge this branch manually instead."
        )
    return MergeContext(
        work_branch=resolve_merge_work_branch(args, repo_path, ops),
        merge_branch=normalized_merge_branches[0],
        file_conflict_mode=args.file_conflict_mode,
        conflict_tool_command=args.conflict_tool_command,
        conflict_tool_timeout_seconds=None if timeout_seconds <= 0 else timeout_seconds,
        confirm_after_conflict_tool=not getattr(args, "no_confirm_after_conflict_tool", False),
        enable_cross_submodule_moves=bool(getattr(args, "enable_cross_submodule_moves", False)),
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
    if args.continue_current_branch and args.work_branch:
        print("--work-branch cannot be used together with --continue-current-branch.", file=sys.stderr)
        return 1

    # Check conflict tool availability before merge
    tool_command = getattr(args, "conflict_tool_command", "") or ""
    if tool_command.strip() and args.file_conflict_mode == "tool":
        tool_name = tool_command.strip().split()[0]
        tool_check = check_tool_availability(tool_name)
        if not tool_check.available:
            print(tool_check.error_message, file=sys.stderr)
            return 1

    clone_values = [args.clone_branch, args.clone_url, args.clone_path]
    clone_enabled = any(value for value in clone_values)
    if clone_enabled and not all(value for value in clone_values):
        print("Clone before merge requires --clone-branch, --clone-url, and --clone-path together.", file=sys.stderr)
        return 1
    if not args.continue_current_branch and not (args.base_remote_branch or "").strip():
        print("--base-remote-branch cannot be empty. Use --continue-current-branch to merge on the current root branch.", file=sys.stderr)
        return 1

    repo_path = Path(args.repo).resolve()
    if clone_enabled:
        try:
            repo_path = clone_repository(args.clone_branch, args.clone_url, Path(args.clone_path), ops)
        except ops._automation_error_class() as exc:
            print(str(exc), file=sys.stderr)
            return 1

    base_remote_branch = None if args.continue_current_branch else args.base_remote_branch.strip()
    merge_branches = [common.normalize_user_merge_ref(branch) for branch in args.merge_branches]
    report_path = repo_path / args.report_file
    ignored_root_untracked_paths = tuple(args.ignore_root_untracked_path or ())
    batch_result = MergeBatchResult()

    try:
        base_context = make_merge_context(args, repo_path, ops)
        base_ref = "HEAD" if args.continue_current_branch else args.base_remote_branch.strip()
        preflight_result = preflight_service.preflight_repository(
            repo_path,
            base_ref,
            merge_branches,
            ignored_root_untracked_paths=ignored_root_untracked_paths,
        )
        if preflight_result.allowed_dirty_submodules:
            if args.allow_dirty_submodules:
                print_dirty_submodule_warning(preflight_result.allowed_dirty_submodules)
            elif args.no_prompt_dirty_submodules:
                print(
                    "Merge preflight found dirty submodules that appear unaffected. "
                    "Re-run with --allow-dirty-submodules to continue:\n"
                    + format_dirty_submodule_list(preflight_result.allowed_dirty_submodules),
                    file=sys.stderr,
                )
                return 1
            elif sys.stdin.isatty():
                if not prompt_to_continue_with_dirty_submodules(preflight_result.allowed_dirty_submodules):
                    print("Merge aborted because dirty submodules were left in place.", file=sys.stderr)
                    return 1
                print_dirty_submodule_warning(preflight_result.allowed_dirty_submodules)
            else:
                print(
                    "Merge preflight found dirty submodules that appear unaffected, but this run is "
                    "non-interactive. Re-run with --allow-dirty-submodules to continue:\n"
                    + format_dirty_submodule_list(preflight_result.allowed_dirty_submodules),
                    file=sys.stderr,
                )
                return 1

        preserved_dirty_paths_by_repo = build_preserved_dirty_path_map(
            repo_path,
            preflight_result.allowed_dirty_submodules,
        )
        preserved_dirty_repos = {
            entry.repo_path.resolve() for entry in preflight_result.allowed_dirty_submodules
        }
        _safe_unlink_report(report_path, repo_path, ops)

        initial_context = replace(base_context, merge_branch=merge_branches[0])
        if base_remote_branch:
            ops.create_root_branch(repo_path, base_remote_branch, initial_context.work_branch)
            refresh_submodules_after_root_checkout(repo_path, ops, preserved_dirty_paths_by_repo)
        checkpoint = ops.collect_repo_snapshot(repo_path)

        for index, merge_branch in enumerate(merge_branches):
            branch_context = replace(
                base_context,
                merge_branch=merge_branch,
                report_entries=[],
                rollback_requested=False,
            )
            completed = process_repository(
                repo_path,
                branch_context,
                root_base_remote_branch=base_remote_branch,
                is_root=True,
                prepare_branch=(index == 0 and base_remote_branch is not None),
                preserved_dirty_paths_by_repo=preserved_dirty_paths_by_repo,
                ignored_root_untracked_paths=ignored_root_untracked_paths,
                ops=ops,
                file_conflicts_service=file_conflicts_service,
                cross_submodule_moves_service=cross_submodule_moves_service,
            )

            if branch_context.report_entries or not completed:
                if can_skip_failed_branch(branch_context, completed):
                    ops.rollback_to_snapshot(
                        repo_path,
                        checkpoint,
                        report_path=None,
                        preserve_report=False,
                        ignored_root_untracked_paths=ignored_root_untracked_paths,
                        preserved_dirty_paths_by_repo=preserved_dirty_paths_by_repo,
                        preserved_dirty_repos=preserved_dirty_repos,
                    )
                    skip_reason = summarize_conflict_entries(branch_context.report_entries)
                    batch_result.skipped_branches.append(merge_branch)
                    batch_result.skipped_branch_reasons[merge_branch] = skip_reason
                    print(f"Skipping branch '{merge_branch}' after rollback: {skip_reason}")
                    continue

                write_report(report_path, branch_context, ops=ops, repo_path=repo_path)
                batch_result.stopped_branch = merge_branch
                should_rollback = branch_context.rollback_requested and can_auto_rollback_failed_branch(branch_context)
                if should_rollback:
                    ops.rollback_to_snapshot(
                        repo_path,
                        checkpoint,
                        report_path=report_path,
                        preserve_report=True,
                        ignored_root_untracked_paths=ignored_root_untracked_paths,
                        preserved_dirty_paths_by_repo=preserved_dirty_paths_by_repo,
                        preserved_dirty_repos=preserved_dirty_repos,
                    )
                    if batch_result.completed_branches:
                        batch_result.rolled_back_to_branch = batch_result.completed_branches[-1]
                    else:
                        batch_result.rolled_back_to_checkpoint = True
                print(f"Merge stopped on branch '{merge_branch}'. See: {report_path}")
                if batch_result.rolled_back_to_branch:
                    print(
                        "Repository rolled back to the result after branch "
                        f"'{batch_result.rolled_back_to_branch}'."
                    )
                elif batch_result.rolled_back_to_checkpoint:
                    print("Repository rolled back to the pre-merge checkpoint.")
                else:
                    print(
                        "Repository was left unchanged after the failed merge branch to preserve local edits. "
                        "Resolve or clean up manually before retrying."
                    )
                return 2

            batch_result.completed_branches.append(merge_branch)
            checkpoint = ops.collect_repo_snapshot(repo_path)

        _safe_unlink_report(report_path, repo_path, ops)
    except ops._automation_error_class() as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if batch_result.skipped_branches and not batch_result.completed_branches:
        print("No branches were merged successfully. Skipped branches: " + ", ".join(batch_result.skipped_branches))
        return 2
    if batch_result.skipped_branches:
        print("Recursive batch merge completed with skipped branches: " + ", ".join(batch_result.completed_branches))
        print("Skipped branches: " + ", ".join(batch_result.skipped_branches))
        return 0
    if len(batch_result.completed_branches) == 1:
        print("Recursive merge completed successfully.")
    else:
        print("Recursive batch merge completed successfully: " + ", ".join(batch_result.completed_branches))
    return 0


def run_cleanup(args: Any, *, manual_module: ModuleType | None, ops: ManualRepositoryOps) -> int:
    set_verbose(manual_module, getattr(args, "verbose", False))
    if args.rebuild_work_branch and not (args.base_remote_branch or "").strip():
        print("--base-remote-branch cannot be empty when rebuilding the work branch.", file=sys.stderr)
        return 1

    print(
        "WARNING: cleanup may recursively abort merges and run git reset --hard HEAD, "
        "discarding tracked or staged local changes in the root repository and submodules.",
        file=sys.stderr,
    )
    if args.rebuild_work_branch:
        print(
            "WARNING: rebuild work branch may move an existing local branch with the same name; "
            "the tool will block if that would hide local committed work.",
            file=sys.stderr,
        )

    repo_path = Path(args.repo).resolve()
    report_file_name = getattr(args, "report_file", REPORT_FILE_NAME)
    report_path = repo_path / report_file_name
    result = CleanupResult()
    fallback_cleanup = bool(getattr(args, "fallback_cleanup", False))

    # Detect if fallback is needed: any in-progress state (generic detection)
    needs_fallback = fallback_cleanup or ops.has_any_in_progress_state(repo_path)

    try:
        if needs_fallback:
            print("Detected in-progress repository state; using fallback cleanup levels.", file=sys.stderr)
            ops.cleanup_repository_fallback(
                repo_path,
                result,
                skip_submodule_update=args.skip_submodule_update,
                ignored_root_untracked_paths=(report_file_name,),
                report_file_name=report_file_name,
            )
        else:
            ops.cleanup_repository(
                repo_path,
                result,
                skip_submodule_update=args.skip_submodule_update,
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

    def run_cleanup(self, args: Any) -> int:
        return run_cleanup(args, manual_module=self.manual_module, ops=self.ops)

    def run_publish(self, args: Any) -> int:
        return self.publish.run_publish(args)