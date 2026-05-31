from __future__ import annotations

import unittest
from pathlib import Path

from split_gitlink_merge import Ancestry, GitlinkDecisionKind, GitlinkMergePlanner


class GitlinkMergePlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = GitlinkMergePlanner()

    def test_parse_and_classify_gitlink_conflict(self) -> None:
        unmerged = "\n".join(
            [
                "160000 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1\tlibs/core",
                "160000 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 2\tlibs/core",
                "160000 cccccccccccccccccccccccccccccccccccccccc 3\tlibs/core",
            ]
        )

        conflicts = self.planner.classify_conflicts(
            Path("repo"),
            names_stdout="libs/core\n",
            unmerged_stdout=unmerged,
        )

        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict.kind, "gitlink")
        self.assertEqual(conflict.stage_sha("1"), "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(conflict.stage_sha("2"), "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        self.assertEqual(conflict.stage_sha("3"), "cccccccccccccccccccccccccccccccccccccccc")

    def test_classify_file_conflict(self) -> None:
        unmerged = "\n".join(
            [
                "100644 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1\tREADME.md",
                "100644 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 2\tREADME.md",
                "100644 cccccccccccccccccccccccccccccccccccccccc 3\tREADME.md",
            ]
        )

        conflicts = self.planner.classify_conflicts(
            Path("repo"),
            names_stdout="README.md\n",
            unmerged_stdout=unmerged,
        )

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].kind, "file")

    def test_choose_resolution_same_sha(self) -> None:
        decision = self.planner.choose_gitlink_resolution(
            base_sha="a",
            ours_sha="b",
            theirs_sha="b",
            ancestry=Ancestry(),
        )

        self.assertEqual(decision.kind, GitlinkDecisionKind.SAME_SHA)
        self.assertEqual(decision.result_sha, "b")
        self.assertFalse(decision.recursive_merge)

    def test_choose_resolution_use_theirs_when_ours_is_base(self) -> None:
        decision = self.planner.choose_gitlink_resolution(
            base_sha="a",
            ours_sha="a",
            theirs_sha="c",
            ancestry=Ancestry(),
        )

        self.assertEqual(decision.kind, GitlinkDecisionKind.USE_THEIRS)
        self.assertEqual(decision.result_sha, "c")

    def test_choose_resolution_fast_forward_to_theirs(self) -> None:
        decision = self.planner.choose_gitlink_resolution(
            base_sha="a",
            ours_sha="b",
            theirs_sha="c",
            ancestry=Ancestry(ours_is_ancestor_of_theirs=True),
        )

        self.assertEqual(decision.kind, GitlinkDecisionKind.FAST_FORWARD_TO_THEIRS)
        self.assertEqual(decision.result_sha, "c")

    def test_choose_resolution_recursive_merge(self) -> None:
        decision = self.planner.choose_gitlink_resolution(
            base_sha="a",
            ours_sha="b",
            theirs_sha="c",
            ancestry=Ancestry(),
        )

        self.assertEqual(decision.kind, GitlinkDecisionKind.RECURSIVE_MERGE)
        self.assertIsNone(decision.result_sha)
        self.assertTrue(decision.recursive_merge)


if __name__ == "__main__":
    unittest.main()
