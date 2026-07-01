"""Manual guided CLI command and prompt boundary."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import common
from .commands import ManualCommandService
from .models import (
    DEFAULT_CONFLICT_TOOL_COMMAND,
    DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS,
    REPORT_FILE_NAME,
    ROOT_BASE_REMOTE_BRANCH,
)


GUIDED_COMMAND_FUNCTIONS: tuple[str, ...] = ()


def prompt_with_default(prompt: str, default: str, *, input_func: Any = input) -> str:
    answer = input_func(f"{prompt} [{default}]: ").strip()
    return answer or default


def prompt_optional(prompt: str, default: str | None = None, *, input_func: Any = input) -> str | None:
    suffix = f" [{default}]" if default else ""
    answer = input_func(f"{prompt}{suffix}: ").strip()
    if not answer:
        return None
    return answer


def prompt_yes_no(prompt: str, default: bool, *, input_func: Any = input) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input_func(f"{prompt} [{suffix}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def run_guided(
    args: argparse.Namespace,
    *,
    run_merge: Any,
    run_cleanup: Any,
    set_verbose: Any,
    input_func: Any = input,
) -> int:
    from .config_persistence import load_config

    saved_config = load_config()

    set_verbose(getattr(args, "verbose", False))
    print("Recursive Submodule Merge Wizard")
    print("================================")
    print("Merge branch inputs default to remote refs: non-origin names become origin/<branch>.")
    print("Nested submodules follow the parent repository's recorded gitlink commits.")
    print("Merge now runs preflight checks only; if a repository needs recovery, run cleanup explicitly before merging.")

    default_repo = saved_config.merge.repo if saved_config.last_command in {"merge", "cleanup"} else str(Path(args.repo).resolve())
    repo = Path(prompt_with_default("Repository path", default_repo, input_func=input_func)).resolve()
    action = prompt_with_default("Choose action: merge or cleanup",
                                  saved_config.last_command if saved_config.last_command in {"merge", "cleanup"} else "merge",
                                  input_func=input_func).lower()
    if action not in {"merge", "cleanup"}:
        print("Unsupported action. Please choose merge or cleanup.", file=sys.stderr)
        return 1

    if action == "merge":
        mc = saved_config.merge
        default_branches_raw = ", ".join(mc.merge_branches) if mc.merge_branches else ""
        merge_branches = common.parse_branch_tokens(
            input_func(
                f"Merge branches in order (branch name or origin/branch; comma or line separated)"
                f"{' [' + default_branches_raw + ']' if default_branches_raw else ''}: "
            ).strip()
            or default_branches_raw
        )
        if not merge_branches:
            print("At least one merge branch is required.", file=sys.stderr)
            return 1

        continue_current_branch = prompt_yes_no(
            "Continue merging on the current root branch instead of creating/resetting a work branch",
            mc.continue_current_branch,
            input_func=input_func,
        )
        base_remote_branch = ROOT_BASE_REMOTE_BRANCH
        work_branch = None
        if not continue_current_branch:
            base_remote_branch = prompt_with_default(
                "Root base remote branch",
                mc.base_remote_branch,
                input_func=input_func,
            )
            work_branch = prompt_optional(
                "Work branch name (leave empty to auto-pick)",
                mc.work_branch or common.compute_work_branch(None),
                input_func=input_func,
            )
        merge_args = argparse.Namespace(
            command="merge",
            merge_branches=merge_branches,
            repo=str(repo),
            clone_branch=prompt_optional("Clone branch before merge (leave empty to skip clone)", input_func=input_func),
            clone_url=None,
            clone_path=None,
            base_remote_branch=base_remote_branch,
            continue_current_branch=continue_current_branch,
            work_branch=work_branch,
            allow_dirty_submodules=prompt_yes_no(
                "Allow unaffected dirty submodules to remain dirty when preflight can prove they are untouched",
                mc.allow_dirty_submodules,
                input_func=input_func,
            ),
            no_prompt_dirty_submodules=False,
            ignore_root_untracked_path=[],
            report_file=prompt_with_default("Conflict report filename", REPORT_FILE_NAME, input_func=input_func),
            file_conflict_mode="tool",
            conflict_tool_command=prompt_with_default(
                "Conflict tool command",
                mc.conflict_tool_command,
                input_func=input_func,
            ),
            conflict_tool_timeout_seconds=int(
                prompt_with_default(
                    "Conflict tool timeout seconds (0 means wait forever)",
                    str(mc.conflict_tool_timeout_seconds),
                    input_func=input_func,
                )
            ),
            verbose=prompt_yes_no("Enable verbose command output", mc.verbose, input_func=input_func),
        )
        if merge_args.clone_branch:
            merge_args.clone_url = prompt_optional("Clone git URL", input_func=input_func)
            merge_args.clone_path = prompt_optional("Clone target path", input_func=input_func)
        return run_merge(merge_args)

    cc = saved_config.cleanup
    rebuild_work_branch = prompt_yes_no("Rebuild the work branch after cleanup", cc.rebuild_work_branch, input_func=input_func)
    base_remote_branch = ROOT_BASE_REMOTE_BRANCH
    work_branch = None
    if rebuild_work_branch:
        base_remote_branch = prompt_with_default("Root base remote branch", ROOT_BASE_REMOTE_BRANCH, input_func=input_func)
        work_branch = prompt_with_default(
            "Work branch name",
            cc.work_branch or common.compute_work_branch(None),
            input_func=input_func,
        )
    cleanup_args = argparse.Namespace(
        command="cleanup",
        repo=str(repo),
        report_file=prompt_with_default("Conflict report filename", REPORT_FILE_NAME, input_func=input_func),
        skip_submodule_update=not prompt_yes_no("Run submodule update after cleanup", not cc.skip_submodule_update, input_func=input_func),
        rebuild_work_branch=rebuild_work_branch,
        base_remote_branch=base_remote_branch,
        work_branch=work_branch,
        allow_clean_x=prompt_yes_no("Allow git clean -x (remove ignored files) during cleanup", cc.allow_clean_x, input_func=input_func),
        verbose=prompt_yes_no("Enable verbose command output", getattr(args, "verbose", False), input_func=input_func),
    )
    return run_cleanup(cleanup_args)


@dataclass(frozen=True)
class ManualGuidedCommandService:
    """Facade for interactive guided-command construction."""

    manual_module: ModuleType | None = None

    def __post_init__(self) -> None:
        return None

    def prompt_with_default(self, prompt: str, default: str) -> str:
        return prompt_with_default(prompt, default)

    def prompt_optional(self, prompt: str, default: str | None = None) -> str | None:
        return prompt_optional(prompt, default)

    def prompt_yes_no(self, prompt: str, default: bool) -> bool:
        return prompt_yes_no(prompt, default)

    def parse_branch_tokens(self, raw_value: str) -> list[str]:
        return common.parse_branch_tokens(raw_value)

    def run_guided(self, args: Any) -> int:
        commands = ManualCommandService(self.manual_module)
        return run_guided(
            args,
            run_merge=commands.run_merge,
            run_cleanup=commands.run_cleanup,
            set_verbose=commands.set_verbose,
        )
