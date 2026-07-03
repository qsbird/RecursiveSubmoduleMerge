"""CI cleanup, snapshot, and restore implementations."""

from __future__ import annotations

import shutil
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
    *,
    scope: dict[str, Any] | None = None,
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
    if scope:
        fallback["scope"] = scope
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


def _append_filesystem_step(
    fallback: dict[str, Any],
    *,
    action: str,
    cwd: Path,
    args: list[str],
    exit_code: int,
    allow_failure: bool = False,
) -> None:
    fallback["steps"].append(
        {
            "cmd": [action, *args],
            "cwd": str(cwd),
            "exit_code": exit_code,
            "allow_failure": allow_failure,
        }
    )


def _fresh_clone_preserve_refs(scope: dict[str, Any] | None) -> list[str]:
    if not isinstance(scope, dict):
        return []
    raw_refs = scope.get("preserve_refs") or []
    if isinstance(raw_refs, str):
        candidates = [raw_refs]
    else:
        try:
            candidates = list(raw_refs)
        except TypeError:
            candidates = []
    refs: list[str] = []
    seen: set[str] = set()
    for raw_ref in candidates:
        ref = str(raw_ref).strip()
        if ref and ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


def run_fresh_clone_fallback(
    ctx: Any,
    repo_path: Path,
    *,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fallback = {
        "level": "level_5_fresh_clone_workspace",
        "repo_path": str(repo_path),
        "started_at": now_iso(),
        "ended_at": None,
        "success": False,
        "steps": [],
        "verify_errors": [],
    }
    if scope:
        fallback["scope"] = scope

    try:
        output = getattr(ctx.report, "data", {}).get("output", {})
        if isinstance(output, dict) and output.get("generated_file_inside_repo"):
            fallback["failure_reason"] = "FRESH_CLONE_UNSAFE_OUTPUT_INSIDE_REPO"
            fallback["failure_detail"] = {
                "message": "fresh clone fallback is disabled when report/log/output paths are inside the repository"
            }
            return fallback

        parent = repo_path.parent
        token = common.sanitize_patch_name(now_iso(), "fresh-clone")
        staging = parent / f".{repo_path.name}.fresh-clone-{token}"
        backup = parent / f".{repo_path.name}.stale-before-fresh-clone-{token}"
        fallback["staging_path"] = str(staging)
        fallback["backup_path"] = str(backup)

        if staging.exists() or backup.exists():
            fallback["failure_reason"] = "FRESH_CLONE_TEMP_PATH_EXISTS"
            fallback["failure_detail"] = {"staging_path": str(staging), "backup_path": str(backup)}
            return fallback

        remote_name = str(getattr(ctx.args, "remote", "origin") or "origin")
        remote = ctx.runner.run(repo_path, ["remote", "get-url", remote_name], allow_failure=True)
        fallback["steps"].append(
            {
                "cmd": [ctx.args.git, "remote", "get-url", remote_name],
                "cwd": str(repo_path),
                "exit_code": remote.exit_code,
                "allow_failure": True,
            }
        )
        remote_url = remote.stdout.strip()
        fallback["remote"] = remote_name
        if remote.exit_code != 0 or not remote_url:
            fallback["failure_reason"] = "FRESH_CLONE_REMOTE_URL_UNAVAILABLE"
            fallback["failure_detail"] = remote.to_dict()
            return fallback
        fallback["remote_url"] = remote_url

        clone = ctx.runner.run(
            parent,
            ["clone", "--recurse-submodules", "--origin", remote_name, remote_url, staging.name],
            allow_failure=True,
        )
        fallback["steps"].append(
            {
                "cmd": [ctx.args.git, "clone", "--recurse-submodules", "--origin", remote_name, remote_url, staging.name],
                "cwd": str(parent),
                "exit_code": clone.exit_code,
                "allow_failure": True,
            }
        )
        if clone.exit_code != 0:
            fallback["failure_reason"] = "FRESH_CLONE_FAILED"
            fallback["failure_detail"] = clone.to_dict()
            return fallback

        for args in (
            ["submodule", "sync", "--recursive"],
            ["submodule", "update", "--init", "--recursive", "--force"],
        ):
            command = ctx.runner.run(staging, args, allow_failure=True)
            fallback["steps"].append(
                {
                    "cmd": [ctx.args.git, *args],
                    "cwd": str(staging),
                    "exit_code": command.exit_code,
                    "allow_failure": True,
                }
            )
            if command.exit_code != 0:
                fallback["failure_reason"] = "FRESH_CLONE_SUBMODULE_UPDATE_FAILED"
                fallback["failure_detail"] = command.to_dict()
                return fallback

        preserved_refs: list[str] = []
        for ref in _fresh_clone_preserve_refs(scope):
            fetch_args = ["fetch", "--no-tags", str(repo_path), ref]
            fetch = ctx.runner.run(staging, fetch_args, allow_failure=True)
            fallback["steps"].append(
                {
                    "cmd": [ctx.args.git, *fetch_args],
                    "cwd": str(staging),
                    "exit_code": fetch.exit_code,
                    "allow_failure": True,
                }
            )
            if fetch.exit_code != 0:
                fallback["failure_reason"] = "FRESH_CLONE_PRESERVE_REF_FAILED"
                fallback["failure_detail"] = {"ref": ref, "command": fetch.to_dict()}
                return fallback
            verify_args = ["rev-parse", "--verify", f"{ref}^{{commit}}"]
            verify = ctx.runner.run(staging, verify_args, allow_failure=True)
            fallback["steps"].append(
                {
                    "cmd": [ctx.args.git, *verify_args],
                    "cwd": str(staging),
                    "exit_code": verify.exit_code,
                    "allow_failure": True,
                }
            )
            if verify.exit_code != 0:
                fallback["failure_reason"] = "FRESH_CLONE_PRESERVE_REF_VERIFY_FAILED"
                fallback["failure_detail"] = {"ref": ref, "command": verify.to_dict()}
                return fallback
            preserved_refs.append(ref)
        if preserved_refs:
            fallback["preserved_refs"] = preserved_refs

        moved_old = False
        try:
            if repo_path.exists():
                shutil.move(str(repo_path), str(backup))
                moved_old = True
                _append_filesystem_step(
                    fallback,
                    action="filesystem",
                    cwd=parent,
                    args=["move", str(repo_path), str(backup)],
                    exit_code=0,
                )
            shutil.move(str(staging), str(repo_path))
            _append_filesystem_step(
                fallback,
                action="filesystem",
                cwd=parent,
                args=["move", str(staging), str(repo_path)],
                exit_code=0,
            )
        except Exception as exc:  # noqa: BLE001 - fallback must report and leave a structured trace.
            fallback["failure_reason"] = "FRESH_CLONE_WORKSPACE_SWAP_FAILED"
            fallback["failure_detail"] = {"type": type(exc).__name__, "message": str(exc)}
            if moved_old and not repo_path.exists() and backup.exists():
                try:
                    shutil.move(str(backup), str(repo_path))
                    _append_filesystem_step(
                        fallback,
                        action="filesystem",
                        cwd=parent,
                        args=["restore", str(backup), str(repo_path)],
                        exit_code=0,
                    )
                except Exception as restore_exc:  # noqa: BLE001 - report secondary restore failure.
                    fallback["restore_failure"] = {
                        "type": type(restore_exc).__name__,
                        "message": str(restore_exc),
                    }
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


def clean_and_prepare_repo(
    ctx: Any,
    repo_path: Path,
    ci_module: ModuleType | None = None,
    *,
    scope: dict[str, Any] | None = None,
) -> None:
    ctx.set_state(_phase(ci_module, "CLEANING"))
    for level in cleanup_steps.ci_prepare_cleanup_levels(ctx.allow_clean_x):
        fallback = run_cleanup_steps(ctx, repo_path, level.name, level.steps, scope=scope)
        ctx.report.add_fallback(fallback)
        if fallback["success"]:
            return

    deep_steps = cleanup_steps.ci_deep_submodule_cleanup_steps(
        repo_path,
        repository_ops.get_submodule_paths(ctx, repo_path),
        ctx.allow_clean_x,
    )

    fallback = run_cleanup_steps(ctx, repo_path, "level_4_deep_submodule_cleanup", deep_steps, scope=scope)
    ctx.report.add_fallback(fallback)
    if fallback["success"]:
        return

    fallback = run_fresh_clone_fallback(ctx, repo_path, scope=scope)
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


def ensure_ready_after_state_change(
    ctx: Any,
    repo_path: Path,
    ci_module: ModuleType | None = None,
    *,
    scope: dict[str, Any] | None = None,
    checkout_args: list[str] | tuple[str, ...] | None = None,
    update_submodules: bool = True,
    max_cleanup_attempts: int = 2,
) -> None:
    """Verify a checkout/update result, cleaning and replaying it when needed."""

    normalized_checkout = list(checkout_args) if checkout_args else None
    verify_history: list[dict[str, Any]] = []

    for attempt in range(max_cleanup_attempts + 1):
        if attempt > 0:
            if normalized_checkout:
                checkout = ctx.runner.run(repo_path, normalized_checkout, allow_failure=True)
                if checkout.exit_code != 0:
                    _raise_ci_error(
                        ci_module,
                        "CLEAN_OR_RESTORE_FAILED",
                        "CLEANING",
                        "POST_STATE_CHANGE_CHECKOUT_FAILED",
                        "failed to restore repository checkout after cleanup",
                        details={
                            "repo_path": str(repo_path),
                            "scope": scope or {},
                            "attempt": attempt,
                            "checkout_args": normalized_checkout,
                            "command": checkout.to_dict(),
                            "verify_history": verify_history,
                        },
                        restore_needed=True,
                    )
            if update_submodules:
                ctx.runner.run(
                    repo_path,
                    ["submodule", "update", "--init", "--recursive", "--force"],
                    allow_failure=True,
                )

        ready, reasons = repository_ops.verify_repo_ready(ctx, repo_path)
        verify_history.append({"attempt": attempt, "ready": ready, "reasons": reasons})
        if ready:
            return
        if attempt >= max_cleanup_attempts:
            _raise_ci_error(
                ci_module,
                "CLEAN_OR_RESTORE_FAILED",
                "CLEANING",
                "POST_STATE_CHANGE_REPO_NOT_READY",
                "repository is not ready after checkout/update cleanup retries",
                details={
                    "repo_path": str(repo_path),
                    "scope": scope or {},
                    "checkout_args": normalized_checkout,
                    "verify_history": verify_history,
                },
                restore_needed=True,
            )

        cleanup_scope = dict(scope or {})
        cleanup_scope.update(
            {
                "post_state_change_attempt": attempt + 1,
                "post_state_change_verify_errors": reasons,
            }
        )
        if normalized_checkout:
            cleanup_scope["checkout_args"] = normalized_checkout
        clean_and_prepare_repo(ctx, repo_path, ci_module, scope=cleanup_scope)


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
