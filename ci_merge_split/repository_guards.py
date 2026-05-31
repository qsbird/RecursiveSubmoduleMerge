"""CI repository guard and preflight boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import preflight
from . import recovery
from . import repository_ops


REPOSITORY_GUARD_FUNCTIONS: tuple[str, ...] = ()


@dataclass(frozen=True)
class CiRepositoryGuards:
    """Facade for CI preflight, cleanup, and post-merge invariants."""

    ci_module: ModuleType | None

    def __post_init__(self) -> None:
        return None

    def preflight_check(
        self,
        ctx: Any,
        repo_path: Path,
        source: str | None,
        target: str | None,
        *,
        label: str,
        resolve_refs: bool,
    ) -> Any:
        return preflight.preflight_check(
            ctx,
            repo_path,
            source,
            target,
            label=label,
            resolve_refs=resolve_refs,
            ci_module=self.ci_module,
        )

    def verify_repo_ready(self, ctx: Any, repo_path: Path) -> tuple[bool, list[str]]:
        return repository_ops.verify_repo_ready(ctx, repo_path)

    def clean_and_prepare_repo(self, ctx: Any, repo_path: Path) -> None:
        recovery.clean_and_prepare_repo(ctx, repo_path, self.ci_module)

    def sync_remote_refs(self, ctx: Any, repo_path: Path) -> None:
        preflight.sync_remote_refs(ctx, repo_path, self.ci_module)

    def run_sync_command(
        self,
        ctx: Any,
        repo_path: Path,
        args: list[str],
        reason: str,
        message: str,
    ) -> Any:
        return preflight.run_sync_command(ctx, repo_path, args, reason, message, self.ci_module)

    def record_preflight(self, ctx: Any, label: str, result: Any) -> None:
        preflight.record_preflight(ctx, label, result)

    def preflight_failure_reason(self, result: Any) -> str:
        return preflight.preflight_failure_reason(result)

    def ensure_clean_after_merge(self, ctx: Any, repo_path: Path) -> None:
        self.ensure_no_unmerged_paths(ctx, repo_path)
        self.ensure_gitmodules_not_changed(ctx, repo_path)
        self.ensure_no_submodule_topology_change(ctx, repo_path)

    def ensure_no_unmerged_paths(self, ctx: Any, repo_path: Path) -> None:
        repository_ops.ensure_no_unmerged_paths(ctx, repo_path, self.ci_module)

    def ensure_gitmodules_not_changed(self, ctx: Any, repo_path: Path) -> None:
        repository_ops.ensure_gitmodules_not_changed(ctx, repo_path, self.ci_module)

    def ensure_no_submodule_topology_change(self, ctx: Any, repo_path: Path) -> None:
        repository_ops.ensure_no_submodule_topology_change(ctx, repo_path, self.ci_module)

    def git_path(self, ctx: Any, repo_path: Path, name: str) -> Path | None:
        return repository_ops.git_path(ctx, repo_path, name)

    def operation_markers(self, ctx: Any, repo_path: Path) -> list[dict[str, str]]:
        return repository_ops.operation_markers(ctx, repo_path)

    def repository_is_clean(self, ctx: Any, repo_path: Path) -> tuple[bool, str]:
        return repository_ops.repository_is_clean(ctx, repo_path)

    def get_submodule_paths(self, ctx: Any, repo_path: Path) -> list[str]:
        return repository_ops.get_submodule_paths(ctx, repo_path)

    def current_head(self, ctx: Any, repo_path: Path) -> str:
        return repository_ops.current_head(ctx, repo_path)

    def current_branch(self, ctx: Any, repo_path: Path) -> str | None:
        return repository_ops.current_branch(ctx, repo_path)

    def display_path_for(self, ctx: Any, repo_path: Path) -> str:
        return repository_ops.display_path_for(ctx, repo_path)

    def remote_first_ref(self, ref: str, remote: str) -> tuple[str, str]:
        return repository_ops.remote_first_ref(ref, remote)

    def resolve_commit_ref(self, ctx: Any, repo_path: Path, ref: str) -> Any:
        return repository_ops.resolve_commit_ref(ctx, repo_path, ref, self.ci_module)
