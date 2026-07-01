"""Manual CLI parser and entry-point runtime split from the original script."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import common
from .commands import ManualCommandService
from .guided_commands import ManualGuidedCommandService, run_guided
from .models import (
    DEFAULT_CONFLICT_TOOL_COMMAND,
    DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS,
    DEFAULT_FILE_CONFLICT_MODE,
    REPORT_FILE_NAME,
    ROOT_BASE_REMOTE_BRANCH,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recursively merge repositories with nested git submodules."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    merge_parser = subparsers.add_parser(
        "merge",
        help="Run the recursive merge workflow.",
    )
    add_merge_arguments(merge_parser)

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Abort failed merges and restore repositories to a reusable state.",
    )
    add_cleanup_arguments(cleanup_parser)

    publish_parser = subparsers.add_parser(
        "publish",
        help="Recursively publish the root HEAD and referenced submodule commits.",
    )
    add_publish_arguments(publish_parser)

    guided_parser = subparsers.add_parser(
        "guided",
        help="Launch an interactive command-line wizard for merge and cleanup.",
    )
    guided_parser.add_argument(
        "--repo",
        default=".",
        help="Default repository path shown in the wizard. Defaults to the current directory.",
    )
    guided_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print executed commands and key progress information.",
    )
    return parser


def add_merge_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "merge_branches",
        nargs="+",
        help="One or more branches or refs to merge in order.",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Root repository path. Defaults to the current directory.",
    )
    parser.add_argument(
        "--clone-branch",
        default=None,
        help="Optional clone branch used before merge. Requires --clone-url and --clone-path.",
    )
    parser.add_argument(
        "--clone-url",
        default=None,
        help="Optional git remote URL used before merge. Requires --clone-branch and --clone-path.",
    )
    parser.add_argument(
        "--clone-path",
        default=None,
        help="Optional target directory used before merge. Requires --clone-branch and --clone-url.",
    )
    branch_mode = parser.add_mutually_exclusive_group()
    branch_mode.add_argument(
        "--base-remote-branch",
        default=ROOT_BASE_REMOTE_BRANCH,
        help=(
            "Root remote branch used to create/reset the work branch before merge. "
            f"Default: {ROOT_BASE_REMOTE_BRANCH}"
        ),
    )
    branch_mode.add_argument(
        "--continue-current-branch",
        action="store_true",
        help="Merge on the currently checked-out root branch instead of creating/resetting a work branch.",
    )
    parser.add_argument(
        "--work-branch",
        default=None,
        help="Work branch name used when creating/resetting the root work branch. If omitted, WORK_BRANCH_TEMPLATE with today's date is used.",
    )
    parser.add_argument(
        "--allow-dirty-submodules",
        action="store_true",
        help=(
            "Allow submodules with local changes to stay dirty when merge preflight can prove "
            "the requested merge refs do not touch their gitlinks."
        ),
    )
    parser.add_argument(
        "--no-prompt-dirty-submodules",
        action="store_true",
        help=(
            "Fail instead of prompting when unaffected dirty submodules are detected. "
            "Useful for GUI and automation callers."
        ),
    )
    parser.add_argument(
        "--report-file",
        default=REPORT_FILE_NAME,
        help=f"Manual-conflict report filename. Default: {REPORT_FILE_NAME}",
    )
    parser.add_argument(
        "--ignore-root-untracked-path",
        action="append",
        default=[],
        help=(
            "Root-repository untracked path to ignore during cleanliness checks. "
            "Can be specified multiple times."
        ),
    )
    parser.add_argument(
        "--file-conflict-mode",
        choices=["tool"],
        default=DEFAULT_FILE_CONFLICT_MODE,
        help="How to handle normal file conflicts. Default: %(default)s",
    )
    parser.add_argument(
        "--conflict-tool-command",
        default=DEFAULT_CONFLICT_TOOL_COMMAND,
        help=(
            "Command used when --file-conflict-mode=tool. "
            "Supports {file}, {repo}, {relative}. "
            f"Default: {DEFAULT_CONFLICT_TOOL_COMMAND}"
        ),
    )
    parser.add_argument(
        "--conflict-tool-timeout-seconds",
        type=int,
        default=DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS,
        help=(
            "Max seconds to wait for each opened conflict file. "
            "Use 0 to wait forever. "
            f"Default: {DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS}"
        ),
    )
    parser.add_argument(
        "--no-confirm-after-conflict-tool",
        action="store_true",
        help=(
            "Continue automatically after the conflict tool exits. "
            "By default, the workflow waits for manual confirmation before staging the file."
        ),
    )
    parser.add_argument(
        "--enable-cross-submodule-moves",
        action="store_true",
        help=(
            "Enable experimental detection and merging of files moved across submodule boundaries. "
            "Disabled by default."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print executed commands and key progress information.",
    )


def add_cleanup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        default=".",
        help="Root repository path. Defaults to the current directory.",
    )
    parser.add_argument(
        "--report-file",
        default=REPORT_FILE_NAME,
        help=f"Manual-conflict report filename to remove from the root repo. Default: {REPORT_FILE_NAME}",
    )
    parser.add_argument(
        "--skip-submodule-update",
        action="store_true",
        help="Skip the final git submodule update --init --recursive step.",
    )
    parser.add_argument(
        "--rebuild-work-branch",
        action="store_true",
        help="After cleanup, recreate the work branch from the configured base branches.",
    )
    parser.add_argument(
        "--base-remote-branch",
        default=ROOT_BASE_REMOTE_BRANCH,
        help=f"Root branch used when rebuilding the work branch. Default: {ROOT_BASE_REMOTE_BRANCH}",
    )
    parser.add_argument(
        "--work-branch",
        default=None,
        help="Work branch name to recreate. If omitted, WORK_BRANCH_TEMPLATE with today's date is used.",
    )
    parser.add_argument(
        "--allow-clean-x",
        action="store_true",
        help="Allow git clean -x (remove ignored files) during cleanup for stubborn states.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print executed commands and key progress information.",
    )


def add_publish_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        default=".",
        help="Root repository path. Defaults to the current directory.",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Remote name used for all repositories. Default: origin",
    )
    parser.add_argument(
        "--push-target",
        "--push_target",
        dest="push_target",
        required=True,
        help="Remote branch name to publish in the root repository and all submodules.",
    )
    parser.add_argument(
        "--check-only",
        "--check_only",
        dest="check_only",
        action="store_true",
        help="Run all publish checks and dry-run pushes without changing local branch refs or pushing.",
    )
    parser.add_argument(
        "--no-sync-local-branches",
        action="store_true",
        help="Do not create best-effort local branch refs that point at the published commits.",
    )
    parser.add_argument(
        "--no-branch-suffix",
        action="store_true",
        help="Do not create a suffix branch when the requested local branch name points elsewhere.",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Skip the interactive publish confirmation and push immediately.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print executed commands and key progress information.",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    normalized = common.normalize_argv(list(sys.argv[1:] if argv is None else argv))
    return parser.parse_args(normalized)


def _check_code_at_startup(args: argparse.Namespace) -> None:
    """Check that the conflict-tool command (typically 'code --wait') is available at startup.

    Only applies when the subcommand is ``merge`` and the conflict-tool-command
    references a VS Code executable that cannot be resolved.  If the check fails
    the function prints the error to stderr and calls ``sys.exit(1)`` so the
    caller never enters the merge workflow.
    """
    if args.command != "merge":
        return

    from .file_conflicts import prepare_conflict_tool_command

    command_template = getattr(args, "conflict_tool_command", None) or DEFAULT_CONFLICT_TOOL_COMMAND
    result = prepare_conflict_tool_command(command_template)
    if result.error:
        print(result.error, file=sys.stderr)
        sys.exit(1)
    if result.message:
        print(result.message)


def main(
    argv: list[str] | None = None,
    *,
    command_service: ManualCommandService | None = None,
    guided_service: ManualGuidedCommandService | None = None,
) -> int:
    from .config_persistence import apply_config_to_args, save_config_from_args

    args = parse_args(argv)
    _check_code_at_startup(args)

    # Apply saved configuration defaults for fields the user didn't override
    args = apply_config_to_args(args)

    commands = command_service or ManualCommandService()
    exit_code = 0

    if args.command == "cleanup":
        exit_code = commands.run_cleanup(args)
    elif args.command == "publish":
        exit_code = commands.run_publish(args)
    elif args.command == "guided":
        if guided_service is not None:
            exit_code = guided_service.run_guided(args)
        else:
            exit_code = run_guided(
                args,
                run_merge=commands.run_merge,
                run_cleanup=commands.run_cleanup,
                set_verbose=commands.set_verbose,
            )
    else:
        exit_code = commands.run_merge(args)

    # Persist successful runs so the next invocation can pre-fill defaults
    if exit_code == 0:
        save_config_from_args(args, args.command)

    return exit_code
