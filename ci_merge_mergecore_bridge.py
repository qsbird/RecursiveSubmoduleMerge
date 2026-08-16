#!/usr/bin/env python3
"""Run external ``ci_merge.py`` with merge_repo routed through split_gitlink_merge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ci_merge_split import CiWorkflowServices
from ci_merge_split.legacy import load_ci_merge_module
from merge_tool_adapters.ci_auto import CiAutoGitlinkMergeService
from split_gitlink_merge import GitlinkMergeError, NonGitlinkConflictError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validation bridge for exercising the external ci_merge module against mergecore.",
    )
    parser.add_argument(
        "--ci-project",
        dest="ci_project",
        help="Path to the external CI merge project. Overrides MERGETOOL_CI_LEGACY_PROJECT.",
    )
    return parser


def _conflict_details(services: CiWorkflowServices, ctx: Any, repo_path: Path) -> list[dict[str, Any]]:
    try:
        return services.report_artifacts.analyze_conflicts(ctx, repo_path).conflicts
    except Exception as exc:  # noqa: BLE001 - bridge must preserve the original failure.
        return [{"repo_path": str(repo_path), "path": None, "type": "bridge_conflict_analysis_failed", "error": str(exc)}]


def _map_non_gitlink_error(
    services: CiWorkflowServices,
    ci_merge: Any,
    ctx: Any,
    root_repo: Path,
    exc: NonGitlinkConflictError,
) -> Any:
    conflict_repo = exc.repo_path
    conflicts = _conflict_details(services, ctx, conflict_repo)
    has_gitlink = any(getattr(conflict, "kind", "") == "gitlink" for conflict in exc.conflicts)
    exit_code = (
        ci_merge.ExitCode.ROOT_CONFLICT
        if conflict_repo.resolve(strict=False) == root_repo.resolve(strict=False)
        else ci_merge.ExitCode.SUBMODULE_CONFLICT
    )
    return ci_merge.CiMergeError(
        exit_code,
        ci_merge.Phase.ANALYZING_CONFLICTS,
        "MERGE_MIXED_CONFLICT" if has_gitlink else "MERGE_NON_GITLINK_CONFLICT",
        "merge contains conflicts that are not pure submodule gitlinks",
        details={"repo_path": str(conflict_repo), "conflicts": conflicts},
        restore_needed=True,
    )


def _merge_repo_with_mergecore(ci_merge: Any, services: CiWorkflowServices):
    def merge_repo_with_mergecore(
        ctx: Any,
        repo_path: Path,
        *,
        source_sha: str,
        target_sha: str,
        base_sha: str | None,
        depth: int,
        display_path: str,
        gitlink_context: Any | None = None,
    ) -> Any:
        del gitlink_context
        ctx.report.data["merge_attempted"] = True
        ctx.set_state(ci_merge.Phase.MERGING_ROOT if depth == 0 else ci_merge.Phase.MERGING_SUBMODULE)
        try:
            result = CiAutoGitlinkMergeService.from_ci_context(ctx, ci_merge).merge_repo(
                repo_path,
                source_sha=source_sha,
                target_sha=target_sha,
                base_sha=base_sha,
                depth=depth,
                display_path=display_path,
            )
        except NonGitlinkConflictError as exc:
            raise _map_non_gitlink_error(services, ci_merge, ctx, repo_path, exc) from exc
        except GitlinkMergeError as exc:
            raise ci_merge.CiMergeError(
                ci_merge.ExitCode.SUBMODULE_CONFLICT if depth else ci_merge.ExitCode.GIT_COMMAND_FAILED,
                ci_merge.Phase.MERGING_SUBMODULE if depth else ci_merge.Phase.MERGING_ROOT,
                "MERGECORE_FAILED",
                str(exc),
                restore_needed=True,
            ) from exc
        return ci_merge.MergeResult(
            result.repo_path,
            result.display_path,
            result.base_sha,
            result.source_sha,
            result.target_sha,
            result.result_sha,
            result.strategy,
        )

    return merge_repo_with_mergecore


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    parsed, passthrough = parser.parse_known_args(argv)
    ci_merge = load_ci_merge_module(parsed.ci_project)
    services = CiWorkflowServices.from_module(ci_merge)
    ci_merge.merge_repo = _merge_repo_with_mergecore(ci_merge, services)
    ci_merge.run_publish_only_workflow = services.publish.run_publish_only_workflow
    return ci_merge.main(passthrough)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
