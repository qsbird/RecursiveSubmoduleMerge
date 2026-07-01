from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from merge_tool_adapters import CiAutoGitlinkMergeService, ManualSemiAutoMergeService
from split_gitlink_merge import GitCommandResult, NonGitlinkConflictError, SubprocessGitBackend


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


class GitlinkFixture:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.sub = base / "sub_src"
        self.parent = base / "parent"
        self.parent_sub = self.parent / "sub"
        self.base_sha = ""
        self.ours_sha = ""
        self.theirs_sha = ""
        self.ours_parent_sha = ""
        self.theirs_parent_sha = ""
        self.parent_base_sha = ""

    def build(self, *, leave_conflicted: bool) -> "GitlinkFixture":
        run_git(self.base, ["init", "-b", "main", str(self.sub)])
        self._configure_identity(self.sub)
        (self.sub / "base.txt").write_text("base\n", encoding="utf-8")
        run_git(self.sub, ["add", "base.txt"])
        run_git(self.sub, ["commit", "-m", "base"])
        self.base_sha = run_git(self.sub, ["rev-parse", "HEAD"]).stdout.strip()

        run_git(self.sub, ["checkout", "-b", "ours"])
        (self.sub / "ours.txt").write_text("ours\n", encoding="utf-8")
        run_git(self.sub, ["add", "ours.txt"])
        run_git(self.sub, ["commit", "-m", "ours"])
        self.ours_sha = run_git(self.sub, ["rev-parse", "HEAD"]).stdout.strip()

        run_git(self.sub, ["checkout", "main"])
        run_git(self.sub, ["checkout", "-b", "theirs"])
        (self.sub / "theirs.txt").write_text("theirs\n", encoding="utf-8")
        run_git(self.sub, ["add", "theirs.txt"])
        run_git(self.sub, ["commit", "-m", "theirs"])
        self.theirs_sha = run_git(self.sub, ["rev-parse", "HEAD"]).stdout.strip()
        run_git(self.sub, ["checkout", "main"])

        run_git(self.base, ["init", "-b", "main", str(self.parent)])
        self._configure_identity(self.parent)
        run_git(self.base, ["clone", str(self.sub), str(self.parent_sub)])
        run_git(self.parent_sub, ["checkout", self.base_sha])
        self._configure_identity(self.parent_sub)
        (self.parent / ".gitmodules").write_text(
            '[submodule "sub"]\n'
            "\tpath = sub\n"
            f"\turl = {str(self.sub).replace(os.sep, '/')}\n",
            encoding="utf-8",
        )
        run_git(self.parent, ["add", ".gitmodules", "sub"])
        run_git(self.parent, ["commit", "-m", "add submodule at base"])
        self.parent_base_sha = run_git(self.parent, ["rev-parse", "HEAD"]).stdout.strip()

        run_git(self.parent, ["checkout", "-b", "ours"])
        run_git(self.parent_sub, ["checkout", self.ours_sha])
        run_git(self.parent, ["add", "sub"])
        run_git(self.parent, ["commit", "-m", "ours gitlink"])
        self.ours_parent_sha = run_git(self.parent, ["rev-parse", "HEAD"]).stdout.strip()

        run_git(self.parent, ["checkout", "main"])
        run_git(self.parent, ["checkout", "-b", "theirs"])
        run_git(self.parent_sub, ["checkout", self.theirs_sha])
        run_git(self.parent, ["add", "sub"])
        run_git(self.parent, ["commit", "-m", "theirs gitlink"])
        self.theirs_parent_sha = run_git(self.parent, ["rev-parse", "HEAD"]).stdout.strip()

        if leave_conflicted:
            run_git(self.parent, ["checkout", "ours"])
            run_git(self.parent_sub, ["checkout", self.ours_sha])
            merge = run_git(self.parent, ["merge", "--no-ff", "--no-commit", "theirs"], check=False)
            if merge.returncode != 1:
                raise AssertionError(f"expected gitlink conflict, got {merge.returncode}\n{merge.stderr}")
        return self

    @staticmethod
    def _configure_identity(repo: Path) -> None:
        run_git(repo, ["config", "user.email", "test@example.com"])
        run_git(repo, ["config", "user.name", "Test User"])


class AdapterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git executable is not available")
        self.temp_dir = tempfile.TemporaryDirectory(prefix="merge-adapter-")
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_manual_semiauto_adapter_resolves_and_commits_root_merge(self) -> None:
        fixture = GitlinkFixture(self.base).build(leave_conflicted=True)

        service = ManualSemiAutoMergeService(SubprocessGitBackend(auto_safe_directory=True))
        summary = service.resolve_current_or_merge(fixture.parent)

        self.assertEqual(summary.resolved_gitlinks, 1)
        self.assertTrue(summary.root_committed)
        self.assertTrue(summary.final_submodule_update)
        self.assertEqual(run_git(fixture.parent, ["status", "--short"]).stdout.strip(), "")
        self.assertEqual(run_git(fixture.parent, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False).returncode, 1)

    def test_ci_auto_adapter_merges_source_into_target_and_commits(self) -> None:
        fixture = GitlinkFixture(self.base).build(leave_conflicted=False)

        service = CiAutoGitlinkMergeService(
            SubprocessGitBackend(auto_safe_directory=True),
            max_depth=16,
        )
        result = service.merge_repo(
            fixture.parent,
            source_sha=fixture.theirs_parent_sha,
            target_sha=fixture.ours_parent_sha,
            base_sha=fixture.parent_base_sha,
            depth=0,
            display_path=".",
        )

        self.assertEqual(result.strategy, "recursive_gitlink_merge")
        self.assertEqual(run_git(fixture.parent, ["status", "--short"]).stdout.strip(), "")
        parents = run_git(fixture.parent, ["show", "-s", "--format=%P", "HEAD"]).stdout.strip().split()
        self.assertEqual(parents, [fixture.ours_parent_sha, fixture.theirs_parent_sha])

    def test_ci_auto_adapter_syncs_submodules_after_clean_gitlink_merge(self) -> None:
        fixture = GitlinkFixture(self.base).build(leave_conflicted=False)

        service = CiAutoGitlinkMergeService(
            SubprocessGitBackend(auto_safe_directory=True),
            max_depth=16,
        )
        result = service.merge_repo(
            fixture.parent,
            source_sha=fixture.theirs_parent_sha,
            target_sha=fixture.parent_base_sha,
            base_sha=fixture.parent_base_sha,
            depth=0,
            display_path=".",
        )

        self.assertEqual(result.strategy, "clean_merge")
        self.assertEqual(run_git(fixture.parent, ["status", "--short"]).stdout.strip(), "")
        self.assertEqual(run_git(fixture.parent_sub, ["rev-parse", "HEAD"]).stdout.strip(), fixture.theirs_sha)

    def test_ci_auto_adapter_reports_only_non_gitlink_conflicts_when_mixed(self) -> None:
        repo = self.base / "fake-parent"
        repo.mkdir()

        class MixedConflictBackend:
            def run(self, repo_path: Path, args: list[str], *, check: bool = True) -> GitCommandResult:
                del check
                if args[:1] == ["merge"]:
                    return GitCommandResult(args=args, cwd=repo_path, returncode=1)
                if args == ["diff", "--name-only", "--diff-filter=U"]:
                    return GitCommandResult(
                        args=args,
                        cwd=repo_path,
                        returncode=0,
                        stdout="file-a.txt\n.gitmodules\nsub\n",
                    )
                if args == ["ls-files", "-u"]:
                    return GitCommandResult(
                        args=args,
                        cwd=repo_path,
                        returncode=0,
                        stdout=(
                            "100644 base-a 1\tfile-a.txt\n"
                            "100644 ours-a 2\tfile-a.txt\n"
                            "100644 theirs-a 3\tfile-a.txt\n"
                            "100644 base-mod 1\t.gitmodules\n"
                            "100644 ours-mod 2\t.gitmodules\n"
                            "100644 theirs-mod 3\t.gitmodules\n"
                            "160000 base-sub 1\tsub\n"
                            "160000 ours-sub 2\tsub\n"
                            "160000 theirs-sub 3\tsub\n"
                        ),
                    )
                return GitCommandResult(args=args, cwd=repo_path, returncode=0)

        service = CiAutoGitlinkMergeService(MixedConflictBackend())

        with self.assertRaises(NonGitlinkConflictError) as raised:
            service.merge_repo(
                repo,
                source_sha="theirs",
                target_sha="ours",
                base_sha="base",
                depth=0,
                display_path=".",
            )

        self.assertEqual(
            [conflict.path for conflict in raised.exception.conflicts],
            [".gitmodules", "file-a.txt"],
        )
        self.assertEqual(
            [conflict.kind for conflict in raised.exception.conflicts],
            ["gitmodules", "file"],
        )


if __name__ == "__main__":
    unittest.main()
