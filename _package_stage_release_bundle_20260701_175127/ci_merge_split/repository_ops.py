"""CI repository query and guard helpers split out of the original script."""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType
from typing import Any

from . import common
from .models import CiMergeError, ExitCode, Phase, RefResolution


def _ci_error_class(ci_module: ModuleType | None) -> type[Exception]:
    return getattr(ci_module, "CiMergeError", CiMergeError) if ci_module is not None else CiMergeError


def _exit_code(ci_module: ModuleType | None, name: str) -> int:
    exit_codes = getattr(ci_module, "ExitCode", ExitCode) if ci_module is not None else ExitCode
    return getattr(exit_codes, name)


def _phase(ci_module: ModuleType | None, name: str) -> str:
    phases = getattr(ci_module, "Phase", Phase) if ci_module is not None else Phase
    return getattr(phases, name)


def _state_or(ctx: Any, ci_module: ModuleType | None, fallback_phase: str) -> str:
    report = getattr(ctx, "report", None)
    data = getattr(report, "data", {}) if report is not None else {}
    return data.get("state") or _phase(ci_module, fallback_phase)


def _raise_ci_error(
    ci_module: ModuleType | None,
    exit_code_name: str,
    phase: str,
    reason: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    restore_needed: bool = False,
) -> None:
    error_cls = _ci_error_class(ci_module)
    raise error_cls(
        _exit_code(ci_module, exit_code_name),
        phase,
        reason,
        message,
        details=details,
        restore_needed=restore_needed,
    )


def git_path(ctx: Any, repo_path: Path, name: str) -> Path | None:
    result = ctx.runner.run(repo_path, ["rev-parse", "--git-path", name], allow_failure=True)
    if result.exit_code != 0 or not result.stdout.strip():
        return None
    candidate = Path(result.stdout.strip())
    return candidate if candidate.is_absolute() else common.resolve_path(repo_path / candidate)


def operation_markers(ctx: Any, repo_path: Path) -> list[dict[str, str]]:
    marker_names = ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"]
    markers: list[dict[str, str]] = []
    for marker_name in marker_names:
        marker_path = git_path(ctx, repo_path, marker_name)
        if marker_path and marker_path.exists():
            markers.append({"name": marker_name, "path": str(marker_path)})
    return markers


def repository_is_clean(ctx: Any, repo_path: Path) -> tuple[bool, str]:
    status = ctx.runner.run(repo_path, ["status", "--porcelain=v1"], allow_failure=True)
    if status.exit_code != 0:
        return False, "git status failed"
    return status.stdout.strip() == "", status.stdout


def get_submodule_paths(ctx: Any, repo_path: Path) -> list[str]:
    gitmodules = repo_path / ".gitmodules"
    if not gitmodules.exists():
        return []
    result = ctx.runner.run(
        repo_path,
        ["config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        allow_failure=True,
    )
    if result.exit_code != 0:
        return []
    paths: list[str] = []
    for line in common.split_lines(result.stdout):
        parts = line.split(None, 1)
        if len(parts) == 2:
            paths.append(parts[1].strip())
    return paths


def rev_parse(ctx: Any, repo_path: Path, ref: str) -> str:
    result = ctx.runner.run(repo_path, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    return result.stdout.strip()


def current_head(ctx: Any, repo_path: Path) -> str:
    return rev_parse(ctx, repo_path, "HEAD")


def current_branch(ctx: Any, repo_path: Path) -> str | None:
    result = ctx.runner.run(
        repo_path,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        allow_failure=True,
    )
    if result.exit_code == 0:
        return result.stdout.strip()
    return None


def display_path_for(ctx: Any, repo_path: Path) -> str:
    try:
        relative = repo_path.resolve(strict=False).relative_to(ctx.root_repo.resolve(strict=False))
        return "." if str(relative) == "." else relative.as_posix()
    except ValueError:
        return str(repo_path)


def should_resolve_ref_directly(ref: str, remote: str) -> bool:
    if ref in {"HEAD", "FETCH_HEAD", "MERGE_HEAD", "ORIG_HEAD", "@"}:
        return True
    if ref.startswith("refs/") or ref.startswith(f"{remote}/"):
        return True
    if re.fullmatch(r"[0-9A-Fa-f]{7,40}", ref):
        return True
    return any(token in ref for token in ("^", "~", ":"))


def remote_first_ref(ref: str, remote: str) -> tuple[str, str]:
    normalized = ref.strip()
    if should_resolve_ref_directly(normalized, remote):
        return normalized, "explicit"
    return f"refs/remotes/{remote}/{normalized}", "remote_tracking"


def resolve_commit_ref(ctx: Any, repo_path: Path, ref: str, ci_module: ModuleType | None = None) -> Any:
    resolved_ref, strategy = remote_first_ref(ref, ctx.args.remote)
    result = ctx.runner.run(
        repo_path,
        ["rev-parse", "--verify", f"{resolved_ref}^{{commit}}"],
        allow_failure=True,
    )
    resolution_cls = getattr(ci_module, "RefResolution", RefResolution) if ci_module is not None else RefResolution
    return resolution_cls(
        input_ref=ref,
        resolved_ref=resolved_ref,
        strategy=strategy,
        sha=result.stdout.strip() if result.exit_code == 0 else None,
        command=result,
    )


def verify_repo_ready(ctx: Any, repo_path: Path) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    markers = operation_markers(ctx, repo_path)
    if markers:
        reasons.append(f"operation in progress: {markers}")
    is_clean, status = repository_is_clean(ctx, repo_path)
    if not is_clean:
        reasons.append(f"working tree not clean: {status}")
    head = ctx.runner.run(repo_path, ["rev-parse", "--verify", "HEAD^{commit}"], allow_failure=True)
    if head.exit_code != 0:
        reasons.append("HEAD is not a valid commit")
    submodules = ctx.runner.run(repo_path, ["submodule", "status", "--recursive"], allow_failure=True)
    if submodules.exit_code != 0:
        reasons.append("submodule status failed")
    else:
        for line in common.split_lines(submodules.stdout):
            prefix = line[0]
            if prefix in {"-", "+", "U"}:
                reasons.append(f"submodule not ready: {line}")
    return not reasons, reasons


def ensure_no_unmerged_paths(ctx: Any, repo_path: Path, ci_module: ModuleType | None = None) -> None:
    unresolved = ctx.runner.run(repo_path, ["diff", "--name-only", "--diff-filter=U"])
    if unresolved.stdout.strip():
        _raise_ci_error(
            ci_module,
            "UNDEFINED_STATE",
            _state_or(ctx, ci_module, "MERGING_ROOT"),
            "UNRESOLVED_CONFLICTS_REMAIN",
            "unmerged paths remain after conflict handling",
            details={"paths": common.split_lines(unresolved.stdout), "repo_path": str(repo_path)},
            restore_needed=True,
        )


def ensure_gitmodules_not_changed(ctx: Any, repo_path: Path, ci_module: ModuleType | None = None) -> None:
    staged = ctx.runner.run(repo_path, ["diff", "--cached", "--name-only", "--", ".gitmodules"], allow_failure=True)
    worktree = ctx.runner.run(repo_path, ["diff", "--name-only", "--", ".gitmodules"], allow_failure=True)
    changed = bool(staged.stdout.strip() or worktree.stdout.strip())
    if changed:
        _raise_ci_error(
            ci_module,
            "UNDEFINED_STATE",
            _state_or(ctx, ci_module, "MERGING_ROOT"),
            "GITMODULES_CHANGED",
            "automatic merge does not accept .gitmodules changes",
            details={
                "repo_path": str(repo_path),
                "staged": common.split_lines(staged.stdout),
                "worktree": common.split_lines(worktree.stdout),
            },
            restore_needed=True,
        )


def ensure_no_submodule_topology_change(ctx: Any, repo_path: Path, ci_module: ModuleType | None = None) -> None:
    """Reject submodule add/delete/rename/type changes while allowing SHA moves."""

    raw = ctx.runner.run(repo_path, ["diff", "--cached", "--raw", "--"], allow_failure=True)
    if raw.exit_code != 0:
        _raise_ci_error(
            ci_module,
            "GIT_COMMAND_FAILED",
            _state_or(ctx, ci_module, "MERGING_ROOT"),
            "RAW_DIFF_FAILED",
            "cannot inspect staged raw diff for submodule topology changes",
            details={"command": raw.to_dict()},
            restore_needed=True,
        )

    blocked_changes: list[dict[str, Any]] = []
    for line in common.split_lines(raw.stdout):
        if not line.startswith(":") or "\t" not in line:
            _raise_ci_error(
                ci_module,
                "UNDEFINED_STATE",
                _state_or(ctx, ci_module, "MERGING_ROOT"),
                "RAW_DIFF_UNPARSEABLE",
                "git raw diff output cannot be parsed",
                details={"line": line, "repo_path": str(repo_path)},
                restore_needed=True,
            )
        meta, paths = line[1:].split("\t", 1)
        fields = meta.split()
        if len(fields) < 5:
            _raise_ci_error(
                ci_module,
                "UNDEFINED_STATE",
                _state_or(ctx, ci_module, "MERGING_ROOT"),
                "RAW_DIFF_UNPARSEABLE",
                "git raw diff metadata cannot be parsed",
                details={"line": line, "repo_path": str(repo_path)},
                restore_needed=True,
            )
        old_mode, new_mode, old_sha, new_sha, status = fields[:5]
        status_letter = status[0]
        if "160000" in {old_mode, new_mode} and status_letter in {"A", "D", "R", "C", "T"}:
            blocked_changes.append(
                {
                    "status": status,
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "old_sha": old_sha,
                    "new_sha": new_sha,
                    "path": paths,
                }
            )

    if blocked_changes:
        _raise_ci_error(
            ci_module,
            "UNDEFINED_STATE",
            _state_or(ctx, ci_module, "MERGING_ROOT"),
            "SUBMODULE_PATH_CHANGED",
            "automatic merge does not accept submodule add/delete/rename/type changes",
            details={"repo_path": str(repo_path), "changes": blocked_changes},
            restore_needed=True,
        )
