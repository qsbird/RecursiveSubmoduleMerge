"""CI snapshot, restore, and cleanup boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import recovery


SNAPSHOT_RESTORE_FUNCTIONS: tuple[str, ...] = ()


@dataclass(frozen=True)
class CiSnapshotRestoreService:
    """Facade for snapshot capture and failure recovery."""

    ci_module: ModuleType | None

    def __post_init__(self) -> None:
        return None

    def record_snapshot(self, ctx: Any, repo_path: Path, source_sha: str, target_sha: str, depth: int) -> dict[str, Any]:
        return recovery.record_snapshot(ctx, repo_path, source_sha, target_sha, depth)

    def restore_repo_to_snapshot(self, ctx: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
        return recovery.restore_repo_to_snapshot(ctx, snapshot, self.ci_module)

    def restore_all_snapshots(self, ctx: Any) -> bool:
        return recovery.restore_all_snapshots(ctx, self.ci_module)

    def run_cleanup_steps(
        self,
        ctx: Any,
        repo_path: Path,
        level_name: str,
        steps: list[dict[str, Any]],
        *,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return recovery.run_cleanup_steps(ctx, repo_path, level_name, steps, scope=scope)

    def clean_and_prepare_repo(
        self,
        ctx: Any,
        repo_path: Path,
        *,
        scope: dict[str, Any] | None = None,
    ) -> None:
        recovery.clean_and_prepare_repo(ctx, repo_path, self.ci_module, scope=scope)
