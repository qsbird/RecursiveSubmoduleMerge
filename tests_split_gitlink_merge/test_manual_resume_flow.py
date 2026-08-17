from __future__ import annotations

import argparse
import shutil
import tempfile
import unittest
from pathlib import Path

from manual_merge_split.file_conflicts import ManualFileConflictService
from manual_merge_split.preflight import ManualPreflightService
from manual_merge_split.repository_ops import ManualRepositoryOps
from manual_merge_split.session_store import MergeSessionStore
from manual_merge_split.session_workflow import ManualMergeSessionService
from tests_split_gitlink_merge import test_engine_integration as engine_fixtures
from tests_split_gitlink_merge.test_engine_integration import run_git


class PausingFileConflictService(ManualFileConflictService):
    def resolve_file_conflicts(self, repo_path, file_conflicts, context):  # type: ignore[no-untyped-def]
        del repo_path, context
        return list(file_conflicts), "test pauses for manual resolution", False


class ResolveChildPauseParentService(ManualFileConflictService):
    def __init__(self, child: Path) -> None:
        super().__init__()
        self.child = child.resolve()

    def resolve_file_conflicts(self, repo_path, file_conflicts, context):  # type: ignore[no-untyped-def]
        del context
        if repo_path.resolve() == self.child:
            for relative_path in file_conflicts:
                (repo_path / relative_path).write_text("resolved child\n", encoding="utf-8")
                run_git(repo_path, ["add", "--", relative_path])
            return [], None, False
        return list(file_conflicts), "pause after child was resolved", False


class ManualNestedResumeFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git executable is not available")
        self.temp_dir = tempfile.TemporaryDirectory(prefix="manual-nested-resume-")
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_nested_submodule_pause_resumes_child_then_parent(self) -> None:
        parent, child, ours_sha, theirs_sha = self._build_conflicting_parent()

        ops = ManualRepositoryOps()
        service = ManualMergeSessionService(
            ops=ops,
            preflight_service=ManualPreflightService(),
            file_conflicts_service=PausingFileConflictService(),
        )
        args = self._merge_args(parent)

        self.assertEqual(service.start_merge(args), 2)
        session = MergeSessionStore(runner=lambda repo, argv: ops.run_git(repo, argv, check=False)).load(parent)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(len(session.repo_stack), 2)
        self.assertEqual(Path(session.current_frame.repo_path), child.resolve())  # type: ignore[union-attr]
        self.assertEqual(run_git(child, ["rev-parse", "MERGE_HEAD"]).stdout.strip(), theirs_sha)

        (child / "conflict.txt").write_text("resolved manually\n", encoding="utf-8")
        run_git(child, ["add", "--", "conflict.txt"])

        self.assertEqual(service.resume(argparse.Namespace(repo=str(parent))), 0)
        self.assertIsNone(
            MergeSessionStore(runner=lambda repo, argv: ops.run_git(repo, argv, check=False)).load(parent)
        )
        child_head = run_git(child, ["rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(run_git(child, ["show", "-s", "--format=%P", "HEAD"]).stdout.strip().split(), [ours_sha, theirs_sha])
        self.assertEqual(run_git(parent, ["rev-parse", "HEAD:sub"]).stdout.strip(), child_head)
        self.assertEqual(len(run_git(parent, ["show", "-s", "--format=%P", "HEAD"]).stdout.strip().split()), 2)
        self.assertEqual(run_git(parent, ["status", "--short"]).stdout.strip(), "")

    def test_nested_abort_restores_each_repo_and_preserves_untracked_files(self) -> None:
        parent, child, ours_sha, _theirs_sha = self._build_conflicting_parent()
        original_parent_head = run_git(parent, ["rev-parse", "HEAD"]).stdout.strip()
        ops = ManualRepositoryOps()
        service = ManualMergeSessionService(
            ops=ops,
            preflight_service=ManualPreflightService(),
            file_conflicts_service=PausingFileConflictService(),
        )
        self.assertEqual(service.start_merge(self._merge_args(parent)), 2)
        untracked = child / "keep-untracked.txt"
        untracked.write_text("keep\n", encoding="utf-8")

        self.assertEqual(service.abort(argparse.Namespace(repo=str(parent), yes=True)), 0)
        self.assertEqual(run_git(parent, ["rev-parse", "HEAD"]).stdout.strip(), original_parent_head)
        self.assertEqual(run_git(child, ["rev-parse", "HEAD"]).stdout.strip(), ours_sha)
        self.assertNotEqual(run_git(parent, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False).returncode, 0)
        self.assertNotEqual(run_git(child, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False).returncode, 0)
        self.assertTrue(untracked.exists())

    def test_abort_restores_a_child_already_popped_from_the_resume_stack(self) -> None:
        parent, child, ours_sha, _theirs_sha = self._build_conflicting_parent()
        run_git(parent, ["checkout", "main"])
        (parent / "root-conflict.txt").write_text("base\n", encoding="utf-8")
        run_git(parent, ["add", "root-conflict.txt"])
        run_git(parent, ["commit", "-m", "root conflict base"])
        root_base = run_git(parent, ["rev-parse", "HEAD"]).stdout.strip()
        ours_gitlink_commit = run_git(parent, ["rev-parse", "ours"]).stdout.strip()
        theirs_gitlink_commit = run_git(parent, ["rev-parse", "theirs"]).stdout.strip()

        run_git(parent, ["checkout", "-b", "combined-ours", root_base])
        run_git(parent, ["cherry-pick", ours_gitlink_commit])
        (parent / "root-conflict.txt").write_text("ours\n", encoding="utf-8")
        run_git(parent, ["add", "root-conflict.txt"])
        run_git(parent, ["commit", "-m", "ours root edit"])
        combined_ours_head = run_git(parent, ["rev-parse", "HEAD"]).stdout.strip()

        run_git(parent, ["checkout", "-b", "combined-theirs", root_base])
        run_git(parent, ["cherry-pick", theirs_gitlink_commit])
        (parent / "root-conflict.txt").write_text("theirs\n", encoding="utf-8")
        run_git(parent, ["add", "root-conflict.txt"])
        run_git(parent, ["commit", "-m", "theirs root edit"])
        run_git(parent, ["checkout", "combined-ours"])
        run_git(child, ["checkout", ours_sha])

        ops = ManualRepositoryOps()
        service = ManualMergeSessionService(
            ops=ops,
            preflight_service=ManualPreflightService(),
            file_conflicts_service=ResolveChildPauseParentService(child),
        )
        self.assertEqual(
            service.start_merge(self._merge_args(parent, base_ref="combined-ours", merge_branch="combined-theirs")),
            2,
        )
        session = MergeSessionStore(runner=lambda repo, argv: ops.run_git(repo, argv, check=False)).load(parent)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(len(session.repo_stack), 1)
        self.assertEqual(len(session.touched_frames), 2)
        self.assertNotEqual(run_git(child, ["rev-parse", "HEAD"]).stdout.strip(), ours_sha)

        self.assertEqual(service.abort(argparse.Namespace(repo=str(parent), yes=True)), 0)
        self.assertEqual(run_git(parent, ["rev-parse", "HEAD"]).stdout.strip(), combined_ours_head)
        self.assertEqual(run_git(child, ["rev-parse", "HEAD"]).stdout.strip(), ours_sha)

    def _build_conflicting_parent(self) -> tuple[Path, Path, str, str]:
        fixture_builder = engine_fixtures.RecursiveGitlinkMergeEngineIntegrationTests(
            methodName="test_recurses_into_submodule_file_conflict_and_lets_policy_resolve_it"
        )
        fixture_builder.base = self.base
        parent = fixture_builder._build_parent_with_submodule_file_conflict()
        child = parent / "sub"
        run_git(parent, ["merge", "--abort"])
        run_git(parent, ["checkout", "ours"])
        ours_sha = run_git(parent, ["rev-parse", "ours:sub"]).stdout.strip()
        theirs_sha = run_git(parent, ["rev-parse", "theirs:sub"]).stdout.strip()
        run_git(child, ["checkout", ours_sha])
        return parent, child, ours_sha, theirs_sha

    @staticmethod
    def _merge_args(
        parent: Path,
        *,
        base_ref: str = "ours",
        merge_branch: str = "theirs",
    ) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(parent),
            merge_branch=merge_branch,
            base_remote_branch=base_ref,
            use_current_branch=False,
            continue_current_branch=False,
            work_branch="manual-merge-work",
            clone_branch=None,
            clone_url=None,
            clone_path=None,
            ignore_root_untracked_path=[],
            conflict_tool_command='code --wait "{file}"',
            conflict_tool_timeout_seconds=0,
            no_confirm_after_conflict_tool=True,
            report_file="merge_manual_todo.txt",
        )


if __name__ == "__main__":
    unittest.main()
