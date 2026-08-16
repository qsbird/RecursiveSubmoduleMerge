"""Reporting and batch-state helper functions for CI merge commands."""

from __future__ import annotations

from typing import Any

from .models import now_iso
from .publish import target_requires_explicit_publish_target


def report_progress(ctx: Any, message: str) -> None:
    reporter = getattr(ctx, "report", None)
    progress = getattr(reporter, "progress", None)
    if callable(progress):
        progress(message)


def report_warning(ctx: Any, reason: str, message: str, details: dict[str, Any] | None = None) -> None:
    reporter = getattr(ctx, "report", None)
    add_warning = getattr(reporter, "add_warning", None)
    if callable(add_warning):
        add_warning(reason, message, details=details)
        return
    data = getattr(reporter, "data", None)
    if isinstance(data, dict):
        data.setdefault("warnings", []).append(
            {
                "reason": reason,
                "message": message,
                "details": details or {},
                "timestamp": now_iso(),
            }
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


def fallback_count(ctx: Any) -> int | None:
    report = getattr(ctx, "report", None)
    data = getattr(report, "data", None)
    if isinstance(data, dict) and isinstance(data.get("fallbacks"), list):
        return len(data["fallbacks"])
    fallbacks = getattr(report, "fallbacks", None)
    if isinstance(fallbacks, list):
        return len(fallbacks)
    return None


def fallback_range(start_index: int | None, end_index: int | None) -> dict[str, int | None]:
    if start_index is None or end_index is None:
        return {"start": None, "end": None, "count": None}
    if end_index <= start_index:
        return {"start": start_index, "end": None, "count": 0}
    return {"start": start_index, "end": end_index - 1, "count": end_index - start_index}


def batch_step_cleanup_scope(index: int, total: int, source: str, target: str) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "batch_step_start",
        "step_index": index,
        "step_total": total,
        "source": source,
        "target": target,
    }
    if index > 1:
        scope["preserve_refs"] = [target]
    return scope


def refresh_batch_step_summaries(batch: dict[str, Any]) -> tuple[list[int], list[int]]:
    steps = batch.get("steps") or []
    successful_steps = [
        int(step["index"])
        for step in steps
        if step.get("status") == "success" and "index" in step
    ]
    failed_steps = [
        int(step["index"])
        for step in steps
        if step.get("status") == "failed" and "index" in step
    ]
    batch["successful_steps"] = successful_steps
    batch["failed_steps"] = failed_steps
    return successful_steps, failed_steps


def serialize_non_gitlink_conflict(conflict: Any) -> dict[str, Any]:
    stages = []
    for stage in getattr(conflict, "stages", ()) or ():
        stages.append(
            {
                "mode": getattr(stage, "mode", None),
                "sha": getattr(stage, "sha", None),
                "stage": getattr(stage, "stage", None),
                "path": getattr(stage, "path", None),
                "raw": getattr(stage, "raw", None),
            }
        )
    kind = getattr(conflict, "kind", None)
    type_by_kind = {
        "file": "file_conflict",
        "gitmodules": "gitmodules_conflict",
        "unparseable": "unparseable_conflict",
        "unsupported_stage": "unsupported_conflict_stage",
        "gitlink": "gitlink_conflict",
    }
    return {
        "repo_path": str(getattr(conflict, "repo_path", "")),
        "path": getattr(conflict, "path", None),
        "type": type_by_kind.get(kind, "file_conflict" if kind != "gitlink" else "gitlink_conflict"),
        "kind": kind,
        "stages": stages,
        "action": "manual_required",
    }


def record_non_gitlink_conflicts(ctx: Any, conflicts: list[dict[str, Any]]) -> None:
    if not conflicts:
        return
    report = getattr(ctx, "report", None)
    add_conflicts = getattr(report, "add_conflicts", None)
    if callable(add_conflicts):
        add_conflicts(conflicts)


def command_result_to_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {
        "args": getattr(value, "args", None),
        "cwd": str(getattr(value, "cwd", "")),
        "returncode": getattr(value, "returncode", None),
        "stdout": getattr(value, "stdout", None),
        "stderr": getattr(value, "stderr", None),
    }


def mark_batch_step_failed(
    batch: dict[str, Any],
    step_entry: dict[str, Any],
    index: int,
    failure: dict[str, Any],
) -> None:
    step_entry["status"] = "failed"
    step_entry["failure"] = failure
    batch.setdefault("errors", []).append(failure)
    batch["failure_step"] = index
    refresh_batch_step_summaries(batch)


def mark_batch_failed(batch: dict[str, Any]) -> None:
    refresh_batch_step_summaries(batch)
    batch["status"] = "failed"


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
