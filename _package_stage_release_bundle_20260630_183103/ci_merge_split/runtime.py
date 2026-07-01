"""CI report, Git execution, and CLI runtime helpers."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import common
from .models import (
    CiMergeError,
    CommandResult,
    ExitCode,
    GitCommandError,
    MergeStepContext,
    Phase,
    now_iso,
)
from .publish import target_requires_explicit_publish_target


class JsonReport:
    """Mutable JSON report plus append-only human-readable command log."""

    def __init__(
        self,
        args: argparse.Namespace,
        job_id: str,
        output_dir: Path,
        report_path: Path,
        log_path: Path,
        generated_file_inside_repo: bool,
    ) -> None:
        self.started_perf = time.perf_counter()
        self.started_at = now_iso()
        self.output_dir = output_dir
        self.report_path = report_path
        self.log_path = log_path
        self.console_log = bool(args.console_log)
        sources = list(getattr(args, "sources", []) or [])
        if not sources:
            raw_source = getattr(args, "source", None)
            if isinstance(raw_source, list):
                sources = [str(source) for source in raw_source]
            elif raw_source:
                sources = [str(raw_source)]
        batch_enabled = len(sources) > 1
        submodule_fetch_scope = getattr(args, "submodule_fetch_scope", "needed")
        self.data: dict[str, Any] = {
            "schema_version": "1.0",
            "status": "running",
            "state": Phase.INIT,
            "exit_code": None,
            "started_at": self.started_at,
            "ended_at": None,
            "duration_ms": None,
            "merge_attempted": False,
            "publish_attempted": False,
            "input": {
                "source": args.source,
                "sources": sources,
                "target": args.target,
                "repo_path": str(common.resolve_path(args.repo_path or Path.cwd())),
                "remote": args.remote,
                "publish": bool(args.publish),
                "publish_only": bool(getattr(args, "publish_only", False)),
                "allow_partial_publish": bool(getattr(args, "allow_partial_publish", False)),
                "from_report": getattr(args, "from_report", None),
                "work_branch": args.work_branch,
                "timeout": args.timeout,
                "max_depth": args.max_depth,
                "submodule_fetch_scope": submodule_fetch_scope,
                "git_network_retries": getattr(args, "git_network_retries", 2),
                "git_network_retry_delay": getattr(args, "git_network_retry_delay", 3.0),
                "diff_context_lines": args.diff_context_lines,
                "expanded_diff_context_lines": getattr(args, "expanded_diff_context_lines", 20),
                "allow_clean_x": bool(args.allow_clean_x),
                "console_log": bool(args.console_log),
                "allow_create_publish_target": bool(getattr(args, "allow_create_publish_target", False)),
            },
            "output": {
                "output_dir": str(output_dir),
                "report_path": str(report_path),
                "log_path": str(log_path),
                "patch_dir": str(output_dir / "patches"),
                "conflict_dir": str(output_dir / "conflicts"),
                "file_dir": str(output_dir / "files"),
                "generated_file_inside_repo": generated_file_inside_repo,
            },
            "resolved": {
                "source_ref": None,
                "source_sha": None,
                "target_ref": None,
                "target_sha": None,
                "merge_base_sha": None,
                "result_sha": None,
            },
            "batch": {
                "enabled": batch_enabled,
                "source_count": len(sources),
                "initial_target": args.target,
                "original_target_ref": None,
                "original_target_sha": None,
                "final_result_sha": None,
                "failure_step": None,
                "steps": [],
                "manual_publish": None,
                "failure_mode": getattr(args, "batch_failure_mode", "continue-success"),
                "status": None,
                "successful_steps": [],
                "failed_steps": [],
                "errors": [],
            },
            "remote_sync": {
                "attempted": False,
                "success": None,
                "remote": args.remote,
                "recursive_submodules": True,
                "submodule_fetch_scope": submodule_fetch_scope,
                "submodule_fetch_scope_effective": None,
                "submodule_fetches": [],
                "submodule_updates": [],
                "submodule_fetch_fallback_reasons": [],
                "started_at": None,
                "ended_at": None,
            },
            "summary": {
                "merged_repositories": 0,
                "submodules_processed": 0,
                "submodule_recursive_merges": 0,
                "fast_forward_submodules": 0,
                "conflicts": 0,
                "fallbacks": 0,
            },
            "preflight": {"passed": None, "checks": [], "failed_checks": []},
            "preflight_runs": [],
            "repositories": [],
            "conflicts": [],
            "fallbacks": [],
            "snapshots": [],
            "publish": {
                "enabled": bool(args.publish),
                "attempted": False,
                "success": None,
                "remote": args.remote,
                "target": args.publish_target or args.target,
                "mode": args.publish_mode,
                "candidate_ref": args.candidate_ref,
                "allow_create_target": bool(getattr(args, "allow_create_publish_target", False)),
                "target_created": False,
                "target_publish_mode": None,
                "root": {
                    "candidate_ref_pushed": False,
                    "target_pushed": False,
                    "success": None,
                },
                "submodules": {
                    "attempted": False,
                    "success": None,
                    "count": 0,
                    "items": [],
                },
            },
            "publish_only": {
                "enabled": bool(getattr(args, "publish_only", False)),
                "from_report": getattr(args, "from_report", None),
                "allow_partial_publish": bool(getattr(args, "allow_partial_publish", False)),
                "partial_publish": False,
                "source_report_status": None,
                "source_report_path": None,
            },
            "commands": [],
            "errors": [],
            "warnings": [],
            "restore": {
                "attempted": False,
                "success": None,
                "errors": [],
                "results": [],
            },
        }
        if generated_file_inside_repo:
            self.add_warning(
                "GENERATED_FILE_INSIDE_REPO",
                "an explicit output/report/log path is inside the target repository",
            )

    def set_state(self, state: str) -> None:
        self.data["state"] = state
        self.append_log(f"\n[{now_iso()}] STATE {state}\n")
        self.console(f"STATE {state}")

    def append_log(self, text: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            self.data["warnings"].append(
                {
                    "reason": "LOG_WRITE_FAILED",
                    "message": str(exc),
                    "timestamp": now_iso(),
                }
            )

    @staticmethod
    def _json_safe_log_value(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): JsonReport._json_safe_log_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [JsonReport._json_safe_log_value(item) for item in value]
        return value

    def add_logic_event(
        self,
        name: str,
        message: str,
        *,
        repo_path: str | Path | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        repo_text = f" repo={repo_path}" if repo_path is not None else ""
        self.append_log(f"\n[{now_iso()}] LOGIC {name}{repo_text} {message}\n")
        if details:
            safe_details = self._json_safe_log_value(details)
            self.append_log(json.dumps(safe_details, ensure_ascii=False, sort_keys=True) + "\n")
        self.console(f"LOGIC {name}{repo_text} {message}")

    def console(self, message: str) -> None:
        if not self.console_log:
            return
        try:
            print(f"[{now_iso()}] {message}", flush=True)
        except OSError:
            pass

    def progress(self, message: str) -> None:
        try:
            print(f"[{now_iso()}] {message}", flush=True)
        except OSError:
            pass

    @staticmethod
    def console_tail(value: str, *, max_lines: int = 3, max_chars: int = 500) -> str:
        lines = common.split_lines(value)
        if not lines:
            return ""
        text = " | ".join(lines[-max_lines:])
        return text if len(text) <= max_chars else text[: max_chars - 3] + "..."

    def console_command_start(self, cmd: list[str], cwd: str, allow_failure: bool) -> None:
        suffix = " allow_failure=true" if allow_failure else ""
        self.console(f"RUN cwd={cwd} $ {' '.join(cmd)}{suffix}")

    def console_command_end(self, command: CommandResult) -> None:
        if command.exit_code == 0:
            status = "DONE"
        elif command.allow_failure:
            status = "WARN"
        else:
            status = "FAIL"
        timeout = " timed_out=true" if command.timed_out else ""
        retry = f" attempt={command.attempt}/{command.max_attempts}" if command.max_attempts > 1 else ""
        network = f" network={command.network_failure_reason}" if command.network_failure_reason else ""
        self.console(
            f"{status} exit={command.exit_code} duration_ms={command.duration_ms}{timeout}{retry}{network} $ {' '.join(command.cmd)}"
        )
        if command.exit_code != 0:
            stderr_tail = self.console_tail(command.stderr)
            stdout_tail = self.console_tail(command.stdout)
            if stderr_tail:
                self.console(f"{status} stderr: {stderr_tail}")
            elif stdout_tail:
                self.console(f"{status} stdout: {stdout_tail}")

    def add_warning(self, reason: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.data["warnings"].append(
            {
                "reason": reason,
                "message": message,
                "details": details or {},
                "timestamp": now_iso(),
            }
        )
        self.console(f"WARNING {reason}: {message}")

    def add_command(self, command: CommandResult) -> None:
        entry = command.to_dict()
        self.data["commands"].append(entry)
        self.append_log(
            "\n"
            f"[{entry['started_at']}] cwd={entry['cwd']}\n"
            f"$ {' '.join(entry['cmd'])}\n"
            f"exit={entry['exit_code']} duration_ms={entry['duration_ms']} "
            f"allow_failure={entry['allow_failure']} timed_out={entry['timed_out']} "
            f"attempt={entry['attempt']}/{entry['max_attempts']} network_failure_reason={entry['network_failure_reason']}\n"
            f"stdout:\n{entry['stdout']}\n"
            f"stderr:\n{entry['stderr']}\n"
        )
        self.console_command_end(command)

    def add_error(self, error: CiMergeError | dict[str, Any]) -> None:
        if isinstance(error, CiMergeError):
            self.data["errors"].append(error.to_report_error())
            self.console(f"ERROR {error.phase} {error.reason}: {error.message}")
        else:
            self.data["errors"].append(error)
            self.console(
                f"ERROR {error.get('phase', 'UNKNOWN')} {error.get('reason', 'ERROR')}: {error.get('message', '')}"
            )

    def add_conflicts(self, conflicts: Iterable[dict[str, Any]]) -> None:
        items = list(conflicts)
        self.data["conflicts"].extend(items)
        self.data["summary"]["conflicts"] = len(self.data["conflicts"])

    def add_fallback(self, fallback: dict[str, Any]) -> None:
        self.data["fallbacks"].append(fallback)
        self.data["summary"]["fallbacks"] = len(self.data["fallbacks"])

    def add_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.data["snapshots"].append(snapshot)

    def add_repository(self, repository: dict[str, Any]) -> None:
        self.data["repositories"].append(repository)
        self.data["summary"]["merged_repositories"] = len(self.data["repositories"])

    def increment_summary(self, name: str, amount: int = 1) -> None:
        self.data["summary"][name] = int(self.data["summary"].get(name, 0)) + amount

    def set_failure(self, error: CiMergeError) -> None:
        self.data["status"] = "failed"
        self.data["exit_code"] = error.exit_code
        self.data["failure_phase"] = error.phase
        self.data["failure_reason"] = error.reason
        self.data["failure_message"] = error.message
        self.set_state(Phase.FAILED)

    def set_success(self) -> None:
        self.data["status"] = "success"
        self.data["exit_code"] = ExitCode.SUCCESS
        self.set_state(Phase.SUCCEEDED)

    def finalize(self, fallback_exit_code: int | None = None) -> None:
        if self.data["exit_code"] is None:
            self.data["exit_code"] = fallback_exit_code
        ended = time.perf_counter()
        self.data["ended_at"] = now_iso()
        self.data["duration_ms"] = common.milliseconds(self.started_perf, ended)

    def write(self) -> bool:
        try:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            with self.report_path.open("w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            return True
        except OSError as exc:
            summary = {
                "status": "failed",
                "exit_code": self.data.get("exit_code") or ExitCode.SYSTEM_EXCEPTION,
                "failure_reason": "REPORT_WRITE_FAILED",
                "message": str(exc),
                "intended_report_path": str(self.report_path),
                "errors": self.data.get("errors", []),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
            return False


@dataclass
class ExecutionContext:
    args: argparse.Namespace
    report: JsonReport
    root_repo: Path
    job_id: str
    timeout: int
    max_depth: int
    allow_clean_x: bool
    runner: "GitRunner" = field(init=False)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    merge_step: MergeStepContext | None = None

    def __post_init__(self) -> None:
        self.runner = GitRunner(self)

    def set_state(self, state: str) -> None:
        self.report.set_state(state)


class GitRunner:
    """Single entry point for every Git command executed by the CI workflow."""

    def __init__(self, ctx: ExecutionContext) -> None:
        self.ctx = ctx
        self.git = ctx.args.git

    @staticmethod
    def add_command_config(env: dict[str, str], key: str, value: str) -> None:
        try:
            index = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
        except ValueError:
            index = 0
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
        env["GIT_CONFIG_COUNT"] = str(index + 1)

    @staticmethod
    def is_network_sensitive(args: list[str]) -> bool:
        if not args:
            return False
        if args[0] in {"clone", "fetch", "push", "pull", "ls-remote"}:
            return True
        return args[:2] in (
            ["submodule", "update"],
            ["submodule", "sync"],
        )

    @staticmethod
    def classify_network_failure(result: CommandResult) -> str | None:
        if result.timed_out:
            return "GIT_COMMAND_TIMEOUT"
        text = f"{result.stderr}\n{result.stdout}".lower()
        patterns = (
            ("GIT_NETWORK_DNS_FAILED", (r"could not resolve host", r"name or service not known", r"temporary failure in name resolution")),
            ("GIT_NETWORK_CONNECT_FAILED", (r"failed to connect", r"connection timed out", r"connection refused", r"couldn't connect")),
            ("GIT_NETWORK_CONNECTION_RESET", (r"connection reset", r"connection was reset", r"connection aborted", r"connection closed")),
            ("GIT_NETWORK_REMOTE_CLOSED", (r"remote end hung up", r"the remote end hung up", r"early eof", r"unexpected disconnect")),
            ("GIT_NETWORK_TLS_FAILED", (r"ssl.*(error|failed)", r"tls.*(error|failed)", r"gnutls recv error")),
            ("GIT_NETWORK_HTTP_TRANSIENT", (r"http code = 50[0234]", r"the requested url returned error: 50[0234]", r"bad gateway", r"service unavailable", r"gateway timeout")),
            ("GIT_NETWORK_RPC_FAILED", (r"rpc failed", r"curl \d+", r"http/2 stream .* was not closed cleanly")),
            ("GIT_NETWORK_BROKEN_PIPE", (r"broken pipe",)),
        )
        for reason, reason_patterns in patterns:
            if any(re.search(pattern, text) for pattern in reason_patterns):
                return reason
        if "network" in text:
            return "GIT_NETWORK_FAILED"
        return None

    def build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_MERGE_AUTOEDIT"] = "no"
        if self.ctx.args.auto_safe_directory:
            self.add_command_config(env, "safe.directory", "*")
        return env

    def run_once(
        self,
        cwd_path: str,
        cmd: list[str],
        env: dict[str, str],
        *,
        allow_failure: bool,
        timeout: int,
        attempt: int,
        max_attempts: int,
    ) -> CommandResult:
        started_at = now_iso()
        started_perf = time.perf_counter()
        try:
            completed = subprocess.run(
                cmd,
                cwd=cwd_path,
                env=env,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
            )
            ended_perf = time.perf_counter()
            return CommandResult(
                cmd=cmd,
                cwd=cwd_path,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                started_at=started_at,
                ended_at=now_iso(),
                duration_ms=common.milliseconds(started_perf, ended_perf),
                allow_failure=allow_failure,
                attempt=attempt,
                max_attempts=max_attempts,
            )
        except subprocess.TimeoutExpired as exc:
            ended_perf = time.perf_counter()
            return CommandResult(
                cmd=cmd,
                cwd=cwd_path,
                exit_code=124,
                stdout=common.stringify_process_output(exc.stdout),
                stderr=common.stringify_process_output(exc.stderr) + "\ncommand timed out",
                started_at=started_at,
                ended_at=now_iso(),
                duration_ms=common.milliseconds(started_perf, ended_perf),
                allow_failure=allow_failure,
                timed_out=True,
                attempt=attempt,
                max_attempts=max_attempts,
                network_failure_reason="GIT_COMMAND_TIMEOUT",
            )
        except OSError as exc:
            ended_perf = time.perf_counter()
            return CommandResult(
                cmd=cmd,
                cwd=cwd_path,
                exit_code=127 if isinstance(exc, FileNotFoundError) else 126,
                stdout="",
                stderr=str(exc),
                started_at=started_at,
                ended_at=now_iso(),
                duration_ms=common.milliseconds(started_perf, ended_perf),
                allow_failure=allow_failure,
                attempt=attempt,
                max_attempts=max_attempts,
            )

    def retry_delay(self, attempt: int) -> float:
        base = float(getattr(self.ctx.args, "git_network_retry_delay", 3.0))
        return max(0.0, base) * (2 ** max(0, attempt - 1))

    def run(
        self,
        cwd: Path | str,
        args: list[str],
        *,
        allow_failure: bool = False,
        timeout: int | None = None,
    ) -> CommandResult:
        cwd_path = str(common.resolve_path(cwd))
        cmd = [self.git, *args]
        env = self.build_env()
        command_timeout = timeout or self.ctx.timeout
        retries = int(getattr(self.ctx.args, "git_network_retries", 2) or 0)
        max_attempts = 1 + max(0, retries) if self.is_network_sensitive(args) else 1
        result: CommandResult | None = None
        for attempt in range(1, max_attempts + 1):
            self.ctx.report.console_command_start(cmd, cwd_path, allow_failure)
            result = self.run_once(
                cwd_path,
                cmd,
                env,
                allow_failure=allow_failure,
                timeout=command_timeout,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            network_reason = self.classify_network_failure(result)
            result.network_failure_reason = network_reason
            self.ctx.report.add_command(result)
            if result.exit_code == 0:
                break
            if not network_reason or attempt >= max_attempts:
                break
            time.sleep(self.retry_delay(attempt))
        assert result is not None
        if result.exit_code != 0 and not allow_failure:
            raise GitCommandError(self.ctx.report.data.get("state") or Phase.INIT, result)
        return result

    def run_binary(
        self,
        cwd: Path | str,
        args: list[str],
        *,
        allow_failure: bool = False,
        timeout: int | None = None,
    ) -> tuple[CommandResult, bytes]:
        cwd_path = str(common.resolve_path(cwd))
        cmd = [self.git, *args]
        started_at = now_iso()
        started_perf = time.perf_counter()
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_MERGE_AUTOEDIT"] = "no"
        if self.ctx.args.auto_safe_directory:
            self.add_command_config(env, "safe.directory", "*")
        self.ctx.report.console_command_start(cmd, cwd_path, allow_failure)
        stdout_bytes = b""
        try:
            completed = subprocess.run(
                cmd,
                cwd=cwd_path,
                env=env,
                shell=False,
                text=False,
                capture_output=True,
                timeout=timeout or self.ctx.timeout,
            )
            ended_perf = time.perf_counter()
            stdout_bytes = common.bytes_process_output(completed.stdout)
            result = CommandResult(
                cmd=cmd,
                cwd=cwd_path,
                exit_code=completed.returncode,
                stdout=f"<binary stdout: {len(stdout_bytes)} bytes>",
                stderr=common.stringify_process_output(completed.stderr),
                started_at=started_at,
                ended_at=now_iso(),
                duration_ms=common.milliseconds(started_perf, ended_perf),
                allow_failure=allow_failure,
            )
        except subprocess.TimeoutExpired as exc:
            ended_perf = time.perf_counter()
            stdout_bytes = common.bytes_process_output(exc.stdout)
            result = CommandResult(
                cmd=cmd,
                cwd=cwd_path,
                exit_code=124,
                stdout=f"<binary stdout before timeout: {len(stdout_bytes)} bytes>",
                stderr=common.stringify_process_output(exc.stderr) + "\ncommand timed out",
                started_at=started_at,
                ended_at=now_iso(),
                duration_ms=common.milliseconds(started_perf, ended_perf),
                allow_failure=allow_failure,
                timed_out=True,
            )
        except OSError as exc:
            ended_perf = time.perf_counter()
            result = CommandResult(
                cmd=cmd,
                cwd=cwd_path,
                exit_code=127 if isinstance(exc, FileNotFoundError) else 126,
                stdout="",
                stderr=str(exc),
                started_at=started_at,
                ended_at=now_iso(),
                duration_ms=common.milliseconds(started_perf, ended_perf),
                allow_failure=allow_failure,
            )

        self.ctx.report.add_command(result)
        if result.exit_code != 0 and not allow_failure:
            if result.timed_out:
                raise CiMergeError(
                    ExitCode.GIT_COMMAND_FAILED,
                    self.ctx.report.data.get("state") or Phase.INIT,
                    "GIT_COMMAND_TIMEOUT",
                    "git command timed out",
                    details={"command": result.to_dict()},
                )
            raise GitCommandError(self.ctx.report.data.get("state") or Phase.INIT, result)
        return result, stdout_bytes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a CI merge candidate and optionally publish it safely.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--source",
        action="append",
        help="source branch/ref to merge from; repeat to merge multiple branches sequentially",
    )
    parser.add_argument(
        "--multi-source",
        "--multi_source",
        dest="multi_source",
        type=common.parse_bool,
        default=False,
        help="split each --source value by comma for multi-branch merge compatibility",
    )
    parser.add_argument("--target", help="target branch/ref to merge into")
    parser.add_argument("--repo_path", help="main repository path")
    parser.add_argument("--report", default="merge-report.json", help="JSON report path")
    parser.add_argument("--remote", default="origin", help="remote name")
    parser.add_argument("--work_branch", help="local work branch name")
    parser.add_argument("--output_dir", help="directory for generated files")
    parser.add_argument("--log", default="merge.log", help="log file path")
    parser.add_argument("--timeout", type=int, default=1800, help="per-command timeout in seconds")
    parser.add_argument("--max_depth", type=int, default=16, help="maximum recursive submodule depth")
    parser.add_argument(
        "--git-network-retries",
        "--git_network_retries",
        dest="git_network_retries",
        type=int,
        default=2,
        help="retry count for transient network-sensitive Git commands",
    )
    parser.add_argument(
        "--git-network-retry-delay",
        "--git_network_retry_delay",
        dest="git_network_retry_delay",
        type=float,
        default=3.0,
        help="base delay in seconds before retrying network-sensitive Git commands",
    )
    parser.add_argument(
        "--submodule-fetch-scope",
        "--submodule_fetch_scope",
        dest="submodule_fetch_scope",
        choices=("needed", "all"),
        default="needed",
        help="fetch refs only for gitlink-affected submodules, or all initialized submodules",
    )
    parser.add_argument(
        "--diff_context_lines",
        type=int,
        default=3,
        help="number of unchanged context lines to include in exported patch files",
    )
    parser.add_argument(
        "--expanded_diff_context_lines",
        type=int,
        default=20,
        help="number of unchanged context lines to include in expanded patch files for review HTML",
    )
    parser.add_argument("--allow_clean_x", type=common.parse_bool, default=False, help="allow git clean -fdx")
    parser.add_argument("--publish", type=common.parse_bool, default=False, help="publish result to target branch")
    parser.add_argument("--publish_only", type=common.parse_bool, default=False, help="publish a previously generated merge report without merging")
    parser.add_argument(
        "--allow_partial_publish",
        type=common.parse_bool,
        default=False,
        help="allow --publish_only to publish a partial_success batch report",
    )
    parser.add_argument("--from_report", help="merge-report.json path to publish when --publish_only true")
    parser.add_argument("--publish_target", help="remote target branch to update")
    parser.add_argument(
        "--allow_create_publish_target",
        type=common.parse_bool,
        default=False,
        help="allow publishing to create --publish_target when it does not exist on the remote",
    )
    parser.add_argument("--publish_mode", default="force-with-lease", help="publish mode")
    parser.add_argument("--candidate_ref", help="optional remote candidate ref")
    parser.add_argument("--job_id", help="stable job id for output and branch naming")
    parser.add_argument("--git", default="git", help="git executable")
    parser.add_argument(
        "--batch_failure_mode",
        choices=("fail-fast", "continue-success"),
        default="continue-success",
        help="how repeated --source batch merges behave after a step fails",
    )
    parser.add_argument(
        "--console_log",
        type=common.parse_bool,
        default=False,
        help="print concise progress logs to stdout while running",
    )
    parser.add_argument(
        "--auto_safe_directory",
        type=common.parse_bool,
        default=True,
        help="trust explicit merge worktrees for this process to avoid Git safe.directory ownership failures",
    )
    return parser


def resolve_output_paths(args: argparse.Namespace, job_id: str) -> tuple[Path, Path, Path, bool]:
    repo_path = common.resolve_path(args.repo_path or Path.cwd())
    repo_name = repo_path.name or "repo"
    if args.output_dir:
        output_dir = common.resolve_path(args.output_dir)
    else:
        output_dir = common.resolve_path(repo_path.parent / ".ci_merge_outputs" / repo_name / job_id)

    report_candidate = Path(args.report or "merge-report.json")
    report_path = (
        common.resolve_path(report_candidate)
        if report_candidate.is_absolute()
        else common.resolve_path(output_dir / report_candidate)
    )

    log_candidate = Path(args.log or "merge.log")
    log_path = (
        common.resolve_path(log_candidate)
        if log_candidate.is_absolute()
        else common.resolve_path(output_dir / log_candidate)
    )

    generated_inside = (
        common.is_path_inside(output_dir, repo_path)
        or common.is_path_inside(report_path, repo_path)
        or common.is_path_inside(log_path, repo_path)
    )
    return output_dir, report_path, log_path, generated_inside


def validate_args(args: argparse.Namespace) -> None:
    if args.publish_only:
        missing = [name for name in ("from_report", "repo_path") if not getattr(args, name)]
    else:
        missing = [name for name in ("target", "repo_path") if not getattr(args, name)]
        if not getattr(args, "sources", None):
            missing.insert(0, "source")
    if missing:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "MISSING_REQUIRED_ARGUMENT",
            f"missing required argument(s): {', '.join('--' + item for item in missing)}",
            details={"missing": missing},
        )
    if args.publish_only and getattr(args, "sources", None):
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "PUBLISH_ONLY_SOURCE_UNSUPPORTED",
            "--source is not used with --publish_only; use --from_report instead",
            details={"sources": args.sources, "from_report": args.from_report},
        )
    if args.timeout <= 0:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "INVALID_TIMEOUT",
            "--timeout must be greater than 0",
            details={"timeout": args.timeout},
        )
    if args.max_depth < 0:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "INVALID_MAX_DEPTH",
            "--max_depth must be greater than or equal to 0",
            details={"max_depth": args.max_depth},
        )
    raw_git_network_retries = getattr(args, "git_network_retries", 2)
    raw_git_network_retry_delay = getattr(args, "git_network_retry_delay", 3.0)
    git_network_retries = 2 if raw_git_network_retries is None else int(raw_git_network_retries)
    git_network_retry_delay = 3.0 if raw_git_network_retry_delay is None else float(raw_git_network_retry_delay)
    if git_network_retries < 0:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "INVALID_GIT_NETWORK_RETRIES",
            "--git_network_retries must be greater than or equal to 0",
            details={"git_network_retries": git_network_retries},
        )
    if git_network_retry_delay < 0:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "INVALID_GIT_NETWORK_RETRY_DELAY",
            "--git_network_retry_delay must be greater than or equal to 0",
            details={"git_network_retry_delay": git_network_retry_delay},
        )
    if args.diff_context_lines < 0:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "INVALID_DIFF_CONTEXT_LINES",
            "--diff_context_lines must be greater than or equal to 0",
            details={"diff_context_lines": args.diff_context_lines},
        )
    expanded_diff_context_lines = getattr(args, "expanded_diff_context_lines", 20)
    if expanded_diff_context_lines < args.diff_context_lines:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "INVALID_EXPANDED_DIFF_CONTEXT_LINES",
            "--expanded_diff_context_lines must be greater than or equal to --diff_context_lines",
            details={
                "diff_context_lines": args.diff_context_lines,
                "expanded_diff_context_lines": expanded_diff_context_lines,
            },
        )
    if args.publish and args.publish_mode != "force-with-lease":
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "UNSUPPORTED_PUBLISH_MODE",
            "only force-with-lease publishing is supported",
            details={"publish_mode": args.publish_mode},
        )
    if args.publish and not args.publish_target:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "PUBLISH_TARGET_REQUIRED",
            "--publish_target must be set for publishing",
            details={"publish_only": bool(args.publish_only), "target": args.target, "remote": args.remote},
        )
    if args.publish and target_requires_explicit_publish_target(args.publish_target, args.remote):
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "PUBLISH_TARGET_REQUIRED",
            "--publish_target must name a remote branch to publish",
            details={"publish_target": args.publish_target, "remote": args.remote},
        )


def normalize_args(args: argparse.Namespace, job_id: str) -> None:
    raw_sources = getattr(args, "source", None)
    if raw_sources is None:
        sources: list[str] = []
    elif isinstance(raw_sources, list):
        sources = []
        for raw_source in raw_sources:
            source = str(raw_source).strip()
            if not source:
                continue
            if getattr(args, "multi_source", False):
                sources.extend(part.strip() for part in source.split(",") if part.strip())
            else:
                sources.append(source)
    else:
        source = str(raw_sources).strip()
        if getattr(args, "multi_source", False):
            sources = [part.strip() for part in source.split(",") if part.strip()]
        else:
            sources = [source] if source else []
    args.sources = sources
    args.source = sources[0] if sources else None
    if args.publish_target:
        args.publish_target = str(args.publish_target).strip() or None
    if args.publish_only:
        args.publish = True
    args.publish_target_explicit = bool(args.publish_target)
    if not args.work_branch:
        args.work_branch = f"ci-merge/{common.sanitize_ref_component(job_id)}"
    if args.publish and not args.candidate_ref:
        args.candidate_ref = f"refs/merge-candidates/{common.sanitize_ref_component(job_id)}"


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(argv)


def create_execution_context(args: argparse.Namespace, job_id: str | None = None) -> ExecutionContext:
    resolved_job_id = common.sanitize_ref_component(
        job_id or args.job_id or os.environ.get("BUILD_TAG") or common.default_job_id()
    )
    normalize_args(args, resolved_job_id)
    output_dir, report_path, log_path, generated_inside = resolve_output_paths(args, resolved_job_id)
    report = JsonReport(args, resolved_job_id, output_dir, report_path, log_path, generated_inside)
    return ExecutionContext(
        args=args,
        report=report,
        root_repo=common.resolve_path(args.repo_path or Path.cwd()),
        job_id=resolved_job_id,
        timeout=args.timeout,
        max_depth=args.max_depth,
        allow_clean_x=bool(args.allow_clean_x),
    )
