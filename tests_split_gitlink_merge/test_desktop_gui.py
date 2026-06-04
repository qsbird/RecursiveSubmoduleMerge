from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from manual_merge_split import runtime as manual_runtime
from manual_merge_split.window_icon import apply_window_icon

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

    def test_build_merge_argv_supports_continue_current_branch(self) -> None:
        argv = desktop_gui.build_merge_argv(
            {
                "repo": "repo",
                "merge_branches": "refs/changes/1",
                "continue_current_branch": True,
                "no_prompt_dirty_submodules": True,
            }
        )

        self.assertIn("--continue-current-branch", argv)
        self.assertNotIn("--base-remote-branch", argv)
        self.assertNotIn("--work-branch", argv)
        self.assertIn("--conflict-tool-command", argv)
        self.assertNotIn("--enable-cross-submodule-moves", argv)
        parsed = manual_runtime.parse_args(argv)
        self.assertEqual(parsed.file_conflict_mode, "tool")
        self.assertFalse(parsed.enable_cross_submodule_moves)

    def test_build_merge_argv_defaults_to_non_prompting_dirty_submodule_guard(self) -> None:
        argv = desktop_gui.build_merge_argv(
            {
                "repo": "repo",
                "merge_branches": "feature",
            }
        )

        self.assertNotIn("--allow-dirty-submodules", argv)
        self.assertIn("--no-prompt-dirty-submodules", argv)

    def test_friendly_output_summarizes_confirmation_and_timeout(self) -> None:
        content = (
            "Manual confirmation required after conflict tool closes: C:/repo/file.txt\n"
            "file.txt: conflict tool timed out after 1800 seconds\n"
            "[STDOUT]\n"
            "[EXIT] 0\n"
        )

        friendly = desktop_gui.format_friendly_output(content)

        self.assertIn("Conflict tool closed", friendly)
        self.assertIn("Conflict tool timed out", friendly)
        self.assertIn("exit code 0", friendly)
        self.assertNotIn("[STDOUT]", friendly)

    def test_friendly_output_supports_chinese_language(self) -> None:
        friendly = desktop_gui.format_friendly_output("$ git status\n[EXIT] 1\n", desktop_gui.LANGUAGE_ZH)

        self.assertIn("命令：git status", friendly)
        self.assertIn("退出码 1", friendly)

    def test_ui_text_entries_have_chinese_and_english(self) -> None:
        for key, translations in desktop_gui.UI_TEXT.items():
            with self.subTest(key=key):
                self.assertIn(desktop_gui.LANGUAGE_EN, translations)
                self.assertIn(desktop_gui.LANGUAGE_ZH, translations)
                self.assertTrue(translations[desktop_gui.LANGUAGE_EN])
                self.assertTrue(translations[desktop_gui.LANGUAGE_ZH])

    def test_build_merge_argv_rejects_removed_preserve_mode(self) -> None:
        with self.assertRaises(desktop_gui.GuiInputError) as raised:
            desktop_gui.build_merge_argv(
                {
                    "repo": "repo",
                    "merge_branches": "refs/changes/1",
                    "continue_current_branch": True,
                    "file_conflict_mode": "preserve",
                    "no_prompt_dirty_submodules": True,
                }
            )

        self.assertIn("removed", str(raised.exception))

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

    def test_build_clone_spec_uses_remote_head_when_branch_is_empty(self) -> None:
        spec = desktop_gui.build_clone_spec(
            {
                "clone_url": "git@example.com:org/demo.git",
                "clone_destination": r"C:\work",
                "clone_directory_name": "demo",
                "clone_branch": "",
            }
        )

        self.assertEqual(spec.command[:5], ["git", "-c", "protocol.file.allow=always", "clone", "-v"])
        self.assertNotIn("--branch", spec.command)
        self.assertEqual(spec.command[-2], "git@example.com:org/demo.git")
        self.assertTrue(str(spec.target_path).replace("\\", "/").endswith("/work/demo"))

    def test_build_clone_spec_adds_optional_branch(self) -> None:
        spec = desktop_gui.build_clone_spec(
            {
                "clone_url": "https://example.com/demo.git",
                "clone_destination": r"C:\work",
                "clone_directory_name": "demo",
                "clone_branch": "main",
            }
        )

        self.assertIn("--branch", spec.command)
        branch_index = spec.command.index("--branch")
        self.assertEqual(spec.command[branch_index + 1], "main")

    def test_build_clone_spec_rejects_nested_directory_name(self) -> None:
        with self.assertRaises(desktop_gui.GuiInputError):
            desktop_gui.build_clone_spec(
                {
                    "clone_url": "https://example.com/demo.git",
                    "clone_destination": r"C:\work",
                    "clone_directory_name": r"nested\demo",
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

    def test_build_publish_argv_matches_manual_cli_parser(self) -> None:
        argv = desktop_gui.build_publish_argv(
            {
                "repo": "repo",
                "publish_remote": "origin",
                "publish_target": "validation-work",
                "publish_check_only": True,
                "publish_sync_local_branches": False,
                "publish_allow_branch_suffix": False,
                "verbose": True,
            }
        )
        parsed = manual_runtime.parse_args(argv)

        self.assertEqual(parsed.command, "publish")
        self.assertEqual(parsed.repo, "repo")
        self.assertEqual(parsed.remote, "origin")
        self.assertEqual(parsed.push_target, "validation-work")
        self.assertTrue(parsed.check_only)
        self.assertTrue(parsed.no_sync_local_branches)
        self.assertTrue(parsed.no_branch_suffix)
        self.assertTrue(parsed.verbose)

    def test_build_publish_argv_requires_target(self) -> None:
        with self.assertRaises(desktop_gui.GuiInputError):
            desktop_gui.build_publish_argv(
                {
                    "repo": "repo",
                    "publish_remote": "origin",
                    "publish_target": "",
                }
            )

    def test_publish_target_defaults_to_blank_for_manual_confirmation(self) -> None:
        self.assertEqual(desktop_gui.DEFAULT_PUBLISH_TARGET_BRANCH, "")

    def test_gui_common_values_enable_verbose_without_visible_toggle(self) -> None:
        class FakeVariable:
            def __init__(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        app = object.__new__(desktop_gui.MergeGuiApp)
        app.repo_var = FakeVariable("repo")
        app.report_file_var = FakeVariable("merge_manual_todo.txt")

        values = desktop_gui.MergeGuiApp._common_values(app)

        self.assertTrue(values["verbose"])
        self.assertNotIn("option_verbose_command_output", desktop_gui.UI_TEXT)
        self.assertNotIn("button_preview_command", desktop_gui.UI_TEXT)
        self.assertIn("button_show_command_preview", desktop_gui.UI_TEXT)
        self.assertIn("button_refresh_command_preview", desktop_gui.UI_TEXT)

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

    def test_launcher_command_prefers_packaged_cli_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            gui_exe = Path(temp_dir) / "MergeToolManual.exe"
            cli_exe = Path(temp_dir) / "MergeToolManualCli.exe"
            gui_exe.write_text("", encoding="utf-8")
            cli_exe.write_text("", encoding="utf-8")

            command = desktop_gui.launcher_command(
                ["publish", "--help"],
                executable=str(gui_exe),
                frozen=True,
            )

        self.assertEqual(command, [str(cli_exe), "--cli", "publish", "--help"])

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

    def test_run_git_query_hides_windows_child_console(self) -> None:
        fake_completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="main\n",
            stderr="",
        )
        with mock.patch("recursive_submodule_merge_gui.hidden_subprocess_kwargs", return_value={"creationflags": 1}):
            with mock.patch("recursive_submodule_merge_gui.subprocess.run", return_value=fake_completed) as run:
                output = desktop_gui.run_git_query(str(Path.cwd()), ["status"])

        self.assertIn("main", output)
        self.assertEqual(run.call_args.kwargs["creationflags"], 1)

    def test_window_icon_is_applied_from_embedded_png(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.icon_calls: list[tuple[object, ...]] = []

            def iconphoto(self, *args: object) -> None:
                self.icon_calls.append(args)

        images: list[dict[str, object]] = []

        def fake_photo_image(**kwargs: object) -> dict[str, object]:
            images.append(kwargs)
            return kwargs

        root = FakeRoot()

        self.assertTrue(apply_window_icon(root, photo_image_factory=fake_photo_image))
        self.assertEqual(len(root.icon_calls), 1)
        self.assertEqual(root.icon_calls[0][0], True)
        self.assertIs(root.icon_calls[0][1], images[0])
        self.assertIs(root._merge_tool_window_icon, images[0])
        self.assertIn("data", images[0])

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
        self.assertIn("{merge,cleanup,publish,guided}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
