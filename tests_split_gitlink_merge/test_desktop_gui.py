from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from manual_merge_split import runtime as manual_runtime

import recursive_submodule_merge_gui as desktop_gui


class DesktopGuiCommandBuilderTests(unittest.TestCase):
    def test_build_merge_argv_matches_manual_cli_parser(self) -> None:
        argv = desktop_gui.build_merge_argv(
            {
                "repo": r"C:\work\repo",
                "merge_branches": "feature\norigin/hotfix",
                "base_remote_branch": "origin/main",
                "work_branch": "validation-work",
                "allow_dirty_submodules": True,
                "no_prompt_dirty_submodules": True,
                "enable_cross_submodule_moves": True,
                "report_file": "merge_manual_todo.txt",
                "ignore_root_untracked_paths": "scratch.txt\nlogs",
                "file_conflict_mode": "tool",
                "conflict_tool_command": 'code --wait "{file}"',
                "conflict_tool_timeout_seconds": "0",
                "verbose": True,
            }
        )

        parsed = manual_runtime.parse_args(argv)

        self.assertEqual(parsed.command, "merge")
        self.assertEqual(parsed.merge_branches, ["feature", "origin/hotfix"])
        self.assertEqual(parsed.repo, r"C:\work\repo")
        self.assertEqual(parsed.base_remote_branch, "origin/main")
        self.assertEqual(parsed.work_branch, "validation-work")
        self.assertTrue(parsed.allow_dirty_submodules)
        self.assertTrue(parsed.no_prompt_dirty_submodules)
        self.assertTrue(parsed.enable_cross_submodule_moves)
        self.assertEqual(parsed.ignore_root_untracked_path, ["scratch.txt", "logs"])
        self.assertEqual(parsed.file_conflict_mode, "tool")
        self.assertEqual(parsed.conflict_tool_timeout_seconds, 0)
        self.assertFalse(parsed.no_confirm_after_conflict_tool)
        self.assertTrue(parsed.verbose)

    def test_build_merge_argv_supports_continue_current_branch_and_preserve_mode(self) -> None:
        argv = desktop_gui.build_merge_argv(
            {
                "repo": "repo",
                "merge_branches": "refs/changes/1",
                "continue_current_branch": True,
                "file_conflict_mode": "preserve",
                "no_prompt_dirty_submodules": True,
            }
        )

        self.assertIn("--continue-current-branch", argv)
        self.assertNotIn("--base-remote-branch", argv)
        self.assertNotIn("--work-branch", argv)
        self.assertNotIn("--conflict-tool-command", argv)
        self.assertNotIn("--enable-cross-submodule-moves", argv)
        parsed = manual_runtime.parse_args(argv)
        self.assertEqual(parsed.file_conflict_mode, "preserve")
        self.assertFalse(parsed.enable_cross_submodule_moves)

    def test_build_merge_argv_validates_clone_fields(self) -> None:
        with self.assertRaises(desktop_gui.GuiInputError):
            desktop_gui.build_merge_argv(
                {
                    "repo": "repo",
                    "merge_branches": "feature",
                    "clone_enabled": True,
                    "clone_branch": "main",
                    "clone_url": "",
                    "clone_path": "target",
                }
            )

    def test_build_cleanup_argv_matches_manual_cli_parser(self) -> None:
        argv = desktop_gui.build_cleanup_argv(
            {
                "repo": "repo",
                "report_file": "merge_manual_todo.txt",
                "skip_submodule_update": True,
                "rebuild_work_branch": True,
                "cleanup_base_remote_branch": "origin/main",
                "cleanup_work_branch": "validation-work",
                "verbose": True,
            }
        )
        parsed = manual_runtime.parse_args(argv)

        self.assertEqual(parsed.command, "cleanup")
        self.assertEqual(parsed.repo, "repo")
        self.assertTrue(parsed.skip_submodule_update)
        self.assertTrue(parsed.rebuild_work_branch)
        self.assertEqual(parsed.base_remote_branch, "origin/main")
        self.assertEqual(parsed.work_branch, "validation-work")
        self.assertTrue(parsed.verbose)

    def test_launcher_command_supports_source_and_frozen_modes(self) -> None:
        source = Path(r"C:\repo\recursive_submodule_merge_gui.py")
        source_command = desktop_gui.launcher_command(
            ["merge", "feature"],
            executable="python",
            source_file=source,
            frozen=False,
        )
        frozen_command = desktop_gui.launcher_command(
            ["cleanup"],
            executable=r"C:\dist\merge-gui.exe",
            frozen=True,
        )

        self.assertEqual(source_command, ["python", str(source), "--cli", "merge", "feature"])
        self.assertEqual(frozen_command, [r"C:\dist\merge-gui.exe", "--cli", "cleanup"])

    def test_safe_directory_value_uses_git_friendly_slashes(self) -> None:
        safe_value = desktop_gui.safe_directory_value(
            r"C:\Users\Qs\Documents\MergeTool_for_Manual\manual_local_execution_validation_run_20260530_141247\repo"
        )

        self.assertNotIn("\\", safe_value)
        self.assertIn("manual_local_execution_validation_run_20260530_141247/repo", safe_value)

    def test_git_safe_directory_env_appends_process_local_config(self) -> None:
        with mock.patch.dict(
            desktop_gui.os.environ,
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "Tester"},
            clear=True,
        ):
            env = desktop_gui.git_safe_directory_env(r"C:\repo")

        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "safe.directory")
        self.assertNotIn("\\", env["GIT_CONFIG_VALUE_1"])

    def test_discover_repositories_finds_validation_repo_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".git").mkdir()
            repo = root / "manual_demo_run" / "repo"
            (repo / ".git").mkdir(parents=True)

            repositories = desktop_gui.discover_repositories(root)

        self.assertIn(("Source tree", str(root.resolve(strict=False))), repositories)
        self.assertIn((r"manual_demo_run\repo", str(repo.resolve(strict=False))), repositories)

    def test_cli_passthrough_allows_manual_help(self) -> None:
        script = Path(__file__).resolve().parents[1] / "recursive_submodule_merge_gui.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--cli", "--help"],
            cwd=str(script.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("{merge,cleanup,guided}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
