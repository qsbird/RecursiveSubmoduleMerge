"""Shared Git cleanup step builders.

The CI and manual workflows still own their different safety policies.  This
module keeps the concrete Git cleanup command sequences in one place so fixes
like nested submodule cleanup do not drift between entry points.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class CleanupStep:
    args: tuple[str, ...]
    allow_failure: bool = False
    cwd: Path | str | None = None


@dataclass(frozen=True)
class CleanupLevel:
    name: str
    steps: tuple[CleanupStep, ...]


def cleanup_step(
    args: Iterable[str],
    *,
    allow_failure: bool = False,
    cwd: Path | str | None = None,
) -> CleanupStep:
    return CleanupStep(tuple(args), allow_failure=allow_failure, cwd=cwd)


def coerce_cleanup_step(value: CleanupStep | Mapping[str, Any]) -> CleanupStep:
    if isinstance(value, CleanupStep):
        return value
    return cleanup_step(
        value["args"],
        allow_failure=bool(value.get("allow_failure", False)),
        cwd=value.get("cwd"),
    )


def step_args(step: CleanupStep | Mapping[str, Any]) -> list[str]:
    return list(coerce_cleanup_step(step).args)


def step_cwd(step: CleanupStep | Mapping[str, Any], default_repo_path: Path) -> Path:
    resolved_step = coerce_cleanup_step(step)
    if resolved_step.cwd is None:
        return default_repo_path
    candidate = Path(resolved_step.cwd)
    if candidate.is_absolute():
        return candidate
    return default_repo_path / candidate


def git_clean_flag(allow_clean_x: bool, *, remove_nested_git_repos: bool = False) -> str:
    force = "-ff" if remove_nested_git_repos else "-f"
    ignored = "x" if allow_clean_x else ""
    return f"{force}d{ignored}"


def abort_operation_step(operation: str, *, allow_failure: bool = True) -> CleanupStep:
    return cleanup_step([operation, "--abort"], allow_failure=allow_failure)


def foreach_abort_operation_step(operation: str, *, allow_failure: bool = True) -> CleanupStep:
    return cleanup_step(
        ["submodule", "foreach", "--recursive", f"git {operation} --abort"],
        allow_failure=allow_failure,
    )


def reset_hard_step(ref: str | None = None, *, cwd: Path | str | None = None) -> CleanupStep:
    args = ["reset", "--hard"]
    if ref:
        args.append(ref)
    return cleanup_step(args, cwd=cwd)


def reset_merge_step() -> CleanupStep:
    return cleanup_step(["reset", "--merge"])


def clean_step(
    allow_clean_x: bool,
    *,
    remove_nested_git_repos: bool = False,
    cwd: Path | str | None = None,
) -> CleanupStep:
    return cleanup_step(
        ["clean", git_clean_flag(allow_clean_x, remove_nested_git_repos=remove_nested_git_repos)],
        cwd=cwd,
    )


def foreach_reset_hard_step(*, allow_failure: bool = True) -> CleanupStep:
    return cleanup_step(
        ["submodule", "foreach", "--recursive", "git reset --hard"],
        allow_failure=allow_failure,
    )


def foreach_clean_step(
    allow_clean_x: bool,
    *,
    remove_nested_git_repos: bool = True,
    allow_failure: bool = True,
) -> CleanupStep:
    flag = git_clean_flag(allow_clean_x, remove_nested_git_repos=remove_nested_git_repos)
    return cleanup_step(
        ["submodule", "foreach", "--recursive", f"git clean {flag}"],
        allow_failure=allow_failure,
    )


def submodule_update_step(
    *,
    force: bool = False,
    paths: Iterable[str] = (),
    allow_failure: bool = False,
) -> CleanupStep:
    args = ["submodule", "update", "--init", "--recursive"]
    if force:
        args.append("--force")
    selected_paths = [path for path in paths if path.strip()]
    if selected_paths:
        args.extend(["--", *selected_paths])
    return cleanup_step(args, allow_failure=allow_failure)


def submodule_sync_step() -> CleanupStep:
    return cleanup_step(["submodule", "sync", "--recursive"])


def fetch_all_prune_step(*, cwd: Path | str | None = None, allow_failure: bool = True) -> CleanupStep:
    return cleanup_step(["fetch", "--all", "--prune"], cwd=cwd, allow_failure=allow_failure)


def ci_prepare_cleanup_levels(allow_clean_x: bool) -> tuple[CleanupLevel, ...]:
    return (
        CleanupLevel(
            "level_1_basic_cleanup",
            (
                reset_hard_step(),
                clean_step(allow_clean_x),
                submodule_update_step(),
            ),
        ),
        CleanupLevel(
            "level_2_abort_and_recursive_cleanup",
            (
                abort_operation_step("merge"),
                abort_operation_step("rebase"),
                abort_operation_step("cherry-pick"),
                reset_hard_step(),
                clean_step(allow_clean_x, remove_nested_git_repos=True),
                foreach_reset_hard_step(),
                foreach_clean_step(allow_clean_x, remove_nested_git_repos=True),
            ),
        ),
        CleanupLevel(
            "level_3_sync_and_force_update",
            (
                submodule_sync_step(),
                fetch_all_prune_step(),
                submodule_update_step(force=True),
            ),
        ),
    )


def ci_deep_submodule_cleanup_steps(
    repo_path: Path,
    submodule_paths: Iterable[str],
    allow_clean_x: bool,
) -> list[CleanupStep]:
    steps: list[CleanupStep] = []
    for submodule_path in submodule_paths:
        sub_repo = repo_path / submodule_path
        steps.extend(
            [
                fetch_all_prune_step(cwd=sub_repo),
                reset_hard_step(cwd=sub_repo),
                clean_step(allow_clean_x, remove_nested_git_repos=True, cwd=sub_repo),
                cleanup_step(["submodule", "update", "--init", "--recursive", "--force"], cwd=sub_repo),
            ]
        )
    return steps


def ci_snapshot_restore_steps(pre_merge_head: str, allow_clean_x: bool) -> tuple[CleanupStep, ...]:
    return (
        abort_operation_step("merge"),
        abort_operation_step("rebase"),
        abort_operation_step("cherry-pick"),
        reset_hard_step(pre_merge_head),
        clean_step(allow_clean_x, remove_nested_git_repos=True),
        foreach_clean_step(allow_clean_x, remove_nested_git_repos=True),
        submodule_update_step(force=True, allow_failure=True),
    )


def ci_batch_continue_restore_steps(target_sha: str, allow_clean_x: bool) -> tuple[CleanupStep, ...]:
    return (
        abort_operation_step("merge"),
        abort_operation_step("rebase"),
        abort_operation_step("cherry-pick"),
        foreach_abort_operation_step("merge"),
        foreach_abort_operation_step("rebase"),
        foreach_abort_operation_step("cherry-pick"),
        reset_hard_step(target_sha),
        clean_step(allow_clean_x, remove_nested_git_repos=True),
        foreach_reset_hard_step(),
        foreach_clean_step(allow_clean_x, remove_nested_git_repos=True),
        submodule_update_step(force=True),
    )


# The manual workflow builds its own four-level ladder in
# ``ManualRepositoryOps._build_cleanup_levels`` and removes its report file
# through ``_safe_unlink_report``.  A second, unexported manual ladder used to
# live here; it had no callers and could only drift away from the one that runs.
