"""CI report, conflict artifact, and patch artifact boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import artifacts
from . import common


REPORT_ARTIFACT_FUNCTIONS: tuple[str, ...] = ()


@dataclass(frozen=True)
class CiReportArtifactsService:
    """Facade over report/artifact responsibilities from ``ci_merge.py``."""

    ci_module: ModuleType | None

    def __post_init__(self) -> None:
        return None

    def analyze_conflicts(self, ctx: Any, repo_path: Path) -> Any:
        return artifacts.analyze_conflicts(ctx, repo_path, self.ci_module)

    def parse_unmerged_index(self, stdout: str) -> dict[str, list[dict[str, str]]]:
        return common.parse_unmerged_index(stdout)

    def stage_sha(self, conflict: dict[str, Any], stage_number: str) -> str | None:
        return common.stage_sha(conflict, stage_number)

    def parse_commit_log(self, stdout: str) -> list[dict[str, str]]:
        return common.parse_commit_log(stdout)

    def parse_name_status(self, stdout: str) -> list[dict[str, str]]:
        return common.parse_name_status(stdout)

    def sanitize_patch_name(self, value: str, fallback: str) -> str:
        return common.sanitize_patch_name(value, fallback)

    def export_conflict_artifacts(self, ctx: Any, repo_path: Path, conflict: dict[str, Any]) -> dict[str, Any]:
        return artifacts.export_conflict_artifacts(ctx, repo_path, conflict)

    def export_repository_patches(
        self,
        ctx: Any,
        repo_path: Path,
        display_path: str,
        ours_sha: str,
        result_sha: str,
        changed_files: list[dict[str, str]],
        warnings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return artifacts.export_repository_patches(
            ctx,
            repo_path,
            display_path,
            ours_sha,
            result_sha,
            changed_files,
            warnings,
        )

    def export_changed_file_artifacts(
        self,
        ctx: Any,
        repo_path: Path,
        display_path: str,
        ours_sha: str,
        result_sha: str,
        changed_files: list[dict[str, str]],
        warnings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return artifacts.export_changed_file_artifacts(
            ctx,
            repo_path,
            display_path,
            ours_sha,
            result_sha,
            changed_files,
            warnings,
        )

    def collect_repository_details(
        self,
        ctx: Any,
        repo_path: Path,
        display_path: str,
        base_sha: str | None,
        target_sha: str,
        source_sha: str,
        result_sha: str,
        strategy: str,
    ) -> None:
        artifacts.collect_repository_details(
            ctx,
            repo_path,
            display_path,
            base_sha,
            target_sha,
            source_sha,
            result_sha,
            strategy,
        )

    def artifact_category_dir(self, ctx: Any, category: str) -> Path:
        return artifacts.artifact_category_dir(ctx, category)

    def relative_output_path(self, ctx: Any, path: Path) -> str:
        return artifacts.relative_output_path(ctx, path)
