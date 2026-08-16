from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manual_merge_split.models import (
    DEFAULT_CONFLICT_TOOL_COMMAND,
    DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS,
    DEFAULT_FILE_CONFLICT_MODE,
    REPORT_FILE_NAME,
    ROOT_BASE_REMOTE_BRANCH,
)


class GuiInputError(ValueError):
    """Raised when form values cannot be translated to CLI arguments."""


@dataclass(frozen=True)
class CommandSpec:
    action: str
    argv: list[str]


@dataclass(frozen=True)
class CloneSpec:
    command: list[str]
    target_path: Path


def _string_value(values: dict[str, Any], key: str, default: str = "") -> str:
    value = values.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _bool_value(values: dict[str, Any], key: str, default: bool = False) -> bool:
    value = values.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_value(values: dict[str, Any], key: str, default: int) -> int:
    raw = _string_value(values, key, str(default))
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise GuiInputError(f"{key} must be an integer.") from exc
    if value < 0:
        raise GuiInputError(f"{key} cannot be negative.")
    return value


def parse_branch_tokens(raw_value: str) -> list[str]:
    normalized = raw_value.replace("\r", "\n").replace(",", "\n")
    tokens: list[str] = []
    for line in normalized.splitlines():
        for part in line.split():
            stripped = part.strip()
            if stripped:
                tokens.append(stripped)
    return tokens


def text_lines(raw_value: str) -> list[str]:
    normalized = raw_value.replace("\r", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def clone_target_path(destination: str | Path, directory_name: str) -> Path:
    directory = str(directory_name).strip()
    if not directory:
        raise GuiInputError("Directory Name is required for clone.")
    directory_path = Path(directory)
    if (
        directory_path.is_absolute()
        or len(directory_path.parts) != 1
        or "/" in directory
        or "\\" in directory
        or directory in {".", ".."}
    ):
        raise GuiInputError("Directory Name must be a single child directory name.")

    raw_destination = str(destination).strip()
    if not raw_destination:
        raise GuiInputError("Destination is required for clone.")
    destination_path = Path(raw_destination).expanduser()
    if destination_path.exists() and not destination_path.is_dir():
        raise GuiInputError(f"Destination is not a directory: {destination_path}")
    return destination_path / directory


def build_clone_spec(values: dict[str, Any]) -> CloneSpec:
    clone_url = _string_value(values, "clone_url")
    if not clone_url:
        raise GuiInputError("Repository URL is required for clone.")
    destination = _string_value(values, "clone_destination")
    target = clone_target_path(destination, _string_value(values, "clone_directory_name"))
    if target.exists():
        if target.is_dir() and not any(target.iterdir()):
            pass
        else:
            raise GuiInputError(f"Clone target already exists and is not empty: {target}")

    command = ["git", "-c", "protocol.file.allow=always", "clone", "-v", "--progress"]
    clone_branch = _string_value(values, "clone_branch")
    if clone_branch:
        command.extend(["--branch", clone_branch])
    command.extend([clone_url, str(target)])
    return CloneSpec(command=command, target_path=target)


def build_merge_argv(values: dict[str, Any]) -> list[str]:
    branches = parse_branch_tokens(_string_value(values, "merge_branches"))
    if not branches:
        raise GuiInputError("At least one merge branch is required.")

    repo = _string_value(values, "repo", ".") or "."
    argv = ["merge", *branches, "--repo", repo]

    if _bool_value(values, "clone_enabled"):
        clone_branch = _string_value(values, "clone_branch")
        clone_url = _string_value(values, "clone_url")
        clone_path = _string_value(values, "clone_path")
        if not (clone_branch and clone_url and clone_path):
            raise GuiInputError("Clone branch, URL, and target path are required when clone is enabled.")
        argv.extend(
            [
                "--clone-branch",
                clone_branch,
                "--clone-url",
                clone_url,
                "--clone-path",
                clone_path,
            ]
        )

    if _bool_value(values, "continue_current_branch"):
        argv.append("--continue-current-branch")
    else:
        base_remote_branch = _string_value(values, "base_remote_branch", ROOT_BASE_REMOTE_BRANCH)
        if not base_remote_branch:
            raise GuiInputError("Base remote branch is required unless continuing on the current branch.")
        argv.extend(["--base-remote-branch", base_remote_branch])
        work_branch = _string_value(values, "work_branch")
        if work_branch:
            argv.extend(["--work-branch", work_branch])

    if _bool_value(values, "enable_cross_submodule_moves"):
        argv.append("--enable-cross-submodule-moves")

    report_file = _string_value(values, "report_file", REPORT_FILE_NAME)
    if report_file:
        argv.extend(["--report-file", report_file])

    for ignored_path in text_lines(_string_value(values, "ignore_root_untracked_paths")):
        argv.extend(["--ignore-root-untracked-path", ignored_path])

    file_conflict_mode = _string_value(values, "file_conflict_mode", DEFAULT_FILE_CONFLICT_MODE)
    if file_conflict_mode != "tool":
        raise GuiInputError("File conflict preserve mode has been removed; merge that branch manually.")
    argv.extend(["--file-conflict-mode", "tool"])
    argv.extend(
        [
            "--conflict-tool-command",
            _string_value(values, "conflict_tool_command", DEFAULT_CONFLICT_TOOL_COMMAND),
            "--conflict-tool-timeout-seconds",
            str(_int_value(values, "conflict_tool_timeout_seconds", DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS)),
        ]
    )

    if _bool_value(values, "verbose"):
        argv.append("--verbose")
    return argv


def build_cleanup_argv(values: dict[str, Any]) -> list[str]:
    repo = _string_value(values, "repo", ".") or "."
    argv = ["cleanup", "--repo", repo]

    report_file = _string_value(values, "report_file", REPORT_FILE_NAME)
    if report_file:
        argv.extend(["--report-file", report_file])

    if _bool_value(values, "skip_submodule_update"):
        argv.append("--skip-submodule-update")

    if _bool_value(values, "rebuild_work_branch"):
        base_remote_branch = _string_value(values, "cleanup_base_remote_branch", ROOT_BASE_REMOTE_BRANCH)
        if not base_remote_branch:
            raise GuiInputError("Cleanup base remote branch is required when rebuilding the work branch.")
        argv.extend(["--rebuild-work-branch", "--base-remote-branch", base_remote_branch])
        work_branch = _string_value(values, "cleanup_work_branch")
        if work_branch:
            argv.extend(["--work-branch", work_branch])

    argv.append("--yes")
    if _bool_value(values, "verbose"):
        argv.append("--verbose")
    return argv


def build_publish_argv(values: dict[str, Any]) -> list[str]:
    repo = _string_value(values, "repo", ".") or "."
    remote = _string_value(values, "publish_remote", "origin") or "origin"
    push_target = _string_value(values, "publish_target")
    if not push_target:
        raise GuiInputError("Push target branch is required.")

    argv = [
        "publish",
        "--repo",
        repo,
        "--remote",
        remote,
        "--push-target",
        push_target,
    ]
    if _bool_value(values, "publish_check_only"):
        argv.append("--check-only")
    if not _bool_value(values, "publish_sync_local_branches", True):
        argv.append("--no-sync-local-branches")
    if not _bool_value(values, "publish_allow_branch_suffix", True):
        argv.append("--no-branch-suffix")
    if _bool_value(values, "verbose"):
        argv.append("--verbose")
    return argv


def build_command_spec(action: str, values: dict[str, Any]) -> CommandSpec:
    if action == "merge":
        return CommandSpec(action=action, argv=build_merge_argv(values))
    if action == "cleanup":
        return CommandSpec(action=action, argv=build_cleanup_argv(values))
    if action == "publish":
        return CommandSpec(action=action, argv=build_publish_argv(values))
    raise GuiInputError(f"Unsupported action: {action}")


__all__ = [
    "CloneSpec",
    "CommandSpec",
    "DEFAULT_CONFLICT_TOOL_COMMAND",
    "DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS",
    "DEFAULT_FILE_CONFLICT_MODE",
    "GuiInputError",
    "REPORT_FILE_NAME",
    "ROOT_BASE_REMOTE_BRANCH",
    "build_cleanup_argv",
    "build_clone_spec",
    "build_command_spec",
    "build_merge_argv",
    "build_publish_argv",
    "clone_target_path",
    "parse_branch_tokens",
    "text_lines",
]
