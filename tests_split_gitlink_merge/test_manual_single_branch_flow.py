from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from manual_merge_split.commands import ManualCommandService
from manual_merge_split.repository_ops import ManualRepositoryOps
from manual_merge_split.runtime import parse_args
from manual_merge_split.session_store import MergeSessionStore


def run_git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


class ManualSingleBranchFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git executable is not available")
        self.temp_dir = tempfile.TemporaryDirectory(prefix="manual-single-")
        self.base = Path(self.temp_dir.name)
        self.commands = ManualCommandService()
        self.ops = ManualRepositoryOps()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cli_rejects_multiple_merge_refs(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["merge", "feature-a", "feature-b", "--repo", "repo"])

    def test_clean_single_branch_merge_commits_successfully(self) -> None:
        repo = self._build_clean_merge_repo()

        args = self._merge_args(repo, "theirs")
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = self.commands.run_merge(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("completed successfully", stdout.getvalue())
        self.assertEqual(run_git(repo, ["status", "--short"]).stdout.strip(), "")
        parents = run_git(repo, ["show", "-s", "--format=%P", "HEAD"]).stdout.strip().split()
        self.assertEqual(len(parents), 2)

    def test_unsupported_conflict_pauses_then_resume_commits_merge(self) -> None:
        repo = self._build_modify_delete_conflict_repo()

        merge_stdout = io.StringIO()
        with redirect_stdout(merge_stdout), redirect_stderr(io.StringIO()):
            exit_code = self.commands.run_merge(self._merge_args(repo, "theirs"))

        self.assertEqual(exit_code, 2)
        self.assertIn("paused", merge_stdout.getvalue().lower())
        self.assertEqual(run_git(repo, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False).returncode, 0)

        status_stdout = io.StringIO()
        with redirect_stdout(status_stdout), redirect_stderr(io.StringIO()):
            self.assertEqual(self.commands.run_status(self._repo_only_args(repo)), 0)
        self.assertIn("paused_manual", status_stdout.getvalue())
        self.assertIn(str(repo.resolve()), status_stdout.getvalue())

        (repo / "conflict.txt").write_text("resolved\n", encoding="utf-8")
        run_git(repo, ["add", "--", "conflict.txt"])

        resume_stdout = io.StringIO()
        with redirect_stdout(resume_stdout), redirect_stderr(io.StringIO()):
            resume_code = self.commands.run_resume(self._repo_only_args(repo))

        self.assertEqual(resume_code, 0)
        self.assertIn("completed", resume_stdout.getvalue().lower())
        self.assertEqual(run_git(repo, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False).returncode, 1)
        self.assertEqual(run_git(repo, ["status", "--short"]).stdout.strip(), "")
        parents = run_git(repo, ["show", "-s", "--format=%P", "HEAD"]).stdout.strip().split()
        self.assertEqual(len(parents), 2)

    def test_resume_accepts_a_valid_user_created_merge_commit(self) -> None:
        repo = self._build_modify_delete_conflict_repo()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.commands.run_merge(self._merge_args(repo, "theirs")), 2)
        (repo / "conflict.txt").write_text("resolved and committed by user\n", encoding="utf-8")
        run_git(repo, ["add", "--", "conflict.txt"])
        run_git(repo, ["commit", "--no-edit"])

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.commands.run_resume(self._repo_only_args(repo)), 0)
        self.assertEqual(len(run_git(repo, ["show", "-s", "--format=%P", "HEAD"]).stdout.strip().split()), 2)
        self.assertEqual(run_git(repo, ["status", "--short"]).stdout.strip(), "")

    def test_failed_resume_keeps_paused_status_and_diagnostics(self) -> None:
        repo = self._build_modify_delete_conflict_repo()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.commands.run_merge(self._merge_args(repo, "theirs")), 2)
        (repo / "conflict.txt").write_text("resolved\n", encoding="utf-8")
        run_git(repo, ["add", "--", "conflict.txt"])

        store = MergeSessionStore(runner=lambda path, argv: self.ops.run_git(path, argv, check=False))
        session = store.load(repo)
        self.assertIsNotNone(session)
        assert session is not None and session.current_frame is not None
        invalid_frame = replace(session.current_frame, expected_merge_head="0" * 40)
        session = store.save(repo, replace(session, repo_stack=(*session.repo_stack[:-1], invalid_frame)))
        expected_conflicts = session.paused_conflicts

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.commands.run_resume(self._repo_only_args(repo)), 1)
        preserved = store.load(repo)
        self.assertIsNotNone(preserved)
        assert preserved is not None
        self.assertEqual(preserved.status.value, "paused_manual")
        self.assertEqual(preserved.paused_conflicts, expected_conflicts)

    def test_abort_restores_original_head_after_paused_merge(self) -> None:
        repo = self._build_modify_delete_conflict_repo()
        original_head = run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            exit_code = self.commands.run_merge(self._merge_args(repo, "theirs"))
        self.assertEqual(exit_code, 2)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            abort_code = self.commands.run_abort(self._abort_args(repo))

        self.assertEqual(abort_code, 0)
        self.assertEqual(run_git(repo, ["rev-parse", "HEAD"]).stdout.strip(), original_head)
        self.assertEqual(run_git(repo, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False).returncode, 1)

    def _merge_args(self, repo: Path, merge_branch: str) -> argparse.Namespace:
        return argparse.Namespace(
            command="merge",
            merge_branch=merge_branch,
            repo=str(repo),
            clone_branch=None,
            clone_url=None,
            clone_path=None,
            base_remote_branch="ours",
            use_current_branch=False,
            continue_current_branch=False,
            work_branch="manual-work",
            allow_dirty_submodules=False,
            no_prompt_dirty_submodules=False,
            report_file="merge_manual_todo.txt",
            ignore_root_untracked_path=[],
            file_conflict_mode="tool",
            conflict_tool_command='code --wait "{file}"',
            conflict_tool_timeout_seconds=0,
            no_confirm_after_conflict_tool=True,
            verbose=False,
        )

    def _repo_only_args(self, repo: Path) -> argparse.Namespace:
        return argparse.Namespace(command="resume", repo=str(repo), verbose=False)

    def _abort_args(self, repo: Path) -> argparse.Namespace:
        return argparse.Namespace(command="abort", repo=str(repo), verbose=False, yes=True)

    def _build_clean_merge_repo(self) -> Path:
        repo = self.base / "clean"
        run_git(self.base, ["init", "-b", "main", str(repo)])
        self._configure_identity(repo)
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        run_git(repo, ["add", "base.txt"])
        run_git(repo, ["commit", "-m", "base"])

        run_git(repo, ["checkout", "-b", "ours"])
        (repo / "ours.txt").write_text("ours\n", encoding="utf-8")
        run_git(repo, ["add", "ours.txt"])
        run_git(repo, ["commit", "-m", "ours"])

        run_git(repo, ["checkout", "main"])
        run_git(repo, ["checkout", "-b", "theirs"])
        (repo / "theirs.txt").write_text("theirs\n", encoding="utf-8")
        run_git(repo, ["add", "theirs.txt"])
        run_git(repo, ["commit", "-m", "theirs"])

        run_git(repo, ["checkout", "ours"])
        return repo

    def _build_modify_delete_conflict_repo(self) -> Path:
        repo = self.base / "modify-delete"
        run_git(self.base, ["init", "-b", "main", str(repo)])
        self._configure_identity(repo)
        (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
        run_git(repo, ["add", "conflict.txt"])
        run_git(repo, ["commit", "-m", "base"])

        run_git(repo, ["checkout", "-b", "ours"])
        (repo / "conflict.txt").write_text("ours\n", encoding="utf-8")
        run_git(repo, ["add", "conflict.txt"])
        run_git(repo, ["commit", "-m", "ours modifies file"])

        run_git(repo, ["checkout", "main"])
        run_git(repo, ["checkout", "-b", "theirs"])
        run_git(repo, ["rm", "conflict.txt"])
        run_git(repo, ["commit", "-m", "theirs deletes file"])

        run_git(repo, ["checkout", "ours"])
        return repo

    @staticmethod
    def _configure_identity(repo: Path) -> None:
        run_git(repo, ["config", "user.email", "test@example.com"])
        run_git(repo, ["config", "user.name", "Test User"])


if __name__ == "__main__":
    unittest.main()
