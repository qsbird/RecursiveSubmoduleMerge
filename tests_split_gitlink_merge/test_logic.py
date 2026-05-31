from __future__ import annotations

import unittest
from pathlib import Path

from split_gitlink_merge import (
    Ancestry,
    Conflict,
    ConflictStage,
    GitlinkMergeError,
    GitlinkMergeLogic,
)


def stage(mode: str, sha: str, number: str, path: str) -> ConflictStage:
    return ConflictStage(mode=mode, sha=sha, stage=number, path=path, raw=f"{mode} {sha} {number}\t{path}")


class GitlinkMergeLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logic = GitlinkMergeLogic()

    def test_groups_gitlink_and_non_gitlink_conflicts(self) -> None:
        gitlink = Conflict(
            repo_path=Path("repo"),
            path="libs/core",
            stages=(
                stage("160000", "a" * 40, "1", "libs/core"),
                stage("160000", "b" * 40, "2", "libs/core"),
                stage("160000", "c" * 40, "3", "libs/core"),
            ),
            kind="gitlink",
        )
        file_conflict = Conflict(
            repo_path=Path("repo"),
            path="README.md",
            stages=(
                stage("100644", "a" * 40, "1", "README.md"),
                stage("100644", "b" * 40, "2", "README.md"),
                stage("100644", "c" * 40, "3", "README.md"),
            ),
            kind="file",
        )

        groups = self.logic.group_conflicts([gitlink, file_conflict])

        self.assertEqual(groups.gitlinks, (gitlink,))
        self.assertEqual(groups.non_gitlinks, (file_conflict,))
        self.assertTrue(groups.has_gitlinks)
        self.assertTrue(groups.has_non_gitlinks)

    def test_build_gitlink_spec_extracts_stages_without_git(self) -> None:
        conflict = Conflict(
            repo_path=Path("repo"),
            path="libs/core",
            stages=(
                stage("160000", "a" * 40, "1", "libs/core"),
                stage("160000", "b" * 40, "2", "libs/core"),
                stage("160000", "c" * 40, "3", "libs/core"),
            ),
            kind="gitlink",
        )

        spec = self.logic.build_gitlink_spec(Path("repo"), conflict)

        self.assertEqual(spec.path, "libs/core")
        self.assertEqual(spec.base_sha, "a" * 40)
        self.assertEqual(spec.ours_sha, "b" * 40)
        self.assertEqual(spec.theirs_sha, "c" * 40)
        self.assertEqual(spec.submodule_repo.name, "core")

    def test_build_gitlink_spec_rejects_missing_ours_or_theirs(self) -> None:
        conflict = Conflict(
            repo_path=Path("repo"),
            path="libs/core",
            stages=(stage("160000", "b" * 40, "2", "libs/core"),),
            kind="gitlink",
        )

        with self.assertRaises(GitlinkMergeError):
            self.logic.build_gitlink_spec(Path("repo"), conflict)

    def test_skips_ancestry_lookup_for_same_or_base_cases(self) -> None:
        same = self.logic.build_gitlink_spec(
            Path("repo"),
            Conflict(
                repo_path=Path("repo"),
                path="sub",
                stages=(
                    stage("160000", "a" * 40, "1", "sub"),
                    stage("160000", "b" * 40, "2", "sub"),
                    stage("160000", "b" * 40, "3", "sub"),
                ),
                kind="gitlink",
            ),
        )
        ours_is_base = self.logic.build_gitlink_spec(
            Path("repo"),
            Conflict(
                repo_path=Path("repo"),
                path="sub",
                stages=(
                    stage("160000", "a" * 40, "1", "sub"),
                    stage("160000", "a" * 40, "2", "sub"),
                    stage("160000", "c" * 40, "3", "sub"),
                ),
                kind="gitlink",
            ),
        )

        self.assertFalse(self.logic.needs_ancestry_lookup(same))
        self.assertFalse(self.logic.needs_ancestry_lookup(ours_is_base))
        self.assertEqual(self.logic.ancestry_for_decision(ours_is_base, Ancestry(True, True)), Ancestry())


if __name__ == "__main__":
    unittest.main()

