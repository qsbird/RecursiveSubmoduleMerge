from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from manual_merge_split.repository_ops import ManualRepositoryOps
from merge_cleanup_core.steps import (
    CleanupLevel,
    CleanupStep,
    ci_prepare_cleanup_levels,
)


class ManualMergeTodoDetectionTests(unittest.TestCase):
    """Tests for detecting manual_merge_todo files that indicate interrupted merge state."""

    def test_manual_merge_todo_exists_true(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            (root / ".git").mkdir(parents=True)
            todo_file = root / "merge_manual_todo.txt"
            todo_file.write_text("branch: origin/feature\n", encoding="utf-8")

            ops = ManualRepositoryOps()
            self.assertTrue(ops.manual_merge_todo_exists(root))

    def test_manual_merge_todo_exists_false(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            (root / ".git").mkdir(parents=True)

            ops = ManualRepositoryOps()
            self.assertFalse(ops.manual_merge_todo_exists(root))

    def test_manual_merge_todo_custom_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            (root / ".git").mkdir(parents=True)
            custom_todo = root / "custom_todo.txt"
            custom_todo.write_text("branch: origin/feature\n", encoding="utf-8")

            ops = ManualRepositoryOps()
            self.assertFalse(ops.manual_merge_todo_exists(root, "merge_manual_todo.txt"))
            self.assertTrue(ops.manual_merge_todo_exists(root, "custom_todo.txt"))


class CleanupFallbackUsesCILevelsTests(unittest.TestCase):
    """Tests verifying that fallback cleanup reuses CI cleanup levels (shared logic)."""

    def test_ci_prepare_cleanup_levels_returns_three_levels(self) -> None:
        levels = ci_prepare_cleanup_levels(allow_clean_x=False)
        self.assertEqual(len(levels), 3)
        for level in levels:
            self.assertIsInstance(level, CleanupLevel)
            self.assertIsInstance(level.steps, tuple)
            self.assertTrue(len(level.steps) > 0)

    def test_ci_level_names_are_ordered(self) -> None:
        levels = ci_prepare_cleanup_levels(allow_clean_x=False)
        names = [level.name for level in levels]
        self.assertEqual(names[0], "level_1_basic_cleanup")
        self.assertEqual(names[1], "level_2_abort_and_recursive_cleanup")
        self.assertEqual(names[2], "level_3_sync_and_force_update")

    def test_ci_levels_include_reset_hard(self) -> None:
        """CI levels must include reset --hard to clean tracked file deletions."""
        levels = ci_prepare_cleanup_levels(allow_clean_x=False)
        all_args = []
        for level in levels:
            for step in level.steps:
                all_args.extend(step.args)
        self.assertIn("reset", all_args)
        self.assertIn("--hard", all_args)

    def test_ci_levels_include_clean_step(self) -> None:
        """CI levels must include clean -fd to remove untracked files (including untracked report files)."""
        levels = ci_prepare_cleanup_levels(allow_clean_x=False)
        all_args = []
        for level in levels:
            for step in level.steps:
                all_args.extend(step.args)
        self.assertIn("clean", all_args)


class CleanupRepositoryFallbackTests(unittest.TestCase):
    """Tests for the generic fallback cleanup method."""

    def test_cleanup_repository_fallback_exists(self) -> None:
        ops = ManualRepositoryOps()
        self.assertTrue(hasattr(ops, "cleanup_repository_fallback"))
        self.assertTrue(callable(ops.cleanup_repository_fallback))

    def test_has_any_in_progress_state_exists(self) -> None:
        ops = ManualRepositoryOps()
        self.assertTrue(hasattr(ops, "has_any_in_progress_state"))
        self.assertTrue(callable(ops.has_any_in_progress_state))

    def test_is_path_tracked_exists(self) -> None:
        ops = ManualRepositoryOps()
        self.assertTrue(hasattr(ops, "_is_path_tracked"))
        self.assertTrue(callable(ops._is_path_tracked))

    def test_fallback_uses_ci_levels_not_manual_levels(self) -> None:
        """Verify that cleanup_repository_fallback imports ci_prepare_cleanup_levels,
        not the manual-specific manual_prepare_cleanup_levels."""
        import inspect
        source = inspect.getsource(ManualRepositoryOps.cleanup_repository_fallback)
        self.assertIn("ci_prepare_cleanup_levels", source)
        self.assertNotIn("manual_prepare_cleanup_levels", source)

    def test_fallback_removes_untracked_report_after_levels(self) -> None:
        """Verify that untracked report files are removed after all cleanup levels run,
        and tracked report files are left to reset --hard."""
        import inspect
        source = inspect.getsource(ManualRepositoryOps.cleanup_repository_fallback)
        # Should check _is_path_tracked before unlinking
        self.assertIn("_is_path_tracked", source)
        # Should NOT unlink inside the level loop
        self.assertNotIn("report_path.unlink()", source.split("# After all levels")[0])

    def test_fallback_trigger_is_generic_not_file_specific(self) -> None:
        """Verify that run_cleanup uses has_any_in_progress_state (generic detection)
        instead of manual_merge_todo_exists (file-specific detection)."""
        from manual_merge_split import commands
        import inspect
        source = inspect.getsource(commands.run_cleanup)
        self.assertIn("has_any_in_progress_state", source)
        self.assertNotIn("manual_merge_todo_exists", source)


if __name__ == "__main__":
    unittest.main()