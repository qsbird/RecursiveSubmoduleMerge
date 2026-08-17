from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manual_merge_split.conflict_classifier import (
    ManualConflictDisposition,
    ManualConflictRecord,
    classify_conflict_records,
)


class ManualConflictClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="manual-classify-")
        self.repo = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_classifies_add_add_regular_file_as_supported_file(self) -> None:
        conflicts = classify_conflict_records(
            self.repo,
            ["conflict.txt"],
            [
                ManualConflictRecord(path="conflict.txt", mode="100644", sha="ours", stage=2),
                ManualConflictRecord(path="conflict.txt", mode="100644", sha="theirs", stage=3),
            ],
            is_binary_path=lambda _repo, _path, _stages: False,
        )

        self.assertEqual(conflicts["conflict.txt"].disposition, ManualConflictDisposition.SUPPORTED_FILE)

    def test_classifies_add_add_gitlink_as_supported_gitlink(self) -> None:
        conflicts = classify_conflict_records(
            self.repo,
            ["sub"],
            [
                ManualConflictRecord(path="sub", mode="160000", sha="ours", stage=2),
                ManualConflictRecord(path="sub", mode="160000", sha="theirs", stage=3),
            ],
            is_binary_path=lambda _repo, _path, _stages: False,
        )

        self.assertEqual(conflicts["sub"].disposition, ManualConflictDisposition.SUPPORTED_GITLINK)

    def test_classifies_modify_delete_as_manual_takeover(self) -> None:
        conflicts = classify_conflict_records(
            self.repo,
            ["conflict.txt"],
            [
                ManualConflictRecord(path="conflict.txt", mode="100644", sha="base", stage=1),
                ManualConflictRecord(path="conflict.txt", mode="100644", sha="ours", stage=2),
            ],
            is_binary_path=lambda _repo, _path, _stages: False,
        )

        self.assertEqual(conflicts["conflict.txt"].disposition, ManualConflictDisposition.MANUAL_TAKEOVER)

    def test_classifies_gitmodules_as_manual_takeover(self) -> None:
        conflicts = classify_conflict_records(
            self.repo,
            [".gitmodules"],
            [
                ManualConflictRecord(path=".gitmodules", mode="100644", sha="ours", stage=2),
                ManualConflictRecord(path=".gitmodules", mode="100644", sha="theirs", stage=3),
            ],
            is_binary_path=lambda _repo, _path, _stages: False,
        )

        self.assertEqual(conflicts[".gitmodules"].disposition, ManualConflictDisposition.MANUAL_TAKEOVER)

    def test_classifies_unparseable_stage_as_fatal(self) -> None:
        conflicts = classify_conflict_records(
            self.repo,
            ["conflict.txt"],
            [
                ManualConflictRecord(path="conflict.txt", mode="100644", sha="ours", stage=2),
                ManualConflictRecord(path="conflict.txt", mode="100644", sha="dupe", stage=2),
            ],
            is_binary_path=lambda _repo, _path, _stages: False,
        )

        self.assertEqual(conflicts["conflict.txt"].disposition, ManualConflictDisposition.FATAL)


if __name__ == "__main__":
    unittest.main()
