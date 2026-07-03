"""Pure parsing and decision logic for recursive gitlink merges."""

from __future__ import annotations

from pathlib import Path

from .model import (
    Ancestry,
    Conflict,
    ConflictStage,
    GitlinkDecision,
    GitlinkDecisionKind,
)


class GitlinkMergePlanner:
    """Pure planner that never executes Git commands."""

    def parse_unmerged_index(self, stdout: str) -> dict[str, list[ConflictStage]]:
        grouped: dict[str, list[ConflictStage]] = {}
        for line in split_nonempty_lines(stdout):
            parts = line.split(None, 3)
            if len(parts) != 4:
                grouped.setdefault("<unparseable>", []).append(
                    ConflictStage(mode="", sha="", stage="", path="<unparseable>", raw=line)
                )
                continue
            mode, sha, stage, path = parts
            grouped.setdefault(path, []).append(
                ConflictStage(mode=mode, sha=sha, stage=stage, path=path, raw=line)
            )
        return grouped

    def classify_conflicts(
        self,
        repo_path: Path,
        *,
        names_stdout: str,
        unmerged_stdout: str,
    ) -> list[Conflict]:
        repo = repo_path.resolve(strict=False)
        grouped = self.parse_unmerged_index(unmerged_stdout)
        paths = sorted(set(split_nonempty_lines(names_stdout)) | set(grouped))
        conflicts: list[Conflict] = []
        for path in paths:
            stages = tuple(grouped.get(path, []))
            stage_numbers = {stage.stage for stage in stages}
            modes = {stage.mode for stage in stages}
            kind = "file"
            if path == ".gitmodules":
                kind = "gitmodules"
            elif path == "<unparseable>" or not stages:
                kind = "unparseable"
            elif modes == {"160000"} and {"2", "3"}.issubset(stage_numbers):
                kind = "gitlink"
            elif not {"2", "3"}.issubset(stage_numbers):
                kind = "unsupported_stage"
            conflicts.append(Conflict(repo_path=repo, path=path, stages=stages, kind=kind))
        return conflicts

    def choose_gitlink_resolution(
        self,
        *,
        base_sha: str | None,
        ours_sha: str,
        theirs_sha: str,
        ancestry: Ancestry,
    ) -> GitlinkDecision:
        if ours_sha == theirs_sha:
            return GitlinkDecision(GitlinkDecisionKind.SAME_SHA, ours_sha)
        if base_sha and ours_sha == base_sha:
            return GitlinkDecision(GitlinkDecisionKind.USE_THEIRS, theirs_sha)
        if base_sha and theirs_sha == base_sha:
            return GitlinkDecision(GitlinkDecisionKind.USE_OURS, ours_sha)
        if ancestry.ours_is_ancestor_of_theirs:
            return GitlinkDecision(GitlinkDecisionKind.FAST_FORWARD_TO_THEIRS, theirs_sha)
        if ancestry.theirs_is_ancestor_of_ours:
            return GitlinkDecision(GitlinkDecisionKind.ALREADY_CONTAINS_THEIRS, ours_sha)
        return GitlinkDecision(GitlinkDecisionKind.RECURSIVE_MERGE, None, recursive_merge=True)


def split_nonempty_lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]
