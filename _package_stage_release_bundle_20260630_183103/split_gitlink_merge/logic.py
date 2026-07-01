"""Pure domain logic for recursive gitlink merges."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model import Ancestry, Conflict, GitlinkMergeContext, GitlinkMergeError


@dataclass(frozen=True)
class ConflictGroups:
    gitlinks: tuple[Conflict, ...]
    non_gitlinks: tuple[Conflict, ...]

    @property
    def has_gitlinks(self) -> bool:
        return bool(self.gitlinks)

    @property
    def has_non_gitlinks(self) -> bool:
        return bool(self.non_gitlinks)


@dataclass(frozen=True)
class GitlinkConflictSpec:
    parent_repo: Path
    path: str
    submodule_repo: Path
    base_sha: str | None
    ours_sha: str
    theirs_sha: str


class GitlinkMergeLogic:
    """Pure logic that does not execute Git commands."""

    def group_conflicts(self, conflicts: list[Conflict]) -> ConflictGroups:
        gitlinks = tuple(conflict for conflict in conflicts if conflict.kind == "gitlink")
        non_gitlinks = tuple(conflict for conflict in conflicts if conflict.kind != "gitlink")
        return ConflictGroups(gitlinks=gitlinks, non_gitlinks=non_gitlinks)

    def build_gitlink_spec(self, parent_repo: Path, conflict: Conflict) -> GitlinkConflictSpec:
        parent = parent_repo.resolve(strict=False)
        path = conflict.path
        base = conflict.stage_sha("1")
        ours = conflict.stage_sha("2")
        theirs = conflict.stage_sha("3")
        if not ours or not theirs:
            raise GitlinkMergeError(f"cannot resolve ours/theirs gitlink SHAs for {parent}:{path}")
        return GitlinkConflictSpec(
            parent_repo=parent,
            path=path,
            submodule_repo=(parent / path).resolve(strict=False),
            base_sha=base,
            ours_sha=ours,
            theirs_sha=theirs,
        )

    def needs_ancestry_lookup(self, spec: GitlinkConflictSpec) -> bool:
        if spec.ours_sha == spec.theirs_sha:
            return False
        if spec.base_sha and (spec.ours_sha == spec.base_sha or spec.theirs_sha == spec.base_sha):
            return False
        return True

    def ancestry_for_decision(
        self,
        spec: GitlinkConflictSpec,
        ancestry: Ancestry | None,
    ) -> Ancestry:
        if self.needs_ancestry_lookup(spec):
            return ancestry or Ancestry()
        return Ancestry()

    def build_recursive_context(self, spec: GitlinkConflictSpec, depth: int) -> GitlinkMergeContext:
        return GitlinkMergeContext(
            parent_repo=spec.parent_repo,
            submodule_path=spec.path,
            submodule_repo=spec.submodule_repo,
            base_sha=spec.base_sha,
            ours_sha=spec.ours_sha,
            theirs_sha=spec.theirs_sha,
            depth=depth,
        )

