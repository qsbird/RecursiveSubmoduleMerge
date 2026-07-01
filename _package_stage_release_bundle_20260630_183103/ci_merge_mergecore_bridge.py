#!/usr/bin/env python3
"""Run ci_merge.py with merge_repo routed through split_gitlink_merge.

This is a validation bridge only.  It leaves the original CI script untouched
while letting the original matrix harness exercise the new mergecore adapter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


CI_PROJECT = Path(r"D:\Git\MergeTool_for_CICD")
if str(CI_PROJECT) not in sys.path:
    sys.path.insert(0, str(CI_PROJECT))

import ci_merge  # type: ignore  # noqa: E402
from ci_merge_split import CiWorkflowServices  # noqa: E402
from merge_tool_adapters.ci_auto import CiAutoGitlinkMergeService  # noqa: E402
from split_gitlink_merge import GitlinkMergeError, NonGitlinkConflictError  # noqa: E402


CI_SERVICES = CiWorkflowServices.from_module(ci_merge)


def _conflict_details(ctx: Any, repo_path: Path) -> list[dict[str, Any]]:
    try:
        return CI_SERVICES.report_artifacts.analyze_conflicts(ctx, repo_path).conflicts
    except Exception as exc:  # noqa: BLE001 - bridge must preserve the original failure.
        return [{"repo_path": str(repo_path), "path": None, "type": "bridge_conflict_analysis_failed", "error": str(exc)}]


def _map_non_gitlink_error(
    ctx: Any,
    root_repo: Path,
    exc: NonGitlinkConflictError,
) -> ci_merge.CiMergeError:
    conflict_repo = exc.repo_path
    conflicts = _conflict_details(ctx, conflict_repo)
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
        raise _map_non_gitlink_error(ctx, repo_path, exc) from exc
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


def run_publish_only_workflow_with_split(ctx: Any) -> None:
    CI_SERVICES.publish.run_publish_only_workflow(ctx)


ci_merge.merge_repo = merge_repo_with_mergecore
ci_merge.run_publish_only_workflow = run_publish_only_workflow_with_split


if __name__ == "__main__":
    raise SystemExit(ci_merge.main(sys.argv[1:]))
