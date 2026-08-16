from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from manual_merge_split.commands import process_repository
from manual_merge_split.cross_submodule_moves import CrossSubmoduleMoveService
from manual_merge_split.file_conflicts import ManualFileConflictService
from manual_merge_split.models import MergeContext
from manual_merge_split.repository_ops import ManualRepositoryOps
from tests_split_gitlink_merge.test_adapters import GitlinkFixture, run_git
from tests_split_gitlink_merge import test_engine_integration as engine_fixtures


class ResolvingFileConflictService(ManualFileConflictService):
    def resolve_moved_away_file_conflicts(
        self,
        repo_path: Path,
        file_conflicts: list[str],
        moved_away_paths: set[str],
    ) -> list[str]:
        del repo_path, file_conflicts, moved_away_paths
        return []

    def resolve_file_conflicts(
        self,
        repo_path: Path,
        file_conflicts: list[str],
        context: MergeContext,
    ) -> tuple[list[str], str | None, bool]:
        del context
        for relative_path in file_conflicts:
            (repo_path / relative_path).write_text("resolved\n", encoding="utf-8")
            run_git(repo_path, ["add", "--", relative_path])
        return [], None, False


class ManualCommonCoreIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git executable is not available")
        self.temp_dir = tempfile.TemporaryDirectory(prefix="manual-core-")
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pure_gitlink_merge_uses_common_core_and_keeps_submodule_work_branch(self) -> None:
        fixture = GitlinkFixture(self.base).build(leave_conflicted=False)
        run_git(fixture.parent, ["checkout", "ours"])
        run_git(fixture.parent_sub, ["checkout", fixture.ours_sha])

        context = MergeContext(
            work_branch="manual-merge-work",
            merge_branch="theirs",
            file_conflict_mode="tool",
            conflict_tool_command='code --wait "{file}"',
            conflict_tool_timeout_seconds=0,
        )
        completed = process_repository(
            fixture.parent,
            context,
            root_base_remote_branch=None,
            is_root=True,
            prepare_branch=False,
            ops=ManualRepositoryOps(),
            file_conflicts_service=ManualFileConflictService(),
            cross_submodule_moves_service=CrossSubmoduleMoveService(),
        )

        self.assertTrue(
            completed,
            f"report={context.report_entries!r} status={run_git(fixture.parent, ['status', '--short']).stdout!r}",
        )
        self.assertEqual(run_git(fixture.parent, ["status", "--short"]).stdout.strip(), "")
        self.assertEqual(
            run_git(fixture.parent_sub, ["branch", "--show-current"]).stdout.strip(),
            context.work_branch,
        )

    def test_mixed_conflicts_resolve_gitlink_then_return_files_to_manual_service(self) -> None:
        fixture = GitlinkFixture(self.base).build(leave_conflicted=False)
        run_git(fixture.parent, ["checkout", "main"])
        run_git(fixture.parent_sub, ["checkout", fixture.base_sha])
        (fixture.parent / "conflict.txt").write_text("base\n", encoding="utf-8")
        run_git(fixture.parent, ["add", "conflict.txt"])
        run_git(fixture.parent, ["commit", "-m", "file conflict base"])

        run_git(fixture.parent, ["checkout", "-b", "manual-ours"])
        run_git(fixture.parent_sub, ["checkout", fixture.ours_sha])
        (fixture.parent / "conflict.txt").write_text("ours\n", encoding="utf-8")
        run_git(fixture.parent, ["add", "conflict.txt", "sub"])
        run_git(fixture.parent, ["commit", "-m", "manual ours"])

        run_git(fixture.parent, ["checkout", "main"])
        run_git(fixture.parent, ["checkout", "-b", "manual-theirs"])
        run_git(fixture.parent_sub, ["checkout", fixture.theirs_sha])
        (fixture.parent / "conflict.txt").write_text("theirs\n", encoding="utf-8")
        run_git(fixture.parent, ["add", "conflict.txt", "sub"])
        run_git(fixture.parent, ["commit", "-m", "manual theirs"])

        run_git(fixture.parent, ["checkout", "manual-ours"])
        run_git(fixture.parent_sub, ["checkout", fixture.ours_sha])
        context = MergeContext(
            work_branch="manual-merge-work",
            merge_branch="manual-theirs",
            file_conflict_mode="tool",
            conflict_tool_command='code --wait "{file}"',
            conflict_tool_timeout_seconds=0,
        )

        completed = process_repository(
            fixture.parent,
            context,
            root_base_remote_branch=None,
            is_root=True,
            prepare_branch=False,
            ops=ManualRepositoryOps(),
            file_conflicts_service=ResolvingFileConflictService(),
            cross_submodule_moves_service=CrossSubmoduleMoveService(),
        )

        self.assertTrue(
            completed,
            f"report={context.report_entries!r} status={run_git(fixture.parent, ['status', '--short']).stdout!r}",
        )
        self.assertEqual((fixture.parent / "conflict.txt").read_text(encoding="utf-8"), "resolved\n")
        self.assertEqual(run_git(fixture.parent, ["status", "--short"]).stdout.strip(), "")
        self.assertEqual(
            run_git(fixture.parent_sub, ["branch", "--show-current"]).stdout.strip(),
            context.work_branch,
        )
        child_parents = run_git(
            fixture.parent_sub,
            ["show", "-s", "--format=%P", "HEAD"],
        ).stdout.strip().split()
        self.assertEqual(child_parents, [fixture.ours_sha, fixture.theirs_sha])

    def test_submodule_file_conflict_is_resolved_through_manual_service(self) -> None:
        fixture_builder = engine_fixtures.RecursiveGitlinkMergeEngineIntegrationTests(
            methodName="test_recurses_into_submodule_file_conflict_and_lets_policy_resolve_it"
        )
        fixture_builder.base = self.base
        parent = fixture_builder._build_parent_with_submodule_file_conflict()
        child = parent / "sub"
        run_git(parent, ["merge", "--abort"])
        run_git(parent, ["checkout", "ours"])
        context = MergeContext(
            work_branch="manual-merge-work",
            merge_branch="theirs",
            file_conflict_mode="tool",
            conflict_tool_command='code --wait "{file}"',
            conflict_tool_timeout_seconds=0,
        )

        completed = process_repository(
            parent,
            context,
            root_base_remote_branch=None,
            is_root=True,
            prepare_branch=False,
            ops=ManualRepositoryOps(),
            file_conflicts_service=ResolvingFileConflictService(),
            cross_submodule_moves_service=CrossSubmoduleMoveService(),
        )

        self.assertTrue(completed, context.report_entries)
        self.assertEqual(run_git(parent, ["status", "--short"]).stdout.strip(), "")
        self.assertEqual(run_git(child, ["status", "--short"]).stdout.strip(), "")
        self.assertEqual((child / "conflict.txt").read_text(encoding="utf-8"), "resolved\n")
        self.assertEqual(run_git(child, ["branch", "--show-current"]).stdout.strip(), context.work_branch)
        child_parents = run_git(child, ["show", "-s", "--format=%P", "HEAD"]).stdout.strip().split()
        self.assertEqual(len(child_parents), 2)

    def test_cross_submodule_move_mode_keeps_legacy_recursive_path(self) -> None:
        fixture = GitlinkFixture(self.base).build(leave_conflicted=False)
        run_git(fixture.parent, ["checkout", "ours"])
        run_git(fixture.parent_sub, ["checkout", fixture.ours_sha])

        context = MergeContext(
            work_branch="manual-merge-work",
            merge_branch="theirs",
            file_conflict_mode="tool",
            conflict_tool_command='code --wait "{file}"',
            conflict_tool_timeout_seconds=0,
            enable_cross_submodule_moves=True,
        )
        with mock.patch(
            "manual_merge_split.commands._resolve_gitlink_conflicts_with_common_core",
            side_effect=AssertionError("common core must not run for cross-submodule move mode"),
        ):
            completed = process_repository(
                fixture.parent,
                context,
                root_base_remote_branch=None,
                is_root=True,
                prepare_branch=False,
                ops=ManualRepositoryOps(),
                file_conflicts_service=ManualFileConflictService(),
                cross_submodule_moves_service=CrossSubmoduleMoveService(),
            )

        self.assertTrue(completed)
        self.assertEqual(run_git(fixture.parent, ["status", "--short"]).stdout.strip(), "")
        self.assertEqual(
            run_git(fixture.parent_sub, ["branch", "--show-current"]).stdout.strip(),
            context.work_branch,
        )


if __name__ == "__main__":
    unittest.main()
