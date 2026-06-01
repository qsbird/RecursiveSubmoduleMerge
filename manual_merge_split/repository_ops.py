"""Manual repository operation boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from . import common
from . import execution
from .models import KNOWN_MISSING_REVISION_MARKERS, CleanupResult, MergeAutomationError, RepoSnapshot


REPOSITORY_OP_FUNCTIONS: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManualRepositoryOps:
    """Facade for Git and repository state operations in manual workflow."""

    manual_module: ModuleType | None = None

    def __post_init__(self) -> None:
        return None

    def run_git(self, repo_path: Path, args: list[str], **kwargs: Any) -> Any:
        runner = getattr(self.manual_module, "run_git", execution.run_git)
        return runner(repo_path, args, **kwargs)

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
        result = self.run_git(repo_path, ["show-ref", "--verify", f"refs/heads/{branch}"], check=False)
        return result.returncode == 0

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

    def working_tree_has_changes(
        self,
        repo_path: Path,
        *,
        ignored_untracked_paths: Iterable[str] = (),
        ignored_paths: Iterable[str] = (),
    ) -> bool:
        result = self.run_git(repo_path, ["status", "--porcelain"])
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
        if not ignored and not ignored_dirty:
            return bool(result.stdout)

        for line in result.stdout.splitlines():
            normalized_line = line.rstrip()
            if not normalized_line:
                continue
            relative_path = common.normalize_status_relative_path(normalized_line[3:])
            if normalized_line.startswith("?? ") and relative_path in ignored:
                continue
            if relative_path in ignored_dirty:
                continue
            return True
        return False

    def repo_has_direct_changes(
        self,
        repo_path: Path,
        *,
        ignored_untracked_paths: Iterable[str] = (),
    ) -> bool:
        result = self.run_git(repo_path, ["status", "--porcelain", "--ignore-submodules=dirty"])
        ignored = {
            common.normalize_status_relative_path(path)
            for path in ignored_untracked_paths
            if path.strip()
        }
        if not ignored:
            return bool(result.stdout)

        for line in result.stdout.splitlines():
            normalized_line = line.rstrip()
            if not normalized_line:
                continue
            if normalized_line.startswith("?? "):
                relative_path = common.normalize_status_relative_path(normalized_line[3:])
                if relative_path in ignored:
                    continue
            return True
        return False

    def get_unmerged_paths(self, repo_path: Path) -> list[str]:
        result = self.run_git(repo_path, ["diff", "--name-only", "--diff-filter=U"])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def is_submodule_path(self, repo_path: Path, relative_path: str) -> bool:
        result = self.run_git(repo_path, ["ls-files", "--stage", "--", relative_path], check=False)
        return "160000" in result.stdout

    def split_conflicts(self, repo_path: Path) -> tuple[list[str], list[str]]:
        submodule_conflicts: list[str] = []
        file_conflicts: list[str] = []
        for path in self.get_unmerged_paths(repo_path):
            if self.is_submodule_path(repo_path, path):
                submodule_conflicts.append(path)
            else:
                file_conflicts.append(path)
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
            fetch_result = self.run_git(repo_path, ["fetch", remote_name], check=False, stream_output=self._verbose())
            if fetch_result.returncode != 0:
                continue
            if self.commit_exists(repo_path, commitish):
                return True
        return False

    def fetch_ref_if_remote(self, repo_path: Path, ref: str) -> None:
        remote, _, remote_branch = ref.partition("/")
        if not remote_branch or not self.remote_exists(repo_path, remote):
            return
        self.run_git(repo_path, ["fetch", remote, remote_branch], stream_output=self._verbose())

    def fetch_all_remotes(self, repo_path: Path) -> None:
        if not self.get_remote_names(repo_path):
            return
        self.run_git(repo_path, ["fetch", "--all", "--prune"], check=False, stream_output=self._verbose())

    def create_root_branch(self, repo_path: Path, base_remote_branch: str, work_branch: str) -> None:
        self.fetch_ref_if_remote(repo_path, base_remote_branch)
        self.run_git(repo_path, ["checkout", "-B", work_branch, common.remote_tracking_ref(base_remote_branch)])

    def create_submodule_branch(self, repo_path: Path, work_branch: str, *, start_point: str | None = None) -> None:
        if start_point:
            self.run_git(repo_path, ["checkout", "-B", work_branch, start_point])
            return
        self.run_git(repo_path, ["checkout", "-B", work_branch])

    def merge_branch(self, repo_path: Path, merge_ref: str) -> None:
        self.fetch_ref_if_remote(repo_path, merge_ref)
        merge_target = common.remote_tracking_ref(merge_ref)
        result = self.run_git(repo_path, ["merge", "--no-ff", merge_target], check=False)
        if result.returncode == 0:
            return
        if self.get_unmerged_paths(repo_path):
            return
        raise self._automation_error(self._format_git_failure(result))

    def update_submodules(self, repo_path: Path, paths: Iterable[str] = ()) -> None:
        args = ["submodule", "update", "--init", "--recursive"]
        selected_paths = [path for path in paths if path.strip()]
        if selected_paths:
            args.extend(["--", *selected_paths])
        self.run_git(
            repo_path,
            args,
            stream_output=self._verbose(),
        )

    def staged_changes_exist(self, repo_path: Path) -> bool:
        result = self.run_git(repo_path, ["diff", "--cached", "--quiet"], check=False)
        if result.returncode == 0:
            return False
        if result.returncode == 1:
            return True
        raise self._automation_error(self._format_git_failure(result))

    def commit_if_needed(self, repo_path: Path, *, fallback_message: str | None = None) -> None:
        if self.get_unmerged_paths(repo_path):
            return

        if self.merge_in_progress(repo_path):
            result = self.run_git(repo_path, ["commit", "--no-edit"], check=False)
        elif fallback_message and self.staged_changes_exist(repo_path):
            result = self.run_git(repo_path, ["commit", "-m", fallback_message], check=False)
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
        ignored_root_untracked_paths: Iterable[str] = (),
        is_root: bool = True,
        preserved_dirty_paths_by_repo: dict[Path, set[str]] | None = None,
        preserved_dirty_repos: set[Path] | None = None,
    ) -> None:
        self.ensure_git_repo(repo_path)
        resolved_repo_path = repo_path.resolve()
        preserved_dirty_repos = preserved_dirty_repos or set()
        ignored_dirty_paths = (
            preserved_dirty_paths_by_repo.get(resolved_repo_path, set())
            if preserved_dirty_paths_by_repo is not None
            else set()
        )
        result.visited_repos.append(repo_path)

        for submodule_path in self.get_submodule_paths(repo_path):
            child_repo = repo_path / submodule_path
            if not child_repo.exists():
                continue
            try:
                self.cleanup_repository(
                    child_repo,
                    result,
                    skip_submodule_update=skip_submodule_update,
                    ignored_root_untracked_paths=(),
                    is_root=False,
                    preserved_dirty_paths_by_repo=preserved_dirty_paths_by_repo,
                    preserved_dirty_repos=preserved_dirty_repos,
                )
            except self._automation_error_class() as exc:
                raise self._automation_error(f"Failed to cleanup submodule repository {child_repo}: {exc}") from exc

        if self.merge_in_progress(repo_path):
            abort_result = self.run_git(repo_path, ["merge", "--abort"], check=False)
            if abort_result.returncode != 0:
                reset_result = self.run_git(repo_path, ["reset", "--merge"], check=False)
                if reset_result.returncode != 0:
                    result.failed_steps.append(self._format_git_failure(abort_result))
                    result.failed_steps.append(self._format_git_failure(reset_result))
                    raise self._automation_error(f"Cleanup could not abort the in-progress merge in {repo_path}")
            result.aborted_merges.append(repo_path)
        elif self.get_unmerged_paths(repo_path):
            if resolved_repo_path in preserved_dirty_repos:
                raise self._automation_error(
                    "A preserved dirty submodule became part of the failed merge and cannot be cleaned "
                    f"without overwriting local changes: {repo_path}"
                )
            reset_result = self.run_git(repo_path, ["reset", "--merge"], check=False)
            if reset_result.returncode != 0:
                result.failed_steps.append(self._format_git_failure(reset_result))
                raise self._automation_error(f"Cleanup could not reset unmerged paths in {repo_path}")
            result.aborted_merges.append(repo_path)

        if (
            self.head_exists(repo_path)
            and self.working_tree_has_changes(repo_path, ignored_paths=ignored_dirty_paths)
            and resolved_repo_path not in preserved_dirty_repos
        ):
            hard_reset_result = self.run_git(repo_path, ["reset", "--hard", "HEAD"], check=False)
            if hard_reset_result.returncode != 0:
                result.failed_steps.append(self._format_git_failure(hard_reset_result))
                raise self._automation_error(f"Cleanup could not discard local changes in {repo_path}")
            result.hard_resets.append(repo_path)

        if not skip_submodule_update and is_root:
            update_result = self.run_git(
                repo_path,
                ["submodule", "update", "--init", "--recursive"],
                check=False,
                stream_output=self._verbose(),
            )
            if update_result.returncode != 0:
                result.failed_steps.append(self._format_git_failure(update_result))
                raise self._automation_error(f"Cleanup could not refresh submodules in {repo_path}")
            result.submodule_updates.append(repo_path)

        ignored_untracked_paths = ignored_root_untracked_paths if is_root else ()
        if self.merge_in_progress(repo_path) or self.working_tree_has_changes(
            repo_path,
            ignored_untracked_paths=ignored_untracked_paths,
            ignored_paths=ignored_dirty_paths,
        ):
            if resolved_repo_path in preserved_dirty_repos and not self.merge_in_progress(repo_path):
                return
            result.not_clean_repos.append(repo_path)
            raise self._automation_error(f"Repository is still not clean after cleanup: {repo_path}")

    def restore_snapshot(self, repo_path: Path, snapshots: dict[Path, Any], *, is_root: bool) -> None:
        self.ensure_git_repo(repo_path)
        snapshot = snapshots.get(repo_path.resolve())
        if snapshot is None:
            raise self._automation_error(f"Missing snapshot for repository: {repo_path}")

        if snapshot.branch_name and self.branch_exists(repo_path, snapshot.branch_name):
            self.run_git(repo_path, ["checkout", snapshot.branch_name])
            self.run_git(repo_path, ["reset", "--hard", snapshot.head_commit])
        else:
            self.run_git(repo_path, ["checkout", "--detach", snapshot.head_commit])

        if is_root:
            self.run_git(repo_path, ["submodule", "update", "--init", "--recursive"], check=False)

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
