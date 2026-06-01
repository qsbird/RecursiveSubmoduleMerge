"""Tkinter desktop GUI for the manual recursive submodule merge workflow.

This file intentionally uses only the Python standard library. It can be run
directly from the source tree or packaged as a single GUI executable together
with the local manual_merge_split package.
"""

from __future__ import annotations

import argparse
import locale
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT_BASE_REMOTE_BRANCH = "origin/main"
WORK_BRANCH_DEFAULT = "validation-work"
REPORT_FILE_NAME = "merge_manual_todo.txt"
DEFAULT_FILE_CONFLICT_MODE = "tool"
DEFAULT_CONFLICT_TOOL_COMMAND = 'code --wait "{file}"'
DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS = 1800
CONFLICT_TOOL_CONFIRMATION_MESSAGE = "Manual confirmation required after conflict tool closes"


class GuiInputError(ValueError):
    """Raised when form values cannot be translated to CLI arguments."""


@dataclass(frozen=True)
class CommandSpec:
    action: str
    argv: list[str]


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

    if _bool_value(values, "allow_dirty_submodules"):
        argv.append("--allow-dirty-submodules")
    if _bool_value(values, "no_prompt_dirty_submodules", True):
        argv.append("--no-prompt-dirty-submodules")
    if _bool_value(values, "enable_cross_submodule_moves"):
        argv.append("--enable-cross-submodule-moves")

    report_file = _string_value(values, "report_file", REPORT_FILE_NAME)
    if report_file:
        argv.extend(["--report-file", report_file])

    for ignored_path in text_lines(_string_value(values, "ignore_root_untracked_paths")):
        argv.extend(["--ignore-root-untracked-path", ignored_path])

    file_conflict_mode = _string_value(values, "file_conflict_mode", DEFAULT_FILE_CONFLICT_MODE)
    if file_conflict_mode not in {"tool", "preserve"}:
        raise GuiInputError("File conflict mode must be 'tool' or 'preserve'.")
    argv.extend(["--file-conflict-mode", file_conflict_mode])
    if file_conflict_mode == "tool":
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

    if _bool_value(values, "verbose"):
        argv.append("--verbose")
    return argv


def build_command_spec(action: str, values: dict[str, Any]) -> CommandSpec:
    if action == "merge":
        return CommandSpec(action=action, argv=build_merge_argv(values))
    if action == "cleanup":
        return CommandSpec(action=action, argv=build_cleanup_argv(values))
    raise GuiInputError(f"Unsupported action: {action}")


def script_path() -> Path:
    return Path(__file__).resolve()


def launcher_command(
    cli_argv: list[str],
    *,
    executable: str | None = None,
    source_file: Path | None = None,
    frozen: bool | None = None,
) -> list[str]:
    exe = executable or sys.executable
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return [exe, "--cli", *cli_argv]
    return [exe, str(source_file or script_path()), "--cli", *cli_argv]


def command_display(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def safe_directory_value(path: str | Path) -> str:
    return Path(path).resolve(strict=False).as_posix()


def add_git_config_env(env: dict[str, str], key: str, value: str) -> None:
    index = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    env[f"GIT_CONFIG_KEY_{index}"] = key
    env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(index + 1)


def git_safe_directory_env(repo: str | Path) -> dict[str, str]:
    env = os.environ.copy()
    add_git_config_env(env, "safe.directory", safe_directory_value(repo))
    return env


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return script_path().parent


def discover_repositories(root: Path | None = None) -> list[tuple[str, str]]:
    base = (root or project_root()).resolve(strict=False)
    candidates: list[tuple[str, Path]] = []
    if (base / ".git").exists():
        candidates.append(("Source tree", base))
    for child in sorted(base.iterdir(), key=lambda path: path.name.lower()):
        if not child.is_dir():
            continue
        repo = child / "repo"
        if (repo / ".git").exists():
            candidates.append((f"{child.name}\\repo", repo))

    seen: set[Path] = set()
    result: list[tuple[str, str]] = []
    for label, path in candidates:
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append((label, str(resolved)))
    return result


def run_cli(cli_argv: list[str]) -> int:
    from manual_merge_split import runtime as manual_runtime

    return manual_runtime.main(cli_argv)


def run_git_query(repo: str, args: list[str]) -> str:
    repo_path = Path(repo).resolve(strict=False)
    safe_repo = safe_directory_value(repo_path)
    command = ["git", "-c", f"safe.directory={safe_repo}", "-C", str(repo_path), *args]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False) or "utf-8",
        errors="replace",
        check=False,
        timeout=30,
        env=git_safe_directory_env(repo_path),
    )
    pieces = [f"$ {command_display(command)}"]
    if completed.stdout.strip():
        pieces.append(completed.stdout.strip())
    if completed.stderr.strip():
        pieces.append(completed.stderr.strip())
    pieces.append(f"exit code: {completed.returncode}")
    return "\n".join(pieces)


class MergeGuiApp:
    def __init__(self, root: Any) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.events: queue.Queue[tuple[str, str | int | None]] = queue.Queue()

        root.title("Recursive Submodule Merge")
        root.geometry("1160x760")
        root.minsize(980, 680)

        self.repo_var = tk.StringVar()
        self.branch_mode_var = tk.StringVar(value="reset")
        self.base_remote_branch_var = tk.StringVar(value=ROOT_BASE_REMOTE_BRANCH)
        self.work_branch_var = tk.StringVar(value=WORK_BRANCH_DEFAULT)
        self.file_conflict_mode_var = tk.StringVar(value=DEFAULT_FILE_CONFLICT_MODE)
        self.conflict_tool_command_var = tk.StringVar(value=DEFAULT_CONFLICT_TOOL_COMMAND)
        self.conflict_tool_timeout_var = tk.StringVar(value=str(DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS))
        self.allow_dirty_var = tk.BooleanVar(value=False)
        self.no_prompt_dirty_var = tk.BooleanVar(value=True)
        self.enable_cross_submodule_moves_var = tk.BooleanVar(value=False)
        self.clone_enabled_var = tk.BooleanVar(value=False)
        self.clone_branch_var = tk.StringVar()
        self.clone_url_var = tk.StringVar()
        self.clone_path_var = tk.StringVar()
        self.report_file_var = tk.StringVar(value=REPORT_FILE_NAME)
        self.verbose_var = tk.BooleanVar(value=True)
        self.rebuild_work_branch_var = tk.BooleanVar(value=True)
        self.skip_submodule_update_var = tk.BooleanVar(value=False)
        self.cleanup_base_remote_branch_var = tk.StringVar(value=ROOT_BASE_REMOTE_BRANCH)
        self.cleanup_work_branch_var = tk.StringVar(value=WORK_BRANCH_DEFAULT)
        self.status_var = tk.StringVar(value="Idle")
        self.repo_presets = discover_repositories()

        if self.repo_presets:
            preferred = next(
                (path for label, path in self.repo_presets if "manual_local_execution" in label),
                self.repo_presets[0][1],
            )
            self.repo_var.set(preferred)
        else:
            self.repo_var.set(str(project_root()))

        self._build_ui()
        self._update_enabled_state()
        self._preview("merge")
        self.root.after(100, self._drain_events)

    def _build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=(12, 10))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Repository").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.repo_entry = ttk.Entry(top, textvariable=self.repo_var)
        self.repo_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(top, text="Browse", command=self._browse_repo).grid(row=0, column=2, padx=(8, 0))

        preset_values = [label for label, _path in self.repo_presets]
        self.preset_var = tk.StringVar(value=preset_values[0] if preset_values else "")
        self.repo_combo = ttk.Combobox(top, textvariable=self.preset_var, values=preset_values, state="readonly", width=34)
        self.repo_combo.grid(row=0, column=3, padx=(8, 0))
        self.repo_combo.bind("<<ComboboxSelected>>", self._select_repo_preset)
        ttk.Button(top, text="Status", command=self._show_status).grid(row=0, column=4, padx=(8, 0))
        ttk.Button(top, text="Branches", command=self._show_branches).grid(row=0, column=5, padx=(8, 0))

        body = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(left, weight=3)
        body.add(right, weight=2)

        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.tabs = ttk.Notebook(left)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        self.merge_tab = ttk.Frame(self.tabs, padding=12)
        self.cleanup_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.merge_tab, text="Merge")
        self.tabs.add(self.cleanup_tab, text="Cleanup")

        self._build_merge_tab()
        self._build_cleanup_tab()
        self._build_output(right)

    def _build_merge_tab(self) -> None:
        ttk = self.ttk

        for column in range(2):
            self.merge_tab.columnconfigure(column, weight=1)

        ttk.Label(self.merge_tab, text="Merge branches, one per line or separated by commas").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self.merge_branches_text = self.tk.Text(self.merge_tab, height=5, wrap="word")
        self.merge_branches_text.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(4, 10))
        self.merge_branches_text.insert("1.0", "origin/feature")

        mode_frame = ttk.LabelFrame(self.merge_tab, text="Root branch mode", padding=8)
        mode_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Radiobutton(
            mode_frame,
            text="Create or reset work branch",
            value="reset",
            variable=self.branch_mode_var,
            command=self._update_enabled_state,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            mode_frame,
            text="Continue current branch",
            value="continue",
            variable=self.branch_mode_var,
            command=self._update_enabled_state,
        ).grid(row=0, column=1, sticky="w", padx=(18, 0))

        self.base_entry = self._entry_row(self.merge_tab, 3, 0, "Base remote branch", self.base_remote_branch_var)
        self.work_entry = self._entry_row(self.merge_tab, 3, 1, "Work branch", self.work_branch_var)

        ttk.Label(self.merge_tab, text="File conflict mode").grid(row=5, column=0, sticky="w")
        self.file_conflict_combo = ttk.Combobox(
            self.merge_tab,
            textvariable=self.file_conflict_mode_var,
            values=["tool", "preserve"],
            state="readonly",
        )
        self.file_conflict_combo.grid(row=6, column=0, sticky="ew", pady=(4, 10), padx=(0, 6))
        self.file_conflict_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_enabled_state())

        self.timeout_entry = self._entry_row(
            self.merge_tab,
            5,
            1,
            "Tool timeout seconds",
            self.conflict_tool_timeout_var,
            pady=(4, 10),
        )
        self.tool_entry = self._entry_row(
            self.merge_tab,
            7,
            0,
            "Conflict tool command",
            self.conflict_tool_command_var,
            columnspan=2,
        )

        options = ttk.LabelFrame(self.merge_tab, text="Options", padding=8)
        options.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Checkbutton(options, text="Allow unaffected dirty submodules", variable=self.allow_dirty_var).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Checkbutton(options, text="Do not prompt for dirty submodules", variable=self.no_prompt_dirty_var).grid(
            row=0, column=1, sticky="w", padx=(18, 0)
        )
        ttk.Checkbutton(
            options,
            text="Enable cross-submodule moved file detection",
            variable=self.enable_cross_submodule_moves_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        clone = ttk.LabelFrame(self.merge_tab, text="Clone before merge", padding=8)
        clone.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        clone.columnconfigure(1, weight=1)
        clone.columnconfigure(3, weight=1)
        ttk.Checkbutton(
            clone,
            text="Enable clone",
            variable=self.clone_enabled_var,
            command=self._update_enabled_state,
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.clone_branch_entry = self._labeled_entry(clone, 1, 0, "Branch", self.clone_branch_var)
        self.clone_url_entry = self._labeled_entry(clone, 1, 2, "URL", self.clone_url_var)
        ttk.Label(clone, text="Target path").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.clone_path_entry = ttk.Entry(clone, textvariable=self.clone_path_var)
        self.clone_path_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
        self.clone_path_button = ttk.Button(clone, text="Browse", command=self._browse_clone_path)
        self.clone_path_button.grid(row=2, column=3, sticky="e", pady=(8, 0))

        ttk.Label(self.merge_tab, text="Ignored root untracked paths").grid(row=11, column=0, columnspan=2, sticky="w")
        self.ignore_paths_text = self.tk.Text(self.merge_tab, height=4, wrap="word")
        self.ignore_paths_text.grid(row=12, column=0, columnspan=2, sticky="nsew", pady=(4, 10))

        buttons = ttk.Frame(self.merge_tab)
        buttons.grid(row=13, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Preview command", command=lambda: self._preview("merge")).grid(row=0, column=0)
        ttk.Button(buttons, text="Start merge", command=lambda: self._start("merge")).grid(row=0, column=1, padx=(8, 0))

    def _build_cleanup_tab(self) -> None:
        ttk = self.ttk
        for column in range(2):
            self.cleanup_tab.columnconfigure(column, weight=1)

        options = ttk.LabelFrame(self.cleanup_tab, text="Cleanup options", padding=8)
        options.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Checkbutton(
            options,
            text="Rebuild work branch after cleanup",
            variable=self.rebuild_work_branch_var,
            command=self._update_enabled_state,
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options, text="Skip submodule update", variable=self.skip_submodule_update_var).grid(
            row=0, column=1, sticky="w", padx=(18, 0)
        )

        self.cleanup_base_entry = self._entry_row(
            self.cleanup_tab,
            1,
            0,
            "Base remote branch",
            self.cleanup_base_remote_branch_var,
        )
        self.cleanup_work_entry = self._entry_row(
            self.cleanup_tab,
            1,
            1,
            "Work branch",
            self.cleanup_work_branch_var,
        )
        self.report_entry = self._entry_row(
            self.cleanup_tab,
            3,
            0,
            "Report file",
            self.report_file_var,
            columnspan=2,
        )

        common = ttk.LabelFrame(self.cleanup_tab, text="Common output", padding=8)
        common.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Checkbutton(common, text="Verbose command output", variable=self.verbose_var).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(self.cleanup_tab)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Preview command", command=lambda: self._preview("cleanup")).grid(row=0, column=0)
        ttk.Button(buttons, text="Start cleanup", command=lambda: self._start("cleanup")).grid(row=0, column=1, padx=(8, 0))

    def _build_output(self, parent: Any) -> None:
        ttk = self.ttk
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        status = ttk.Frame(parent)
        status.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(status, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(8, 0))
        self.confirm_continue_button = ttk.Button(
            status,
            text="Continue",
            command=lambda: self._send_process_input("\n"),
            state="disabled",
        )
        self.confirm_continue_button.grid(row=0, column=2, padx=(8, 0))
        self.confirm_reopen_button = ttk.Button(
            status,
            text="Reopen",
            command=lambda: self._send_process_input("r\n"),
            state="disabled",
        )
        self.confirm_reopen_button.grid(row=0, column=3, padx=(8, 0))
        self.confirm_stop_button = ttk.Button(
            status,
            text="Stop",
            command=lambda: self._send_process_input("s\n"),
            state="disabled",
        )
        self.confirm_stop_button.grid(row=0, column=4, padx=(8, 0))
        ttk.Button(status, text="Clear log", command=self._clear_log).grid(row=0, column=5, padx=(8, 0))

        ttk.Label(parent, text="Command preview").grid(row=1, column=0, sticky="w")
        self.command_text = self.tk.Text(parent, height=5, wrap="word")
        self.command_text.grid(row=2, column=0, sticky="ew", pady=(4, 10))
        self.command_text.configure(state="disabled")

        log_frame = ttk.Frame(parent)
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = self.tk.Text(log_frame, wrap="word", bg="#101418", fg="#e9eef2", insertbackground="#e9eef2")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        inspect = ttk.Frame(parent)
        inspect.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(inspect, text="Show report", command=self._show_report).grid(row=0, column=0, sticky="w")

    def _entry_row(
        self,
        parent: Any,
        row: int,
        column: int,
        label: str,
        variable: Any,
        *,
        columnspan: int = 1,
        pady: tuple[int, int] = (4, 10),
    ) -> Any:
        ttk = self.ttk
        ttk.Label(parent, text=label).grid(row=row, column=column, columnspan=columnspan, sticky="w")
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row + 1, column=column, columnspan=columnspan, sticky="ew", padx=(0, 6), pady=pady)
        return entry

    def _labeled_entry(self, parent: Any, row: int, column: int, label: str, variable: Any) -> Any:
        ttk = self.ttk
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w")
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=column + 1, sticky="ew", padx=(8, 8))
        return entry

    def _common_values(self) -> dict[str, Any]:
        return {
            "repo": self.repo_var.get(),
            "report_file": self.report_file_var.get(),
            "verbose": self.verbose_var.get(),
        }

    def _merge_values(self) -> dict[str, Any]:
        values = self._common_values()
        values.update(
            {
                "merge_branches": self.merge_branches_text.get("1.0", "end").strip(),
                "continue_current_branch": self.branch_mode_var.get() == "continue",
                "base_remote_branch": self.base_remote_branch_var.get(),
                "work_branch": self.work_branch_var.get(),
                "file_conflict_mode": self.file_conflict_mode_var.get(),
                "conflict_tool_command": self.conflict_tool_command_var.get(),
                "conflict_tool_timeout_seconds": self.conflict_tool_timeout_var.get(),
                "allow_dirty_submodules": self.allow_dirty_var.get(),
                "no_prompt_dirty_submodules": self.no_prompt_dirty_var.get(),
                "enable_cross_submodule_moves": self.enable_cross_submodule_moves_var.get(),
                "clone_enabled": self.clone_enabled_var.get(),
                "clone_branch": self.clone_branch_var.get(),
                "clone_url": self.clone_url_var.get(),
                "clone_path": self.clone_path_var.get(),
                "ignore_root_untracked_paths": self.ignore_paths_text.get("1.0", "end").strip(),
            }
        )
        return values

    def _cleanup_values(self) -> dict[str, Any]:
        values = self._common_values()
        values.update(
            {
                "rebuild_work_branch": self.rebuild_work_branch_var.get(),
                "cleanup_base_remote_branch": self.cleanup_base_remote_branch_var.get(),
                "cleanup_work_branch": self.cleanup_work_branch_var.get(),
                "skip_submodule_update": self.skip_submodule_update_var.get(),
            }
        )
        return values

    def _values_for_action(self, action: str) -> dict[str, Any]:
        return self._merge_values() if action == "merge" else self._cleanup_values()

    def _preview(self, action: str) -> CommandSpec | None:
        try:
            spec = build_command_spec(action, self._values_for_action(action))
            command = launcher_command(spec.argv)
            self._set_command_text(command_display(command))
            return spec
        except GuiInputError as exc:
            self._set_command_text(str(exc))
            return None

    def _start(self, action: str) -> None:
        from tkinter import messagebox

        if self.process is not None and self.process.poll() is None:
            messagebox.showwarning("Command running", "A command is already running.")
            return
        spec = self._preview(action)
        if spec is None:
            messagebox.showerror("Invalid input", self.command_text.get("1.0", "end").strip())
            return

        command = launcher_command(spec.argv)
        self._append_log(f"$ {command_display(command)}\n")
        self.status_var.set(f"Running {action}...")
        self.cancel_button.configure(state="normal")
        self._set_confirmation_buttons("disabled")

        self.process = subprocess.Popen(
            command,
            cwd=str(project_root()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            encoding=locale.getpreferredencoding(False) or "utf-8",
            errors="replace",
            bufsize=1,
            env=git_safe_directory_env(self.repo_var.get()),
        )
        self.reader_thread = threading.Thread(target=self._read_process_output, args=(self.process,), daemon=True)
        self.reader_thread.start()

    def _read_process_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                self.events.put(("log", line))
        finally:
            process.stdout.close()
        self.events.put(("done", process.wait()))

    def _cancel(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self._append_log("\nCancelling command...\n")
        self.process.terminate()

    def _set_confirmation_buttons(self, state: str) -> None:
        self.confirm_continue_button.configure(state=state)
        self.confirm_reopen_button.configure(state=state)
        self.confirm_stop_button.configure(state=state)

    def _send_process_input(self, payload: str) -> None:
        if self.process is None or self.process.poll() is not None or self.process.stdin is None:
            self._append_log("\nNo running command is waiting for confirmation.\n")
            self._set_confirmation_buttons("disabled")
            return
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._append_log(f"\nCould not send confirmation: {exc}\n")
            self._set_confirmation_buttons("disabled")
            return
        self._set_confirmation_buttons("disabled")
        self.status_var.set("Running...")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    line = str(payload)
                    self._append_log(line)
                    if CONFLICT_TOOL_CONFIRMATION_MESSAGE in line:
                        self._set_confirmation_buttons("normal")
                        self.status_var.set("Waiting for conflict confirmation...")
                elif kind == "done":
                    exit_code = int(payload) if payload is not None else -1
                    self.cancel_button.configure(state="disabled")
                    self._set_confirmation_buttons("disabled")
                    self.status_var.set("Completed" if exit_code == 0 else f"Failed with exit code {exit_code}")
                    self._append_log(f"\nCommand finished with exit code {exit_code}.\n")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _show_status(self) -> None:
        self._run_git_query(["status", "--short", "--branch"])

    def _show_branches(self) -> None:
        self._run_git_query(["branch", "-a"])

    def _run_git_query(self, args: list[str]) -> None:
        repo = self.repo_var.get()

        def worker() -> None:
            try:
                output = run_git_query(repo, args)
            except Exception as exc:  # pragma: no cover - defensive GUI boundary
                output = str(exc)
            self.events.put(("log", output + "\n"))

        threading.Thread(target=worker, daemon=True).start()

    def _show_report(self) -> None:
        from tkinter import messagebox

        report_file = Path(self.report_file_var.get() or REPORT_FILE_NAME)
        if report_file.is_absolute() or ".." in report_file.parts:
            messagebox.showerror("Invalid report path", "Report file must stay inside the repository.")
            return
        report_path = Path(self.repo_var.get()).resolve(strict=False) / report_file
        if not report_path.exists():
            self._append_log(f"\nReport does not exist: {report_path}\n")
            return
        content = report_path.read_text(encoding="utf-8", errors="replace")
        self._append_log(f"\n--- {report_path} ---\n{content}\n")

    def _browse_repo(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(initialdir=self.repo_var.get() or str(project_root()))
        if selected:
            self.repo_var.set(selected)

    def _browse_clone_path(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(initialdir=self.clone_path_var.get() or str(project_root()))
        if selected:
            self.clone_path_var.set(selected)

    def _select_repo_preset(self, _event: Any = None) -> None:
        selected_label = self.preset_var.get()
        for label, path in self.repo_presets:
            if label == selected_label:
                self.repo_var.set(path)
                return

    def _update_enabled_state(self) -> None:
        reset_state = "normal" if self.branch_mode_var.get() == "reset" else "disabled"
        self.base_entry.configure(state=reset_state)
        self.work_entry.configure(state=reset_state)

        tool_state = "normal" if self.file_conflict_mode_var.get() == "tool" else "disabled"
        self.tool_entry.configure(state=tool_state)
        self.timeout_entry.configure(state=tool_state)

        clone_state = "normal" if self.clone_enabled_var.get() else "disabled"
        self.clone_branch_entry.configure(state=clone_state)
        self.clone_url_entry.configure(state=clone_state)
        self.clone_path_entry.configure(state=clone_state)
        self.clone_path_button.configure(state=clone_state)

        rebuild_state = "normal" if self.rebuild_work_branch_var.get() else "disabled"
        self.cleanup_base_entry.configure(state=rebuild_state)
        self.cleanup_work_entry.configure(state=rebuild_state)

    def _set_command_text(self, content: str) -> None:
        self.command_text.configure(state="normal")
        self.command_text.delete("1.0", "end")
        self.command_text.insert("1.0", content)
        self.command_text.configure(state="disabled")

    def _append_log(self, content: str) -> None:
        self.log_text.insert("end", content)
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", "end")


def launch_gui() -> int:
    try:
        import tkinter as tk
    except ImportError as exc:
        print(f"Tkinter is not available in this Python installation: {exc}", file=sys.stderr)
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(
            "Tkinter could not start. A full Python installation with Tcl/Tk is required.\n"
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    MergeGuiApp(root)
    root.mainloop()
    return 0


def parse_gui_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Desktop GUI for recursive submodule merge.")
    parser.add_argument("--version", action="version", version="recursive_submodule_merge_gui")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args[:1] == ["--cli"]:
        return run_cli(raw_args[1:])
    parse_gui_args(raw_args)
    return launch_gui()


if __name__ == "__main__":
    raise SystemExit(main())
