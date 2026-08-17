"""Tkinter desktop GUI for the manual recursive submodule merge workflow.

This file intentionally uses only the Python standard library. It can be run
directly from the source tree or packaged as a single GUI executable together
with the local manual_merge_split package.
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from gui_command_builders import (
    CloneSpec,
    CommandSpec,
    DEFAULT_CONFLICT_TOOL_COMMAND,
    DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS,
    DEFAULT_FILE_CONFLICT_MODE,
    GuiInputError,
    REPORT_FILE_NAME,
    ROOT_BASE_REMOTE_BRANCH,
    build_cleanup_argv,
    build_clone_spec,
    build_command_spec,
    build_merge_argv,
    build_publish_argv,
    clone_target_path,
    parse_branch_tokens,
    text_lines,
)
from manual_merge_split.repository_ops import (
    RISK_CONFIRMATION_EVENT_PREFIX,
    RISK_CONFIRMATION_PROTOCOL_ENV,
    RISK_CONFIRMATION_PROTOCOL_VERSION,
)
from manual_merge_split.window_icon import apply_window_icon
from subprocess_window import hidden_subprocess_kwargs


WORK_BRANCH_DEFAULT = "validation-work"
CONFLICT_TOOL_CONFIRMATION_MESSAGE = "Manual confirmation required after conflict tool closes"
CONTINUE_BASE_REMOTE_PLACEHOLDER = "HEAD"
CONTINUE_WORK_BRANCH_PLACEHOLDER = "current branch"
OUTPUT_MODE_OPTIMIZED = "optimized"
OUTPUT_MODE_RAW = "raw"
LANGUAGE_EN = "en"
LANGUAGE_ZH = "zh"
REPOSITORY_SOURCE_EXISTING = "existing"
REPOSITORY_SOURCE_CLONE = "clone"
DEFAULT_PUBLISH_TARGET_BRANCH = ""

UI_TEXT: dict[str, dict[str, str]] = {
    "window_title": {
        LANGUAGE_EN: "Recursive Submodule Merge",
        LANGUAGE_ZH: "递归子模块合并",
    },
    "label_repository": {LANGUAGE_EN: "Repository", LANGUAGE_ZH: "仓库"},
    "frame_repository_source": {LANGUAGE_EN: "Repository Source", LANGUAGE_ZH: "仓库来源"},
    "source_existing_repository": {LANGUAGE_EN: "Existing repository", LANGUAGE_ZH: "已有仓库"},
    "source_new_clone_repository": {LANGUAGE_EN: "New git clone repository", LANGUAGE_ZH: "新 git clone 仓库"},
    "label_repository_url": {LANGUAGE_EN: "Repository URL", LANGUAGE_ZH: "远端仓库地址"},
    "label_destination": {LANGUAGE_EN: "Destination", LANGUAGE_ZH: "父目录"},
    "label_directory_name": {LANGUAGE_EN: "Directory Name", LANGUAGE_ZH: "子目录名"},
    "label_clone_branch": {LANGUAGE_EN: "Branch", LANGUAGE_ZH: "分支"},
    "label_clone_target": {LANGUAGE_EN: "Clone target", LANGUAGE_ZH: "Clone 目标"},
    "hint_clone_branch": {
        LANGUAGE_EN: "Leave empty to use remote HEAD.",
        LANGUAGE_ZH: "留空表示使用远端默认 HEAD。",
    },
    "button_clone": {LANGUAGE_EN: "Clone", LANGUAGE_ZH: "Clone"},
    "label_language": {LANGUAGE_EN: "Language", LANGUAGE_ZH: "语言"},
    "language_english": {LANGUAGE_EN: "English", LANGUAGE_ZH: "English"},
    "language_chinese": {LANGUAGE_EN: "中文", LANGUAGE_ZH: "中文"},
    "button_browse": {LANGUAGE_EN: "Browse", LANGUAGE_ZH: "浏览"},
    "button_status": {LANGUAGE_EN: "Status", LANGUAGE_ZH: "状态"},
    "button_branches": {LANGUAGE_EN: "Branches", LANGUAGE_ZH: "分支"},
    "tab_merge": {LANGUAGE_EN: "Merge", LANGUAGE_ZH: "合并"},
    "tab_cleanup": {LANGUAGE_EN: "Cleanup", LANGUAGE_ZH: "清理"},
    "tab_publish": {LANGUAGE_EN: "Publish", LANGUAGE_ZH: "发布"},
    "label_merge_branch": {
        LANGUAGE_EN: "Merge branch or ref",
        LANGUAGE_ZH: "待合并分支或引用",
    },
    "frame_root_branch_mode": {LANGUAGE_EN: "Root branch mode", LANGUAGE_ZH: "根仓库分支模式"},
    "mode_reset_work_branch": {
        LANGUAGE_EN: "Create or reset work branch",
        LANGUAGE_ZH: "创建或重置工作分支",
    },
    "mode_use_current_branch": {
        LANGUAGE_EN: "Use current branch",
        LANGUAGE_ZH: "使用当前分支",
    },
    "label_base_remote_branch": {LANGUAGE_EN: "Base remote branch", LANGUAGE_ZH: "基准远端分支"},
    "label_work_branch": {LANGUAGE_EN: "Work branch", LANGUAGE_ZH: "工作分支"},
    "button_show_advanced": {LANGUAGE_EN: "Show advanced options", LANGUAGE_ZH: "显示高级选项"},
    "button_hide_advanced": {LANGUAGE_EN: "Hide advanced options", LANGUAGE_ZH: "隐藏高级选项"},
    "frame_advanced_options": {LANGUAGE_EN: "Advanced options", LANGUAGE_ZH: "高级选项"},
    "label_conflict_tool_command": {
        LANGUAGE_EN: "Conflict tool command",
        LANGUAGE_ZH: "冲突工具命令",
    },
    "label_tool_timeout_seconds": {
        LANGUAGE_EN: "Tool timeout seconds",
        LANGUAGE_ZH: "工具超时时间（秒）",
    },
    "hint_clean_worktree_required": {
        LANGUAGE_EN: (
            "Merge requires a clean working tree in the root repository and every submodule. "
            "Use the Cleanup tab to discard local changes first; you are responsible for any data loss."
        ),
        LANGUAGE_ZH: (
            "合并前根仓库和所有子模块的工作区必须干净。"
            "如需丢弃本地修改，请先使用「清理」页；由此造成的数据丢失由你自行负责。"
        ),
    },
    "hint_cleanup_responsibility": {
        LANGUAGE_EN: (
            "Cleanup is the supported way to make a dirty repository mergeable. "
            "It can recursively reset and delete uncommitted work. Continue only if you accept that loss."
        ),
        LANGUAGE_ZH: (
            "清理是让脏仓库可以合并的入口。"
            "它会递归 reset 并删除未提交内容。只有在你接受该损失时才继续。"
        ),
    },
    "label_branch": {LANGUAGE_EN: "Branch", LANGUAGE_ZH: "分支"},
    "label_url": {LANGUAGE_EN: "URL", LANGUAGE_ZH: "URL"},
    "label_target_path": {LANGUAGE_EN: "Target path", LANGUAGE_ZH: "目标路径"},
    "label_ignored_root_untracked_paths": {
        LANGUAGE_EN: "Ignored root untracked paths",
        LANGUAGE_ZH: "忽略的根仓库未跟踪路径",
    },
    "button_show_command_preview": {LANGUAGE_EN: "Show command preview", LANGUAGE_ZH: "显示命令预览"},
    "button_hide_command_preview": {LANGUAGE_EN: "Hide command preview", LANGUAGE_ZH: "隐藏命令预览"},
    "button_refresh_command_preview": {LANGUAGE_EN: "Refresh", LANGUAGE_ZH: "刷新"},
    "button_merge_status": {LANGUAGE_EN: "Session status", LANGUAGE_ZH: "会话状态"},
    "button_resume_merge": {LANGUAGE_EN: "Resume merge", LANGUAGE_ZH: "继续合并"},
    "button_abort_merge": {LANGUAGE_EN: "Abort merge", LANGUAGE_ZH: "终止合并"},
    "message_abort_risk_title": {LANGUAGE_EN: "Abort manual merge?", LANGUAGE_ZH: "终止手动合并？"},
    "message_abort_risk_body": {
        LANGUAGE_EN: "Abort restores repositories involved in this merge session and discards manual conflict edits. Untracked files are not deleted. Continue?",
        LANGUAGE_ZH: "终止操作会恢复本次会话涉及的仓库，并丢弃尚未提交的冲突处理内容；未跟踪文件不会被删除。是否继续？",
    },
    "button_start_merge": {LANGUAGE_EN: "Start merge", LANGUAGE_ZH: "开始合并"},
    "frame_cleanup_options": {LANGUAGE_EN: "Cleanup options", LANGUAGE_ZH: "清理选项"},
    "option_rebuild_work_branch": {
        LANGUAGE_EN: "Rebuild work branch after cleanup",
        LANGUAGE_ZH: "清理后重建工作分支",
    },
    "option_skip_submodule_update": {
        LANGUAGE_EN: "Skip submodule update",
        LANGUAGE_ZH: "跳过子模块更新",
    },
    "label_report_file": {LANGUAGE_EN: "Report file", LANGUAGE_ZH: "报告文件"},
    "button_start_cleanup": {LANGUAGE_EN: "Start cleanup", LANGUAGE_ZH: "开始清理"},
    "frame_publish_options": {LANGUAGE_EN: "Publish options", LANGUAGE_ZH: "发布选项"},
    "label_publish_remote": {LANGUAGE_EN: "Remote", LANGUAGE_ZH: "远端"},
    "label_publish_target": {LANGUAGE_EN: "Push target branch (required)", LANGUAGE_ZH: "推送目标分支（必填）"},
    "option_publish_check_only": {
        LANGUAGE_EN: "Check only",
        LANGUAGE_ZH: "仅检查",
    },
    "option_publish_sync_local_branches": {
        LANGUAGE_EN: "Sync local branch refs",
        LANGUAGE_ZH: "同步本地分支引用",
    },
    "option_publish_allow_branch_suffix": {
        LANGUAGE_EN: "Allow suffix branch if the target name points elsewhere",
        LANGUAGE_ZH: "目标分支名被占用时允许创建后缀分支",
    },
    "hint_publish": {
        LANGUAGE_EN: (
            "Publishes root HEAD and submodule commits recorded by root HEAD. "
            "It runs git submodule update --init --recursive first, never checks out branches, "
            "and never force-pushes. The push target is intentionally blank by default."
        ),
        LANGUAGE_ZH: (
            "发布以根仓 HEAD 为准，并使用根仓 HEAD 记录的子模块 commit。"
            "发布前会先执行 git submodule update --init --recursive，不切换分支，也不会强制推送。"
            "推送目标默认留空，请手动确认后填写。"
        ),
    },
    "button_start_publish": {LANGUAGE_EN: "Start publish", LANGUAGE_ZH: "开始发布"},
    "message_tool_not_found_title": {
        LANGUAGE_EN: "Conflict tool not found",
        LANGUAGE_ZH: "冲突工具未找到",
    },
    "message_tool_not_found_body": {
        LANGUAGE_EN: "The conflict tool command '{tool}' was not found on the system PATH.\n\nPlease install the tool or change the conflict tool command in advanced options.",
        LANGUAGE_ZH: "冲突工具命令 '{tool}' 在系统 PATH 中未找到。\n\n请安装该工具或在高级选项中更改冲突工具命令。",
    },
    "status_publish_checking": {
        LANGUAGE_EN: "Checking publish plan...",
        LANGUAGE_ZH: "正在检查发布计划...",
    },
    "status_publish_check_failed": {
        LANGUAGE_EN: "Publish check failed",
        LANGUAGE_ZH: "发布检查失败",
    },
    "message_publish_check_failed_title": {
        LANGUAGE_EN: "Publish check failed",
        LANGUAGE_ZH: "发布检查失败",
    },
    "log_publish_check_start": {
        LANGUAGE_EN: "Running publish check...\n",
        LANGUAGE_ZH: "正在运行发布检查...\n",
    },
    "log_publish_check_complete": {
        LANGUAGE_EN: "Publish check completed. {count} repositories to push.\n",
        LANGUAGE_ZH: "发布检查完成。{count} 个仓库待推送。\n",
    },
    "log_publish_check_no_items": {
        LANGUAGE_EN: "Publish check completed. All repositories are up to date.\n",
        LANGUAGE_ZH: "发布检查完成。所有仓库已是最新。\n",
    },
    "log_publish_cancelled": {
        LANGUAGE_EN: "Publish cancelled by user.\n",
        LANGUAGE_ZH: "发布已被用户取消。\n",
    },
    "log_publish_executing": {
        LANGUAGE_EN: "Executing publish for {count} selected repositories...\n",
        LANGUAGE_ZH: "正在对 {count} 个选中仓库执行发布...\n",
    },
    "button_cancel": {LANGUAGE_EN: "Cancel", LANGUAGE_ZH: "取消"},
    "button_continue": {LANGUAGE_EN: "Continue", LANGUAGE_ZH: "继续"},
    "button_reopen": {LANGUAGE_EN: "Reopen", LANGUAGE_ZH: "重新打开"},
    "button_stop": {LANGUAGE_EN: "Stop", LANGUAGE_ZH: "停止"},
    "button_clear_log": {LANGUAGE_EN: "Clear log", LANGUAGE_ZH: "清空日志"},
    "label_console_output": {LANGUAGE_EN: "Console output", LANGUAGE_ZH: "控制台输出"},
    "output_optimized": {LANGUAGE_EN: "Optimized", LANGUAGE_ZH: "优化输出"},
    "output_raw": {LANGUAGE_EN: "Raw", LANGUAGE_ZH: "原始输出"},
    "label_command_preview": {LANGUAGE_EN: "Command preview", LANGUAGE_ZH: "命令预览"},
    "button_show_report": {LANGUAGE_EN: "Show report", LANGUAGE_ZH: "显示报告"},
    "status_idle": {LANGUAGE_EN: "Idle", LANGUAGE_ZH: "空闲"},
    "status_cloning": {LANGUAGE_EN: "Cloning repository...", LANGUAGE_ZH: "正在 clone 仓库..."},
    "status_clone_completed": {LANGUAGE_EN: "Clone completed", LANGUAGE_ZH: "Clone 已完成"},
    "status_running_action": {LANGUAGE_EN: "Running {action}...", LANGUAGE_ZH: "正在运行{action}..."},
    "status_running": {LANGUAGE_EN: "Running...", LANGUAGE_ZH: "正在运行..."},
    "status_waiting_conflict": {
        LANGUAGE_EN: "Waiting for conflict confirmation...",
        LANGUAGE_ZH: "等待冲突确认...",
    },
    "status_conflict_timeout": {
        LANGUAGE_EN: "Conflict tool timed out; stopping...",
        LANGUAGE_ZH: "冲突工具超时，正在停止...",
    },
    "status_waiting_risk_confirmation": {
        LANGUAGE_EN: "Waiting for risk confirmation...",
        LANGUAGE_ZH: "\u7b49\u5f85\u98ce\u9669\u786e\u8ba4...",
    },
    "status_completed": {LANGUAGE_EN: "Completed", LANGUAGE_ZH: "已完成"},
    "status_failed": {
        LANGUAGE_EN: "Failed with exit code {exit_code}",
        LANGUAGE_ZH: "失败，退出码 {exit_code}",
    },
    "action_merge": {LANGUAGE_EN: "merge", LANGUAGE_ZH: "合并"},
    "action_status": {LANGUAGE_EN: "session status", LANGUAGE_ZH: "会话状态"},
    "action_resume": {LANGUAGE_EN: "resume", LANGUAGE_ZH: "继续"},
    "action_abort": {LANGUAGE_EN: "abort", LANGUAGE_ZH: "终止"},
    "action_cleanup": {LANGUAGE_EN: "cleanup", LANGUAGE_ZH: "清理"},
    "action_publish": {LANGUAGE_EN: "publish", LANGUAGE_ZH: "发布"},
    "message_command_running_title": {LANGUAGE_EN: "Command running", LANGUAGE_ZH: "命令正在运行"},
    "message_command_running_body": {
        LANGUAGE_EN: "A command is already running.",
        LANGUAGE_ZH: "已有命令正在运行。",
    },
    "message_invalid_input_title": {LANGUAGE_EN: "Invalid input", LANGUAGE_ZH: "输入无效"},
    "message_clone_source_not_ready": {
        LANGUAGE_EN: "Clone the repository first, or switch to Existing repository before running this command.",
        LANGUAGE_ZH: "请先 Clone 仓库，或切回已有仓库后再运行该命令。",
    },
    "message_cleanup_risk_title": {
        LANGUAGE_EN: "Cleanup may discard local changes",
        LANGUAGE_ZH: "\u6e05\u7406\u53ef\u80fd\u4e22\u5f03\u672c\u5730\u4fee\u6539",
    },
    "message_cleanup_risk_body": {
        LANGUAGE_EN: (
            "Cleanup may recursively abort in-progress Git operations and run git reset --hard plus git clean. "
            "Uncommitted, staged, and untracked local changes in the root repository and submodules can be discarded. "
            "You are responsible for that data loss. Commit or stash anything you still need.\n\nContinue cleanup?"
        ),
        LANGUAGE_ZH: (
            "清理可能递归中止进行中的 Git 操作，并执行 git reset --hard 和 git clean，"
            "丢弃根仓库和子模块中未提交、已暂存和未跟踪的本地修改。"
            "由此造成的数据丢失由你自行负责。请先 commit 或 stash 仍需保留的内容。\n\n是否继续清理？"
        ),
    },
    "message_clone_running_body": {
        LANGUAGE_EN: "A command is already running. Wait for it to finish before cloning.",
        LANGUAGE_ZH: "已有命令正在运行，请等待结束后再 clone。",
    },
    "message_invalid_report_path_title": {
        LANGUAGE_EN: "Invalid report path",
        LANGUAGE_ZH: "报告路径无效",
    },
    "message_invalid_report_path_body": {
        LANGUAGE_EN: "Report file must stay inside the repository.",
        LANGUAGE_ZH: "报告文件必须位于仓库内部。",
    },
    "log_starting_action": {
        LANGUAGE_EN: "Starting {action}: {command}\n",
        LANGUAGE_ZH: "开始{action}：{command}\n",
    },
    "log_clone_completed": {
        LANGUAGE_EN: "\nClone completed. Repository is now set to: {path}\n",
        LANGUAGE_ZH: "\nClone 已完成，当前仓库已设置为：{path}\n",
    },
    "log_cancelling": {LANGUAGE_EN: "\nCancelling command...\n", LANGUAGE_ZH: "\n正在取消命令...\n"},
    "log_no_confirmation": {
        LANGUAGE_EN: "\nNo running command is waiting for confirmation.\n",
        LANGUAGE_ZH: "\n没有正在等待确认的命令。\n",
    },
    "log_send_confirmation_failed": {
        LANGUAGE_EN: "\nCould not send confirmation: {error}\n",
        LANGUAGE_ZH: "\n无法发送确认：{error}\n",
    },
    "message_risk_confirmation_title": {
        LANGUAGE_EN: "Risk Confirmation",
        LANGUAGE_ZH: "\u98ce\u9669\u786e\u8ba4",
    },
    "message_risk_confirmation_body": {
        LANGUAGE_EN: (
            "{message}\n\n"
            "Reason: {reason}\n\n"
            "{details}\n\n"
            "Continuing may overwrite, hide, or mix local changes. Continue?"
        ),
        LANGUAGE_ZH: (
            "{message}\n\n"
            "\u539f\u56e0\uff1a{reason}\n\n"
            "{details}\n\n"
            "\u7ee7\u7eed\u64cd\u4f5c\u53ef\u80fd\u8986\u76d6\u3001\u9690\u85cf\u6216\u6df7\u5165\u672c\u5730\u4fee\u6539\u3002\u662f\u5426\u7ee7\u7eed\uff1f"
        ),
    },
    "log_risk_confirmed": {
        LANGUAGE_EN: "\nRisk confirmed by user; continuing.\n",
        LANGUAGE_ZH: "\n\u7528\u6237\u5df2\u786e\u8ba4\u98ce\u9669\uff0c\u7ee7\u7eed\u6267\u884c\u3002\n",
    },
    "log_risk_rejected": {
        LANGUAGE_EN: "\nRisk rejected by user; stopping this operation.\n",
        LANGUAGE_ZH: "\n\u7528\u6237\u62d2\u7edd\u98ce\u9669\uff0c\u505c\u6b62\u672c\u6b21\u64cd\u4f5c\u3002\n",
    },
    "log_finished": {
        LANGUAGE_EN: "\nCommand finished with exit code {exit_code}.\n",
        LANGUAGE_ZH: "\n命令已结束，退出码 {exit_code}。\n",
    },
    "log_report_missing": {
        LANGUAGE_EN: "\nReport does not exist: {path}\n",
        LANGUAGE_ZH: "\n报告文件不存在：{path}\n",
    },
    "friendly_command": {LANGUAGE_EN: "Command: {command}\n", LANGUAGE_ZH: "命令：{command}\n"},
    "friendly_git_repository": {
        LANGUAGE_EN: "Git repository: {path}\n",
        LANGUAGE_ZH: "Git 仓库：{path}\n",
    },
    "friendly_exit": {
        LANGUAGE_EN: "Git command finished with exit code {exit_code}.\n",
        LANGUAGE_ZH: "Git 命令已结束，退出码 {exit_code}。\n",
    },
    "friendly_clone_target": {LANGUAGE_EN: "Clone target: {path}\n", LANGUAGE_ZH: "Clone 目标：{path}\n"},
    "friendly_conflict_closed": {
        LANGUAGE_EN: "Conflict tool closed. Choose Continue, Reopen, or Stop to proceed.\n",
        LANGUAGE_ZH: "冲突工具已关闭。请选择继续、重新打开或停止。\n",
    },
    "friendly_conflict_timeout": {
        LANGUAGE_EN: "Conflict tool timed out. The merge will stop and roll back when required.\n",
        LANGUAGE_ZH: "冲突工具已超时。合并会停止，并在需要时回滚。\n",
    },
    "friendly_merge_success": {
        LANGUAGE_EN: "Merge completed successfully.\n",
        LANGUAGE_ZH: "合并已成功完成。\n",
    },
    "friendly_publish_success": {
        LANGUAGE_EN: "Publish completed successfully.\n",
        LANGUAGE_ZH: "发布已成功完成。\n",
    },
    "friendly_publish_check_success": {
        LANGUAGE_EN: "Publish check completed successfully.\n",
        LANGUAGE_ZH: "发布检查已成功完成。\n",
    },
    "friendly_error": {LANGUAGE_EN: "Error: {message}\n", LANGUAGE_ZH: "错误：{message}\n"},
    "backup_warning_title": {
        LANGUAGE_EN: "Backup Reminder",
        LANGUAGE_ZH: "备份提醒",
    },
    "backup_warning_message": {
        LANGUAGE_EN: (
            "Before using this merge tool, please ensure:\n\n"
            "1. All local changes have been pushed to the remote repository.\n"
            "2. A manual backup of the affected folders has been created.\n\n"
            "Code loss caused by not backing up to a remote location\n"
            "is unrecoverable. Please back up before proceeding.\n\n"
            "Click OK to confirm you have backed up and wish to continue."
        ),
        LANGUAGE_ZH: (
            "在使用本合并工具之前，请确保：\n\n"
            "1. 所有本地修改已推送至远端仓库。\n"
            "2. 已手动备份相关修改的文件夹。\n\n"
            "因远端没有备份而造成的代码丢失\n"
            "无法找回，请在使用前做好备份。\n\n"
            "点击确定以确认您已备份并继续。"
        ),
    },
}


def localized_text(key: str, language: str = LANGUAGE_EN, **kwargs: Any) -> str:
    translations = UI_TEXT.get(key)
    if translations is None:
        return key.format(**kwargs)
    template = translations.get(language) or translations[LANGUAGE_EN]
    return template.format(**kwargs)


def format_friendly_output_line(line: str, language: str = LANGUAGE_EN) -> str:
    stripped = line.strip()
    newline = "\n" if line.endswith("\n") else ""
    if not stripped:
        return line

    lower = stripped.lower()
    if stripped.startswith("$ "):
        return localized_text("friendly_command", language, command=stripped[2:]).rstrip("\n") + newline
    if stripped.startswith("[RUN] "):
        return localized_text("friendly_git_repository", language, path=stripped[6:]).rstrip("\n") + newline
    if stripped in {"[STDOUT]", "[STDERR]"}:
        return ""
    if stripped.startswith("[EXIT]"):
        exit_code = stripped.removeprefix("[EXIT]").strip()
        return localized_text("friendly_exit", language, exit_code=exit_code).rstrip("\n") + newline
    if stripped.startswith("Clone target resolved to:"):
        return localized_text(
            "friendly_clone_target",
            language,
            path=stripped.partition(":")[2].strip(),
        ).rstrip("\n") + newline
    if stripped.startswith("Submodule merge required:"):
        return f"{stripped}{newline}"
    if CONFLICT_TOOL_CONFIRMATION_MESSAGE in stripped:
        return localized_text("friendly_conflict_closed", language)
    if stripped.startswith(RISK_CONFIRMATION_EVENT_PREFIX):
        return ""
    if "conflict tool timed out after" in lower:
        return localized_text("friendly_conflict_timeout", language)
    if stripped.startswith("Merge stopped on branch"):
        return f"{stripped}{newline}"
    if stripped.startswith("Skipping branch"):
        return f"{stripped}{newline}"
    if stripped.startswith("Repository rolled back"):
        return f"{stripped}{newline}"
    if stripped.startswith("Recursive merge completed successfully."):
        return localized_text("friendly_merge_success", language)
    if stripped.startswith("Recursive batch merge completed successfully"):
        return f"{stripped}{newline}"
    if stripped.startswith("Recursive batch merge completed with skipped branches"):
        return f"{stripped}{newline}"
    if stripped.startswith("Publish completed successfully."):
        return localized_text("friendly_publish_success", language)
    if stripped.startswith("Publish check completed successfully."):
        return localized_text("friendly_publish_check_success", language)
    if lower.startswith(("fatal:", "error:", "failed", "merge preflight found", "cannot ", "vs code command")):
        return localized_text("friendly_error", language, message=stripped).rstrip("\n") + newline
    return line


def format_friendly_output(content: str, language: str = LANGUAGE_EN) -> str:
    return "".join(format_friendly_output_line(line, language) for line in content.splitlines(keepends=True))


@dataclass(frozen=True)
class RiskConfirmationEvent:
    reason: str
    message: str
    details: dict[str, Any]


def parse_risk_confirmation_event(line: str) -> RiskConfirmationEvent | None:
    stripped = line.strip()
    if not stripped.startswith(RISK_CONFIRMATION_EVENT_PREFIX):
        return None

    raw_payload = stripped.removeprefix(RISK_CONFIRMATION_EVENT_PREFIX).strip()
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None

    if payload.get("protocol") != RISK_CONFIRMATION_PROTOCOL_VERSION:
        return None

    details = payload.get("details")
    return RiskConfirmationEvent(
        reason=str(payload.get("reason") or "UNKNOWN_RISK"),
        message=str(payload.get("message") or "A risky operation requires confirmation."),
        details=details if isinstance(details, dict) else {},
    )


def format_risk_confirmation_details(details: dict[str, Any], *, max_items: int = 30) -> str:
    lines: list[str] = []
    remaining = max_items
    for key, value in details.items():
        if value in (None, "", [], (), {}):
            continue
        label = str(key)
        if isinstance(value, list):
            lines.append(f"{label}:")
            for item in value[:remaining]:
                lines.append(f"  - {item}")
            if len(value) > remaining:
                lines.append("  - ...")
            remaining -= min(len(value), remaining)
        else:
            lines.append(f"{label}: {value}")
        if remaining <= 0:
            lines.append("...")
            break
    return "\n".join(lines) if lines else "No additional details."




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
        cli_exe = Path(exe).with_name(f"{Path(exe).stem}Cli.exe")
        if cli_exe.exists():
            return [str(cli_exe), "--cli", *cli_argv]
        return [exe, "--cli", *cli_argv]
    return [exe, str(source_file or script_path()), "--cli", *cli_argv]


def command_display(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def safe_directory_value(path: str | Path) -> str:
    raw_path = str(path).strip()
    windows_path = PureWindowsPath(raw_path)
    if windows_path.is_absolute():
        return windows_path.as_posix()
    return Path(raw_path).resolve(strict=False).as_posix()


def add_git_config_env(env: dict[str, str], key: str, value: str) -> None:
    index = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    env[f"GIT_CONFIG_KEY_{index}"] = key
    env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(index + 1)


def git_safe_directory_env(repo: str | Path, *, enable_risk_confirmation_protocol: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    add_git_config_env(env, "safe.directory", safe_directory_value(repo))
    if enable_risk_confirmation_protocol:
        env[RISK_CONFIRMATION_PROTOCOL_ENV] = RISK_CONFIRMATION_PROTOCOL_VERSION
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
        **hidden_subprocess_kwargs(),
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
        self.current_action: str | None = None
        self.pending_clone_target: Path | None = None
        self.reader_thread: threading.Thread | None = None
        self.events: queue.Queue[tuple[str, str | int | None]] = queue.Queue()
        self.raw_log_chunks: list[str] = []
        self.friendly_log_chunks: list[str] = []
        self._localized_widgets: list[tuple[Any, str, str]] = []
        self._localized_tabs: list[tuple[Any, str]] = []
        self._scroll_widget_to_canvas: dict[Any, Any] = {}
        self._left_scroll_canvases: list[Any] = []

        self.language_var = tk.StringVar(value=LANGUAGE_EN)
        root.title(localized_text("window_title", self.language_var.get()))
        apply_window_icon(root)
        root.geometry("1160x760")
        root.minsize(980, 680)

        self.repo_var = tk.StringVar()
        self.repo_source_var = tk.StringVar(value=REPOSITORY_SOURCE_EXISTING)
        self.branch_mode_var = tk.StringVar(value="reset")
        self.base_remote_branch_var = tk.StringVar(value=ROOT_BASE_REMOTE_BRANCH)
        self.work_branch_var = tk.StringVar(value=WORK_BRANCH_DEFAULT)
        self.merge_branch_var = tk.StringVar(value="origin/feature")
        self.conflict_tool_command_var = tk.StringVar(value=DEFAULT_CONFLICT_TOOL_COMMAND)
        self.conflict_tool_timeout_var = tk.StringVar(value=str(DEFAULT_CONFLICT_TOOL_TIMEOUT_SECONDS))
        self.clone_url_var = tk.StringVar()
        self.clone_destination_var = tk.StringVar()
        self.clone_directory_var = tk.StringVar()
        self.clone_branch_var = tk.StringVar()
        self.clone_target_var = tk.StringVar()
        self.report_file_var = tk.StringVar(value=REPORT_FILE_NAME)
        self.rebuild_work_branch_var = tk.BooleanVar(value=True)
        self.skip_submodule_update_var = tk.BooleanVar(value=False)
        self.cleanup_base_remote_branch_var = tk.StringVar(value=ROOT_BASE_REMOTE_BRANCH)
        self.cleanup_work_branch_var = tk.StringVar(value=WORK_BRANCH_DEFAULT)
        self.publish_remote_var = tk.StringVar(value="origin")
        self.publish_target_var = tk.StringVar(value=DEFAULT_PUBLISH_TARGET_BRANCH)
        self.publish_check_only_var = tk.BooleanVar(value=False)
        self.publish_sync_local_branches_var = tk.BooleanVar(value=True)
        self.publish_allow_branch_suffix_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar()
        self._status_key = "status_idle"
        self._status_kwargs: dict[str, Any] = {}
        self.advanced_options_var = tk.BooleanVar(value=False)
        self.command_preview_var = tk.BooleanVar(value=False)
        self.output_mode_var = tk.StringVar(value=OUTPUT_MODE_OPTIMIZED)
        self._continue_branch_placeholders_visible = False
        self._saved_base_remote_branch = ROOT_BASE_REMOTE_BRANCH
        self._saved_work_branch = WORK_BRANCH_DEFAULT
        self._publish_params: dict[str, Any] = {}
        self.repo_presets = discover_repositories()

        if self.repo_presets:
            preferred = next(
                (path for label, path in self.repo_presets if "manual_local_execution" in label),
                self.repo_presets[0][1],
            )
            self.repo_var.set(preferred)
        else:
            self.repo_var.set(str(project_root()))

        for variable in (self.clone_destination_var, self.clone_directory_var):
            variable.trace_add("write", lambda *_args: self._refresh_clone_target_preview())

        self._build_ui()
        self._set_status("status_idle")
        self._apply_language()
        self._update_enabled_state()
        self._preview("merge")
        self.root.after(500, self._show_backup_warning)
        self.root.after(100, self._drain_events)

    def _t(self, key: str, **kwargs: Any) -> str:
        return localized_text(key, self.language_var.get(), **kwargs)

    def _localize(self, widget: Any, key: str, option: str = "text") -> Any:
        self._localized_widgets.append((widget, option, key))
        widget.configure(**{option: self._t(key)})
        return widget

    def _track_tab(self, tab: Any, key: str) -> None:
        self._localized_tabs.append((tab, key))
        self.tabs.tab(tab, text=self._t(key))

    def _apply_language(self) -> None:
        self.root.title(self._t("window_title"))
        for widget, option, key in self._localized_widgets:
            widget.configure(**{option: self._t(key)})
        for tab, key in self._localized_tabs:
            self.tabs.tab(tab, text=self._t(key))
        self._sync_repository_source_visibility()
        self._sync_advanced_options_visibility()
        self._sync_command_preview_visibility()
        self._set_status(self._status_key, **self._status_kwargs)
        self.root.after_idle(self._refresh_left_scroll_regions)

    def _set_status(self, key: str, **kwargs: Any) -> None:
        self._status_key = key
        self._status_kwargs = dict(kwargs)
        display_kwargs = dict(kwargs)
        if key == "status_running_action" and "action" in display_kwargs:
            display_kwargs["action"] = self._action_label(str(display_kwargs["action"]))
        self.status_var.set(self._t(key, **display_kwargs))

    def _action_label(self, action: str) -> str:
        if action == "merge":
            return self._t("action_merge")
        if action == "status":
            return self._t("action_status")
        if action == "resume":
            return self._t("action_resume")
        if action == "abort":
            return self._t("action_abort")
        if action == "cleanup":
            return self._t("action_cleanup")
        if action == "publish":
            return self._t("action_publish")
        return action

    def _build_scrollable_tab(self) -> tuple[Any, Any]:
        tk = self.tk
        ttk = self.ttk

        outer = ttk.Frame(self.tabs)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scroll.set)

        content = ttk.Frame(canvas, padding=12)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def update_scroll_region(_event: Any = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fit_content_width(event: Any) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        content.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", fit_content_width)

        self._left_scroll_canvases.append(canvas)
        for widget in (outer, canvas, scroll, content):
            self._scroll_widget_to_canvas[widget] = canvas
        return outer, content

    def _scroll_canvas_for_event(self, event: Any) -> Any | None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            canvas = self._scroll_widget_to_canvas.get(widget)
            if canvas is not None:
                return canvas
            widget = getattr(widget, "master", None)
        return None

    def _on_left_mousewheel(self, event: Any) -> str | None:
        canvas = self._scroll_canvas_for_event(event)
        if canvas is None:
            return None

        event_num = getattr(event, "num", None)
        if event_num == 4:
            amount = -1
        elif event_num == 5:
            amount = 1
        else:
            delta = getattr(event, "delta", 0)
            if not delta:
                return None
            amount = -int(delta / abs(delta))
        canvas.yview_scroll(amount, "units")
        return "break"

    def _refresh_left_scroll_regions(self) -> None:
        for canvas in self._left_scroll_canvases:
            canvas.configure(scrollregion=canvas.bbox("all"))

    def _sync_repository_source_visibility(self) -> None:
        if not hasattr(self, "existing_repo_frame") or not hasattr(self, "clone_repo_frame"):
            return
        if self.repo_source_var.get() == REPOSITORY_SOURCE_CLONE:
            self.existing_repo_frame.grid_remove()
            self.clone_repo_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        else:
            self.clone_repo_frame.grid_remove()
            self.existing_repo_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._refresh_clone_target_preview()

    def _refresh_clone_target_preview(self) -> None:
        destination = self.clone_destination_var.get().strip()
        directory_name = self.clone_directory_var.get().strip()
        if not destination or not directory_name:
            self.clone_target_var.set("")
            return
        try:
            self.clone_target_var.set(str(clone_target_path(destination, directory_name)))
        except GuiInputError:
            self.clone_target_var.set("")

    def _build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = self._localize(ttk.LabelFrame(self.root, padding=(12, 10)), "frame_repository_source")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        source_bar = ttk.Frame(top)
        source_bar.grid(row=0, column=0, sticky="ew")
        source_bar.columnconfigure(2, weight=1)
        self._localize(
            ttk.Radiobutton(
                source_bar,
                value=REPOSITORY_SOURCE_EXISTING,
                variable=self.repo_source_var,
                command=self._sync_repository_source_visibility,
            ),
            "source_existing_repository",
        ).grid(row=0, column=0, sticky="w")
        self._localize(
            ttk.Radiobutton(
                source_bar,
                value=REPOSITORY_SOURCE_CLONE,
                variable=self.repo_source_var,
                command=self._sync_repository_source_visibility,
            ),
            "source_new_clone_repository",
        ).grid(row=0, column=1, sticky="w", padx=(18, 0))

        language = ttk.Frame(top)
        language.grid(row=0, column=1, sticky="e", padx=(16, 0))
        self._localize(ttk.Label(language), "label_language").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            language,
            text=self._t("language_english"),
            value=LANGUAGE_EN,
            variable=self.language_var,
            command=self._apply_language,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Radiobutton(
            language,
            text=self._t("language_chinese"),
            value=LANGUAGE_ZH,
            variable=self.language_var,
            command=self._apply_language,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.existing_repo_frame = ttk.Frame(top)
        self.existing_repo_frame.columnconfigure(1, weight=1)
        self._localize(ttk.Label(self.existing_repo_frame), "label_repository").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.repo_entry = ttk.Entry(self.existing_repo_frame, textvariable=self.repo_var)
        self.repo_entry.grid(row=0, column=1, sticky="ew")
        self._localize(ttk.Button(self.existing_repo_frame, command=self._browse_repo), "button_browse").grid(
            row=0, column=2, padx=(8, 0)
        )

        preset_values = [label for label, _path in self.repo_presets]
        self.preset_var = tk.StringVar(value=preset_values[0] if preset_values else "")
        self.repo_combo = ttk.Combobox(
            self.existing_repo_frame,
            textvariable=self.preset_var,
            values=preset_values,
            state="readonly",
            width=34,
        )
        self.repo_combo.grid(row=0, column=3, padx=(8, 0))
        self.repo_combo.bind("<<ComboboxSelected>>", self._select_repo_preset)
        self._localize(ttk.Button(self.existing_repo_frame, command=self._show_status), "button_status").grid(
            row=0, column=4, padx=(8, 0)
        )
        self._localize(ttk.Button(self.existing_repo_frame, command=self._show_branches), "button_branches").grid(
            row=0, column=5, padx=(8, 0)
        )

        self.clone_repo_frame = ttk.Frame(top)
        self.clone_repo_frame.columnconfigure(1, weight=1)
        self.clone_repo_frame.columnconfigure(3, weight=1)
        self._localize(ttk.Label(self.clone_repo_frame), "label_repository_url").grid(row=0, column=0, sticky="w")
        self.clone_url_entry = ttk.Entry(self.clone_repo_frame, textvariable=self.clone_url_var)
        self.clone_url_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(0, 6))
        self._localize(ttk.Label(self.clone_repo_frame), "label_destination").grid(row=1, column=0, sticky="w")
        self.clone_destination_entry = ttk.Entry(self.clone_repo_frame, textvariable=self.clone_destination_var)
        self.clone_destination_entry.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(0, 6))
        self.clone_destination_button = self._localize(
            ttk.Button(self.clone_repo_frame, command=self._browse_clone_destination),
            "button_browse",
        )
        self.clone_destination_button.grid(row=1, column=2, sticky="w", pady=(0, 6))
        self._localize(ttk.Label(self.clone_repo_frame), "label_directory_name").grid(row=2, column=0, sticky="w")
        self.clone_directory_entry = ttk.Entry(self.clone_repo_frame, textvariable=self.clone_directory_var)
        self.clone_directory_entry.grid(row=2, column=1, sticky="ew", padx=(8, 8), pady=(0, 6))
        self._localize(ttk.Label(self.clone_repo_frame), "label_clone_branch").grid(row=2, column=2, sticky="w")
        self.clone_branch_entry = ttk.Entry(self.clone_repo_frame, textvariable=self.clone_branch_var)
        self.clone_branch_entry.grid(row=2, column=3, sticky="ew", padx=(8, 0), pady=(0, 6))
        self._localize(ttk.Label(self.clone_repo_frame, foreground="#6b7280"), "hint_clone_branch").grid(
            row=3, column=3, sticky="w", padx=(8, 0), pady=(0, 6)
        )
        self._localize(ttk.Label(self.clone_repo_frame), "label_clone_target").grid(row=3, column=0, sticky="w")
        ttk.Label(self.clone_repo_frame, textvariable=self.clone_target_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(0, 6)
        )
        self.clone_button = self._localize(ttk.Button(self.clone_repo_frame, command=self._start_clone), "button_clone")
        self.clone_button.grid(row=4, column=3, sticky="e")
        self._sync_repository_source_visibility()

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
        self.merge_tab_container, self.merge_tab = self._build_scrollable_tab()
        self.cleanup_tab_container, self.cleanup_tab = self._build_scrollable_tab()
        self.publish_tab_container, self.publish_tab = self._build_scrollable_tab()
        self.tabs.add(self.merge_tab_container, text=self._t("tab_merge"))
        self.tabs.add(self.cleanup_tab_container, text=self._t("tab_cleanup"))
        self.tabs.add(self.publish_tab_container, text=self._t("tab_publish"))
        self._track_tab(self.merge_tab_container, "tab_merge")
        self._track_tab(self.cleanup_tab_container, "tab_cleanup")
        self._track_tab(self.publish_tab_container, "tab_publish")
        self.tabs.bind("<<NotebookTabChanged>>", lambda _event: self._refresh_command_preview_if_visible())
        self.root.bind_all("<MouseWheel>", self._on_left_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_left_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_left_mousewheel, add="+")

        self._build_merge_tab()
        self._build_cleanup_tab()
        self._build_publish_tab()
        self._build_output(right)

        self._apply_saved_config()

    def _build_merge_tab(self) -> None:
        ttk = self.ttk

        for column in range(2):
            self.merge_tab.columnconfigure(column, weight=1)

        self._localize(ttk.Label(self.merge_tab), "label_merge_branch").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self.merge_branch_entry = ttk.Entry(self.merge_tab, textvariable=self.merge_branch_var)
        self.merge_branch_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10))

        mode_frame = self._localize(ttk.LabelFrame(self.merge_tab, padding=8), "frame_root_branch_mode")
        mode_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self._localize(ttk.Radiobutton(
            mode_frame,
            value="reset",
            variable=self.branch_mode_var,
            command=self._update_enabled_state,
        ), "mode_reset_work_branch").grid(row=0, column=0, sticky="w")
        self._localize(ttk.Radiobutton(
            mode_frame,
            value="continue",
            variable=self.branch_mode_var,
            command=self._update_enabled_state,
        ), "mode_use_current_branch").grid(row=0, column=1, sticky="w", padx=(18, 0))

        self.base_entry = self._entry_row(self.merge_tab, 3, 0, "label_base_remote_branch", self.base_remote_branch_var)
        self.work_entry = self._entry_row(self.merge_tab, 3, 1, "label_work_branch", self.work_branch_var)
        self._localize(
            ttk.Label(self.merge_tab, foreground="#6b7280", wraplength=640),
            "hint_clean_worktree_required",
        ).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        self.advanced_toggle_button = ttk.Button(
            self.merge_tab,
            command=self._toggle_advanced_options,
        )
        self.advanced_toggle_button.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.advanced_frame = self._localize(ttk.LabelFrame(self.merge_tab, padding=8), "frame_advanced_options")
        self.advanced_frame.columnconfigure(0, weight=1)
        self.advanced_frame.columnconfigure(1, weight=1)

        self.tool_entry = self._entry_row(
            self.advanced_frame,
            0,
            0,
            "label_conflict_tool_command",
            self.conflict_tool_command_var,
            columnspan=2,
        )
        self.timeout_entry = self._entry_row(
            self.advanced_frame,
            2,
            0,
            "label_tool_timeout_seconds",
            self.conflict_tool_timeout_var,
            columnspan=1,
        )

        self._localize(ttk.Label(self.advanced_frame), "label_ignored_root_untracked_paths").grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="w",
        )
        self.ignore_paths_text = self.tk.Text(self.advanced_frame, height=4, wrap="word")
        self.ignore_paths_text.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(4, 10))

        buttons = ttk.Frame(self.merge_tab)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e")
        self._localize(ttk.Button(buttons, command=lambda: self._start("status")), "button_merge_status").grid(
            row=0, column=0, padx=(0, 8)
        )
        self._localize(ttk.Button(buttons, command=lambda: self._start("resume")), "button_resume_merge").grid(
            row=0, column=1, padx=(0, 8)
        )
        self._localize(ttk.Button(buttons, command=lambda: self._start("abort")), "button_abort_merge").grid(
            row=0, column=2, padx=(0, 8)
        )
        self._localize(ttk.Button(buttons, command=lambda: self._start("merge")), "button_start_merge").grid(
            row=0, column=3
        )

    def _build_cleanup_tab(self) -> None:
        ttk = self.ttk
        for column in range(2):
            self.cleanup_tab.columnconfigure(column, weight=1)

        self._localize(
            ttk.Label(self.cleanup_tab, foreground="#6b7280", wraplength=640),
            "hint_cleanup_responsibility",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        options = self._localize(ttk.LabelFrame(self.cleanup_tab, padding=8), "frame_cleanup_options")
        options.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self._localize(ttk.Checkbutton(
            options,
            variable=self.rebuild_work_branch_var,
            command=self._update_enabled_state,
        ), "option_rebuild_work_branch").grid(row=0, column=0, sticky="w")
        self._localize(ttk.Checkbutton(options, variable=self.skip_submodule_update_var), "option_skip_submodule_update").grid(
            row=0, column=1, sticky="w", padx=(18, 0)
        )

        self.cleanup_base_entry = self._entry_row(
            self.cleanup_tab,
            2,
            0,
            "label_base_remote_branch",
            self.cleanup_base_remote_branch_var,
        )
        self.cleanup_work_entry = self._entry_row(
            self.cleanup_tab,
            2,
            1,
            "label_work_branch",
            self.cleanup_work_branch_var,
        )
        self.report_entry = self._entry_row(
            self.cleanup_tab,
            4,
            0,
            "label_report_file",
            self.report_file_var,
            columnspan=2,
        )

        buttons = ttk.Frame(self.cleanup_tab)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e")
        self._localize(ttk.Button(buttons, command=lambda: self._start("cleanup")), "button_start_cleanup").grid(
            row=0, column=0
        )

    def _build_publish_tab(self) -> None:
        ttk = self.ttk
        for column in range(2):
            self.publish_tab.columnconfigure(column, weight=1)

        self._localize(
            ttk.Label(self.publish_tab, foreground="#6b7280", wraplength=640),
            "hint_publish",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        self._entry_row(
            self.publish_tab,
            1,
            0,
            "label_publish_remote",
            self.publish_remote_var,
        )
        self._entry_row(
            self.publish_tab,
            1,
            1,
            "label_publish_target",
            self.publish_target_var,
        )

        options = self._localize(ttk.LabelFrame(self.publish_tab, padding=8), "frame_publish_options")
        options.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        options.columnconfigure(0, weight=1)
        self._localize(ttk.Checkbutton(
            options,
            variable=self.publish_check_only_var,
        ), "option_publish_check_only").grid(row=0, column=0, sticky="w")
        self._localize(ttk.Checkbutton(
            options,
            variable=self.publish_sync_local_branches_var,
        ), "option_publish_sync_local_branches").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._localize(ttk.Checkbutton(
            options,
            variable=self.publish_allow_branch_suffix_var,
        ), "option_publish_allow_branch_suffix").grid(row=2, column=0, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(self.publish_tab)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e")
        self._localize(ttk.Button(buttons, command=lambda: self._start("publish")), "button_start_publish").grid(
            row=0, column=0
        )

    def _build_output(self, parent: Any) -> None:
        ttk = self.ttk
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        status = ttk.Frame(parent)
        status.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.cancel_button = self._localize(ttk.Button(status, command=self._cancel, state="disabled"), "button_cancel")
        self.cancel_button.grid(row=0, column=1, padx=(8, 0))
        self.confirm_continue_button = self._localize(ttk.Button(
            status,
            command=lambda: self._send_process_input("\n"),
            state="disabled",
        ), "button_continue")
        self.confirm_continue_button.grid(row=0, column=2, padx=(8, 0))
        self.confirm_reopen_button = self._localize(ttk.Button(
            status,
            command=lambda: self._send_process_input("r\n"),
            state="disabled",
        ), "button_reopen")
        self.confirm_reopen_button.grid(row=0, column=3, padx=(8, 0))
        self.confirm_stop_button = self._localize(ttk.Button(
            status,
            command=lambda: self._send_process_input("s\n"),
            state="disabled",
        ), "button_stop")
        self.confirm_stop_button.grid(row=0, column=4, padx=(8, 0))
        self._localize(ttk.Button(status, command=self._clear_log), "button_clear_log").grid(
            row=0, column=5, padx=(8, 0)
        )

        mode = ttk.Frame(parent)
        mode.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self._localize(ttk.Label(mode), "label_console_output").grid(row=0, column=0, sticky="w")
        self._localize(ttk.Radiobutton(
            mode,
            value=OUTPUT_MODE_OPTIMIZED,
            variable=self.output_mode_var,
            command=self._render_log,
        ), "output_optimized").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self._localize(ttk.Radiobutton(
            mode,
            value=OUTPUT_MODE_RAW,
            variable=self.output_mode_var,
            command=self._render_log,
        ), "output_raw").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.command_preview_toggle_button = ttk.Button(mode, command=self._toggle_command_preview)
        self.command_preview_toggle_button.grid(row=0, column=3, sticky="e", padx=(16, 0))

        self.command_preview_frame = ttk.Frame(parent)
        self.command_preview_frame.columnconfigure(0, weight=1)
        self._localize(ttk.Label(self.command_preview_frame), "label_command_preview").grid(row=0, column=0, sticky="w")
        self._localize(
            ttk.Button(self.command_preview_frame, command=self._preview_selected_action),
            "button_refresh_command_preview",
        ).grid(row=0, column=1, sticky="e")
        self.command_text = self.tk.Text(self.command_preview_frame, height=5, wrap="word")
        self.command_text.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10))
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
        self._localize(ttk.Button(inspect, command=self._show_report), "button_show_report").grid(
            row=0, column=0, sticky="w"
        )
        self._sync_advanced_options_visibility()
        self._sync_command_preview_visibility()

    def _entry_row(
        self,
        parent: Any,
        row: int,
        column: int,
        label_key: str,
        variable: Any,
        *,
        columnspan: int = 1,
        pady: tuple[int, int] = (4, 10),
    ) -> Any:
        ttk = self.ttk
        self._localize(ttk.Label(parent), label_key).grid(row=row, column=column, columnspan=columnspan, sticky="w")
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row + 1, column=column, columnspan=columnspan, sticky="ew", padx=(0, 6), pady=pady)
        return entry

    def _labeled_entry(self, parent: Any, row: int, column: int, label_key: str, variable: Any) -> Any:
        ttk = self.ttk
        self._localize(ttk.Label(parent), label_key).grid(row=row, column=column, sticky="w")
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=column + 1, sticky="ew", padx=(8, 8))
        return entry

    def _toggle_advanced_options(self) -> None:
        self.advanced_options_var.set(not self.advanced_options_var.get())
        self._sync_advanced_options_visibility()

    def _sync_advanced_options_visibility(self) -> None:
        expanded = bool(self.advanced_options_var.get())
        if hasattr(self, "advanced_frame"):
            if expanded:
                self.advanced_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(0, 10))
            else:
                self.advanced_frame.grid_remove()
        if hasattr(self, "advanced_toggle_button"):
            self.advanced_toggle_button.configure(
                text=self._t("button_hide_advanced" if expanded else "button_show_advanced")
            )
        self.root.after_idle(self._refresh_left_scroll_regions)

    def _toggle_command_preview(self) -> None:
        self.command_preview_var.set(not self.command_preview_var.get())
        self._sync_command_preview_visibility()
        if self.command_preview_var.get():
            self._preview_selected_action()

    def _sync_command_preview_visibility(self) -> None:
        expanded = bool(self.command_preview_var.get())
        if hasattr(self, "command_preview_frame"):
            if expanded:
                self.command_preview_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
            else:
                self.command_preview_frame.grid_remove()
        if hasattr(self, "command_preview_toggle_button"):
            self.command_preview_toggle_button.configure(
                text=self._t("button_hide_command_preview" if expanded else "button_show_command_preview")
            )

    def _selected_action(self) -> str:
        if not hasattr(self, "tabs"):
            return "merge"
        selected = str(self.tabs.select())
        if hasattr(self, "cleanup_tab_container") and selected == str(self.cleanup_tab_container):
            return "cleanup"
        if hasattr(self, "publish_tab_container") and selected == str(self.publish_tab_container):
            return "publish"
        return "merge"

    def _preview_selected_action(self) -> CommandSpec | None:
        return self._preview(self._selected_action())

    def _refresh_command_preview_if_visible(self) -> None:
        if bool(self.command_preview_var.get()):
            self._preview_selected_action()

    def _sync_branch_mode_fields(self) -> None:
        continue_mode = self.branch_mode_var.get() == "continue"
        if continue_mode:
            if not self._continue_branch_placeholders_visible:
                self._saved_base_remote_branch = self.base_remote_branch_var.get()
                self._saved_work_branch = self.work_branch_var.get()
                self._continue_branch_placeholders_visible = True
            self.base_remote_branch_var.set(CONTINUE_BASE_REMOTE_PLACEHOLDER)
            self.work_branch_var.set(CONTINUE_WORK_BRANCH_PLACEHOLDER)
            self.base_entry.configure(state="disabled")
            self.work_entry.configure(state="disabled")
            return

        if self._continue_branch_placeholders_visible:
            self.base_remote_branch_var.set(self._saved_base_remote_branch)
            self.work_branch_var.set(self._saved_work_branch)
            self._continue_branch_placeholders_visible = False
        self.base_entry.configure(state="normal")
        self.work_entry.configure(state="normal")

    def _common_values(self) -> dict[str, Any]:
        return {
            "repo": self.repo_var.get(),
            "report_file": self.report_file_var.get(),
            "verbose": True,
        }

    def _merge_values(self) -> dict[str, Any]:
        values = self._common_values()
        values.update(
            {
                "merge_branch": self.merge_branch_var.get(),
                "use_current_branch": self.branch_mode_var.get() == "continue",
                "base_remote_branch": self.base_remote_branch_var.get(),
                "work_branch": self.work_branch_var.get(),
                "file_conflict_mode": DEFAULT_FILE_CONFLICT_MODE,
                "conflict_tool_command": self.conflict_tool_command_var.get(),
                "conflict_tool_timeout_seconds": self.conflict_tool_timeout_var.get(),
                "clone_enabled": False,
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

    def _publish_values(self) -> dict[str, Any]:
        values = self._common_values()
        values.update(
            {
                "publish_remote": self.publish_remote_var.get(),
                "publish_target": self.publish_target_var.get(),
                "publish_check_only": self.publish_check_only_var.get(),
                "publish_sync_local_branches": self.publish_sync_local_branches_var.get(),
                "publish_allow_branch_suffix": self.publish_allow_branch_suffix_var.get(),
            }
        )
        return values

    def _values_for_action(self, action: str) -> dict[str, Any]:
        if action == "merge":
            return self._merge_values()
        if action in {"status", "resume", "abort"}:
            return self._common_values()
        if action == "cleanup":
            return self._cleanup_values()
        if action == "publish":
            return self._publish_values()
        raise GuiInputError(f"Unsupported action: {action}")

    def _preview_requested(self, action: str) -> CommandSpec | None:
        self.command_preview_var.set(True)
        self._sync_command_preview_visibility()
        return self._preview(action)

    def _preview(self, action: str) -> CommandSpec | None:
        if self.repo_source_var.get() == REPOSITORY_SOURCE_CLONE:
            self._set_command_text(self._t("message_clone_source_not_ready"))
            return None
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
        from manual_merge_split.file_conflicts import prepare_conflict_tool_command

        if self.process is not None and self.process.poll() is None:
            messagebox.showwarning(
                self._t("message_command_running_title"),
                self._t("message_command_running_body"),
            )
            return
        if self.repo_source_var.get() == REPOSITORY_SOURCE_CLONE:
            messagebox.showerror(
                self._t("message_invalid_input_title"),
                self._t("message_clone_source_not_ready"),
            )
            return

        # Validate the configured conflict tool with the same Windows-aware
        # resolution used by the CLI and conflict-resolution workflow.
        if action == "merge":
            tool_command = self.conflict_tool_command_var.get().strip() if hasattr(self, "conflict_tool_command_var") else ""
            prepared_tool = prepare_conflict_tool_command(tool_command)
            if prepared_tool.error:
                messagebox.showwarning(
                    self._t("message_tool_not_found_title"),
                    prepared_tool.error,
                )
                return

        spec = self._preview(action)
        if spec is None:
            messagebox.showerror(self._t("message_invalid_input_title"), self.command_text.get("1.0", "end").strip())
            return
        if action == "cleanup" and not messagebox.askyesno(
            self._t("message_cleanup_risk_title"),
            self._t("message_cleanup_risk_body"),
        ):
            return
        if action == "abort" and not messagebox.askyesno(
            self._t("message_abort_risk_title"),
            self._t("message_abort_risk_body"),
        ):
            return

        # Publish uses three-phase flow: check → dialog → execute
        if action == "publish":
            self._start_publish(spec)
            return

        command = launcher_command(spec.argv)
        self.current_action = action
        self.pending_clone_target = None
        self._append_log(
            f"$ {command_display(command)}\n",
            friendly=self._t("log_starting_action", action=self._action_label(action), command=command_display(command)),
        )
        self._set_status("status_running_action", action=action)
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
            env=git_safe_directory_env(self.repo_var.get(), enable_risk_confirmation_protocol=True),
            **hidden_subprocess_kwargs(),
        )
        self.reader_thread = threading.Thread(target=self._read_process_output, args=(self.process,), daemon=True)
        self.reader_thread.start()

    def _start_clone(self) -> None:
        from tkinter import messagebox

        if self.process is not None and self.process.poll() is None:
            messagebox.showwarning(
                self._t("message_command_running_title"),
                self._t("message_clone_running_body"),
            )
            return
        try:
            spec = build_clone_spec(
                {
                    "clone_url": self.clone_url_var.get(),
                    "clone_destination": self.clone_destination_var.get(),
                    "clone_directory_name": self.clone_directory_var.get(),
                    "clone_branch": self.clone_branch_var.get(),
                }
            )
        except GuiInputError as exc:
            messagebox.showerror(self._t("message_invalid_input_title"), str(exc))
            return

        spec.target_path.parent.mkdir(parents=True, exist_ok=True)
        self.current_action = "clone"
        self.pending_clone_target = spec.target_path
        clone_command_text = f"$ {command_display(spec.command)}\n"
        self._append_log(
            clone_command_text,
            friendly=format_friendly_output(clone_command_text, self.language_var.get()),
        )
        self._set_status("status_cloning")
        self.cancel_button.configure(state="normal")
        self._set_confirmation_buttons("disabled")

        self.process = subprocess.Popen(
            spec.command,
            cwd=str(project_root()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            encoding=locale.getpreferredencoding(False) or "utf-8",
            errors="replace",
            bufsize=1,
            env=git_safe_directory_env(spec.target_path),
            **hidden_subprocess_kwargs(),
        )
        self.reader_thread = threading.Thread(target=self._read_process_output, args=(self.process,), daemon=True)
        self.reader_thread.start()

    def _start_publish(self, spec: CommandSpec) -> None:
        """Three-phase publish flow: check → user dialog → execute."""
        from tkinter import messagebox
        from manual_merge_split.publish import ManualPublishService, PublishResult

        repo_path = Path(self.repo_var.get()).resolve()
        remote = self.publish_remote_var.get().strip() or "origin"
        push_target = self.publish_target_var.get().strip()
        sync_local_branches = bool(self.publish_sync_local_branches_var.get())
        allow_suffix_branch = bool(self.publish_allow_branch_suffix_var.get())
        check_only = bool(self.publish_check_only_var.get())
        language = self.language_var.get()

        # Phase 1: Run publish check in background thread
        self._append_log(self._t("log_publish_check_start"))
        self._set_status("status_publish_checking")

        def run_check() -> None:
            try:
                service = ManualPublishService()
                result = service.run_publish_check(
                    repo_path,
                    remote=remote,
                    push_target=push_target,
                    sync_local_branches=sync_local_branches,
                    allow_suffix_branch=allow_suffix_branch,
                )
                self.events.put(("publish_check_done", result))
            except Exception as exc:
                self.events.put(("publish_check_failed", str(exc)))

        threading.Thread(target=run_check, daemon=True).start()

        # Phase 2 & 3: Handle check result in _drain_events
        # We store the publish parameters for use in the execute phase
        self._publish_params = {
            "remote": remote,
            "push_target": push_target,
            "sync_local_branches": sync_local_branches,
            "allow_suffix_branch": allow_suffix_branch,
            "check_only": check_only,
        }

    def _handle_publish_check_done(self, result: Any) -> None:
        """Handle the result of publish check phase."""
        from manual_merge_split.publish_dialog import PublishConfirmDialog

        # Log the check results
        pushable_count = sum(1 for item in result.items if not item.skipped)
        if pushable_count == 0:
            self._append_log(self._t("log_publish_check_no_items"))
            self._set_status("status_completed")
            return

        self._append_log(self._t("log_publish_check_complete", count=pushable_count))

        # If check-only mode, we're done
        if self._publish_params.get("check_only"):
            self._set_status("status_completed")
            return

        # Phase 2: Show confirmation dialog
        language = self.language_var.get()
        dialog = PublishConfirmDialog(self.root, result, language=language)
        self.root.wait_window(dialog.top)

        if not dialog.confirmed:
            self._append_log(self._t("log_publish_cancelled"))
            self._set_status("status_idle")
            return

        selected_paths = dialog.selected_display_paths()
        if not selected_paths:
            self._append_log(self._t("log_publish_cancelled"))
            self._set_status("status_idle")
            return

        # Phase 3: Execute publish in background thread
        self._append_log(self._t("log_publish_executing", count=len(selected_paths)))
        self._set_status("status_running_action", action="publish")

        params = self._publish_params
        repo_path = Path(self.repo_var.get()).resolve()

        def run_execute() -> None:
            try:
                from manual_merge_split.publish import ManualPublishService
                service = ManualPublishService()
                execute_result = service.run_publish_execute(
                    result,
                    remote=params["remote"],
                    push_target=params["push_target"],
                    sync_local_branches=params["sync_local_branches"],
                    allow_suffix_branch=params["allow_suffix_branch"],
                    selected_display_paths=selected_paths,
                )
                self.events.put(("publish_execute_done", execute_result))
            except Exception as exc:
                self.events.put(("publish_execute_failed", str(exc)))

        threading.Thread(target=run_execute, daemon=True).start()

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
        self._append_log(self._t("log_cancelling"))
        self.process.terminate()

    def _set_confirmation_buttons(self, state: str) -> None:
        self.confirm_continue_button.configure(state=state)
        self.confirm_reopen_button.configure(state=state)
        self.confirm_stop_button.configure(state=state)

    def _send_process_input(self, payload: str) -> None:
        if self.process is None or self.process.poll() is not None or self.process.stdin is None:
            self._append_log(self._t("log_no_confirmation"))
            self._set_confirmation_buttons("disabled")
            return
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._append_log(self._t("log_send_confirmation_failed", error=exc))
            self._set_confirmation_buttons("disabled")
            return
        self._set_confirmation_buttons("disabled")
        self._set_status("status_running")

    def _handle_risk_confirmation_event(self, event: RiskConfirmationEvent) -> None:
        from tkinter import messagebox

        self._set_confirmation_buttons("disabled")
        self._set_status("status_waiting_risk_confirmation")
        details = format_risk_confirmation_details(event.details)
        confirmed = messagebox.askyesno(
            self._t("message_risk_confirmation_title"),
            self._t(
                "message_risk_confirmation_body",
                reason=event.reason,
                message=event.message,
                details=details,
            ),
            parent=self.root,
        )
        if confirmed:
            self._append_log(self._t("log_risk_confirmed"))
            self._send_process_input("y\n")
            return
        self._append_log(self._t("log_risk_rejected"))
        self._send_process_input("n\n")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    line = str(payload)
                    self._append_log(line, friendly=format_friendly_output(line, self.language_var.get()))
                    risk_event = parse_risk_confirmation_event(line)
                    if risk_event is not None:
                        self._handle_risk_confirmation_event(risk_event)
                    elif CONFLICT_TOOL_CONFIRMATION_MESSAGE in line:
                        self._set_confirmation_buttons("normal")
                        self._set_status("status_waiting_conflict")
                    elif "conflict tool timed out after" in line.lower():
                        self._set_confirmation_buttons("disabled")
                        self._set_status("status_conflict_timeout")
                elif kind == "publish_check_done":
                    self._handle_publish_check_done(payload)
                elif kind == "publish_check_failed":
                    from tkinter import messagebox
                    self._append_log(self._t("friendly_error", message=str(payload)))
                    self._set_status("status_publish_check_failed")
                    messagebox.showerror(
                        self._t("message_publish_check_failed_title"),
                        str(payload),
                    )
                elif kind == "publish_execute_done":
                    self._set_status("status_completed")
                    self._append_log(
                        "\nPublish completed.\n",
                        friendly=self._t("friendly_publish_success"),
                    )
                elif kind == "publish_execute_failed":
                    self._set_status("status_failed", exit_code=1)
                    self._append_log(
                        self._t("friendly_error", message=str(payload)),
                    )
                elif kind == "done":
                    exit_code = int(payload) if payload is not None else -1
                    completed_action = self.current_action
                    clone_target = self.pending_clone_target
                    self.current_action = None
                    self.pending_clone_target = None
                    self.cancel_button.configure(state="disabled")
                    self._set_confirmation_buttons("disabled")
                    if completed_action == "clone" and exit_code == 0 and clone_target is not None:
                        self.repo_var.set(str(clone_target))
                        self.repo_source_var.set(REPOSITORY_SOURCE_EXISTING)
                        self._sync_repository_source_visibility()
                        self._set_status("status_clone_completed")
                        self._append_log(
                            f"\nCommand finished with exit code {exit_code}.\n",
                            friendly=self._t("log_clone_completed", path=clone_target),
                        )
                    elif exit_code == 0:
                        self._set_status("status_completed")
                        self._append_log(
                            f"\nCommand finished with exit code {exit_code}.\n",
                            friendly=self._t("log_finished", exit_code=exit_code),
                        )
                    else:
                        self._set_status("status_failed", exit_code=exit_code)
                        self._append_log(
                            f"\nCommand finished with exit code {exit_code}.\n",
                            friendly=self._t("log_finished", exit_code=exit_code),
                        )
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

    def _show_backup_warning(self) -> None:
        """Display a mandatory backup-reminder dialog on startup."""
        from tkinter import messagebox

        messagebox.showinfo(
            self._t("backup_warning_title"),
            self._t("backup_warning_message"),
        )

    def _show_report(self) -> None:
        from tkinter import messagebox

        report_file = Path(self.report_file_var.get() or REPORT_FILE_NAME)
        if report_file.is_absolute() or ".." in report_file.parts:
            messagebox.showerror(
                self._t("message_invalid_report_path_title"),
                self._t("message_invalid_report_path_body"),
            )
            return
        report_path = Path(self.repo_var.get()).resolve(strict=False) / report_file
        if not report_path.exists():
            self._append_log(self._t("log_report_missing", path=report_path))
            return
        content = report_path.read_text(encoding="utf-8", errors="replace")
        self._append_log(f"\n--- {report_path} ---\n{content}\n")

    def _browse_repo(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(initialdir=self.repo_var.get() or str(project_root()))
        if selected:
            self.repo_var.set(selected)

    def _browse_clone_destination(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(initialdir=self.clone_destination_var.get() or str(project_root()))
        if selected:
            self.clone_destination_var.set(selected)

    def _select_repo_preset(self, _event: Any = None) -> None:
        selected_label = self.preset_var.get()
        for label, path in self.repo_presets:
            if label == selected_label:
                self.repo_var.set(path)
                return

    def _apply_saved_config(self) -> None:
        """Pre-fill GUI form fields with values from the last successful run."""
        try:
            from manual_merge_split.config_persistence import load_config
        except Exception:
            return

        config = load_config()
        last_cmd = config.last_command

        # --- Merge tab ---
        mc = config.merge
        if mc.repo and mc.repo != ".":
            self.repo_var.set(mc.repo)
        if mc.base_remote_branch:
            self.base_remote_branch_var.set(mc.base_remote_branch)
        if mc.work_branch:
            self.work_branch_var.set(mc.work_branch)
        if mc.merge_branch:
            self.merge_branch_var.set(mc.merge_branch)
        if mc.use_current_branch:
            self.branch_mode_var.set("continue")
        if mc.conflict_tool_command:
            self.conflict_tool_command_var.set(mc.conflict_tool_command)
        if mc.conflict_tool_timeout_seconds:
            self.conflict_tool_timeout_var.set(str(mc.conflict_tool_timeout_seconds))

        # --- Publish tab ---
        pc = config.publish
        if pc.remote:
            self.publish_remote_var.set(pc.remote)
        if pc.push_target:
            self.publish_target_var.set(pc.push_target)
        self.publish_sync_local_branches_var.set(not pc.no_sync_local_branches)
        self.publish_allow_branch_suffix_var.set(not pc.no_branch_suffix)

        # --- Cleanup tab ---
        cc = config.cleanup
        if cc.repo and cc.repo != ".":
            self.repo_var.set(cc.repo)
        if cc.work_branch:
            self.cleanup_work_branch_var.set(cc.work_branch)
        self.rebuild_work_branch_var.set(cc.rebuild_work_branch)
        self.skip_submodule_update_var.set(cc.skip_submodule_update)

        # Switch to the last-used tab for convenience
        if last_cmd == "publish" and hasattr(self, "tabs"):
            self.tabs.select(self.publish_tab_container)
        elif last_cmd == "cleanup" and hasattr(self, "tabs"):
            self.tabs.select(self.cleanup_tab_container)

        self._update_enabled_state()

    def _update_enabled_state(self) -> None:
        self._sync_branch_mode_fields()

        self.tool_entry.configure(state="normal")
        self.timeout_entry.configure(state="normal")

        rebuild_state = "normal" if self.rebuild_work_branch_var.get() else "disabled"
        self.cleanup_base_entry.configure(state=rebuild_state)
        self.cleanup_work_entry.configure(state=rebuild_state)

    def _set_command_text(self, content: str) -> None:
        self.command_text.configure(state="normal")
        self.command_text.delete("1.0", "end")
        self.command_text.insert("1.0", content)
        self.command_text.configure(state="disabled")

    def _append_log(self, content: str, *, friendly: str | None = None) -> None:
        self.raw_log_chunks.append(content)
        self.friendly_log_chunks.append(content if friendly is None else friendly)
        display = content if self.output_mode_var.get() == OUTPUT_MODE_RAW else (content if friendly is None else friendly)
        if not display:
            return
        self.log_text.insert("end", display)
        self.log_text.see("end")

    def _render_log(self) -> None:
        chunks = self.raw_log_chunks if self.output_mode_var.get() == OUTPUT_MODE_RAW else self.friendly_log_chunks
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "".join(chunks))
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.raw_log_chunks.clear()
        self.friendly_log_chunks.clear()
        self.log_text.delete("1.0", "end")


def _warm_up_tcl_for_tkinter() -> None:
    if os.name != "nt":
        return

    try:
        import ctypes
    except ImportError:
        return

    base_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    prefix_candidates = [
        base_dir,
        Path(sys.executable).parent,
        Path(getattr(sys, "base_prefix", "")),
        Path(getattr(sys, "prefix", "")),
    ]
    tcl_data_candidates = [
        base_dir / "_tcl_data",
        *(prefix / "tcl" / "tcl8.6" for prefix in prefix_candidates),
    ]
    tk_data_candidates = [
        base_dir / "_tk_data",
        *(prefix / "tcl" / "tk8.6" for prefix in prefix_candidates),
    ]
    dll_candidates = [
        base_dir / "tcl86t.dll",
        *(prefix / "DLLs" / "tcl86t.dll" for prefix in prefix_candidates),
    ]

    tcl_data = next((path for path in tcl_data_candidates if (path / "init.tcl").exists()), None)
    tk_data = next((path for path in tk_data_candidates if (path / "tk.tcl").exists()), None)
    tcl_dll = next((path for path in dll_candidates if path.exists()), None)
    if tcl_data is None or tcl_dll is None:
        return

    os.environ.setdefault("TCL_LIBRARY", tcl_data.as_posix())
    if tk_data is not None:
        os.environ.setdefault("TK_LIBRARY", tk_data.as_posix())

    try:
        tcl = ctypes.CDLL(str(tcl_dll))
        tcl.Tcl_FindExecutable.argtypes = [ctypes.c_char_p]
        tcl.Tcl_FindExecutable.restype = None
        tcl.Tcl_CreateInterp.argtypes = []
        tcl.Tcl_CreateInterp.restype = ctypes.c_void_p
        tcl.Tcl_Eval.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        tcl.Tcl_Eval.restype = ctypes.c_int
        tcl.Tcl_Init.argtypes = [ctypes.c_void_p]
        tcl.Tcl_Init.restype = ctypes.c_int

        tcl.Tcl_FindExecutable(Path(sys.executable).as_posix().encode("utf-8"))
        interp = tcl.Tcl_CreateInterp()
        if interp:
            tcl.Tcl_Eval(interp, b"pwd")
            tcl.Tcl_Init(interp)
    except Exception:
        return


def launch_gui(*, smoke: bool = False) -> int:
    _warm_up_tcl_for_tkinter()
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
    if smoke:
        root.update()
        root.destroy()
        return 0
    root.mainloop()
    return 0


def parse_gui_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Desktop GUI for recursive submodule merge.")
    parser.add_argument("--version", action="version", version="recursive_submodule_merge_gui")
    parser.add_argument("--smoke-gui", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args[:1] == ["--cli"]:
        return run_cli(raw_args[1:])
    args = parse_gui_args(raw_args)
    return launch_gui(smoke=args.smoke_gui)


if __name__ == "__main__":
    raise SystemExit(main())
