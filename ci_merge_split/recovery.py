"""CI cleanup, snapshot, and restore implementations."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from merge_cleanup_core import CleanupStep, coerce_cleanup_step, git_clean_flag
from merge_cleanup_core import steps as cleanup_steps

from . import common
from .models import CiMergeError, ExitCode, Phase, now_iso
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


def _error_to_report(exc: Exception) -> dict[str, Any]:
    to_report = getattr(exc, "to_report_error", None)
    if callable(to_report):
        return to_report()
    return {"reason": type(exc).__name__, "message": str(exc), "timestamp": now_iso()}


def run_cleanup_steps(
    ctx: Any,
    repo_path: Path,
    level_name: str,
    steps: list[CleanupStep | dict[str, Any]] | tuple[CleanupStep | dict[str, Any], ...],
) -> dict[str, Any]:
    fallback = {
        "level": level_name,
        "repo_path": str(repo_path),
        "started_at": now_iso(),
        "ended_at": None,
        "success": False,
        "steps": [],
        "verify_errors": [],
    }
    try:
        for raw_step in steps:
            step = coerce_cleanup_step(raw_step)
            args = list(step.args)
            allow_failure = step.allow_failure
            cwd = common.resolve_path(step.cwd if step.cwd is not None else repo_path)
            result = ctx.runner.run(cwd, args, allow_failure=allow_failure)
            fallback["steps"].append(
                {
                    "cmd": [ctx.args.git, *args],
                    "cwd": str(cwd),
                    "exit_code": result.exit_code,
                    "allow_failure": allow_failure,
                }
            )
            if result.exit_code != 0 and not allow_failure:
                fallback["failure_reason"] = "CLEAN_COMMAND_FAILED"
                fallback["failure_detail"] = result.to_dict()
                return fallback
        ready, reasons = repository_ops.verify_repo_ready(ctx, repo_path)
        fallback["success"] = ready
        fallback["verify_errors"] = reasons
        return fallback
    except Exception as exc:  # noqa: BLE001 - cleanup must report structured fallback failures.
        fallback["failure_reason"] = getattr(exc, "reason", type(exc).__name__)
        fallback["failure_detail"] = _error_to_report(exc)
        return fallback
    finally:
        fallback["ended_at"] = now_iso()


def clean_flag(ctx: Any, *, remove_nested_git_repos: bool = False) -> str:
    return git_clean_flag(ctx.allow_clean_x, remove_nested_git_repos=remove_nested_git_repos)


def clean_and_prepare_repo(ctx: Any, repo_path: Path, ci_module: ModuleType | None = None) -> None:
    ctx.set_state(_phase(ci_module, "CLEANING"))
    for level in cleanup_steps.ci_prepare_cleanup_levels(ctx.allow_clean_x):
        fallback = run_cleanup_steps(ctx, repo_path, level.name, level.steps)
        ctx.report.add_fallback(fallback)
        if fallback["success"]:
            return

    deep_steps = cleanup_steps.ci_deep_submodule_cleanup_steps(
        repo_path,
        repository_ops.get_submodule_paths(ctx, repo_path),
        ctx.allow_clean_x,
    )

    fallback = run_cleanup_steps(ctx, repo_path, "level_4_deep_submodule_cleanup", deep_steps)
    ctx.report.add_fallback(fallback)
    if fallback["success"]:
        return

    _raise_ci_error(
        ci_module,
        "CLEAN_OR_RESTORE_FAILED",
        "CLEANING",
        "CLEAN_FAILED",
        "repository cleanup failed after all fallback levels",
        details={"last_fallback": fallback},
    )


def record_snapshot(
    ctx: Any,
    repo_path: Path,
    source_sha: str,
    target_sha: str,
    depth: int,
) -> dict[str, Any]:
    pre_merge_head = repository_ops.current_head(ctx, repo_path)
    snapshot = {
        "repo_path": str(repo_path),
        "pre_merge_head": pre_merge_head,
        "current_branch": repository_ops.current_branch(ctx, repo_path),
        "source_sha": source_sha,
        "target_sha": target_sha,
        "depth": depth,
        "timestamp": now_iso(),
    }
    ctx.snapshots.append(snapshot)
    ctx.report.add_snapshot(snapshot)
    return snapshot


def restore_repo_to_snapshot(ctx: Any, snapshot: dict[str, Any], ci_module: ModuleType | None = None) -> dict[str, Any]:
    repo_path = common.resolve_path(snapshot["repo_path"])
    pre_merge_head = snapshot["pre_merge_head"]
    branch = snapshot.get("current_branch")
    result = {
        "repo_path": str(repo_path),
        "pre_merge_head": pre_merge_head,
        "current_branch": branch,
        "started_at": now_iso(),
        "ended_at": None,
        "success": False,
        "errors": [],
    }
    try:
        for step in cleanup_steps.ci_snapshot_restore_steps(pre_merge_head, ctx.allow_clean_x):
            cwd = common.resolve_path(step.cwd if step.cwd is not None else repo_path)
            ctx.runner.run(cwd, list(step.args), allow_failure=step.allow_failure)
        if branch:
            checkout = ctx.runner.run(repo_path, ["checkout", branch], allow_failure=True)
            if checkout.exit_code != 0:
                result["errors"].append({"reason": "CHECKOUT_ORIGINAL_BRANCH_FAILED", "command": checkout.to_dict()})
        ready, reasons = repository_ops.verify_repo_ready(ctx, repo_path)
        result["success"] = ready
        result["errors"].extend({"reason": "RESTORE_VERIFY_FAILED", "message": item} for item in reasons)
    except Exception as exc:  # noqa: BLE001 - restore should collect errors and continue other snapshots.
        result["errors"].append(_error_to_report(exc))
    finally:
        result["ended_at"] = now_iso()
    return result


def restore_all_snapshots(ctx: Any, ci_module: ModuleType | None = None) -> bool:
    ctx.set_state(_phase(ci_module, "RESTORING"))
    restore_data = ctx.report.data.setdefault("restore", {"attempted": False, "success": False, "results": [], "errors": []})
    restore_data["attempted"] = True
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for snapshot in reversed(ctx.snapshots):
        repo = snapshot["repo_path"]
        if repo in seen:
            continue
        seen.add(repo)
        restore_result = restore_repo_to_snapshot(ctx, snapshot, ci_module)
        results.append(restore_result)
    success = bool(results) and all(item["success"] for item in results)
    restore_data["success"] = success
    restore_data["results"] = results
    for item in results:
        if not item["success"]:
            restore_data["errors"].extend(item.get("errors", []))
    return success
