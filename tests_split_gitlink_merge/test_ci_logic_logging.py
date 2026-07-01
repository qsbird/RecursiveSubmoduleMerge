from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_merge_split import runtime as ci_runtime
from merge_tool_adapters.ci_auto import CiAutoMergePolicy
from split_gitlink_merge import MergeEvent


class CiLogicLoggingTests(unittest.TestCase):
    def test_json_report_writes_logic_event_to_merge_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            output = root / "out"
            repo.mkdir()
            args = ci_runtime.parse_arguments(
                [
                    "--source",
                    "feature",
                    "--target",
                    "main",
                    "--repo_path",
                    str(repo),
                    "--output_dir",
                    str(output),
                    "--job_id",
                    "job-42",
                ]
            )
            ci_runtime.normalize_args(args, "job-42")
            report = ci_runtime.JsonReport(args, "job-42", *ci_runtime.resolve_output_paths(args, "job-42"))

            report.add_logic_event(
                "ci.merge.start",
                "starting merge",
                repo_path=repo,
                details={"display_path": ".", "submodule": Path("libs/core")},
            )

            log_text = report.log_path.read_text(encoding="utf-8")
            self.assertIn("LOGIC ci.merge.start", log_text)
            self.assertIn("starting merge", log_text)
            self.assertIn(str(Path("libs/core")).replace("\\", "\\\\"), log_text)

    def test_ci_policy_logs_core_merge_events(self) -> None:
        class FakeReport:
            def __init__(self) -> None:
                self.events: list[tuple[str, str, Path | str | None, dict[str, object]]] = []

            def add_logic_event(
                self,
                name: str,
                message: str,
                *,
                repo_path: Path | str | None = None,
                details: dict[str, object] | None = None,
            ) -> None:
                self.events.append((name, message, repo_path, details or {}))

        class FakeContext:
            def __init__(self) -> None:
                self.report = FakeReport()

        ctx = FakeContext()
        policy = CiAutoMergePolicy(ctx=ctx)
        policy.on_event(
            MergeEvent(
                name="gitlink.decision",
                repo_path=Path("repo"),
                message="selected recursive_merge for gitlink sub",
                data={"path": "sub", "strategy": "recursive_merge"},
            )
        )

        self.assertEqual(ctx.report.events[0][0], "gitlink.decision")
        self.assertEqual(ctx.report.events[0][3]["strategy"], "recursive_merge")


if __name__ == "__main__":
    unittest.main()
