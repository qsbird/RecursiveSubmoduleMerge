"""CI remote-sync and preflight implementations."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from . import common
from .models import CiMergeError, ExitCode, Phase, PreflightResult, now_iso
from . import repository_ops


def _ci_error_class(ci_module: ModuleType | None) -> type[Exception]:
    return getattr(ci_module, "CiMergeError", CiMergeError) if ci_module is not None else CiMergeError


def _exit_code(ci_module: ModuleType | None, name: str) -> int:
    exit_codes = getattr(ci_module, "ExitCode", ExitCode) if ci_module is not None else ExitCode
    return getattr(exit_codes, name)


def _phase(ci_module: ModuleType | None, name: str) -> str:
    phases = getattr(ci_module, "Phase", Phase) if ci_module is not None else Phase
    return getattr(phases, name)


def _raise_ci_error(
    ci_module: ModuleType | None,
    exit_code_name: str,
    phase_name: str,
    reason: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    restore_needed: bool = False,
) -> None:
    error_cls = _ci_error_class(ci_module)
    raise error_cls(
        _exit_code(ci_module, exit_code_name),
        _phase(ci_module, phase_name),
        reason,
        message,
        details=details,
        restore_needed=restore_needed,
    )


def _preflight_result_class(ci_module: ModuleType | None) -> type[Any]:
    return getattr(ci_module, "PreflightResult", PreflightResult) if ci_module is not None else PreflightResult


def add_check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    reason: str | None = None,
    **details: Any,
) -> None:
    entry: dict[str, Any] = {"name": name, "passed": passed}
    if reason:
        entry["reason"] = reason
    entry.update({key: value for key, value in details.items() if value is not None})
    checks.append(entry)


def run_sync_command(
    ctx: Any,
    repo_path: Path,
    args: list[str],
    reason: str,
    message: str,
    ci_module: ModuleType | None = None,
) -> Any:
    result = ctx.runner.run(repo_path, args, allow_failure=True)
    if result.exit_code != 0:
        _raise_ci_error(
            ci_module,
            "GIT_COMMAND_FAILED",
            "SYNCING_REFS",
            reason,
            message,
            details={"repo_path": str(repo_path), "command": result.to_dict()},
            restore_needed=False,
        )
    return result


def sync_remote_refs(ctx: Any, repo_path: Path, ci_module: ModuleType | None = None) -> None:
    ctx.set_state(_phase(ci_module, "SYNCING_REFS"))
    sync_report = ctx.report.data["remote_sync"]
    sync_report["attempted"] = True
    sync_report["started_at"] = now_iso()
    try:
        run_sync_command(
            ctx,
            repo_path,
            ["fetch", ctx.args.remote, "--prune"],
            "REMOTE_FETCH_FAILED",
            "failed to fetch remote refs before resolving source and target",
            ci_module,
        )
        run_sync_command(
            ctx,
            repo_path,
            ["submodule", "sync", "--recursive"],
            "SUBMODULE_SYNC_FAILED",
            "failed to sync submodule URLs before recursive fetch",
            ci_module,
        )
        run_sync_command(
            ctx,
            repo_path,
            ["submodule", "foreach", "--recursive", "git fetch --all --prune"],
            "SUBMODULE_FETCH_FAILED",
            "failed to recursively fetch submodule refs before merge",
            ci_module,
        )
        sync_report["success"] = True
    except Exception:
        sync_report["success"] = False
        raise
    finally:
        sync_report["ended_at"] = now_iso()


def preflight_check(
    ctx: Any,
    repo_path: Path,
    source: str | None,
    target: str | None,
    *,
    label: str,
    resolve_refs: bool,
    ci_module: ModuleType | None = None,
) -> Any:
    ctx.set_state(_phase(ci_module, "PREFLIGHT"))
    checks: list[dict[str, Any]] = []
    source_ref: str | None = None
    source_sha: str | None = None
    target_ref: str | None = None
    target_sha: str | None = None
    merge_base_sha: str | None = None
    result_cls = _preflight_result_class(ci_module)

    exists = repo_path.exists()
    add_check(checks, "repo_path_exists", exists, None if exists else "repo_path does not exist", path=str(repo_path))
    if not exists:
        result = result_cls(False, checks, [item for item in checks if not item["passed"]])
        record_preflight(ctx, label, result)
        return result

    is_directory = repo_path.is_dir()
    add_check(
        checks,
        "repo_path_is_directory",
        is_directory,
        None if is_directory else "repo_path is not a directory",
        path=str(repo_path),
    )
    if not is_directory:
        result = result_cls(False, checks, [item for item in checks if not item["passed"]])
        record_preflight(ctx, label, result)
        return result

    inside = ctx.runner.run(repo_path, ["rev-parse", "--is-inside-work-tree"], allow_failure=True)
    is_git_repo = inside.exit_code == 0 and inside.stdout.strip() == "true"
    add_check(
        checks,
        "is_git_repository",
        is_git_repo,
        None if is_git_repo else "path is not a git work tree",
    )
    if not is_git_repo:
        result = result_cls(False, checks, [item for item in checks if not item["passed"]])
        record_preflight(ctx, label, result)
        return result

    top = ctx.runner.run(repo_path, ["rev-parse", "--show-toplevel"], allow_failure=True)
    add_check(
        checks,
        "resolve_toplevel",
        top.exit_code == 0 and bool(top.stdout.strip()),
        None if top.exit_code == 0 else "cannot resolve repository top-level path",
        toplevel=top.stdout.strip() or None,
    )

    markers = repository_ops.operation_markers(ctx, repo_path)
    add_check(
        checks,
        "no_operation_in_progress",
        not markers,
        None if not markers else "merge/rebase/cherry-pick/revert operation is in progress",
        markers=markers or None,
    )

    is_clean, status = repository_ops.repository_is_clean(ctx, repo_path)
    add_check(
        checks,
        "working_tree_clean",
        is_clean,
        None if is_clean else "working tree is not clean",
        status=status or None,
    )

    gitmodules = repo_path / ".gitmodules"
    if gitmodules.exists():
        parsed = ctx.runner.run(repo_path, ["config", "-f", ".gitmodules", "--list"], allow_failure=True)
        add_check(
            checks,
            "gitmodules_parseable",
            parsed.exit_code == 0,
            None if parsed.exit_code == 0 else ".gitmodules cannot be parsed",
        )
    else:
        add_check(checks, "gitmodules_parseable", True, gitmodules="absent")

    submodules = ctx.runner.run(repo_path, ["submodule", "status", "--recursive"], allow_failure=True)
    submodule_ok = submodules.exit_code == 0
    bad_submodules: list[str] = []
    if submodule_ok:
        for line in common.split_lines(submodules.stdout):
            if line[0] in {"-", "+", "U"}:
                bad_submodules.append(line)
    add_check(
        checks,
        "submodules_initialized_and_at_head",
        submodule_ok and not bad_submodules,
        None if submodule_ok and not bad_submodules else "submodules are not initialized or not at expected HEAD",
        bad_submodules=bad_submodules or None,
    )

    if resolve_refs and source and target:
        source_resolution = repository_ops.resolve_commit_ref(ctx, repo_path, source, ci_module)
        source_ref = source_resolution.resolved_ref
        source_sha = source_resolution.sha
        add_check(
            checks,
            "resolve_source",
            source_resolution.command.exit_code == 0 and bool(source_sha),
            None if source_resolution.command.exit_code == 0 else "source ref cannot be resolved to commit",
            ref=source,
            resolved_ref=source_ref,
            resolution_strategy=source_resolution.strategy,
            sha=source_sha,
        )

        target_resolution = repository_ops.resolve_commit_ref(ctx, repo_path, target, ci_module)
        target_ref = target_resolution.resolved_ref
        target_sha = target_resolution.sha
        add_check(
            checks,
            "resolve_target",
            target_resolution.command.exit_code == 0 and bool(target_sha),
            None if target_resolution.command.exit_code == 0 else "target ref cannot be resolved to commit",
            ref=target,
            resolved_ref=target_ref,
            resolution_strategy=target_resolution.strategy,
            sha=target_sha,
        )

        if source_sha and target_sha:
            merge_base = ctx.runner.run(repo_path, ["merge-base", source_sha, target_sha], allow_failure=True)
            if merge_base.exit_code == 0:
                merge_base_sha = merge_base.stdout.strip()
            add_check(
                checks,
                "source_target_have_merge_base",
                merge_base.exit_code == 0 and bool(merge_base_sha),
                None if merge_base.exit_code == 0 else "source and target have no merge-base",
                sha=merge_base_sha,
            )

    result = result_cls(
        passed=all(item["passed"] for item in checks),
        checks=checks,
        failed_checks=[item for item in checks if not item["passed"]],
        source_ref=source_ref,
        source_sha=source_sha,
        target_ref=target_ref,
        target_sha=target_sha,
        merge_base_sha=merge_base_sha,
    )
    record_preflight(ctx, label, result)
    return result


def record_preflight(ctx: Any, label: str, result: Any) -> None:
    entry = {
        "label": label,
        "passed": result.passed,
        "checks": result.checks,
        "failed_checks": result.failed_checks,
        "timestamp": now_iso(),
    }
    ctx.report.data["preflight_runs"].append(entry)
    if label == "initial":
        ctx.report.data["preflight"] = {
            "passed": result.passed,
            "checks": result.checks,
            "failed_checks": result.failed_checks,
        }


def preflight_failure_reason(result: Any) -> str:
    if not result.failed_checks:
        return "PREFLIGHT_FAILED"
    first = result.failed_checks[0]["name"]
    mapping = {
        "resolve_source": "SOURCE_REF_NOT_FOUND",
        "resolve_target": "TARGET_REF_NOT_FOUND",
        "source_target_have_merge_base": "NO_MERGE_BASE",
        "working_tree_clean": "WORKTREE_NOT_CLEAN",
        "no_operation_in_progress": "OPERATION_IN_PROGRESS",
        "submodules_initialized_and_at_head": "SUBMODULE_NOT_READY",
        "gitmodules_parseable": "GITMODULES_INVALID",
        "is_git_repository": "NOT_A_GIT_REPOSITORY",
        "repo_path_exists": "REPO_PATH_NOT_FOUND",
    }
    return mapping.get(first, "PREFLIGHT_FAILED")
