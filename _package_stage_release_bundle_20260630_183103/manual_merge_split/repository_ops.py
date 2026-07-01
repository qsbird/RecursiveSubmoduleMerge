"""Manual repository operation boundary."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from merge_cleanup_core import steps as cleanup_steps

from . import common
from . import execution
from .messages import ManualCommitMessage
from .models import KNOWN_MISSING_REVISION_MARKERS, CleanupResult, MergeAutomationError, RepoSnapshot, UnmergedConflict


REPOSITORY_OP_FUNCTIONS: tuple[str, ...] = ()
RISK_CONFIRMATION_PROTOCOL_ENV = "MERGE_TOOL_RISK_CONFIRMATION_PROTOCOL"
RISK_CONFIRMATION_PROTOCOL_VERSION = "stdio-json-v1"
RISK_CONFIRMATION_EVENT_PREFIX = "MERGE_TOOL_RISK_CONFIRMATION_REQUIRED "


@dataclass(frozen=True)
class ManualRepositoryOps:
    """Facade for Git and repository state operations in manual workflow."""

    manual_module: ModuleType | None = None

    def __post_init__(self) -> None:
        return None

    def run_git(self, repo_path: Path, args: list[str], **kwargs: Any) -> Any:
        runner = getattr(self.manual_module, "run_git", execution.run_git)
        return runner(repo_path, args, **kwargs)

    def run_cleanup_step(self, repo_path: Path, step: cleanup_steps.CleanupStep, **kwargs: Any) -> Any:
        return self.run_git(repo_path, list(step.args), **kwargs)

    def _automation_error_class(self) -> type[Exception]:
        return getattr(self.manual_module, "MergeAutomationError", MergeAutomationError)

    def _automation_error(self, message: str) -> Exception:
        return self._automation_error_class()(message)

    def _format_git_failure(self, result: Any) -> str:
        formatter = getattr(self.manual_module, "format_git_failure", None)
        if callable(formatter):
            return formatter(result)
        return execution.format_git_failure(result)

    def _verbose(self) -> bool:
        return bool(getattr(self.manual_module, "VERBOSE", execution.VERBOSE))

    def _verbose_print(self, message: str) -> None:
        printer = getattr(self.manual_module, "verbose_print", None)
        if callable(printer):
            printer(message)
            return
        execution.verbose_print(message)

    def infer_clone_directory_name(self, clone_url: str) -> str:
        return common.infer_clone_directory_name(clone_url)

    def resolve_clone_target_path(self, clone_url: str, clone_path: Path, *, create_parent: bool = True) -> Path:
        return common.resolve_clone_target_path(clone_url, clone_path, create_parent=create_parent)

    def normalize_status_relative_path(self, path: str) -> str:
        return common.normalize_status_relative_path(path)

    def looks_like_commit_id(self, value: str) -> bool:
        return common.looks_like_commit_id(value)

    def normalize_user_merge_ref(self, ref: str) -> str:
        return common.normalize_user_merge_ref(ref)

    def remote_tracking_ref(self, ref: str) -> str:
        return common.remote_tracking_ref(ref)

    def ensure_git_repo(self, repo_path: Path) -> None:
        self.run_git(repo_path, ["rev-parse", "--show-toplevel"])

    def head_exists(self, repo_path: Path) -> bool:
        result = self.run_git(repo_path, ["rev-parse", "--verify", "HEAD"], check=False)
        return result.returncode == 0

    def branch_exists(self, repo_path: Path, branch: str) -> bool:
        result = self.run_git(repo_path, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False)
        if result.returncode == 0:
            return True
        if result.returncode in (1, 128):
            return False
        raise self._automation_error(self._format_git_failure(result))

    def try_resolve_commit(self, repo_path: Path, commitish: str) -> str | None:
        result = self.run_git(repo_path, ["rev-parse", "--verify", f"{commitish}^{{commit}}"], check=False)
        if result.returncode != 0:
            if self.is_missing_revision_error(result.stderr):
                return None
            raise self._automation_error(self._format_git_failure(result))
        return result.stdout.strip() or None

    def local_branch_head(self, repo_path: Path, branch: str) -> str | None:
        if not self.branch_exists(repo_path, branch):
            return None
        return self.resolve_commit(repo_path, f"refs/heads/{branch}")

    def _json_safe_value(self, value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): self._json_safe_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._json_safe_value(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _emit_risk_confirmation_event(self, *, reason: str, message: str, details: dict[str, Any]) -> None:
        payload = {
            "protocol": RISK_CONFIRMATION_PROTOCOL_VERSION,
            "reason": reason,
            "message": message,
            "details": self._json_safe_value(details),
        }
        print(
            RISK_CONFIRMATION_EVENT_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )

    def confirm_risky_operation(self, *, reason: str, message: str, details: dict[str, Any]) -> bool:
        confirmer = getattr(self.manual_module, "confirm_risky_operation", None)
        if callable(confirmer):
            return bool(confirmer(reason=reason, message=message, details=details))

        print("", file=sys.stderr)
        print(f"WARNING {reason}: {message}", file=sys.stderr)
        for key, value in details.items():
            if value in (None, "", [], (), {}):
                continue
            print(f"{key}:", file=sys.stderr)
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    print(f"  - {item}", file=sys.stderr)
            else:
                print(f"  {value}", file=sys.stderr)

        if not sys.stdin.isatty():
            if os.environ.get(RISK_CONFIRMATION_PROTOCOL_ENV) == RISK_CONFIRMATION_PROTOCOL_VERSION:
                self._emit_risk_confirmation_event(reason=reason, message=message, details=details)
                answer = sys.stdin.readline().strip().lower()
                return answer in {"y", "yes"}
            print("Non-interactive run cannot confirm this risky operation.", file=sys.stderr)
            return False

        answer = input("Continue despite this risk? [y/N]: ").strip().lower()
        return answer in {"y", "yes"}

    def require_risky_operation_confirmation(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any],
        failure_message: str,
    ) -> None:
        if self.confirm_risky_operation(reason=reason, message=message, details=details):
            return
        raise self._automation_error(failure_message)

    def changed_files_between_commits(self, repo_path: Path, left_ref: str, right_ref: str) -> list[str]:
        result = self.run_git(repo_path, ["diff", "--name-only", left_ref, right_ref, "--"], check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def ensure_branch_reset_is_safe(
        self,
        repo_path: Path,
        branch: str,
        target_ref: str,
        *,
        operation: str,
    ) -> None:
        current_head = self.local_branch_head(repo_path, branch)
        if current_head is None:
            return

        target_head = self.resolve_commit(repo_path, target_ref)

        if current_head == target_head:
            return

        failure_message = (
            "Refusing to reset existing local branch "
            f"'{branch}' in {repo_path} from {current_head} to {target_head} during {operation}. "
            "This could hide local committed work. Use an unused work branch name, "
            "or manually preserve/delete the existing branch before retrying."
        )
        self.require_risky_operation_confirmation(
            reason="LOCAL_BRANCH_RESET_RISK",
            message=(
                f"Continuing will reset local branch '{branch}' from {current_head} to {target_head}. "
                "Local commits that are only reachable from that branch may become hidden."
            ),
            details={
                "repository": str(repo_path),
                "operation": operation,
                "branch": branch,
                "current_head": current_head,
                "target_ref": target_ref,
                "target_head": target_head,
                "affected_files": self.changed_files_between_commits(repo_path, target_head, current_head),
            },
            failure_message=failure_message,
        )

    def ensure_detached_head_checkout_is_safe(
        self,
        repo_path: Path,
        target_ref: str,
        *,
        operation: str,
    ) -> None:
        if self.current_branch_name(repo_path) is not None:
            return

        current_head = self.try_resolve_commit(repo_path, "HEAD")
        target_head = self.resolve_commit(repo_path, target_ref)
        if current_head is None or current_head == target_head:
            return

        failure_message = (
            "Refusing to leave detached HEAD "
            f"{current_head} in {repo_path} for {target_head} during {operation}. "
            "This could hide local committed work that is not protected by a branch. "
            "Create a branch for the current HEAD or choose Continue Current Branch from a named branch."
        )
        self.require_risky_operation_confirmation(
            reason="DETACHED_HEAD_CHECKOUT_RISK",
            message=(
                f"Continuing will move detached HEAD from {current_head} to {target_head}. "
                "The current detached commit may become difficult to find if it is not referenced elsewhere."
            ),
            details={
                "repository": str(repo_path),
                "operation": operation,
                "current_head": current_head,
                "target_ref": target_ref,
                "target_head": target_head,
                "affected_files": self.changed_files_between_commits(repo_path, target_head, current_head),
            },
            failure_message=failure_message,
        )

    def current_branch_name(self, repo_path: Path) -> str | None:
        result = self.run_git(repo_path, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def has_git_identity(self, repo_path: Path) -> bool:
        user_name = self.run_git(repo_path, ["config", "--get", "user.name"], check=False)
        user_email = self.run_git(repo_path, ["config", "--get", "user.email"], check=False)
        return bool(user_name.stdout.strip()) and bool(user_email.stdout.strip())

    def merge_in_progress(self, repo_path: Path) -> bool:
        result = self.run_git(repo_path, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False)
        return result.returncode == 0

    def _filter_status_lines(
        self,
        status_output: str,
        *,
        ignored_untracked_paths: Iterable[str] = (),
        ignored_paths: Iterable[str] = (),
    ) -> list[str]:
        ignored = {
            common.normalize_status_relative_path(path)
            for path in ignored_untracked_paths
            if path.strip()
        }
        ignored_dirty = {
            common.normalize_status_relative_path(path)
            for path in ignored_paths
            if path.strip()
        }

        filtered_lines: list[str] = []
        for line in status_output.splitlines():
            normalized_line = line.rstrip()
            if not normalized_line:
                continue
            relative_path = common.normalize_status_relative_path(normalized_line[3:])
            if normalized_line.startswith("?? ") and relative_path in ignored:
                continue
            if relative_path in ignored_dirty:
                continue
            filtered_lines.append(normalized_line)
        return filtered_lines

    def working_tree_status_lines(
        self,
        repo_path: Path,
        *,
        ignored_untracked_paths: Iterable[str] = (),
        ignored_paths: Iterable[str] = (),
    ) -> list[str]:
        result = self.run_git(repo_path, ["status", "--porcelain"])
        return self._filter_status_lines(
            result.stdout,
            ignored_untracked_paths=ignored_untracked_paths,
            ignored_paths=ignored_paths,
        )

    def working_tree_has_changes(
        self,
        repo_path: Path,
        *,
        ignored_untracked_paths: Iterable[str] = (),
        ignored_paths: Iterable[str] = (),
    ) -> bool:
        return bool(
            self.working_tree_status_lines(
                repo_path,
                ignored_untracked_paths=ignored_untracked_paths,
                ignored_paths=ignored_paths,
            )
        )

    def repo_direct_status_lines(
        self,
        repo_path: Path,
        *,
        ignored_untracked_paths: Iterable[str] = (),
    ) -> list[str]:
        result = self.run_git(repo_path, ["status", "--porcelain", "--ignore-submodules=dirty"])
        return self._filter_status_lines(result.stdout, ignored_untracked_paths=ignored_untracked_paths)

    def repo_has_direct_changes(
        self,
        repo_path: Path,
        *,
        ignored_untracked_paths: Iterable[str] = (),
    ) -> bool:
        return bool(self.repo_direct_status_lines(repo_path, ignored_untracked_paths=ignored_untracked_paths))

    def get_unmerged_paths(self, repo_path: Path) -> list[str]:
        result = self.run_git(repo_path, ["diff", "--name-only", "--diff-filter=U"])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def is_submodule_path(self, repo_path: Path, relative_path: str) -> bool:
        result = self.run_git(repo_path, ["ls-files", "--stage", "--", relative_path], check=False)
        return "160000" in result.stdout

    def collect_unmerged_conflicts(self, repo_path: Path) -> list[UnmergedConflict]:
        result = self.run_git(repo_path, ["ls-files", "-u"], check=False)
        if result.returncode != 0 or not result.stdout:
            return []

        modes_by_path: dict[str, set[str]] = {}
        stages_by_path: dict[str, set[int]] = {}
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=3)
            if len(parts) != 4:
                continue
            mode, _sha, stage_token, path = parts
            try:
                stage = int(stage_token)
            except ValueError:
                continue
            modes_by_path.setdefault(path, set()).add(mode)
            stages_by_path.setdefault(path, set()).add(stage)

        return [
            UnmergedConflict(
                path=path,
                modes=tuple(sorted(modes_by_path[path])),
                stages=tuple(sorted(stages_by_path[path])),
            )
            for path in modes_by_path
        ]

    def split_conflicts_by_stage(self, repo_path: Path) -> tuple[list[str], list[str], list[UnmergedConflict]]:
        submodule_conflicts: list[str] = []
        file_conflicts: list[str] = []
        incomplete_stage_conflicts: list[UnmergedConflict] = []
        for conflict in self.collect_unmerged_conflicts(repo_path):
            if not conflict.is_complete:
                incomplete_stage_conflicts.append(conflict)
            elif conflict.is_gitlink:
                submodule_conflicts.append(conflict.path)
            else:
                file_conflicts.append(conflict.path)
        return submodule_conflicts, file_conflicts, incomplete_stage_conflicts

    def split_conflicts(self, repo_path: Path) -> tuple[list[str], list[str]]:
        submodule_conflicts, file_conflicts, _incomplete_stage_conflicts = self.split_conflicts_by_stage(repo_path)
        return submodule_conflicts, file_conflicts

    def get_submodule_paths(self, repo_path: Path) -> list[str]:
        gitmodules_path = repo_path / ".gitmodules"
        if not gitmodules_path.exists():
            return []

        result = self.run_git(
            repo_path,
            ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return []

        paths: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                paths.append(parts[1])
        return paths

    def get_remote_names(self, repo_path: Path) -> list[str]:
        result = self.run_git(repo_path, ["remote"], check=False)
        if result.returncode != 0 or not result.stdout:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def remote_exists(self, repo_path: Path, remote_name: str) -> bool:
        return remote_name in self.get_remote_names(repo_path)

    def is_missing_revision_error(self, stderr: str) -> bool:
        markers = getattr(self.manual_module, "KNOWN_MISSING_REVISION_MARKERS", KNOWN_MISSING_REVISION_MARKERS)
        return any(marker in stderr for marker in markers)

    def resolve_commit(self, repo_path: Path, commitish: str) -> str:
        result = self.run_git(repo_path, ["rev-parse", "--verify", f"{commitish}^{{commit}}"], check=False)
        if result.returncode != 0:
            raise self._automation_error(self._format_git_failure(result))
        return result.stdout.strip()

    def commit_exists(self, repo_path: Path, commitish: str) -> bool:
        result = self.run_git(repo_path, ["rev-parse", "--verify", f"{commitish}^{{commit}}"], check=False)
        if result.returncode != 0:
            if self.is_missing_revision_error(result.stderr):
                return False
            raise self._automation_error(self._format_git_failure(result))
        return result.returncode == 0

    def ensure_commit_available(self, repo_path: Path, commitish: str) -> bool:
        if self.commit_exists(repo_path, commitish):
            return True

        for remote_name in self.get_remote_names(repo_path):
            exact_fetch_result = self.run_git(
                repo_path,
                ["fetch", "--no-recurse-submodules", remote_name, commitish],
                check=False,
                stream_output=self._verbose(),
            )
            if exact_fetch_result.returncode == 0 and self.commit_exists(repo_path, commitish):
                return True
            fetch_result = self.run_git(
                repo_path,
                ["fetch", "--no-recurse-submodules", remote_name],
                check=False,
                stream_output=self._verbose(),
            )
            if fetch_result.returncode != 0:
                continue
            if self.commit_exists(repo_path, commitish):
                return True
        return False

    def fetch_ref_if_remote(self, repo_path: Path, ref: str) -> None:
        remote, _, remote_branch = ref.partition("/")
        if not remote_branch or not self.remote_exists(repo_path, remote):
            return
        remote_ref = f"refs/heads/{remote_branch}"
        tracking_ref = common.remote_tracking_ref(ref)
        self.run_git(
            repo_path,
            ["fetch", "--no-recurse-submodules", remote, f"+{remote_ref}:{tracking_ref}"],
            stream_output=self._verbose(),
        )

    def fetch_all_remotes(self, repo_path: Path) -> None:
        if not self.get_remote_names(repo_path):
            return
        self.run_git(
            repo_path,
            ["fetch", "--no-recurse-submodules", "--all", "--prune"],
            check=False,
            stream_output=self._verbose(),
        )

    def create_root_branch(self, repo_path: Path, base_remote_branch: str, work_branch: str) -> None:
        self.fetch_ref_if_remote(repo_path, base_remote_branch)
        start_point = common.remote_tracking_ref(base_remote_branch)
        self.ensure_branch_reset_is_safe(
            repo_path,
            work_branch,
            start_point,
            operation="root work-branch creation",
        )
        self.ensure_detached_head_checkout_is_safe(
            repo_path,
            start_point,
            operation="root work-branch creation",
        )
        self.run_git(repo_path, ["checkout", "-B", work_branch, start_point])

    def create_submodule_branch(self, repo_path: Path, work_branch: str, *, start_point: str | None = None) -> None:
        reset_target = start_point or "HEAD"
        self.ensure_branch_reset_is_safe(
            repo_path,
            work_branch,
            reset_target,
            operation="submodule work-branch creation",
        )
        self.ensure_detached_head_checkout_is_safe(
            repo_path,
            reset_target,
            operation="submodule work-branch creation",
        )
        if start_point:
            self.run_git(repo_path, ["checkout", "-B", work_branch, start_point])
            return
        self.run_git(repo_path, ["checkout", "-B", work_branch])

    def merge_branch(self, repo_path: Path, merge_ref: str) -> str:
        self.fetch_ref_if_remote(repo_path, merge_ref)
        merge_target = common.remote_tracking_ref(merge_ref)
        result = self.run_git(repo_path, ["merge", "--no-ff", "--no-commit", merge_target], check=False)
        if result.returncode == 0:
            return merge_target
        if self.get_unmerged_paths(repo_path):
            return merge_target
        raise self._automation_error(self._format_git_failure(result))

    def update_submodules(self, repo_path: Path, paths: Iterable[str] = ()) -> None:
        selected_paths = [path for path in paths if path.strip()]
        self.run_cleanup_step(
            repo_path,
            cleanup_steps.submodule_update_step(paths=selected_paths),
            stream_output=self._verbose(),
        )

    def staged_changes_exist(self, repo_path: Path) -> bool:
        result = self.run_git(repo_path, ["diff", "--cached", "--quiet"], check=False)
        if result.returncode == 0:
            return False
        if result.returncode == 1:
            return True
        raise self._automation_error(self._format_git_failure(result))

    def commit_if_needed(
        self,
        repo_path: Path,
        *,
        message: ManualCommitMessage | None = None,
        fallback_message: str | None = None,
    ) -> None:
        if self.get_unmerged_paths(repo_path):
            return

        if self.merge_in_progress(repo_path):
            commit_args = ["commit", *message.git_args()] if message else ["commit", "--no-edit"]
            result = self.run_git(repo_path, commit_args, check=False)
        elif self.staged_changes_exist(repo_path):
            if message:
                result = self.run_git(repo_path, ["commit", *message.git_args()], check=False)
            elif fallback_message:
                result = self.run_git(repo_path, ["commit", "-m", fallback_message], check=False)
            else:
                return
        else:
            return

        if result.returncode != 0:
            raise self._automation_error(self._format_git_failure(result))

    def stage_path(self, repo_path: Path, relative_path: str) -> None:
        self.run_git(repo_path, ["add", "--", relative_path])

    def collect_repo_snapshot(self, repo_path: Path) -> list[Any]:
        self.ensure_git_repo(repo_path)
        head_commit = self.run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()
        snapshot_cls = getattr(self.manual_module, "RepoSnapshot", RepoSnapshot)
        snapshots = [
            snapshot_cls(
                repo_path=repo_path.resolve(),
                head_commit=head_commit,
                branch_name=self.current_branch_name(repo_path),
            )
        ]
        for submodule_path in self.get_submodule_paths(repo_path):
            child_repo = repo_path / submodule_path
            if not child_repo.exists():
                continue
            try:
                snapshots.extend(self.collect_repo_snapshot(child_repo))
            except Exception as exc:  # noqa: BLE001 - mirrors original broad recovery context.
                raise self._automation_error(
                    f"Failed to collect snapshot for submodule repository {child_repo}: {exc}"
                ) from exc
        return snapshots

    def rollback_to_snapshot(
        self,
        repo_path: Path,
        checkpoint: list[Any],
        *,
        report_path: Path | None,
        preserve_report: bool,
        ignored_root_untracked_paths: Iterable[str],
        preserved_dirty_paths_by_repo: dict[Path, set[str]],
        preserved_dirty_repos: set[Path],
    ) -> None:
        report_contents = None
        if preserve_report and report_path is not None and report_path.exists():
            report_contents = report_path.read_text(encoding="utf-8")
            report_path.unlink()
        self.cleanup_repository(
            repo_path,
            CleanupResult(),
            skip_submodule_update=True,
            ignored_root_untracked_paths=ignored_root_untracked_paths,
            preserved_dirty_paths_by_repo=preserved_dirty_paths_by_repo,
            preserved_dirty_repos=preserved_dirty_repos,
        )
        self.restore_snapshot(
            repo_path,
            {item.repo_path: item for item in checkpoint},
            is_root=True,
        )
        if preserve_report and report_contents is not None and report_path is not None:
            report_path.write_text(report_contents, encoding="utf-8")

    def cleanup_repository(
        self,
        repo_path: Path,
        result: Any,
        *,
        skip_submodule_update: bool,
        allow_clean_x: bool = False,
        ignored_root_untracked_paths: Iterable[str] = (),
        is_root: bool = True,
        preserved_dirty_paths_by_repo: dict[Path, set[str]] | None = None,
        preserved_dirty_repos: set[Path] | None = None,
    ) -> None:
        """Recursively clean a repository using multi-level fallback.

        Each repository is first visited recursively into its submodules, then
        the repository itself is cleaned by trying Level 1 → Level 2 → Level 3
        → Level 4.  The first level that succeeds ends the cleanup for that
        repo.  If all levels fail, the method raises with a detailed manual-
        recovery message so the user knows exactly what went wrong.
        """
        self.ensure_git_repo(repo_path)
        resolved_repo_path = repo_path.resolve()
        preserved_dirty_repos = preserved_dirty_repos or set()
        ignored_dirty_paths = (
            preserved_dirty_paths_by_repo.get(resolved_repo_path, set())
            if preserved_dirty_paths_by_repo is not None
            else set()
        )
        result.visited_repos.append(repo_path)

        # --- recurse into submodules first ---
        for submodule_path in self.get_submodule_paths(repo_path):
            child_repo = repo_path / submodule_path
            if not child_repo.exists():
                continue
            try:
                self.cleanup_repository(
                    child_repo,
                    result,
                    skip_submodule_update=skip_submodule_update,
                    allow_clean_x=allow_clean_x,
                    ignored_root_untracked_paths=(),
                    is_root=False,
                    preserved_dirty_paths_by_repo=preserved_dirty_paths_by_repo,
                    preserved_dirty_repos=preserved_dirty_repos,
                )
            except self._automation_error_class() as exc:
                raise self._automation_error(
                    f"Failed to cleanup submodule repository {child_repo}: {exc}"
                ) from exc

        # --- mark aborted merges and hard resets in the result (best-effort) ---
        if self.merge_in_progress(repo_path):
            result.aborted_merges.append(repo_path)
        elif self.get_unmerged_paths(repo_path):
            if resolved_repo_path not in preserved_dirty_repos:
                result.aborted_merges.append(repo_path)

        if (
            self.head_exists(repo_path)
            and self.working_tree_has_changes(repo_path, ignored_paths=ignored_dirty_paths)
            and resolved_repo_path not in preserved_dirty_repos
        ):
            result.hard_resets.append(repo_path)

        # --- multi-level cleanup fallback ---
        submodule_paths = self.get_submodule_paths(repo_path)
        levels = self._build_cleanup_levels(
            repo_path, submodule_paths, allow_clean_x, skip_submodule_update, is_root,
        )

        tried: list[str] = []
        for level_name, steps in levels:
            success = self._try_cleanup_level(
                repo_path,
                level_name,
                steps,
                result,
                ignored_paths=ignored_dirty_paths,
            )
            tried.append(level_name)
            if success:
                return

        # all levels failed — build a detailed error message
        detail_lines = "\n".join(f"  - {name}" for name in tried)
        allow_x_hint = ""
        if not allow_clean_x:
            allow_x_hint = "\n  3. Re-run with --allow-clean-x to let git clean remove ignored files."
        raise self._automation_error(
            f"Cleanup failed for repository: {repo_path}\n"
            f"All cleanup levels exhausted:\n{detail_lines}\n\n"
            "Manual recovery options:\n"
            f"  1. cd {repo_path}\n"
            "  2. git status  # inspect the remaining untracked/ignored files\n"
            + allow_x_hint +
            f"\n  4. If still failing, manually delete and re-clone this repository."
        )

    def _try_cleanup_level(
        self,
        repo_path: Path,
        level_name: str,
        steps: list[cleanup_steps.CleanupStep],
        result: Any,
        *,
        ignored_paths: set[str] = frozenset(),
    ) -> bool:
        """Execute one cleanup level; return True if the repo is clean afterward."""
        for step in steps:
            step_result = self.run_cleanup_step(repo_path, step, check=False, stream_output=self._verbose())
            if step_result.returncode != 0 and not step.allow_failure:
                self._verbose_print(f"[cleanup] {level_name}: step failed: {' '.join(step.args)}")
                return False

        if self.merge_in_progress(repo_path):
            return False
        if self.working_tree_has_changes(repo_path, ignored_paths=ignored_paths):
            return False
        return True

    def _build_cleanup_levels(
        self,
        repo_path: Path,
        submodule_paths: list[str],
        allow_clean_x: bool,
        skip_submodule_update: bool,
        is_root: bool,
    ) -> list[tuple[str, list[cleanup_steps.CleanupStep]]]:
        """Build the ordered list of cleanup levels for a single repository."""
        levels: list[tuple[str, list[cleanup_steps.CleanupStep]]] = []

        # Level 1 — basic (matching the original single-level behaviour)
        level1: list[cleanup_steps.CleanupStep] = [
            cleanup_steps.abort_operation_step("merge"),
            cleanup_steps.abort_operation_step("rebase"),
            cleanup_steps.abort_operation_step("cherry-pick"),
            cleanup_steps.reset_merge_step(),
            cleanup_steps.reset_hard_step("HEAD"),
            cleanup_steps.clean_step(allow_clean_x),
        ]
        if not skip_submodule_update and is_root:
            level1.append(cleanup_steps.submodule_update_step())
        levels.append(("level_1_basic", level1))

        # Level 2 — remove nested git repos too
        level2: list[cleanup_steps.CleanupStep] = [
            cleanup_steps.abort_operation_step("merge"),
            cleanup_steps.abort_operation_step("rebase"),
            cleanup_steps.abort_operation_step("cherry-pick"),
            cleanup_steps.reset_merge_step(),
            cleanup_steps.reset_hard_step("HEAD"),
            cleanup_steps.clean_step(allow_clean_x, remove_nested_git_repos=True),
        ]
        if not skip_submodule_update and is_root:
            level2.append(cleanup_steps.submodule_update_step())
        levels.append(("level_2_delete_nested_git", level2))

        # Level 3 — foreach-abort and foreach-clean
        level3: list[cleanup_steps.CleanupStep] = [
            cleanup_steps.abort_operation_step("merge"),
            cleanup_steps.abort_operation_step("rebase"),
            cleanup_steps.abort_operation_step("cherry-pick"),
            cleanup_steps.foreach_abort_operation_step("merge"),
            cleanup_steps.foreach_abort_operation_step("rebase"),
            cleanup_steps.foreach_abort_operation_step("cherry-pick"),
            cleanup_steps.reset_hard_step("HEAD"),
            cleanup_steps.clean_step(allow_clean_x, remove_nested_git_repos=True),
            cleanup_steps.foreach_reset_hard_step(),
            cleanup_steps.foreach_clean_step(allow_clean_x, remove_nested_git_repos=True),
        ]
        if not skip_submodule_update and is_root:
            level3.append(cleanup_steps.submodule_update_step())
        levels.append(("level_3_foreach_abort_and_clean", level3))

        # Level 4 — deep submodule cleanup (per-submodule fetch + reset + clean)
        if submodule_paths:
            deep_steps = cleanup_steps.ci_deep_submodule_cleanup_steps(
                repo_path, submodule_paths, allow_clean_x,
            )
            level4: list[cleanup_steps.CleanupStep] = [
                cleanup_steps.abort_operation_step("merge"),
                cleanup_steps.abort_operation_step("rebase"),
                cleanup_steps.abort_operation_step("cherry-pick"),
                cleanup_steps.reset_merge_step(),
                cleanup_steps.reset_hard_step("HEAD"),
                *deep_steps,
                cleanup_steps.clean_step(allow_clean_x, remove_nested_git_repos=True),
            ]
            if not skip_submodule_update and is_root:
                level4.append(cleanup_steps.submodule_update_step())
            levels.append(("level_4_deep_submodule_cleanup", level4))

        return levels

    def restore_snapshot(self, repo_path: Path, snapshots: dict[Path, Any], *, is_root: bool) -> None:
        self.ensure_git_repo(repo_path)
        snapshot = snapshots.get(repo_path.resolve())
        if snapshot is None:
            raise self._automation_error(f"Missing snapshot for repository: {repo_path}")

        if snapshot.branch_name and self.branch_exists(repo_path, snapshot.branch_name):
            self.ensure_branch_reset_is_safe(
                repo_path,
                snapshot.branch_name,
                snapshot.head_commit,
                operation="snapshot restore",
            )
            self.run_git(repo_path, ["checkout", snapshot.branch_name])
            self.run_cleanup_step(repo_path, cleanup_steps.reset_hard_step(snapshot.head_commit))
        else:
            self.ensure_detached_head_checkout_is_safe(
                repo_path,
                snapshot.head_commit,
                operation="snapshot restore",
            )
            self.run_git(repo_path, ["checkout", "--detach", snapshot.head_commit])

        if is_root:
            self.run_cleanup_step(repo_path, cleanup_steps.submodule_update_step(), check=False)

        for submodule_path in self.get_submodule_paths(repo_path):
            child_repo = repo_path / submodule_path
            if not child_repo.exists():
                continue
            if child_repo.resolve() not in snapshots:
                continue
            try:
                self.restore_snapshot(child_repo, snapshots, is_root=False)
            except self._automation_error_class() as exc:
                raise self._automation_error(f"Failed to restore snapshot for submodule repository {child_repo}: {exc}") from exc
