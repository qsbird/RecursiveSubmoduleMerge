"""CI report, conflict, patch, and file artifact implementations."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

from . import common
from .models import ConflictAnalysis, Phase
from .repository_ops import display_path_for


def _conflict_analysis_class(ci_module: ModuleType | None) -> type[Any]:
    return getattr(ci_module, "ConflictAnalysis", ConflictAnalysis) if ci_module is not None else ConflictAnalysis


def write_binary_artifact(ctx: Any, path: Path, content: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "path": str(path),
        "relative_path": relative_output_path(ctx, path),
        "size_bytes": path.stat().st_size,
    }


def write_json_artifact(ctx: Any, path: Path, data: dict[str, Any]) -> dict[str, Any]:
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    record = write_binary_artifact(ctx, path, content)
    record["encoding"] = "utf-8"
    return record


def conflict_artifact_dir(ctx: Any, repo_path: Path, conflict: dict[str, Any]) -> Path:
    repo_name = common.sanitize_patch_name(display_path_for(ctx, repo_path), "root")
    path = str(conflict.get("path") or "unknown")
    sha = common.stage_sha(conflict, "2") or common.stage_sha(conflict, "1") or common.stage_sha(conflict, "3") or "no-sha"
    file_name = f"{common.sanitize_patch_name(path, 'conflict')}-{sha[:12]}"
    return artifact_category_dir(ctx, "conflicts") / repo_name / file_name


def export_conflict_stage_artifact(
    ctx: Any,
    repo_path: Path,
    artifact_dir: Path,
    stage: dict[str, str],
    warnings: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None]:
    stage_number = stage.get("stage") or ""
    label = common.CONFLICT_STAGE_LABELS.get(stage_number, f"stage_{stage_number or 'unknown'}")
    sha = stage.get("sha")
    if not sha:
        warnings.append({"reason": "CONFLICT_STAGE_SHA_MISSING", "stage": stage})
        return label, None

    type_result = ctx.runner.run(repo_path, ["cat-file", "-t", sha], allow_failure=True)
    object_type = type_result.stdout.strip() if type_result.exit_code == 0 else None
    record: dict[str, Any] = {
        "role": label,
        "stage": stage_number,
        "mode": stage.get("mode"),
        "sha": sha,
        "object_type": object_type,
    }
    if type_result.exit_code != 0:
        warnings.append({"reason": "CONFLICT_STAGE_TYPE_FAILED", "stage": stage, "command": type_result.to_dict()})
        return label, record
    if object_type != "blob":
        warnings.append({"reason": "CONFLICT_STAGE_NOT_BLOB", "stage": stage, "object_type": object_type})
        return label, record

    content_result, content = ctx.runner.run_binary(repo_path, ["cat-file", "-p", sha], allow_failure=True)
    if content_result.exit_code != 0:
        warnings.append({"reason": "CONFLICT_STAGE_EXPORT_FAILED", "stage": stage, "command": content_result.to_dict()})
        return label, record
    try:
        record.update(write_binary_artifact(ctx, artifact_dir / label, content))
    except OSError as exc:
        warnings.append({"reason": "CONFLICT_STAGE_WRITE_FAILED", "stage": stage, "message": str(exc)})
    return label, record


def export_worktree_conflict_artifact(
    ctx: Any,
    repo_path: Path,
    artifact_dir: Path,
    conflict_path: str,
    warnings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    source_path = common.resolve_path(repo_path / conflict_path)
    if not common.is_path_inside(source_path, repo_path):
        warnings.append({"reason": "CONFLICT_WORKTREE_PATH_OUTSIDE_REPO", "path": str(source_path)})
        return None
    if not source_path.exists() or source_path.is_dir():
        warnings.append({"reason": "CONFLICT_WORKTREE_FILE_MISSING", "path": str(source_path)})
        return None
    try:
        content = source_path.read_bytes()
        record = write_binary_artifact(ctx, artifact_dir / "worktree", content)
        record.update({"role": "worktree", "source_path": str(source_path)})
        return record
    except OSError as exc:
        warnings.append({"reason": "CONFLICT_WORKTREE_EXPORT_FAILED", "path": str(source_path), "message": str(exc)})
        return None


def export_conflict_artifacts(ctx: Any, repo_path: Path, conflict: dict[str, Any]) -> dict[str, Any]:
    artifact_dir = conflict_artifact_dir(ctx, repo_path, conflict)
    warnings: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {
        "artifact_dir": str(artifact_dir),
        "artifact_relative_dir": relative_output_path(ctx, artifact_dir),
        "stages": {},
        "worktree": None,
        "warnings": warnings,
    }

    for stage in conflict.get("stages") or []:
        label, record = export_conflict_stage_artifact(ctx, repo_path, artifact_dir, stage, warnings)
        if label and record:
            artifacts["stages"][label] = record

    conflict_path = conflict.get("path")
    if conflict_path:
        artifacts["worktree"] = export_worktree_conflict_artifact(
            ctx,
            repo_path,
            artifact_dir,
            str(conflict_path),
            warnings,
        )

    metadata = {
        "repo_path": str(repo_path),
        "display_path": display_path_for(ctx, repo_path),
        "path": conflict.get("path"),
        "type": conflict.get("type"),
        "action": conflict.get("action"),
        "stages": conflict.get("stages") or [],
        "artifacts": {key: value for key, value in artifacts.items() if key != "metadata"},
    }
    try:
        artifacts["metadata"] = write_json_artifact(ctx, artifact_dir / "metadata.json", metadata)
    except OSError as exc:
        warnings.append({"reason": "CONFLICT_METADATA_WRITE_FAILED", "message": str(exc)})
    return artifacts


def analyze_conflicts(ctx: Any, repo_path: Path, ci_module: ModuleType | None = None) -> Any:
    ctx.set_state(Phase.ANALYZING_CONFLICTS)
    names = ctx.runner.run(repo_path, ["diff", "--name-only", "--diff-filter=U"])
    unmerged = ctx.runner.run(repo_path, ["ls-files", "-u"])
    grouped = common.parse_unmerged_index(unmerged.stdout)
    paths = sorted(set(common.split_lines(names.stdout)) | set(grouped.keys()))
    conflicts: list[dict[str, Any]] = []
    gitlink_conflicts: list[dict[str, Any]] = []

    if not paths:
        unknown = {
            "repo_path": str(repo_path),
            "path": None,
            "type": "unknown_conflict_state",
            "action": "manual_required",
        }
        return _conflict_analysis_class(ci_module)([unknown], [])

    for path in paths:
        stages = grouped.get(path, [])
        stage_numbers = {stage.get("stage") for stage in stages}
        modes = {stage.get("mode") for stage in stages}
        entry = {
            "repo_path": str(repo_path),
            "path": path,
            "stages": stages,
            "action": "manual_required",
        }
        if path == ".gitmodules":
            entry["type"] = "gitmodules_conflict"
        elif not stages or "<unparseable>" == path:
            entry["type"] = "unparseable_conflict"
        elif not {"1", "2", "3"}.issubset(stage_numbers):
            entry["type"] = "unsupported_conflict_stage"
        elif modes == {"160000"}:
            submodule_path = repo_path / path
            if not submodule_path.exists():
                entry["type"] = "missing_submodule_path"
            else:
                probe = ctx.runner.run(
                    submodule_path,
                    ["rev-parse", "--is-inside-work-tree"],
                    allow_failure=True,
                )
                if probe.exit_code == 0 and probe.stdout.strip() == "true":
                    entry["type"] = "gitlink_conflict"
                    entry["action"] = "recursive_merge"
                    gitlink_conflicts.append(entry)
                else:
                    entry["type"] = "submodule_not_initialized"
        else:
            entry["type"] = "file_conflict"
        if path and stages and modes != {"160000"}:
            entry["artifacts"] = export_conflict_artifacts(ctx, repo_path, entry)
        conflicts.append(entry)

    ctx.report.add_conflicts(conflicts)
    return _conflict_analysis_class(ci_module)(conflicts, gitlink_conflicts)


def relative_output_path(ctx: Any, path: Path) -> str:
    try:
        return path.relative_to(ctx.report.output_dir).as_posix()
    except ValueError:
        return str(path)


def artifact_category_dir(ctx: Any, category: str) -> Path:
    root = ctx.report.output_dir / category
    merge_step = getattr(ctx, "merge_step", None)
    if merge_step and getattr(merge_step, "artifact_prefix", None):
        return root / merge_step.artifact_prefix
    return root


def write_patch_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


def patch_pathspecs(changed_file: dict[str, str]) -> list[str]:
    specs: list[str] = []
    for key in ("old_path", "path"):
        value = changed_file.get(key)
        if value and value not in specs:
            specs.append(value)
    return specs


def diff_context_arg(ctx: Any) -> str:
    return f"--unified={ctx.args.diff_context_lines}"


def export_patch(
    ctx: Any,
    repo_path: Path,
    args: list[str],
    output_path: Path,
    warnings: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any] | None:
    result = ctx.runner.run(repo_path, args, allow_failure=True)
    if result.exit_code != 0:
        warnings.append({"reason": reason, "command": result.to_dict()})
        return None
    try:
        write_patch_file(output_path, result.stdout)
    except OSError as exc:
        warnings.append(
            {
                "reason": "PATCH_WRITE_FAILED",
                "message": str(exc),
                "path": str(output_path),
            }
        )
        return None
    return {
        "path": str(output_path),
        "relative_path": relative_output_path(ctx, output_path),
        "size_bytes": output_path.stat().st_size,
        "context_lines": ctx.args.diff_context_lines,
    }


def export_repository_patches(
    ctx: Any,
    repo_path: Path,
    display_path: str,
    ours_sha: str,
    result_sha: str,
    changed_files: list[dict[str, str]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    repo_name = common.sanitize_patch_name(display_path, "root")
    patch_root = artifact_category_dir(ctx, "patches") / repo_name
    diff_range = f"{ours_sha}..{result_sha}"
    repo_patch_path = patch_root / "repository.patch"
    repo_patch = export_patch(
        ctx,
        repo_path,
        ["diff", "--no-ext-diff", "--binary", "--find-renames", diff_context_arg(ctx), diff_range],
        repo_patch_path,
        warnings,
        "REPOSITORY_PATCH_EXPORT_FAILED",
    )

    file_patches: list[dict[str, Any]] = []
    files_dir = patch_root / "files"
    for index, changed_file in enumerate(changed_files, start=1):
        specs = patch_pathspecs(changed_file)
        if not specs:
            warnings.append(
                {
                    "reason": "FILE_PATCH_SKIPPED",
                    "message": "changed file entry has no path",
                    "changed_file": changed_file,
                }
            )
            continue
        file_name = common.sanitize_patch_name(specs[-1], f"file-{index}")
        file_patch_path = files_dir / f"{index:04d}-{file_name}.patch"
        file_patch = export_patch(
            ctx,
            repo_path,
            ["diff", "--no-ext-diff", "--binary", "--find-renames", diff_context_arg(ctx), diff_range, "--", *specs],
            file_patch_path,
            warnings,
            "FILE_PATCH_EXPORT_FAILED",
        )
        if file_patch:
            changed_file["patch_path"] = file_patch["path"]
            changed_file["patch_relative_path"] = file_patch["relative_path"]
            file_patch.update(
                {
                    "status": changed_file.get("status"),
                    "path": changed_file.get("path"),
                    "old_path": changed_file.get("old_path"),
                }
            )
            file_patches.append(file_patch)

    return {"repository": repo_patch, "files": file_patches}


def changed_file_primary_path(changed_file: dict[str, str], fallback: str) -> str:
    return changed_file.get("path") or changed_file.get("old_path") or fallback


def changed_file_old_path(changed_file: dict[str, str]) -> str | None:
    return changed_file.get("old_path") or changed_file.get("path")


def changed_file_new_path(changed_file: dict[str, str]) -> str | None:
    return changed_file.get("path")


def changed_file_content_dir(
    ctx: Any,
    display_path: str,
    changed_file: dict[str, str],
    index: int,
) -> Path:
    repo_name = common.sanitize_patch_name(display_path, "root")
    path_name = common.sanitize_patch_name(changed_file_primary_path(changed_file, f"file-{index}"), f"file-{index}")
    return artifact_category_dir(ctx, "files") / repo_name / f"{index:04d}-{path_name}"


def tree_blob_exists(ctx: Any, repo_path: Path, ref: str, path: str) -> bool:
    result = ctx.runner.run(repo_path, ["cat-file", "-t", f"{ref}:{path}"], allow_failure=True)
    return result.exit_code == 0 and result.stdout.strip() == "blob"


def export_tree_file_artifact(
    ctx: Any,
    repo_path: Path,
    ref: str,
    file_path: str | None,
    artifact_path: Path,
    role: str,
    warnings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not file_path:
        return None
    if not tree_blob_exists(ctx, repo_path, ref, file_path):
        return None

    content_result, content = ctx.runner.run_binary(repo_path, ["cat-file", "-p", f"{ref}:{file_path}"], allow_failure=True)
    if content_result.exit_code != 0:
        warnings.append(
            {
                "reason": "FILE_CONTENT_EXPORT_FAILED",
                "role": role,
                "ref": ref,
                "path": file_path,
                "command": content_result.to_dict(),
            }
        )
        return None
    try:
        record = write_binary_artifact(ctx, artifact_path, content)
        record.update({"role": role, "ref": ref, "source_path": file_path})
        return record
    except OSError as exc:
        warnings.append(
            {
                "reason": "FILE_CONTENT_WRITE_FAILED",
                "role": role,
                "ref": ref,
                "path": file_path,
                "message": str(exc),
            }
        )
        return None


def export_changed_file_artifacts(
    ctx: Any,
    repo_path: Path,
    display_path: str,
    ours_sha: str,
    result_sha: str,
    changed_files: list[dict[str, str]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for index, changed_file in enumerate(changed_files, start=1):
        artifact_dir = changed_file_content_dir(ctx, display_path, changed_file, index)
        old_path = changed_file_old_path(changed_file)
        new_path = changed_file_new_path(changed_file)
        status = changed_file.get("status") or ""
        record: dict[str, Any] = {
            "artifact_dir": str(artifact_dir),
            "artifact_relative_dir": relative_output_path(ctx, artifact_dir),
            "status": status,
            "path": new_path,
            "old_path": changed_file.get("old_path"),
            "ours": None,
            "result": None,
            "metadata": None,
        }

        ours_record = export_tree_file_artifact(ctx, repo_path, ours_sha, old_path, artifact_dir / "ours", "ours", warnings)
        result_record = export_tree_file_artifact(
            ctx,
            repo_path,
            result_sha,
            new_path,
            artifact_dir / "result",
            "result",
            warnings,
        )
        record["ours"] = ours_record
        record["result"] = result_record

        metadata = {
            "repo_path": str(repo_path),
            "display_path": display_path,
            "status": status,
            "path": new_path,
            "old_path": changed_file.get("old_path"),
            "ours_ref": ours_sha,
            "result_ref": result_sha,
            "artifacts": {key: value for key, value in record.items() if key != "metadata"},
        }
        try:
            record["metadata"] = write_json_artifact(ctx, artifact_dir / "metadata.json", metadata)
        except OSError as exc:
            warnings.append({"reason": "FILE_CONTENT_METADATA_WRITE_FAILED", "path": new_path or old_path, "message": str(exc)})

        if ours_record or result_record:
            changed_file["content_artifacts"] = record
            files.append(record)

    return {"files": files}


def collect_repository_details(
    ctx: Any,
    repo_path: Path,
    display_path: str,
    base_sha: str | None,
    ours_sha: str,
    theirs_sha: str,
    result_sha: str,
    strategy: str,
) -> dict[str, Any]:
    ctx.set_state(Phase.COLLECTING_REPORT)
    commits: list[dict[str, str]] = []
    changed_files: list[dict[str, str]] = []
    diff_stat = ""
    warnings: list[dict[str, Any]] = []

    if base_sha:
        log = ctx.runner.run(
            repo_path,
            ["log", "--pretty=format:%H%x09%an%x09%ae%x09%aI%x09%s", f"{base_sha}..{result_sha}"],
            allow_failure=True,
        )
        if log.exit_code == 0:
            commits = common.parse_commit_log(log.stdout)
        else:
            warnings.append({"reason": "COMMIT_LOG_FAILED", "command": log.to_dict()})

    stat = ctx.runner.run(repo_path, ["diff", "--stat", "--find-renames", f"{ours_sha}..{result_sha}"], allow_failure=True)
    if stat.exit_code == 0:
        diff_stat = stat.stdout
    else:
        warnings.append({"reason": "DIFF_STAT_FAILED", "command": stat.to_dict()})

    names = ctx.runner.run(repo_path, ["diff", "--name-status", "--find-renames", f"{ours_sha}..{result_sha}"], allow_failure=True)
    if names.exit_code == 0:
        changed_files = common.parse_name_status(names.stdout)
    else:
        warnings.append({"reason": "CHANGED_FILES_FAILED", "command": names.to_dict()})

    patches = export_repository_patches(
        ctx,
        repo_path,
        display_path,
        ours_sha,
        result_sha,
        changed_files,
        warnings,
    )
    content_artifacts = export_changed_file_artifacts(
        ctx,
        repo_path,
        display_path,
        ours_sha,
        result_sha,
        changed_files,
        warnings,
    )

    repository = {
        "path": display_path,
        "repo_path": str(repo_path),
        "base": base_sha,
        "ours": ours_sha,
        "theirs": theirs_sha,
        "result": result_sha,
        "strategy": strategy,
        "commits": commits,
        "changed_files": changed_files,
        "diff_stat": diff_stat,
        "patch_path": patches["repository"]["path"] if patches.get("repository") else None,
        "patch_relative_path": patches["repository"]["relative_path"] if patches.get("repository") else None,
        "patches": patches,
        "content_artifacts": content_artifacts,
        "warnings": warnings,
    }
    ctx.report.add_repository(repository)
    return repository
