"""Manual file-conflict resolution boundary."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from . import common
from . import execution
from .models import ConflictEntry, FileConflictResolution


FILE_CONFLICT_FUNCTIONS: tuple[str, ...] = ()
CONFLICT_TOOL_CONFIRMATION_MESSAGE = "Manual confirmation required after conflict tool closes"
CONFLICT_TOOL_CONFIRMATION_PROMPT = (
    "Press Enter to continue, type r to reopen the conflict tool, or type s to stop and preserve the conflict: "
)


def render_conflict_tool_command(repo_path: Path, relative_path: str, command_template: str) -> str:
    return command_template.format(
        file=str((repo_path / relative_path).resolve()),
        repo=str(repo_path.resolve()),
        relative=relative_path,
    )


def record_file_conflicts(
    repo_path: Path,
    file_conflicts: list[str],
    context: Any,
    *,
    reason: str,
    category: str = "manual_conflict",
) -> None:
    if not file_conflicts:
        return
    context.report_entries.append(
        ConflictEntry(
            repo_path=repo_path,
            files=file_conflicts,
            reason=reason,
            category=category,
        )
    )


def get_unmerged_paths(repo_path: Path, *, run_git: Any) -> list[str]:
    result = run_git(repo_path, ["diff", "--name-only", "--diff-filter=U"])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def emit_line(output_func: Any, message: str) -> None:
    try:
        output_func(message, flush=True)
    except TypeError:
        output_func(message)


def prompt_after_conflict_tool(
    repo_path: Path,
    relative_path: str,
    *,
    input_func: Any = input,
    output_func: Any = print,
) -> str:
    emit_line(output_func, f"{CONFLICT_TOOL_CONFIRMATION_MESSAGE}: {repo_path / relative_path}")
    while True:
        try:
            answer = input_func(CONFLICT_TOOL_CONFIRMATION_PROMPT)
        except EOFError:
            return "stop"
        normalized = answer.strip().lower()
        if normalized in {"", "c", "continue", "y", "yes"}:
            return "continue"
        if normalized in {"r", "reopen", "retry"}:
            return "reopen"
        if normalized in {"s", "stop", "p", "preserve"}:
            return "stop"
        emit_line(output_func, "Please press Enter, or type r/reopen, or s/stop.")


def resolve_moved_away_file_conflicts(
    repo_path: Path,
    file_conflicts: Iterable[str],
    moved_away_paths: Iterable[str],
    *,
    run_git: Any,
    get_unmerged_paths: Any,
    verbose_print: Any | None = None,
) -> list[str]:
    moved_away = {
        common.normalize_status_relative_path(path)
        for path in moved_away_paths
        if path.strip()
    }
    resolved: list[str] = []
    for relative_path in file_conflicts:
        normalized_path = common.normalize_status_relative_path(relative_path)
        if normalized_path not in moved_away:
            continue
        result = run_git(repo_path, ["rm", "-f", "--", relative_path], check=False)
        if result.returncode != 0:
            result = run_git(repo_path, ["add", "-u", "--", relative_path], check=False)
        if result.returncode == 0 and relative_path not in get_unmerged_paths(repo_path):
            if callable(verbose_print):
                verbose_print(
                    "Resolved moved-away file conflict by keeping deletion at old location: "
                    f"{repo_path / relative_path}"
                )
            resolved.append(relative_path)
    return resolved


def resolve_single_file_conflict(
    repo_path: Path,
    relative_path: str,
    context: Any,
    *,
    run_git: Any,
    popen_factory: Any = subprocess.Popen,
    input_func: Any = input,
    output_func: Any = print,
) -> FileConflictResolution:
    if context.file_conflict_mode == "preserve":
        return FileConflictResolution(reason="current run is configured to preserve conflicts")

    while True:
        command = render_conflict_tool_command(
            repo_path,
            relative_path,
            context.conflict_tool_command,
        )
        try:
            process = popen_factory(command, cwd=repo_path, shell=True)
        except OSError as exc:
            return FileConflictResolution(reason=f"failed to launch conflict tool: {exc}")

        try:
            if context.conflict_tool_timeout_seconds is None:
                process.wait()
            else:
                process.wait(timeout=context.conflict_tool_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return FileConflictResolution(
                reason=f"conflict tool timed out after {context.conflict_tool_timeout_seconds} seconds",
                rollback_requested=True,
            )

        if process.returncode not in (0, None):
            return FileConflictResolution(reason=f"conflict tool exited with code {process.returncode}")

        if not getattr(context, "confirm_after_conflict_tool", False):
            break

        confirmation = prompt_after_conflict_tool(
            repo_path,
            relative_path,
            input_func=input_func,
            output_func=output_func,
        )
        if confirmation == "continue":
            break
        if confirmation == "reopen":
            continue
        return FileConflictResolution(reason="manual confirmation stopped after the conflict tool closed")

    failure_reasons: list[str] = []
    was_unmerged = relative_path in get_unmerged_paths(repo_path, run_git=run_git)
    run_git(repo_path, ["add", "--", relative_path], check=False)

    marker_check = run_git(
        repo_path,
        ["diff", "--check", "--cached", "--", relative_path],
        check=False,
    )
    marker_output = "\n".join(
        value.strip()
        for value in (marker_check.stdout, marker_check.stderr)
        if value and value.strip()
    )
    if "leftover conflict marker" in marker_output.lower():
        failure_reasons.append("file still contains conflict markers after the tool closed")

    if was_unmerged and relative_path in get_unmerged_paths(repo_path, run_git=run_git):
        failure_reasons.append("conflict is still unresolved after the tool closed")

    if failure_reasons:
        return FileConflictResolution(reason="; ".join(dict.fromkeys(failure_reasons)))

    return FileConflictResolution(reason=None)


def resolve_file_conflicts(
    repo_path: Path,
    file_conflicts: list[str],
    context: Any,
    *,
    run_git: Any,
    popen_factory: Any = subprocess.Popen,
    input_func: Any = input,
    output_func: Any = print,
) -> tuple[list[str], str, bool]:
    unresolved: list[str] = []
    reasons: list[str] = []
    rollback_requested = False
    for relative_path in file_conflicts:
        resolution = resolve_single_file_conflict(
            repo_path,
            relative_path,
            context,
            run_git=run_git,
            popen_factory=popen_factory,
            input_func=input_func,
            output_func=output_func,
        )
        rollback_requested = rollback_requested or resolution.rollback_requested
        if resolution.reason is None:
            continue
        unresolved.append(relative_path)
        reasons.append(f"{relative_path}: {resolution.reason}")

    if not unresolved:
        return [], "", rollback_requested

    return unresolved, "; ".join(reasons), rollback_requested


@dataclass(frozen=True)
class ManualFileConflictService:
    """Facade for file conflicts and external conflict-tool invocation."""

    manual_module: ModuleType | None = None

    def __post_init__(self) -> None:
        return None

    def _run_git(self, repo_path: Path, args: list[str], **kwargs: Any) -> Any:
        runner = getattr(self.manual_module, "run_git", execution.run_git)
        return runner(repo_path, args, **kwargs)

    def _verbose_print(self, message: str) -> None:
        printer = getattr(self.manual_module, "verbose_print", None)
        if callable(printer):
            printer(message)
            return
        execution.verbose_print(message)

    def render_conflict_tool_command(self, repo_path: Path, relative_path: str, command_template: str) -> str:
        return render_conflict_tool_command(repo_path, relative_path, command_template)

    def resolve_single_file_conflict(self, repo_path: Path, relative_path: str, context: Any) -> Any:
        return resolve_single_file_conflict(
            repo_path,
            relative_path,
            context,
            run_git=self._run_git,
        )

    def resolve_file_conflicts(self, repo_path: Path, file_conflicts: list[str], context: Any) -> tuple[list[str], str, bool]:
        return resolve_file_conflicts(
            repo_path,
            file_conflicts,
            context,
            run_git=self._run_git,
        )

    def record_file_conflicts(
        self,
        repo_path: Path,
        unresolved_files: list[str],
        context: Any,
        *,
        reason: str,
        category: str = "manual_conflict",
    ) -> None:
        record_file_conflicts(
            repo_path,
            unresolved_files,
            context,
            reason=reason,
            category=category,
        )

    def resolve_moved_away_file_conflicts(
        self,
        repo_path: Path,
        file_conflicts: list[str],
        moved_away_paths: set[str],
    ) -> list[str]:
        return resolve_moved_away_file_conflicts(
            repo_path,
            file_conflicts,
            moved_away_paths,
            run_git=self._run_git,
            get_unmerged_paths=lambda path: get_unmerged_paths(path, run_git=self._run_git),
            verbose_print=self._verbose_print,
        )
