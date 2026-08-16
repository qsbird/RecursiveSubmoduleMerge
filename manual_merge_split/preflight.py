"""Manual merge preflight and dirty-submodule safety boundary."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from . import common
from .models import DirtyRepoAssessment, MergePreflightResult
from .repository_ops import ManualRepositoryOps


@dataclass(frozen=True)
class ToolAvailabilityResult:
    """External tool availability check result."""

    tool_name: str
    available: bool
    resolved_path: str | None
    error_message: str | None


def check_tool_availability(tool_name: str) -> ToolAvailabilityResult:
    """Check whether an external tool command is available on the system PATH.

    Uses ``shutil.which`` to locate the tool.  Returns a
    :class:`ToolAvailabilityResult` indicating whether the tool was found,
    its absolute path (if found), and a human-readable error message (if not).
    """
    resolved = shutil.which(tool_name)
    if resolved is not None:
        return ToolAvailabilityResult(
            tool_name=tool_name,
            available=True,
            resolved_path=resolved,
            error_message=None,
        )
    return ToolAvailabilityResult(
        tool_name=tool_name,
        available=False,
        resolved_path=None,
        error_message=(
            f"Conflict tool command '{tool_name}' was not found on the system PATH. "
            "Please install the tool or change the conflict tool command in advanced options."
        ),
    )


PREFLIGHT_FUNCTIONS: tuple[str, ...] = ()


def collect_submodule_lineages(
    repo_path: Path,
    ops: ManualRepositoryOps,
    *,
    lineage: tuple[str, ...] = (),
) -> dict[Path, tuple[str, ...]]:
    lineages: dict[Path, tuple[str, ...]] = {}
    for submodule_path in ops.get_submodule_paths(repo_path):
        child_repo = repo_path / submodule_path
        if not child_repo.exists():
            continue
        child_lineage = (*lineage, submodule_path)
        lineages[child_repo.resolve()] = child_lineage
        lineages.update(collect_submodule_lineages(child_repo, ops, lineage=child_lineage))
    return lineages


def local_revision_for_ref(repo_path: Path, ref: str, ops: ManualRepositoryOps) -> str:
    remote, _, remote_branch = ref.partition("/")
    if remote_branch and ops.remote_exists(repo_path, remote):
        return common.remote_tracking_ref(ref)
    return ref


def resolve_commit_variants_for_refs(repo_path: Path, refs: Iterable[str], ops: ManualRepositoryOps) -> list[str]:
    return common.unique_preserving_order(
        ops.resolve_commit(repo_path, local_revision_for_ref(repo_path, ref, ops))
        for ref in refs
    )


def get_submodule_commit_at_revision(
    repo_path: Path,
    revision: str,
    relative_path: str,
    ops: ManualRepositoryOps,
) -> str | None:
    result = ops.run_git(repo_path, ["ls-tree", revision, "--", relative_path], check=False)
    if result.returncode != 0 or not result.stdout:
        return None

    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        mode, object_type, commit_sha = parts[0], parts[1], parts[2]
        if mode == "160000" and object_type == "commit":
            return commit_sha
    return None


def assess_dirty_submodule_impact(
    root_repo_path: Path,
    lineage: tuple[str, ...],
    root_commit_variants: Iterable[str],
    ops: ManualRepositoryOps,
) -> tuple[bool, str]:
    current_repo = root_repo_path
    current_commit_variants = common.unique_preserving_order(root_commit_variants)
    display_parts: list[str] = []

    for depth, submodule_path in enumerate(lineage):
        child_commit_variants: list[str] = []
        entry_missing = False
        for commitish in current_commit_variants:
            child_commit = get_submodule_commit_at_revision(current_repo, commitish, submodule_path, ops)
            if child_commit is None:
                entry_missing = True
                continue
            child_commit_variants.append(child_commit)

        display_parts.append(submodule_path)
        display_path = "/".join(display_parts)
        unique_child_variants = common.unique_preserving_order(child_commit_variants)

        if entry_missing or len(unique_child_variants) > 1:
            if depth == len(lineage) - 1:
                if entry_missing:
                    return True, f"planned merges may replace or remove submodule '{display_path}'"
                return True, f"planned merges may change the gitlink for submodule '{display_path}'"

            if entry_missing:
                return True, (
                    "planned merges may replace or remove ancestor submodule "
                    f"'{display_path}', so nested dirty changes cannot be preserved safely"
                )

            child_repo = current_repo / submodule_path
            unavailable_commits = [
                commitish
                for commitish in unique_child_variants
                if not ops.ensure_commit_available(child_repo, commitish)
            ]
            if unavailable_commits:
                return True, (
                    "could not verify nested submodule safety because these commits are unavailable in "
                    f"{child_repo}: {', '.join(unavailable_commits)}"
                )
            current_repo = child_repo
            current_commit_variants = unique_child_variants
            continue

        return False, f"planned merges keep the gitlink unchanged for ancestor '{display_path}'"

    return True, "dirty repository could not be mapped back to a declared submodule path"


def format_dirty_submodule_list(entries: Iterable[DirtyRepoAssessment]) -> str:
    return "\n".join(
        f"- {entry.root_relative_path} ({entry.repo_path})"
        + (f": {entry.reason}" if entry.reason else "")
        for entry in entries
    )


def build_preserved_dirty_path_map(
    root_repo_path: Path,
    assessments: Iterable[DirtyRepoAssessment],
) -> dict[Path, set[str]]:
    preserved: dict[Path, set[str]] = {}
    for entry in assessments:
        current_repo = root_repo_path.resolve()
        for submodule_path in entry.lineage:
            preserved.setdefault(current_repo, set()).add(submodule_path)
            current_repo = (current_repo / submodule_path).resolve()
    return preserved


CLEAN_WORKTREE_HINT = (
    "Manual merge requires a clean working tree in the root repository and every submodule. "
    "Commit or stash your changes, or run cleanup first. Cleanup discards local changes; "
    "you are responsible for that data loss. "
    "CLI: python -m manual_merge_split cleanup --repo <path>   "
    "GUI: use the Cleanup tab."
)


def format_uncommitted_changes_error(
    repo_path: Path,
    status_lines: Iterable[str],
    *,
    kind: str = "repository",
) -> str:
    listed = "\n".join(f"  {line}" for line in status_lines if str(line).strip())
    body = f"{listed}\n" if listed else ""
    return (
        f"Manual merge refused because {kind} has uncommitted changes: {repo_path}\n"
        f"{body}"
        f"{CLEAN_WORKTREE_HINT}"
    )


def ensure_repo_ready(
    repo_path: Path,
    ops: ManualRepositoryOps,
    *,
    require_clean: bool = True,
    ignored_dirty_paths: Iterable[str] = (),
    ignored_untracked_paths: Iterable[str] = (),
) -> None:
    if ops.merge_in_progress(repo_path):
        raise ops._automation_error(f"Repository already has an in-progress merge: {repo_path}")
    if require_clean:
        status_lines = ops.working_tree_status_lines(
            repo_path,
            ignored_untracked_paths=ignored_untracked_paths,
            ignored_paths=ignored_dirty_paths,
        )
        if status_lines:
            raise ops._automation_error(format_uncommitted_changes_error(repo_path, status_lines))
    if not ops.has_git_identity(repo_path):
        raise ops._automation_error(
            "Git user.name / user.email is not configured for repository: "
            f"{repo_path}"
        )


def refresh_repository_for_merge(
    repo_path: Path,
    ops: ManualRepositoryOps,
    *,
    visited: set[Path] | None = None,
) -> None:
    ops.ensure_git_repo(repo_path)
    resolved_repo_path = repo_path.resolve()
    if visited is None:
        visited = set()
    if resolved_repo_path in visited:
        return
    visited.add(resolved_repo_path)

    sync_result = ops.run_git(
        repo_path,
        ["submodule", "sync", "--recursive"],
        check=False,
        stream_output=ops._verbose(),
    )
    if sync_result.returncode != 0:
        ops._verbose_print(f"Failed to sync submodule configuration in {repo_path}")

    for submodule_path in ops.get_submodule_paths(repo_path):
        child_repo = repo_path / submodule_path
        if not common.path_contains_git_repo(child_repo):
            ops.run_git(
                repo_path,
                ["submodule", "update", "--init", "--recursive", "--", submodule_path],
                stream_output=ops._verbose(),
            )
        if not common.path_contains_git_repo(child_repo):
            raise ops._automation_error(f"Failed to initialize submodule repository before merge: {child_repo}")

    for submodule_path in ops.get_submodule_paths(repo_path):
        child_repo = repo_path / submodule_path
        if not child_repo.exists() or not common.path_contains_git_repo(child_repo):
            continue
        refresh_repository_for_merge(child_repo, ops, visited=visited)


def preflight_repository(
    repo_path: Path,
    *,
    base_ref: str | None,
    merge_refs: list[str],
    ignored_root_untracked_paths: Iterable[str] = (),
    ops: ManualRepositoryOps,
) -> MergePreflightResult:
    if base_ref is None:
        raise ops._automation_error("Base ref is required for merge preflight")

    ops.ensure_git_repo(repo_path)
    ensure_repo_ready(repo_path, ops, require_clean=False)
    status_lines = ops.repo_direct_status_lines(
        repo_path,
        ignored_untracked_paths=ignored_root_untracked_paths,
    )
    if status_lines:
        raise ops._automation_error(
            format_uncommitted_changes_error(repo_path, status_lines, kind="root repository")
        )

    refresh_repository_for_merge(repo_path, ops)
    ops.fetch_ref_if_remote(repo_path, base_ref)
    for merge_ref in merge_refs:
        ops.fetch_ref_if_remote(repo_path, merge_ref)

    lineages = collect_submodule_lineages(repo_path, ops)
    dirty_submodules: list[DirtyRepoAssessment] = []
    for child_repo, lineage in lineages.items():
        try:
            ops.ensure_git_repo(child_repo)
            ensure_repo_ready(child_repo, ops, require_clean=False)
        except ops._automation_error_class() as exc:
            raise ops._automation_error(f"Merge preflight failed for submodule repository {child_repo}: {exc}") from exc
        child_status = ops.repo_direct_status_lines(child_repo)
        if not child_status:
            continue
        dirty_submodules.append(
            DirtyRepoAssessment(
                repo_path=child_repo,
                lineage=lineage,
                reason="local submodule changes detected",
            )
        )

    if dirty_submodules:
        details = format_dirty_submodule_list(dirty_submodules)
        raise ops._automation_error(
            "Manual merge refused because submodules have uncommitted changes:\n"
            f"{details}\n"
            f"{CLEAN_WORKTREE_HINT}"
        )

    return MergePreflightResult()


@dataclass(frozen=True)
class ManualPreflightService:
    """Facade for preflight checks before the manual merge workflow."""

    manual_module: ModuleType | None = None

    def __post_init__(self) -> None:
        return None

    def refresh_repository_for_merge(self, repo_path: Path) -> None:
        refresh_repository_for_merge(repo_path, ManualRepositoryOps(self.manual_module))

    def preflight_repository(
        self,
        repo_path: Path,
        base_ref: str | None,
        merge_refs: list[str],
        *,
        ignored_root_untracked_paths: Iterable[str] = (),
    ) -> Any:
        return preflight_repository(
            repo_path,
            base_ref=base_ref,
            merge_refs=merge_refs,
            ignored_root_untracked_paths=ignored_root_untracked_paths,
            ops=ManualRepositoryOps(self.manual_module),
        )

    def collect_submodule_lineages(self, repo_path: Path) -> dict[Path, tuple[str, ...]]:
        return collect_submodule_lineages(repo_path, ManualRepositoryOps(self.manual_module))

    def assess_dirty_submodule_impact(
        self,
        repo_path: Path,
        lineage: tuple[str, ...],
        root_commit_variants: Iterable[str],
    ) -> tuple[bool, str]:
        return assess_dirty_submodule_impact(
            repo_path,
            lineage,
            root_commit_variants,
            ManualRepositoryOps(self.manual_module),
        )

    def format_dirty_submodule_list(self, entries: Iterable[Any]) -> str:
        return format_dirty_submodule_list(entries)

    def build_preserved_dirty_path_map(self, root_repo_path: Path, entries: Iterable[Any]) -> dict[Path, set[str]]:
        return build_preserved_dirty_path_map(root_repo_path, entries)

    def resolve_commit_variants_for_refs(self, repo_path: Path, refs: Iterable[str]) -> list[str]:
        return resolve_commit_variants_for_refs(repo_path, refs, ManualRepositoryOps(self.manual_module))

    def check_conflict_tool(self, conflict_tool_command: str) -> ToolAvailabilityResult:
        """Check whether the conflict tool command is available on the system.

        Extracts the tool name from the command string (first token before
        any flags) and checks it with :func:`check_tool_availability`.
        """
        tool_name = conflict_tool_command.strip().split()[0] if conflict_tool_command.strip() else ""
        if not tool_name:
            return ToolAvailabilityResult(
                tool_name="",
                available=False,
                resolved_path=None,
                error_message="Conflict tool command is empty.",
            )
        return check_tool_availability(tool_name)
