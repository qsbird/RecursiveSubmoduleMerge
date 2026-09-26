from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from manual_merge_split.models import MergeAutomationError
from manual_merge_split.publish import (
    PublishPlanItem,
    PublishResult,
    run_publish,
    run_publish_check,
    run_publish_execute,
)
from manual_merge_split.repository_ops import ManualRepositoryOps


ROOT_SHA = "a" * 40
CHILD_SHA = "b" * 40
ROOT_PARENT_SHA = "c" * 40
CHILD_PARENT_SHA = "d" * 40
GRANDCHILD_SHA = "e" * 40
GRANDCHILD_PARENT_SHA = "f" * 40


class FakeGitResult:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakePublishOps(ManualRepositoryOps):
    """Minimal fake ops for publish check/execute testing.

    Overrides high-level repository methods to avoid needing real Git repos.
    """

    def __init__(self, root: Path, submodule: Path) -> None:
        super().__init__()
        self.root = root.resolve(strict=False)
        self.submodule = submodule.resolve(strict=False)
        self._remotes: dict[Path, list[str]] = {
            self.root: ["origin"],
            self.submodule: ["origin"],
        }
        self._remote_heads: dict[Path, str | None] = {
            self.root: None,
            self.submodule: None,
        }

    def _repo_key(self, repo_path: Path) -> Path:
        return repo_path.resolve(strict=False)

    def ensure_git_repo(self, repo_path: Path) -> None:
        pass  # Fake - assume all repos are valid

    def remote_exists(self, repo_path: Path, remote_name: str) -> bool:
        return remote_name in self._remotes.get(self._repo_key(repo_path), [])

    def resolve_commit(self, repo_path: Path, ref: str) -> str:
        repo = self._repo_key(repo_path)
        if repo == self.root:
            return ROOT_SHA
        return CHILD_SHA

    def commit_exists(self, repo_path: Path, commitish: str) -> bool:
        return commitish in (ROOT_SHA, CHILD_SHA)

    def get_submodule_paths(self, repo_path: Path) -> list[str]:
        if self._repo_key(repo_path) == self.root:
            return ["sub"]
        return []

    def run_git(self, repo_path: Path, args: list[str], **kwargs: Any) -> FakeGitResult:
        repo = self._repo_key(repo_path)

        if args == ["check-ref-format", "--branch", "publish-target"]:
            return FakeGitResult()
        if args == ["check-ref-format", "refs/heads/publish-target"]:
            return FakeGitResult()
        if args[:1] == ["check-ref-format"]:
            return FakeGitResult(stderr="invalid ref", returncode=1)

        if args == ["submodule", "update", "--init", "--recursive"]:
            return FakeGitResult()

        if args[:2] == ["config", "--blob"]:
            if repo == self.root and args[2] == f"{ROOT_SHA}:.gitmodules":
                return FakeGitResult(stdout="submodule.sub.path sub\n")
            return FakeGitResult(returncode=1)

        if args == ["ls-tree", ROOT_SHA, "--", "sub"]:
            return FakeGitResult(stdout=f"160000 commit {CHILD_SHA}\tsub\n")

        if args[:2] == ["rev-parse", "--verify"]:
            commitish = args[2]
            if commitish == f"{ROOT_SHA}^1^{{commit}}":
                return FakeGitResult(stderr="root commit has no parent", returncode=1)
            if commitish == f"refs/heads/publish-target^{{commit}}":
                return FakeGitResult(stderr="unknown revision", returncode=1)
            return FakeGitResult(stdout=ROOT_SHA if repo == self.root else CHILD_SHA)

        if args == ["rev-parse", "FETCH_HEAD"]:
            sha = self._remote_heads.get(repo, "")
            return FakeGitResult(stdout=f"{sha or ''}\n")

        if args[:1] == ["branch"]:
            return FakeGitResult()

        if args[:1] == ["ls-remote"]:
            sha = self._remote_heads.get(repo)
            return FakeGitResult(stdout=(f"{sha}\trefs/heads/publish-target\n" if sha else ""))

        if args[:1] == ["fetch"]:
            return FakeGitResult()

        if args[:1] == ["merge-base"]:
            return FakeGitResult(returncode=1)  # Not ancestor by default

        if args[:1] == ["push"]:
            return FakeGitResult()

        if args[:2] == ["rev-parse", "--git-path"]:
            return FakeGitResult(stdout=f".git/{args[2]}\n")

        return FakeGitResult()

    def _automation_error_class(self) -> type[Exception]:
        return MergeAutomationError


class PublishCheckTests(unittest.TestCase):
    def make_ops(self, temp_dir: str) -> tuple[Path, Path, FakePublishOps]:
        root = Path(temp_dir) / "root"
        submodule = root / "sub"
        (root / ".git").mkdir(parents=True)
        (submodule / ".git").mkdir(parents=True)
        return root, submodule, FakePublishOps(root, submodule)

    def test_run_publish_check_returns_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)
            result = run_publish_check(
                root,
                remote="origin",
                push_target="publish-target",
                sync_local_branches=True,
                allow_suffix_branch=True,
                ops=ops,
            )
            self.assertIsInstance(result, PublishResult)
            self.assertTrue(result.success)
            self.assertTrue(result.check_only)
            self.assertGreaterEqual(len(result.items), 1)

    def test_run_publish_check_warns_that_submodule_update_changes_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, _submodule, ops = self.make_ops(temp_dir)
            stderr = StringIO()
            with mock.patch("manual_merge_split.publish._sys.stderr", stderr):
                run_publish_check(
                    root,
                    remote="origin",
                    push_target="publish-target",
                    sync_local_branches=False,
                    allow_suffix_branch=True,
                    ops=ops,
                )
            warning = stderr.getvalue()
            self.assertIn("git submodule update --init --recursive", warning)
            self.assertIn("--check-only", warning)

    def test_run_publish_check_includes_display_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)
            result = run_publish_check(
                root,
                remote="origin",
                push_target="publish-target",
                sync_local_branches=True,
                allow_suffix_branch=True,
                ops=ops,
            )
            for item in result.items:
                self.assertIsInstance(item, PublishPlanItem)
                self.assertIsNotNone(item.display_path)

    def test_run_publish_execute_with_selected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)
            result = run_publish_check(
                root,
                remote="origin",
                push_target="publish-target",
                sync_local_branches=True,
                allow_suffix_branch=True,
                ops=ops,
            )
            selected = {item.display_path for item in result.items}
            execute_result = run_publish_execute(
                result,
                remote="origin",
                push_target="publish-target",
                sync_local_branches=True,
                allow_suffix_branch=True,
                selected_display_paths=selected,
                ops=ops,
            )
            self.assertIsInstance(execute_result, PublishResult)
            self.assertTrue(execute_result.success)
            self.assertFalse(execute_result.check_only)

    def test_run_publish_execute_with_empty_selection_returns_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)
            result = run_publish_check(
                root,
                remote="origin",
                push_target="publish-target",
                sync_local_branches=True,
                allow_suffix_branch=True,
                ops=ops,
            )
            execute_result = run_publish_execute(
                result,
                remote="origin",
                push_target="publish-target",
                sync_local_branches=True,
                allow_suffix_branch=True,
                selected_display_paths=set(),
                ops=ops,
            )
            self.assertTrue(execute_result.success)


class ThreeLevelPublishOps(FakePublishOps):
    def __init__(self, root: Path, submodule: Path, grandchild: Path) -> None:
        super().__init__(root, submodule)
        self.grandchild = grandchild.resolve(strict=False)
        self._remotes[self.grandchild] = ["origin"]
        self._remote_heads[self.grandchild] = None
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def resolve_commit(self, repo_path: Path, ref: str) -> str:
        repo = self._repo_key(repo_path)
        if repo == self.root:
            return ROOT_SHA
        if repo == self.submodule:
            return CHILD_SHA
        return GRANDCHILD_SHA

    def commit_exists(self, repo_path: Path, commitish: str) -> bool:
        return commitish in {
            ROOT_SHA,
            ROOT_PARENT_SHA,
            CHILD_SHA,
            CHILD_PARENT_SHA,
            GRANDCHILD_SHA,
            GRANDCHILD_PARENT_SHA,
        }

    def get_submodule_paths(self, repo_path: Path) -> list[str]:
        repo = self._repo_key(repo_path)
        if repo == self.root:
            return ["sub"]
        if repo == self.submodule:
            return ["nested"]
        return []

    def run_git(self, repo_path: Path, args: list[str], **kwargs: Any) -> FakeGitResult:
        repo = self._repo_key(repo_path)
        self.calls.append((repo, tuple(args)))

        if args == ["submodule", "update", "--init", "--recursive"]:
            (self.grandchild / ".git").mkdir(parents=True, exist_ok=True)
            return FakeGitResult()

        if args[:2] == ["config", "--blob"]:
            if repo == self.root and args[2] == f"{ROOT_SHA}:.gitmodules":
                return FakeGitResult(stdout="submodule.sub.path sub\n")
            if repo == self.submodule and args[2] == f"{CHILD_SHA}:.gitmodules":
                return FakeGitResult(stdout="submodule.nested.path nested\n")
            return FakeGitResult(returncode=1)

        gitlinks = {
            (self.root, ROOT_SHA, "sub"): CHILD_SHA,
            (self.root, ROOT_PARENT_SHA, "sub"): CHILD_PARENT_SHA,
            (self.submodule, CHILD_SHA, "nested"): GRANDCHILD_SHA,
            (self.submodule, CHILD_PARENT_SHA, "nested"): GRANDCHILD_PARENT_SHA,
        }
        if len(args) == 4 and args[0] == "ls-tree" and args[2] == "--":
            sha = gitlinks.get((repo, args[1], args[3]))
            if sha:
                return FakeGitResult(stdout=f"160000 commit {sha}\t{args[3]}\n")
            return FakeGitResult(returncode=1)

        if args == ["rev-parse", "--verify", f"{ROOT_SHA}^1^{{commit}}"]:
            return FakeGitResult(stdout=f"{ROOT_PARENT_SHA}\n")

        return super().run_git(repo_path, args, **kwargs)


class UnchangedIntermediatePublishOps(ThreeLevelPublishOps):
    def run_git(self, repo_path: Path, args: list[str], **kwargs: Any) -> FakeGitResult:
        repo = self._repo_key(repo_path)
        if args == ["ls-tree", ROOT_PARENT_SHA, "--", "sub"]:
            self.calls.append((repo, tuple(args)))
            return FakeGitResult(stdout=f"160000 commit {CHILD_SHA}\tsub\n")
        if args == ["rev-list", "--max-count=1", CHILD_SHA, "--not", "--remotes"]:
            self.calls.append((repo, tuple(args)))
            return FakeGitResult()
        if args == ["rev-list", "--max-count=1", GRANDCHILD_SHA, "--not", "--remotes"]:
            self.calls.append((repo, tuple(args)))
            return FakeGitResult(stdout=f"{GRANDCHILD_SHA}\n")
        return super().run_git(repo_path, args, **kwargs)


class RecursivePublishCliTests(unittest.TestCase):
    def test_check_only_initializes_and_checks_three_nested_levels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            submodule = root / "sub"
            grandchild = submodule / "nested"
            (root / ".git").mkdir(parents=True)
            (submodule / ".git").mkdir(parents=True)
            self.assertFalse(grandchild.exists())
            ops = ThreeLevelPublishOps(root, submodule, grandchild)
            args = SimpleNamespace(
                repo=str(root),
                remote="origin",
                push_target="publish-target",
                check_only=True,
                no_interactive=True,
                no_sync_local_branches=True,
                no_branch_suffix=False,
                verbose=False,
            )

            exit_code = run_publish(args, ops=ops)

            self.assertEqual(exit_code, 0)
            self.assertTrue((grandchild / ".git").exists())
            checked_repositories = [
                repo
                for repo, command in ops.calls
                if command[:2] == ("push", "--dry-run")
            ]
            self.assertEqual(
                checked_repositories,
                [grandchild.resolve(), submodule.resolve(), root.resolve()],
            )
            self.assertTrue(
                all(
                    command[2] == "--recurse-submodules=no"
                    for _repo, command in ops.calls
                    if command[:2] == ("push", "--dry-run")
                )
            )

    def test_unchanged_reachable_intermediate_still_checks_unpublished_nested_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            submodule = root / "sub"
            grandchild = submodule / "nested"
            (root / ".git").mkdir(parents=True)
            (submodule / ".git").mkdir(parents=True)
            ops = UnchangedIntermediatePublishOps(root, submodule, grandchild)
            args = SimpleNamespace(
                repo=str(root),
                remote="origin",
                push_target="publish-target",
                check_only=True,
                no_interactive=True,
                no_sync_local_branches=True,
                no_branch_suffix=False,
                verbose=False,
            )

            exit_code = run_publish(args, ops=ops)

            self.assertEqual(exit_code, 0)
            checked_repositories = [
                repo
                for repo, command in ops.calls
                if command[:2] == ("push", "--dry-run")
            ]
            self.assertEqual(checked_repositories, [grandchild.resolve(), root.resolve()])

class PublishPlanItemTests(unittest.TestCase):
    def test_plan_item_fields(self) -> None:
        item = PublishPlanItem(
            repo_path=Path("/tmp/repo"),
            display_path="repo",
            desired_sha="abc123",
            depth=0,
        )
        self.assertEqual(item.repo_path, Path("/tmp/repo"))
        self.assertEqual(item.display_path, "repo")
        self.assertEqual(item.desired_sha, "abc123")
        self.assertFalse(item.skipped)

    def test_plan_item_skipped(self) -> None:
        item = PublishPlanItem(
            repo_path=Path("/tmp/repo"),
            display_path="repo",
            desired_sha="abc123",
            depth=0,
            skipped=True,
        )
        self.assertTrue(item.skipped)

    def test_plan_item_default_fields(self) -> None:
        item = PublishPlanItem(
            repo_path=Path("/tmp/repo"),
            display_path=".",
            desired_sha="def456",
            depth=0,
        )
        self.assertIsNone(item.local_branch)
        self.assertEqual(item.local_branch_action, "not_requested")
        self.assertIsNone(item.remote_before_sha)
        self.assertEqual(item.remote_state, "unknown")
        self.assertFalse(item.dry_run_ok)
        self.assertFalse(item.pushed)
        self.assertFalse(item.skipped)
        self.assertFalse(item.reachable_on_remote)
        self.assertEqual(item.messages, [])


if __name__ == "__main__":
    unittest.main()
