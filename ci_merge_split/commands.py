"""CI command and workflow orchestration split out of ``ci_merge.py``."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from . import runtime
from .models import CiMergeError, CommandResult, ExitCode, MergeStepContext, Phase, PreflightResult, now_iso
from .preflight import preflight_failure_reason
from .publish import target_requires_explicit_publish_target
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


def set_resolved_fields(
    ctx: Any,
    *,
    source_ref: str | None,
    source_sha: str | None,
    target_ref: str | None,
    target_sha: str | None,
    merge_base_sha: str | None,
    result_sha: str | None = None,
) -> None:
    resolved = ctx.report.data["resolved"]
    resolved["source_ref"] = source_ref
    resolved["source_sha"] = source_sha
    resolved["target_ref"] = target_ref
    resolved["target_sha"] = target_sha
    resolved["merge_base_sha"] = merge_base_sha
    if result_sha is not None:
        resolved["result_sha"] = result_sha


def batch_step_artifact_scope(ctx: Any, artifact_prefix: str | None) -> dict[str, Any]:
    if not artifact_prefix:
        return {
            "prefix": None,
            "patch_dir": str(ctx.report.output_dir / "patches"),
            "conflict_dir": str(ctx.report.output_dir / "conflicts"),
            "file_dir": str(ctx.report.output_dir / "files"),
        }
    return {
        "prefix": artifact_prefix,
        "patch_dir": str(ctx.report.output_dir / "patches" / artifact_prefix),
        "conflict_dir": str(ctx.report.output_dir / "conflicts" / artifact_prefix),
        "file_dir": str(ctx.report.output_dir / "files" / artifact_prefix),
    }


def command_range(ctx: Any, start_index: int) -> dict[str, int | None]:
    commands = ctx.report.data.setdefault("commands", [])
    end_index = len(commands) - 1
    if end_index < start_index:
        return {"start": start_index, "end": None, "count": 0}
    return {"start": start_index, "end": end_index, "count": end_index - start_index + 1}


def restore_batch_continue_state(
    ctx: Any,
    repo_path: Path,
    target_sha: str,
    services: CiWorkflowServices | None = None,
) -> dict[str, Any]:
    resolved_services = services or CiWorkflowServices.from_local()
    ctx.set_state(Phase.RESTORING)
    clean_flag = "-fdx" if ctx.allow_clean_x else "-fd"
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
        for operation in ("merge", "rebase", "cherry-pick"):
            run([operation, "--abort"], allow_failure=True)
        run(["submodule", "foreach", "--recursive", "git merge --abort"], allow_failure=True)
        run(["submodule", "foreach", "--recursive", "git rebase --abort"], allow_failure=True)
        run(["submodule", "foreach", "--recursive", "git cherry-pick --abort"], allow_failure=True)
        run(["reset", "--hard", target_sha])
        run(["clean", clean_flag])
        run(["submodule", "foreach", "--recursive", "git reset --hard"], allow_failure=True)
        run(["submodule", "foreach", "--recursive", f"git clean {clean_flag}"], allow_failure=True)
        run(["submodule", "update", "--init", "--recursive", "--force"])
        ready, reasons = resolved_services.guards.verify_repo_ready(ctx, repo_path)
        result["success"] = ready
        result["verify_errors"] = reasons
    except CiMergeError as exc:
        result["failure"] = exc.to_report_error()
    finally:
        result["ended_at"] = now_iso()
    return result


def manual_publish_info(ctx: Any, original_target_sha: str, final_result_sha: str) -> dict[str, Any]:
    publish_target = ctx.args.publish_target
    if not publish_target and not target_requires_explicit_publish_target(ctx.args.target, ctx.args.remote):
        publish_target = ctx.args.target
    info = {
        "remote": ctx.args.remote,
        "target": publish_target,
        "expected_remote_sha": original_target_sha,
        "result_sha": final_result_sha,
        "supported": bool(publish_target),
    }
    if publish_target:
        info["command"] = (
            f"git push --recurse-submodules=check --force-with-lease={publish_target}:{original_target_sha} "
            f"{ctx.args.remote} HEAD:{publish_target}"
        )
    else:
        info["message"] = "--publish_target is required to build a manual publish command for this target"
    return info


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

    merge_result = resolved_merge_repo(
        ctx,
        repo_path,
        source_sha=initial.source_sha,
        target_sha=initial.target_sha,
        base_sha=initial.merge_base_sha,
        depth=0,
        display_path=".",
    )
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
            "command_range": {"start": command_start, "end": None, "count": 0},
            "artifact_scope": batch_step_artifact_scope(ctx, artifact_prefix),
            "continued_after_failure": False,
            "restore_after_failure": None,
        }
        batch["steps"].append(step_entry)

        try:
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
        except CiMergeError as exc:
            step_entry["status"] = "failed"
            step_entry["failure"] = exc.to_report_error()
            batch["errors"].append(exc.to_report_error())
            batch["failure_step"] = index
            last_error = exc
            if failure_mode != "continue-success":
                raise
            if not last_success_sha:
                raise
            restore_result = restore_batch_continue_state(ctx, repo_path, last_success_sha, resolved_services)
            step_entry["restore_after_failure"] = restore_result
            if not restore_result.get("success"):
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
            continue
        finally:
            ctx.merge_step = None
            step_entry["ended_at"] = now_iso()
            step_entry["command_range"] = command_range(ctx, command_start)

    successful_steps = [step["index"] for step in batch["steps"] if step.get("status") == "success"]
    failed_steps = [step["index"] for step in batch["steps"] if step.get("status") == "failed"]
    batch["successful_steps"] = successful_steps
    batch["failed_steps"] = failed_steps

    if not final_result_sha or not original_target_sha:
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
        raise CiMergeError(
            ExitCode.UNDEFINED_STATE,
            Phase.PREFLIGHT,
            "FINAL_PREFLIGHT_FAILED",
            "final repository preflight failed after merge",
            details={"failed_checks": final.failed_checks},
            restore_needed=True,
        )

    batch["manual_publish"] = manual_publish_info(ctx, original_target_sha, final_result_sha)
    if failed_steps:
        batch["status"] = "partial_success"
        ctx.report.data["status"] = "partial_success"
        ctx.report.data["exit_code"] = ExitCode.SUCCESS
        ctx.report.set_state(Phase.SUCCEEDED)
    else:
        batch["status"] = "success"
        ctx.report.set_success()


def run_workflow(
    ctx: Any,
    services: CiWorkflowServices | None = None,
    merge_repo: MergeRepoCallable | None = None,
) -> None:
    resolved_services = services or CiWorkflowServices.from_local()
    runtime.validate_args(ctx.args)
    repo_path = ctx.root_repo

    if ctx.args.publish_only:
        resolved_services.publish.run_publish_only_workflow(ctx)
        return

    if can_attempt_cleanup(ctx, repo_path):
        resolved_services.guards.clean_and_prepare_repo(ctx, repo_path)
        resolved_services.guards.sync_remote_refs(ctx, repo_path)

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
