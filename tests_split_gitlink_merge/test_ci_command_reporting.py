from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from ci_merge_split import command_reporting
from ci_merge_split import commands as ci_commands
from tests_split_gitlink_merge.test_remaining_split_facades import FakeCiCommandResult, FakeCiContext


class CiCommandReportingTests(unittest.TestCase):
    def test_commands_module_reexports_reporting_helpers(self) -> None:
        helper_names = (
            "report_progress",
            "report_warning",
            "set_resolved_fields",
            "batch_step_artifact_scope",
            "command_range",
            "fallback_count",
            "fallback_range",
            "batch_step_cleanup_scope",
            "refresh_batch_step_summaries",
            "serialize_non_gitlink_conflict",
            "record_non_gitlink_conflicts",
            "command_result_to_dict",
            "mark_batch_step_failed",
            "mark_batch_failed",
            "manual_publish_info",
        )

        for name in helper_names:
            self.assertIs(getattr(ci_commands, name), getattr(command_reporting, name))

    def test_reporting_helpers_update_batch_and_report_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = FakeCiContext(Path(temp_dir))

            command_reporting.report_progress(ctx, "batch start")
            self.assertEqual(ctx.report.progress_messages, ["batch start"])

            command_reporting.report_warning(ctx, "WARN_REASON", "warning message", details={"step": 1})
            self.assertEqual(
                ctx.report.data["warnings"],
                [{"reason": "WARN_REASON", "message": "warning message", "details": {"step": 1}}],
            )

            command_reporting.set_resolved_fields(
                ctx,
                source_ref="refs/remotes/origin/source",
                source_sha="sourcesha",
                target_ref="refs/remotes/origin/main",
                target_sha="targetsha",
                merge_base_sha="basesha",
                result_sha="resultsha",
            )
            self.assertEqual(ctx.report.data["resolved"]["result_sha"], "resultsha")

            self.assertEqual(
                command_reporting.batch_step_artifact_scope(ctx, "batch/step-01"),
                {
                    "prefix": "batch/step-01",
                    "patch_dir": str(ctx.report.output_dir / "patches" / "batch/step-01"),
                    "conflict_dir": str(ctx.report.output_dir / "conflicts" / "batch/step-01"),
                    "file_dir": str(ctx.report.output_dir / "files" / "batch/step-01"),
                },
            )

            ctx.report.data["commands"] = [{"cmd": ["git", "status"]}, {"cmd": ["git", "diff"]}]
            self.assertEqual(command_reporting.command_range(ctx, 1), {"start": 1, "end": 1, "count": 1})

            ctx.report.fallbacks.extend([{"level": "one"}, {"level": "two"}])
            self.assertEqual(command_reporting.fallback_count(ctx), 2)
            self.assertEqual(command_reporting.fallback_range(1, 2), {"start": 1, "end": 1, "count": 1})
            self.assertEqual(command_reporting.batch_step_cleanup_scope(2, 3, "source", "target")["preserve_refs"], ["target"])

            command_reporting.record_non_gitlink_conflicts(ctx, [{"path": "file.txt"}])
            self.assertEqual(ctx.report.data["summary"]["conflicts"], 1)

            batch = {
                "steps": [
                    {"index": 1, "status": "success"},
                    {"index": 2, "status": "running"},
                ]
            }
            step_entry = batch["steps"][1]
            failure = {"reason": "FAILED"}
            command_reporting.mark_batch_step_failed(batch, step_entry, 2, failure)
            self.assertEqual(batch["failed_steps"], [2])
            self.assertEqual(batch["successful_steps"], [1])
            self.assertEqual(batch["failure_step"], 2)
            self.assertEqual(batch["errors"], [failure])

            command_reporting.mark_batch_failed(batch)
            self.assertEqual(batch["status"], "failed")

    def test_serialization_and_manual_publish_helpers(self) -> None:
        conflict = types.SimpleNamespace(
            repo_path=Path("/tmp/repo"),
            path="libs/child",
            kind="gitmodules",
            stages=[
                types.SimpleNamespace(mode="160000", sha="abc", stage=2, path="libs/child", raw="raw-data"),
            ],
        )
        self.assertEqual(
            command_reporting.serialize_non_gitlink_conflict(conflict),
            {
                "repo_path": "/tmp/repo",
                "path": "libs/child",
                "type": "gitmodules_conflict",
                "kind": "gitmodules",
                "stages": [
                    {
                        "mode": "160000",
                        "sha": "abc",
                        "stage": 2,
                        "path": "libs/child",
                        "raw": "raw-data",
                    }
                ],
                "action": "manual_required",
            },
        )

        result = FakeCiCommandResult(["status"], stdout="ok\n")
        self.assertEqual(command_reporting.command_result_to_dict(result), result.to_dict())

        plain_result = types.SimpleNamespace(
            args=["status"],
            cwd=Path("/tmp/repo"),
            returncode=3,
            stdout="stdout",
            stderr="stderr",
        )
        self.assertEqual(
            command_reporting.command_result_to_dict(plain_result),
            {
                "args": ["status"],
                "cwd": "/tmp/repo",
                "returncode": 3,
                "stdout": "stdout",
                "stderr": "stderr",
            },
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = FakeCiContext(Path(temp_dir))

            publish_info = command_reporting.manual_publish_info(ctx, "targetsha", "resultsha")
            self.assertEqual(publish_info["target"], "main")
            self.assertTrue(publish_info["supported"])
            self.assertIn("--force-with-lease=main:targetsha", publish_info["command"])

            ctx.args.target = "HEAD"
            ctx.args.publish_target = None
            manual_only = command_reporting.manual_publish_info(ctx, "targetsha", "resultsha")
            self.assertIsNone(manual_only["target"])
            self.assertFalse(manual_only["supported"])
            self.assertIn("--publish_target is required", manual_only["message"])

    def test_report_warning_falls_back_to_report_data_when_needed(self) -> None:
        report = types.SimpleNamespace(data={})
        ctx = types.SimpleNamespace(report=report)

        command_reporting.report_warning(ctx, "WARN_REASON", "warning message")

        warning = report.data["warnings"][0]
        self.assertEqual(warning["reason"], "WARN_REASON")
        self.assertEqual(warning["message"], "warning message")
        self.assertEqual(warning["details"], {})
        self.assertIsInstance(warning["timestamp"], str)


if __name__ == "__main__":
    unittest.main()
