from __future__ import annotations

import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from manual_merge_split import config_persistence, runtime as manual_runtime


class ManualCliParserTests(unittest.TestCase):
    def test_merge_parser_accepts_single_branch_with_new_flag(self) -> None:
        parsed = manual_runtime.parse_args(
            [
                "feature",
                "--repo",
                "repo",
                "--use-current-branch",
                "--file-conflict-mode",
                "tool",
            ]
        )

        self.assertEqual(parsed.command, "merge")
        self.assertEqual(parsed.merge_branches, ["feature"])
        self.assertTrue(parsed.continue_current_branch)

    def test_merge_parser_keeps_hidden_deprecated_alias(self) -> None:
        parsed = manual_runtime.parse_args(
            [
                "feature",
                "--continue-current-branch",
                "--file-conflict-mode",
                "tool",
            ]
        )

        self.assertEqual(parsed.merge_branches, ["feature"])
        self.assertTrue(parsed.continue_current_branch)

    def test_merge_parser_rejects_multiple_branches(self) -> None:
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                manual_runtime.parse_args(["feature", "origin/hotfix", "--file-conflict-mode", "tool"])

    def test_status_dispatch_delegates_when_service_method_exists(self) -> None:
        calls: list[str] = []
        command_service = types.SimpleNamespace(
            run_merge=lambda _args: 91,
            run_cleanup=lambda _args: 92,
            run_publish=lambda _args: 93,
            run_status=lambda args: calls.append(args.repo) or 17,
        )

        result = manual_runtime.main(["status", "--repo", "repo"], command_service=command_service)

        self.assertEqual(result, 17)
        self.assertEqual(calls, ["repo"])

    def test_resume_dispatch_reports_placeholder_when_backend_missing(self) -> None:
        command_service = types.SimpleNamespace(
            run_merge=lambda _args: 91,
            run_cleanup=lambda _args: 92,
            run_publish=lambda _args: 93,
        )
        stderr = io.StringIO()

        with mock.patch("sys.stderr", new=stderr):
            result = manual_runtime.main(["resume", "--repo", "repo"], command_service=command_service)

        self.assertEqual(result, 1)
        self.assertIn("resume", stderr.getvalue())
        self.assertIn("not available", stderr.getvalue())


class ConfigPersistenceTests(unittest.TestCase):
    def test_load_config_migrates_single_legacy_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "merge_tool_last_run.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "last_command": "merge",
                        "merge": {
                            "repo": "repo",
                            "merge_branches": ["origin/feature"],
                            "continue_current_branch": True,
                            "enable_cross_submodule_moves": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("manual_merge_split.config_persistence.config_file_path", return_value=config_path):
                config = config_persistence.load_config()

        self.assertEqual(config.merge.merge_branch, "origin/feature")
        self.assertTrue(config.merge.use_current_branch)

    def test_load_config_does_not_truncate_multi_branch_legacy_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "merge_tool_last_run.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "last_command": "merge",
                        "merge": {
                            "merge_branches": ["origin/feature", "origin/hotfix"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("manual_merge_split.config_persistence.config_file_path", return_value=config_path):
                config = config_persistence.load_config()

        self.assertEqual(config.merge.merge_branch, "")

    def test_save_config_from_args_persists_single_branch_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "merge_tool_last_run.json"
            args = types.SimpleNamespace(
                repo="repo",
                base_remote_branch="origin/main",
                work_branch="work",
                merge_branches=["origin/feature"],
                continue_current_branch=True,
                conflict_tool_command='code --wait "{file}"',
                conflict_tool_timeout_seconds=0,
                verbose=True,
            )

            with mock.patch("manual_merge_split.config_persistence.config_file_path", return_value=config_path):
                config_persistence.save_config_from_args(args, "merge")
                payload = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["merge"]["merge_branch"], "origin/feature")
        self.assertTrue(payload["merge"]["use_current_branch"])
        self.assertNotIn("merge_branches", payload["merge"])
        self.assertNotIn("enable_cross_submodule_moves", payload["merge"])


if __name__ == "__main__":
    unittest.main()
