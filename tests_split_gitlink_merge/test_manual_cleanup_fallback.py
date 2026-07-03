from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manual_merge_split.models import CleanupResult
from manual_merge_split.repository_ops import ManualRepositoryOps


class ManualMergeTodoDetectionTests(unittest.TestCase):
    def test_manual_merge_todo_exists_true(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir(parents=True)
            (root / "merge_manual_todo.txt").write_text("branch: origin/feature\n", encoding="utf-8")

            self.assertTrue(ManualRepositoryOps().manual_merge_todo_exists(root))

    def test_manual_merge_todo_exists_false(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir(parents=True)

            self.assertFalse(ManualRepositoryOps().manual_merge_todo_exists(root))

    def test_manual_merge_todo_custom_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir(parents=True)
            (root / "custom_todo.txt").write_text("branch: origin/feature\n", encoding="utf-8")

            ops = ManualRepositoryOps()
            self.assertFalse(ops.manual_merge_todo_exists(root))
            self.assertTrue(ops.manual_merge_todo_exists(root, "custom_todo.txt"))


class UnifiedCleanupLevelTests(unittest.TestCase):
    def test_cleanup_builds_four_ordered_levels_for_submodules(self) -> None:
        levels = ManualRepositoryOps()._build_cleanup_levels(
            Path("repo"),
            ["child"],
            allow_clean_x=False,
            skip_submodule_update=False,
            is_root=True,
        )

        self.assertEqual(
            [name for name, _steps in levels],
            [
                "level_1_basic",
                "level_2_delete_nested_git",
                "level_3_foreach_abort_and_clean",
                "level_4_deep_submodule_cleanup",
            ],
        )
        self.assertTrue(all(steps for _name, steps in levels))

    def test_cleanup_omits_deep_level_without_submodules(self) -> None:
        levels = ManualRepositoryOps()._build_cleanup_levels(
            Path("repo"),
            [],
            allow_clean_x=False,
            skip_submodule_update=True,
            is_root=True,
        )

        self.assertEqual(len(levels), 3)
        self.assertNotIn("level_4_deep_submodule_cleanup", [name for name, _steps in levels])


class CleanupRepositoryFallbackCompatibilityTests(unittest.TestCase):
    def test_fallback_delegates_to_unified_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir(parents=True)
            ops = ManualRepositoryOps()
            result = CleanupResult()

            with patch.object(ManualRepositoryOps, "cleanup_repository") as cleanup:
                ops.cleanup_repository_fallback(
                    root,
                    result,
                    skip_submodule_update=True,
                    allow_clean_x=True,
                )

            cleanup.assert_called_once()
            self.assertTrue(cleanup.call_args.kwargs["allow_clean_x"])
            self.assertTrue(cleanup.call_args.kwargs["skip_submodule_update"])

    def test_fallback_removes_only_untracked_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir(parents=True)
            report = root / "merge_manual_todo.txt"
            report.write_text("todo\n", encoding="utf-8")
            ops = ManualRepositoryOps()
            result = CleanupResult()

            with (
                patch.object(ManualRepositoryOps, "cleanup_repository"),
                patch.object(ManualRepositoryOps, "_is_path_tracked", return_value=False),
            ):
                ops.cleanup_repository_fallback(root, result)

            self.assertFalse(report.exists())
            self.assertEqual(result.removed_reports, [report])

    def test_fallback_preserves_tracked_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir(parents=True)
            report = root / "merge_manual_todo.txt"
            report.write_text("tracked\n", encoding="utf-8")
            ops = ManualRepositoryOps()
            result = CleanupResult()

            with (
                patch.object(ManualRepositoryOps, "cleanup_repository"),
                patch.object(ManualRepositoryOps, "_is_path_tracked", return_value=True),
            ):
                ops.cleanup_repository_fallback(root, result)

            self.assertTrue(report.exists())
            self.assertEqual(result.removed_reports, [])


if __name__ == "__main__":
    unittest.main()