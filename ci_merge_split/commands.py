"""CI command and workflow orchestration split out of ``ci_merge.py``."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from merge_cleanup_core import steps as cleanup_steps

from . import runtime
from . import repository_ops
from .command_reporting import (
    batch_step_artifact_scope,
    batch_step_cleanup_scope,
    command_range,
    command_result_to_dict,
    fallback_count,
    fallback_range,
    manual_publish_info,
    mark_batch_failed,
    mark_batch_step_failed,
    record_non_gitlink_conflicts,
    refresh_batch_step_summaries,
    report_progress,
    report_warning,
    serialize_non_gitlink_conflict,
    set_resolved_fields,
)
from .models import CiMergeError, CommandResult, ExitCode, MergeStepContext, Phase, PreflightResult, now_iso
from .preflight import preflight_failure_reason
from .workflow import CiWorkflowServices


COMMAND_FUNCTIONS: tuple[str, ...] = ()
MergeRepoCallable = Callable[..., Any]


def default_merge_repo(
    ctx: Any,
    repo_path: Path,
    *,
    source_sha: str,
    target_sha: str,
    base_sha: str | None,
    depth: int,
    display_path: str,
) -> Any:
    from merge_tool_adapters.ci_auto import CiAutoGitlinkMergeService

    return CiAutoGitlinkMergeService.from_ci_context(ctx).merge_repo(
        repo_path,
        source_sha=source_sha,
        target_sha=target_sha,
        base_sha=base_sha,
        depth=depth,
        display_path=display_path,
    )


def sync_root_refs_for_target_resolution(
    ctx: Any,
    repo_path: Path,
    services: CiWorkflowServices,
) -> None:
    ctx.set_state(Phase.SYNCING_REFS)
    command = services.guards.run_sync_command(
        ctx,
        repo_path,
        ["fetch", "--no-recurse-submodules", ctx.args.remote, "--prune"],
        "REMOTE_FETCH_FAILED",
        "failed to fetch remote refs before target checkout",
    )
    remote_sync = ctx.report.data.setdefault("remote_sync", {})
    if isinstance(remote_sync, dict):
        remote_sync["root_fetch_before_target_checkout"] = command.to_dict()


def checkout_detached_and_prepare(
    ctx: Any,
    repo_path: Path,
    services: CiWorkflowServices,
    target_sha: str,
    *,
    scope: dict[str, Any],
) -> None:
    checkout_args = ["checkout", "--detach", target_sha]
    checkout = ctx.runner.run(repo_path, checkout_args, allow_failure=True)
    if checkout.exit_code != 0:
        retry_scope = dict(scope)
        retry_scope.update(
            {
                "type": f"{scope.get('type', 'checkout')}_pre_checkout_retry",
                "checkout_failure": checkout.to_dict(),
            }
        )
        services.guards.clean_and_prepare_repo(ctx, repo_path, scope=retry_scope)
        checkout = ctx.runner.run(repo_path, checkout_args, allow_failure=True)
        if checkout.exit_code != 0:
            raise CiMergeError(
                ExitCode.CLEAN_OR_RESTORE_FAILED,
                Phase.CLEANING,
                "TARGET_CHECKOUT_FAILED_AFTER_CLEANUP",
                "failed to checkout target state after cleanup",
                details={"repo_path": str(repo_path), "target_sha": target_sha, "command": checkout.to_dict()},
                restore_needed=True,
            )
    ctx.runner.run(repo_path, ["submodule", "update", "--init", "--recursive", "--force"], allow_failure=True)
    services.guards.ensure_ready_after_state_change(
        ctx,
        repo_path,
        scope=scope,
        checkout_args=checkout_args,
    )


def prepare_batch_step(
    ctx: Any,
    repo_path: Path,
    services: CiWorkflowServices,
    step_entry: dict[str, Any],
    *,
    index: int,
    total: int,
    source: str,
    target: str,
) -> None:
    started_at = now_iso()
    start_count = fallback_count(ctx)
    cleanup_entry: dict[str, Any] = {
        "attempted": True,
        "success": False,
        "started_at": started_at,
        "ended_at": None,
        "fallback_range": {"start": start_count, "end": None, "count": None},
    }
    step_entry["pre_step_cleanup"] = cleanup_entry
    try:
        cleanup_scope = batch_step_cleanup_scope(index, total, source, target)
        target_resolution = repository_ops.resolve_commit_ref(ctx, repo_path, target)
        if target_resolution.sha:
            checkout_detached_and_prepare(
                ctx,
                repo_path,
                services,
                target_resolution.sha,
                scope=cleanup_scope,
            )
        cleanup_entry["success"] = True
    except Exception as exc:
        cleanup_entry["error"] = exception_to_batch_error(ctx, exc)
        raise
    finally:
        end_count = fallback_count(ctx)
        cleanup_entry["ended_at"] = now_iso()
        cleanup_entry["fallback_range"] = fallback_range(start_count, end_count)


def exception_to_batch_error(ctx: Any, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, CiMergeError):
        return exc.to_report_error()
    converted = convert_merge_exception(ctx, exc)
    if converted is not None:
        return converted.to_report_error()
    return {
        "exit_code": ExitCode.SYSTEM_EXCEPTION,
        "phase": ctx.report.data.get("state") or Phase.INIT,
        "reason": "SYSTEM_EXCEPTION",
        "message": str(exc),
        "details": {"type": type(exc).__name__},
        "timestamp": now_iso(),
    }


def convert_commit_availability_exception(ctx: Any, exc: Exception) -> CiMergeError | None:
    try:
        from split_gitlink_merge import RequiredCommitFetchError, RequiredCommitMissingError
    except ImportError:
        return None

    repo_path = getattr(exc, "repo_path", None)
    sha = getattr(exc, "sha", None)
    details = {
        "type": type(exc).__name__,
        "repo_path": str(repo_path) if repo_path is not None else None,
        "sha": sha,
    }
    if isinstance(exc, RequiredCommitFetchError):
        details["command"] = command_result_to_dict(getattr(exc, "fetch_result", None))
        return CiMergeError(
            ExitCode.GIT_COMMAND_FAILED,
            Phase.MERGING_SUBMODULE,
            "SUBMODULE_COMMIT_FETCH_FAILED",
            str(exc),
            details=details,
            restore_needed=True,
        )
    if isinstance(exc, RequiredCommitMissingError):
        details["command"] = command_result_to_dict(getattr(exc, "check_result", None))
        return CiMergeError(
            ExitCode.SUBMODULE_CONFLICT,
            Phase.MERGING_SUBMODULE,
            "SUBMODULE_COMMIT_NOT_FOUND",
            str(exc),
            details=details,
            restore_needed=True,
        )
    return None


def convert_skippable_merge_exception(ctx: Any, exc: Exception) -> CiMergeError | None:
    try:
        from split_gitlink_merge import NonGitlinkConflictError
    except ImportError:
        return None

    if not isinstance(exc, NonGitlinkConflictError):
        return None

    repo_path = getattr(exc, "repo_path", None)
    resolved_repo = Path(repo_path).resolve(strict=False) if repo_path is not None else None
    root_repo = Path(getattr(ctx, "root_repo", Path.cwd())).resolve(strict=False)
    is_root_conflict = resolved_repo is None or resolved_repo == root_repo
    raw_conflicts = list(getattr(exc, "conflicts", []) or [])
    non_gitlink_conflicts = [
        conflict for conflict in raw_conflicts if getattr(conflict, "kind", None) != "gitlink"
    ]
    conflicts = [
        serialize_non_gitlink_conflict(conflict)
        for conflict in (non_gitlink_conflicts or raw_conflicts)
    ]
    record_non_gitlink_conflicts(ctx, conflicts)
    conflict_paths = [
        str(conflict.get("path"))
        for conflict in conflicts
        if conflict.get("path") is not None
    ]
    return CiMergeError(
        ExitCode.ROOT_CONFLICT if is_root_conflict else ExitCode.SUBMODULE_CONFLICT,
        Phase.MERGING_ROOT if is_root_conflict else Phase.MERGING_SUBMODULE,
        "NON_GITLINK_CONFLICT",
        str(exc),
        details={
            "type": type(exc).__name__,
            "repo_path": str(repo_path) if repo_path is not None else None,
            "conflicts": conflicts,
            "conflict_paths": conflict_paths,
            "conflict_count": len(conflicts),
        },
        restore_needed=True,
    )


def convert_merge_exception(ctx: Any, exc: Exception) -> CiMergeError | None:
    converted = convert_commit_availability_exception(ctx, exc)
    if converted is not None:
        return converted
    return convert_skippable_merge_exception(ctx, exc)

def restore_batch_continue_state(
    ctx: Any,
    repo_path: Path,
    target_sha: str,
    services: CiWorkflowServices | None = None,
) -> dict[str, Any]:
    resolved_services = services or CiWorkflowServices.from_local()
    ctx.set_state(Phase.RESTORING)
    result: dict[str, Any] = {
        "target_sha": target_sha,
        "started_at": now_iso(),
        "ended_at": None,
        "success": False,
        "steps": [],
        "verify_errors": [],
    }

    def run(args: list[str], allow_failure: bool = False) -> CommandResult:
        command = ctx.runner.run(repo_path, args, allow_failure=allow_failure)
        result["steps"].append(
            {
                "cmd": [ctx.args.git, *args],
                "cwd": str(repo_path),
                "exit_code": command.exit_code,
                "allow_failure": allow_failure,
            }
        )
        return command

    try:
        for step in cleanup_steps.ci_batch_continue_restore_steps(target_sha, ctx.allow_clean_x):
            run(list(step.args), allow_failure=step.allow_failure)
        ready, reasons = resolved_services.guards.verify_repo_ready(ctx, repo_path)
        result["success"] = ready
        result["verify_errors"] = reasons
    except CiMergeError as exc:
        result["failure"] = exc.to_report_error()
    finally:
        result["ended_at"] = now_iso()
    return result

def can_attempt_cleanup(ctx: Any, repo_path: Path) -> bool:
    if not repo_path.exists() or not repo_path.is_dir():
        return False
    probe = ctx.runner.run(repo_path, ["rev-parse", "--is-inside-work-tree"], allow_failure=True)
    return probe.exit_code == 0 and probe.stdout.strip() == "true"


def fail_preflight(result: PreflightResult) -> None:
    raise CiMergeError(
        ExitCode.PREFLIGHT_FAILED,
        Phase.PREFLIGHT,
        preflight_failure_reason(result),
        "preflight check failed",
        details={"failed_checks": result.failed_checks},
        restore_needed=False,
    )


def ensure_preflight_shas(result: PreflightResult) -> None:
    if result.source_sha and result.target_sha:
        return
    raise CiMergeError(
        ExitCode.UNDEFINED_STATE,
        Phase.RESOLVING_REFS,
        "RESOLVED_SHA_MISSING",
        "preflight passed but source or target SHA is missing",
        details={"preflight": result.__dict__},
        restore_needed=False,
    )


def run_single_workflow(
    ctx: Any,
    services: CiWorkflowServices | None = None,
    merge_repo: MergeRepoCallable | None = None,
) -> None:
    resolved_services = services or CiWorkflowServices.from_local()
    resolved_merge_repo = merge_repo or default_merge_repo
    repo_path = ctx.root_repo
    report_progress(ctx, f"SINGLE start source={ctx.args.source} target={ctx.args.target}")

    initial = resolved_services.guards.preflight_check(
        ctx,
        repo_path,
        ctx.args.source,
        ctx.args.target,
        label="initial",
        resolve_refs=True,
    )
    if not initial.passed:
        fail_preflight(initial)

    ensure_preflight_shas(initial)
    set_resolved_fields(
        ctx,
        source_ref=initial.source_ref,
        source_sha=initial.source_sha,
        target_ref=initial.target_ref,
        target_sha=initial.target_sha,
        merge_base_sha=initial.merge_base_sha,
    )

    try:
        merge_result = resolved_merge_repo(
            ctx,
            repo_path,
            source_sha=initial.source_sha,
            target_sha=initial.target_sha,
            base_sha=initial.merge_base_sha,
            depth=0,
            display_path=".",
        )
    except CiMergeError:
        raise
    except Exception as exc:
        converted = convert_merge_exception(ctx, exc)
        if converted is not None:
            report_progress(ctx, f"SINGLE failed reason={converted.reason}")
            raise converted from exc
        raise
    ctx.report.data["resolved"]["result_sha"] = merge_result.result_sha

    final = resolved_services.guards.preflight_check(
        ctx,
        repo_path,
        None,
        None,
        label="final",
        resolve_refs=False,
    )
    if not final.passed:
        raise CiMergeError(
            ExitCode.UNDEFINED_STATE,
            Phase.PREFLIGHT,
            "FINAL_PREFLIGHT_FAILED",
            "final repository preflight failed after merge",
            details={"failed_checks": final.failed_checks},
            restore_needed=True,
        )

    if ctx.args.publish:
        resolved_services.publish.publish_result(ctx, repo_path, initial.target_sha, merge_result.result_sha)
    ctx.report.set_success()
    report_progress(ctx, f"SINGLE success result={merge_result.result_sha}")


def run_batch_workflow(
    ctx: Any,
    services: CiWorkflowServices | None = None,
    merge_repo: MergeRepoCallable | None = None,
) -> None:
    resolved_services = services or CiWorkflowServices.from_local()
    resolved_merge_repo = merge_repo or default_merge_repo
    repo_path = ctx.root_repo
    sources = list(ctx.args.sources)
    batch = ctx.report.data["batch"]
    failure_mode = ctx.args.batch_failure_mode
    batch["failure_mode"] = failure_mode
    batch["status"] = "running"
    report_progress(
        ctx,
        f"BATCH start sources={len(sources)} target={ctx.args.target} mode={failure_mode}",
    )
    current_target = ctx.args.target
    original_target_ref: str | None = None
    original_target_sha: str | None = None
    original_merge_base_sha: str | None = None
    final_result_sha: str | None = None
    last_source_ref: str | None = None
    last_source_sha: str | None = None
    last_success_sha: str | None = None
    last_error: CiMergeError | None = None

    for index, source in enumerate(sources, start=1):
        artifact_prefix = f"batch/step-{index:02d}"
        command_start = len(ctx.report.data.setdefault("commands", []))
        step_entry: dict[str, Any] = {
            "index": index,
            "total": len(sources),
            "source": source,
            "target": current_target,
            "status": "running",
            "started_at": now_iso(),
            "ended_at": None,
            "source_ref": None,
            "source_sha": None,
            "target_ref": None,
            "target_sha": None,
            "merge_base_sha": None,
            "result_sha": None,
            "strategy": None,
            "preflight": None,
            "pre_step_cleanup": None,
            "command_range": {"start": command_start, "end": None, "count": 0},
            "artifact_scope": batch_step_artifact_scope(ctx, artifact_prefix),
            "continued_after_failure": False,
            "restore_after_failure": None,
        }
        batch["steps"].append(step_entry)
        report_progress(ctx, f"STEP {index}/{len(sources)} start source={source} target={current_target}")

        try:
            prepare_batch_step(
                ctx,
                repo_path,
                resolved_services,
                step_entry,
                index=index,
                total=len(sources),
                source=source,
                target=current_target,
            )
            preflight = resolved_services.guards.preflight_check(
                ctx,
                repo_path,
                source,
                current_target,
                label="initial" if index == 1 else f"batch_step_{index:02d}",
                resolve_refs=True,
            )
            step_entry["preflight"] = {
                "passed": preflight.passed,
                "failed_checks": preflight.failed_checks,
            }
            if index == 1:
                original_target_ref = preflight.target_ref
                original_target_sha = preflight.target_sha
                original_merge_base_sha = preflight.merge_base_sha
                batch["original_target_ref"] = original_target_ref
                batch["original_target_sha"] = original_target_sha
                last_success_sha = original_target_sha

            step_entry.update(
                {
                    "source_ref": preflight.source_ref,
                    "source_sha": preflight.source_sha,
                    "target_ref": preflight.target_ref,
                    "target_sha": preflight.target_sha,
                    "merge_base_sha": preflight.merge_base_sha,
                }
            )
            if not preflight.passed:
                fail_preflight(preflight)
            ensure_preflight_shas(preflight)
            set_resolved_fields(
                ctx,
                source_ref=preflight.source_ref,
                source_sha=preflight.source_sha,
                target_ref=preflight.target_ref,
                target_sha=preflight.target_sha,
                merge_base_sha=preflight.merge_base_sha,
            )

            ctx.merge_step = MergeStepContext(
                index=index,
                total=len(sources),
                source=source,
                source_ref=preflight.source_ref,
                source_sha=preflight.source_sha or "",
                target=current_target,
                target_ref=preflight.target_ref,
                target_sha=preflight.target_sha or "",
                merge_base_sha=preflight.merge_base_sha,
                artifact_prefix=artifact_prefix,
            )
            merge_result = resolved_merge_repo(
                ctx,
                repo_path,
                source_sha=preflight.source_sha,
                target_sha=preflight.target_sha,
                base_sha=preflight.merge_base_sha,
                depth=0,
                display_path=".",
            )
            final_result_sha = merge_result.result_sha
            last_source_ref = preflight.source_ref
            last_source_sha = preflight.source_sha
            current_target = merge_result.result_sha
            last_success_sha = merge_result.result_sha
            batch["final_result_sha"] = final_result_sha
            ctx.report.data["resolved"]["result_sha"] = final_result_sha
            step_entry.update(
                {
                    "status": "success",
                    "result_sha": merge_result.result_sha,
                    "strategy": merge_result.strategy,
                }
            )
            report_progress(ctx, f"STEP {index}/{len(sources)} success result={merge_result.result_sha}")
        except CiMergeError as exc:
            mark_batch_step_failed(batch, step_entry, index, exc.to_report_error())
            last_error = exc
            report_progress(ctx, f"STEP {index}/{len(sources)} failed reason={exc.reason}")
            if failure_mode != "continue-success":
                mark_batch_failed(batch)
                raise
            if not last_success_sha:
                mark_batch_failed(batch)
                raise
            restore_result = restore_batch_continue_state(ctx, repo_path, last_success_sha, resolved_services)
            step_entry["restore_after_failure"] = restore_result
            if not restore_result.get("success"):
                mark_batch_failed(batch)
                raise CiMergeError(
                    ExitCode.CLEAN_OR_RESTORE_FAILED,
                    Phase.RESTORING,
                    "BATCH_CONTINUE_RESTORE_FAILED",
                    "failed to restore repository before continuing batch merge",
                    details={"step": step_entry, "restore": restore_result},
                    restore_needed=bool(ctx.snapshots),
                )
            current_target = last_success_sha
            step_entry["continued_after_failure"] = True
            report_progress(ctx, f"STEP {index}/{len(sources)} skipped continue_from={last_success_sha}")
            continue
        except Exception as exc:
            converted = convert_merge_exception(ctx, exc)
            if converted is not None:
                mark_batch_step_failed(batch, step_entry, index, converted.to_report_error())
                last_error = converted
                report_progress(ctx, f"STEP {index}/{len(sources)} failed reason={converted.reason}")
                if failure_mode != "continue-success":
                    mark_batch_failed(batch)
                    raise converted from exc
                if not last_success_sha:
                    mark_batch_failed(batch)
                    raise converted from exc
                restore_result = restore_batch_continue_state(ctx, repo_path, last_success_sha, resolved_services)
                step_entry["restore_after_failure"] = restore_result
                if not restore_result.get("success"):
                    mark_batch_failed(batch)
                    raise CiMergeError(
                        ExitCode.CLEAN_OR_RESTORE_FAILED,
                        Phase.RESTORING,
                        "BATCH_CONTINUE_RESTORE_FAILED",
                        "failed to restore repository before continuing batch merge",
                        details={"step": step_entry, "restore": restore_result},
                        restore_needed=bool(ctx.snapshots),
                    ) from exc
                current_target = last_success_sha
                step_entry["continued_after_failure"] = True
                report_progress(ctx, f"STEP {index}/{len(sources)} skipped continue_from={last_success_sha}")
                continue
            failure = exception_to_batch_error(ctx, exc)
            mark_batch_step_failed(batch, step_entry, index, failure)
            report_progress(ctx, f"STEP {index}/{len(sources)} failed reason={failure['reason']}")
            mark_batch_failed(batch)
            raise
        finally:
            ctx.merge_step = None
            step_entry["ended_at"] = now_iso()
            step_entry["command_range"] = command_range(ctx, command_start)

    successful_steps, failed_steps = refresh_batch_step_summaries(batch)

    if not final_result_sha or not original_target_sha:
        mark_batch_failed(batch)
        if last_error:
            raise last_error
        raise CiMergeError(
            ExitCode.UNDEFINED_STATE,
            Phase.MERGING_ROOT,
            "BATCH_RESULT_SHA_MISSING",
            "batch merge completed without a final result SHA",
            details={"batch": batch},
            restore_needed=bool(ctx.snapshots),
        )

    set_resolved_fields(
        ctx,
        source_ref=last_source_ref,
        source_sha=last_source_sha,
        target_ref=original_target_ref,
        target_sha=original_target_sha,
        merge_base_sha=original_merge_base_sha,
        result_sha=final_result_sha,
    )

    final = resolved_services.guards.preflight_check(
        ctx,
        repo_path,
        None,
        None,
        label="final",
        resolve_refs=False,
    )
    if not final.passed:
        mark_batch_failed(batch)
        raise CiMergeError(
            ExitCode.UNDEFINED_STATE,
            Phase.PREFLIGHT,
            "FINAL_PREFLIGHT_FAILED",
            "final repository preflight failed after merge",
            details={"failed_checks": final.failed_checks},
            restore_needed=True,
        )

    batch["manual_publish"] = manual_publish_info(ctx, original_target_sha, final_result_sha)
    if ctx.args.publish:
        if failed_steps and not bool(getattr(ctx.args, "allow_partial_publish", False)):
            report_warning(
                ctx,
                "BATCH_PARTIAL_PUBLISH_SKIPPED",
                "batch publish was skipped because one or more source branches failed; "
                "set --allow_partial_publish true to publish the partial_success result",
                details={"successful_steps": successful_steps, "failed_steps": failed_steps},
            )
            report_progress(ctx, "BATCH publish skipped reason=BATCH_PARTIAL_PUBLISH_SKIPPED")
        else:
            resolved_services.publish.publish_result(ctx, repo_path, original_target_sha, final_result_sha)
            report_progress(ctx, f"BATCH publish success result={final_result_sha}")

    if failed_steps:
        batch["status"] = "partial_success"
        ctx.report.data["status"] = "partial_success"
        ctx.report.data["exit_code"] = ExitCode.PARTIAL_SUCCESS
        ctx.report.set_state(Phase.SUCCEEDED)
        report_progress(
            ctx,
            f"BATCH partial_success success={successful_steps} failed={failed_steps} result={final_result_sha}",
        )
    else:
        batch["status"] = "success"
        ctx.report.set_success()
        report_progress(ctx, f"BATCH success steps={successful_steps} result={final_result_sha}")


def run_workflow(
    ctx: Any,
    services: CiWorkflowServices | None = None,
    merge_repo: MergeRepoCallable | None = None,
) -> None:
    resolved_services = services or CiWorkflowServices.from_local()
    runtime.validate_args(ctx.args)
    repo_path = ctx.root_repo
    source_count = len(ctx.args.sources)
    mode = "batch" if source_count > 1 else "single"
    report_progress(ctx, f"CI start mode={mode} sources={source_count} target={ctx.args.target}")

    if ctx.args.publish_only:
        report_progress(ctx, "PUBLISH_ONLY start")
        resolved_services.publish.run_publish_only_workflow(ctx)
        report_progress(ctx, "PUBLISH_ONLY done")
        return

    if can_attempt_cleanup(ctx, repo_path):
        report_progress(ctx, "PREPARE pre-checkout repository start")
        resolved_services.guards.clean_and_prepare_repo(
            ctx,
            repo_path,
            scope={"type": "workflow_pre_checkout_prepare", "mode": mode, "source_count": source_count},
        )
        report_progress(ctx, "PREPARE pre-checkout repository done")

        report_progress(ctx, "SYNC root refs for target checkout start")
        sync_root_refs_for_target_resolution(ctx, repo_path, resolved_services)
        report_progress(ctx, "SYNC root refs for target checkout done")

        target_sha_for_checkout: str | None = None
        target_checkout_scope = {
            "type": "workflow_target_checkout",
            "mode": mode,
            "source_count": source_count,
            "target": ctx.args.target,
        }
        if ctx.args.target:
            target_resolution = repository_ops.resolve_commit_ref(ctx, repo_path, ctx.args.target)
            target_sha_for_checkout = target_resolution.sha
            if target_sha_for_checkout:
                report_progress(ctx, "CHECKOUT target repository start")
                checkout_detached_and_prepare(
                    ctx,
                    repo_path,
                    resolved_services,
                    target_sha_for_checkout,
                    scope=target_checkout_scope,
                )
                report_progress(ctx, "CHECKOUT target repository done")

        report_progress(ctx, "SYNC remote refs start")
        resolved_services.guards.sync_remote_refs(ctx, repo_path)
        report_progress(ctx, "SYNC remote refs done")

        if target_sha_for_checkout:
            report_progress(ctx, "CHECKOUT target after sync start")
            post_sync_scope = dict(target_checkout_scope)
            post_sync_scope["type"] = "workflow_post_sync_target_checkout"
            checkout_detached_and_prepare(
                ctx,
                repo_path,
                resolved_services,
                target_sha_for_checkout,
                scope=post_sync_scope,
            )
            report_progress(ctx, "CHECKOUT target after sync done")

    if len(ctx.args.sources) > 1:
        run_batch_workflow(ctx, resolved_services, merge_repo)
    else:
        run_single_workflow(ctx, resolved_services, merge_repo)


def handle_failure(
    ctx: Any,
    error: CiMergeError,
    services: CiWorkflowServices | None = None,
) -> int:
    resolved_services = services or CiWorkflowServices.from_local()
    ctx.report.add_error(error)
    ctx.report.set_failure(error)
    report_progress(ctx, f"CI failed reason={error.reason} exit={error.exit_code}")
    if error.restore_needed:
        restore_ok = resolved_services.snapshots.restore_all_snapshots(ctx)
        if not restore_ok:
            restore_error = {
                "exit_code": ExitCode.CLEAN_OR_RESTORE_FAILED,
                "phase": Phase.RESTORING,
                "reason": "RESTORE_FAILED",
                "message": "one or more repositories failed to restore",
                "details": ctx.report.data["restore"],
                "timestamp": now_iso(),
            }
            ctx.report.data["errors"].append(restore_error)
    return error.exit_code


def main(
    argv: list[str] | None = None,
    *,
    services: CiWorkflowServices | None = None,
    merge_repo: MergeRepoCallable | None = None,
) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    try:
        args = runtime.parse_arguments(argv)
    except SystemExit as exc:
        return int(exc.code)

    resolved_services = services or CiWorkflowServices.from_local()
    ctx = runtime.create_execution_context(args)
    if ctx.report.data["output"]["generated_file_inside_repo"]:
        print(
            "warning: generated report/log/output path is inside the target repository",
            file=sys.stderr,
        )

    exit_code = ExitCode.SUCCESS
    try:
        run_workflow(ctx, resolved_services, merge_repo)
    except CiMergeError as exc:
        exit_code = handle_failure(ctx, exc, resolved_services)
    except Exception as exc:  # noqa: BLE001 - top-level guard must report system exceptions.
        error = CiMergeError(
            ExitCode.SYSTEM_EXCEPTION,
            ctx.report.data.get("state") or Phase.INIT,
            "SYSTEM_EXCEPTION",
            str(exc),
            details={"type": type(exc).__name__},
            restore_needed=bool(ctx.snapshots),
        )
        exit_code = handle_failure(ctx, error, resolved_services)

    ctx.report.finalize(exit_code)
    if not ctx.report.write():
        ctx.report.console(f"REPORT_WRITE_FAILED path={ctx.report.report_path}")
        return exit_code if exit_code != ExitCode.SUCCESS else ExitCode.SYSTEM_EXCEPTION
    ctx.report.console(
        "FINISHED "
        f"status={ctx.report.data.get('status')} "
        f"exit_code={ctx.report.data.get('exit_code')} "
        f"report={ctx.report.report_path} "
        f"log={ctx.report.log_path}"
    )
    return int(ctx.report.data["exit_code"])


@dataclass(frozen=True)
class CiCommandService:
    """Facade for CI merge workflow entry points."""

    services: CiWorkflowServices
    merge_repo: MergeRepoCallable = default_merge_repo

    @classmethod
    def from_local(cls) -> "CiCommandService":
        return cls(CiWorkflowServices.from_local())

    @classmethod
    def from_module(cls, ci_module: ModuleType) -> "CiCommandService":
        def merge_repo_with_module(ctx: Any, repo_path: Path, **kwargs: Any) -> Any:
            from merge_tool_adapters.ci_auto import CiAutoGitlinkMergeService

            return CiAutoGitlinkMergeService.from_ci_context(ctx, ci_module).merge_repo(repo_path, **kwargs)

        return cls(CiWorkflowServices.from_module(ci_module), merge_repo_with_module)

    def run_single_workflow(self, ctx: Any) -> None:
        run_single_workflow(ctx, self.services, self.merge_repo)

    def run_batch_workflow(self, ctx: Any) -> None:
        run_batch_workflow(ctx, self.services, self.merge_repo)

    def run_workflow(self, ctx: Any) -> None:
        run_workflow(ctx, self.services, self.merge_repo)

    def handle_failure(self, ctx: Any, error: CiMergeError) -> int:
        return handle_failure(ctx, error, self.services)

    def main(self, argv: list[str] | None = None) -> int:
        return main(argv, services=self.services, merge_repo=self.merge_repo)
