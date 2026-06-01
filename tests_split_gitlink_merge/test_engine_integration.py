from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from split_gitlink_merge import Conflict, GitActionExecutor, RecursiveGitlinkMergeEngine, SubprocessGitBackend
from split_gitlink_merge.policy import MergePolicy


class RecordingFileConflictPolicy(MergePolicy):
    def __init__(self) -> None:
        super().__init__()
        self.file_conflicts: list[tuple[Path, int, list[str]]] = []

    def handle_file_conflicts(
        self,
        actions: object,
        repo_path: Path,
        conflicts: list[Conflict],
        depth: int,
    ) -> None:
        self.file_conflicts.append((repo_path, depth, [conflict.path for conflict in conflicts]))
        for conflict in conflicts:
            (repo_path / conflict.path).write_text("resolved in manual policy\n", encoding="utf-8")
            actions.run(repo_path, ["add", "--", conflict.path])


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


class RecursiveGitlinkMergeEngineIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git executable is not available")
        self.temp_dir = tempfile.TemporaryDirectory(prefix="split-gitlink-core-")
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolves_real_parent_gitlink_conflict(self) -> None:
        parent = self._build_parent_with_gitlink_conflict()
        before = run_git(parent, ["ls-files", "-u"]).stdout.splitlines()
        self.assertEqual(len(before), 3)

        backend = SubprocessGitBackend(auto_safe_directory=True)
        actions = GitActionExecutor(backend)
        engine = RecursiveGitlinkMergeEngine(actions)
        result = engine.resolve_active_conflicts(parent)

        after = run_git(parent, ["ls-files", "-u"]).stdout.splitlines()
        status = run_git(parent, ["status", "--short"]).stdout.strip()
        merge_head = run_git(parent, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False)

        self.assertEqual(result.resolved_gitlinks, 1)
        self.assertEqual(after, [])
        self.assertEqual(status, "M  sub")
        self.assertEqual(merge_head.returncode, 0)

    def test_recurses_into_submodule_file_conflict_and_lets_policy_resolve_it(self) -> None:
        parent = self._build_parent_with_submodule_file_conflict()
        before_parent_conflicts = run_git(parent, ["ls-files", "-u"]).stdout.splitlines()
        self.assertEqual(len(before_parent_conflicts), 3)

        policy = RecordingFileConflictPolicy()
        backend = SubprocessGitBackend(auto_safe_directory=True)
        actions = GitActionExecutor(backend)
        engine = RecursiveGitlinkMergeEngine(actions, policy=policy)
        result = engine.resolve_active_conflicts(parent)

        parent_conflicts_after = run_git(parent, ["ls-files", "-u"]).stdout.splitlines()
        parent_status = run_git(parent, ["status", "--short"]).stdout.strip()
        sub = parent / "sub"
        sub_conflicts_after = run_git(sub, ["ls-files", "-u"]).stdout.splitlines()
        sub_status = run_git(sub, ["status", "--short"]).stdout.strip()
        sub_merge_parents = run_git(sub, ["show", "-s", "--format=%P", "HEAD"]).stdout.strip().split()

        self.assertEqual(result.resolved_gitlinks, 1)
        self.assertEqual(parent_conflicts_after, [])
        self.assertEqual(parent_status, "M  sub")
        self.assertEqual(sub_conflicts_after, [])
        self.assertEqual(sub_status, "")
        self.assertEqual((sub / "conflict.txt").read_text(encoding="utf-8"), "resolved in manual policy\n")
        self.assertEqual(policy.file_conflicts, [(sub.resolve(strict=False), 1, ["conflict.txt"])])
        self.assertEqual(len(sub_merge_parents), 2)

    def _build_parent_with_gitlink_conflict(self) -> Path:
        sub = self.base / "sub_src"
        parent = self.base / "parent"
        parent_sub = parent / "sub"

        run_git(self.base, ["init", "-b", "main", str(sub)])
        self._configure_identity(sub)
        (sub / "base.txt").write_text("base\n", encoding="utf-8")
        run_git(sub, ["add", "base.txt"])
        run_git(sub, ["commit", "-m", "base"])
        base_sha = run_git(sub, ["rev-parse", "HEAD"]).stdout.strip()

        run_git(sub, ["checkout", "-b", "ours"])
        (sub / "ours.txt").write_text("ours\n", encoding="utf-8")
        run_git(sub, ["add", "ours.txt"])
        run_git(sub, ["commit", "-m", "ours"])
        ours_sha = run_git(sub, ["rev-parse", "HEAD"]).stdout.strip()

        run_git(sub, ["checkout", "main"])
        run_git(sub, ["checkout", "-b", "theirs"])
        (sub / "theirs.txt").write_text("theirs\n", encoding="utf-8")
        run_git(sub, ["add", "theirs.txt"])
        run_git(sub, ["commit", "-m", "theirs"])
        theirs_sha = run_git(sub, ["rev-parse", "HEAD"]).stdout.strip()
        run_git(sub, ["checkout", "main"])

        run_git(self.base, ["init", "-b", "main", str(parent)])
        self._configure_identity(parent)
        run_git(self.base, ["clone", str(sub), str(parent_sub)])
        run_git(parent_sub, ["checkout", base_sha])
        self._configure_identity(parent_sub)
        (parent / ".gitmodules").write_text(
            '[submodule "sub"]\n'
            "\tpath = sub\n"
            f"\turl = {str(sub).replace(os.sep, '/')}\n",
            encoding="utf-8",
        )
        run_git(parent, ["add", ".gitmodules", "sub"])
        run_git(parent, ["commit", "-m", "add submodule at base"])

        run_git(parent, ["checkout", "-b", "ours"])
        run_git(parent_sub, ["checkout", ours_sha])
        run_git(parent, ["add", "sub"])
        run_git(parent, ["commit", "-m", "ours gitlink"])

        run_git(parent, ["checkout", "main"])
        run_git(parent, ["checkout", "-b", "theirs"])
        run_git(parent_sub, ["checkout", theirs_sha])
        run_git(parent, ["add", "sub"])
        run_git(parent, ["commit", "-m", "theirs gitlink"])

        run_git(parent, ["checkout", "ours"])
        run_git(parent_sub, ["checkout", ours_sha])
        merge = run_git(parent, ["merge", "--no-ff", "--no-commit", "theirs"], check=False)
        self.assertEqual(merge.returncode, 1, merge.stderr)
        return parent

    def _build_parent_with_submodule_file_conflict(self) -> Path:
        sub = self.base / "conflicting_sub_src"
        parent = self.base / "parent_with_submodule_file_conflict"
        parent_sub = parent / "sub"

        run_git(self.base, ["init", "-b", "main", str(sub)])
        self._configure_identity(sub)
        (sub / "conflict.txt").write_text("base\n", encoding="utf-8")
        run_git(sub, ["add", "conflict.txt"])
        run_git(sub, ["commit", "-m", "base file"])
        base_sha = run_git(sub, ["rev-parse", "HEAD"]).stdout.strip()

        run_git(sub, ["checkout", "-b", "ours"])
        (sub / "conflict.txt").write_text("ours\n", encoding="utf-8")
        run_git(sub, ["add", "conflict.txt"])
        run_git(sub, ["commit", "-m", "ours edits conflict file"])
        ours_sha = run_git(sub, ["rev-parse", "HEAD"]).stdout.strip()

        run_git(sub, ["checkout", "main"])
        run_git(sub, ["checkout", "-b", "theirs"])
        (sub / "conflict.txt").write_text("theirs\n", encoding="utf-8")
        run_git(sub, ["add", "conflict.txt"])
        run_git(sub, ["commit", "-m", "theirs edits conflict file"])
        theirs_sha = run_git(sub, ["rev-parse", "HEAD"]).stdout.strip()
        run_git(sub, ["checkout", "main"])

        run_git(self.base, ["init", "-b", "main", str(parent)])
        self._configure_identity(parent)
        run_git(self.base, ["clone", str(sub), str(parent_sub)])
        run_git(parent_sub, ["checkout", base_sha])
        self._configure_identity(parent_sub)
        (parent / ".gitmodules").write_text(
            '[submodule "sub"]\n'
            "\tpath = sub\n"
            f"\turl = {str(sub).replace(os.sep, '/')}\n",
            encoding="utf-8",
        )
        run_git(parent, ["add", ".gitmodules", "sub"])
        run_git(parent, ["commit", "-m", "add submodule at base"])

        run_git(parent, ["checkout", "-b", "ours"])
        run_git(parent_sub, ["checkout", ours_sha])
        run_git(parent, ["add", "sub"])
        run_git(parent, ["commit", "-m", "ours gitlink to conflicting file edit"])

        run_git(parent, ["checkout", "main"])
        run_git(parent, ["checkout", "-b", "theirs"])
        run_git(parent_sub, ["checkout", theirs_sha])
        run_git(parent, ["add", "sub"])
        run_git(parent, ["commit", "-m", "theirs gitlink to conflicting file edit"])

        run_git(parent, ["checkout", "ours"])
        run_git(parent_sub, ["checkout", ours_sha])
        merge = run_git(parent, ["merge", "--no-ff", "--no-commit", "theirs"], check=False)
        self.assertEqual(merge.returncode, 1, merge.stderr)
        return parent

    @staticmethod
    def _configure_identity(repo: Path) -> None:
        run_git(repo, ["config", "user.email", "test@example.com"])
        run_git(repo, ["config", "user.name", "Test User"])


if __name__ == "__main__":
    unittest.main()
