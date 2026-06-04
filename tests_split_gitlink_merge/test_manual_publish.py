from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from manual_merge_split.models import MergeAutomationError
from manual_merge_split.publish import run_publish_workflow


ROOT_SHA = "a" * 40
CHILD_SHA = "b" * 40
REMOTE_SHA = "c" * 40


class FakeGitResult:
    def __init__(
        self,
        args: list[str],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.args = args
        self.cwd = Path("repo")
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakePublishOps:
    def __init__(self, root: Path, submodule: Path) -> None:
        self.root = root.resolve(strict=False)
        self.submodule = submodule.resolve(strict=False)
        self.calls: list[tuple[Path, tuple[str, ...], dict[str, Any]]] = []
        self.branches: dict[Path, dict[str, str]] = {
            self.root: {},
            self.submodule: {},
        }
        self.remote_heads: dict[Path, str | None] = {
            self.root: None,
            self.submodule: None,
        }
        self.ancestor_result: dict[tuple[Path, str, str], bool] = {}

    def _repo_key(self, repo_path: Path) -> Path:
        return repo_path.resolve(strict=False)

    def _automation_error_class(self) -> type[Exception]:
        return MergeAutomationError

    def _automation_error(self, message: str) -> Exception:
        return MergeAutomationError(message)

    def _format_git_failure(self, result: FakeGitResult) -> str:
        return f"git {' '.join(result.args)} failed: {result.stderr}"

    def _verbose(self) -> bool:
        return False

    def ensure_git_repo(self, _repo_path: Path) -> None:
        return None

    def remote_exists(self, _repo_path: Path, remote_name: str) -> bool:
        return remote_name == "origin"

    def get_submodule_paths(self, repo_path: Path) -> list[str]:
        return ["sub"] if self._repo_key(repo_path) == self.root else []

    def commit_exists(self, repo_path: Path, commitish: str) -> bool:
        repo = self._repo_key(repo_path)
        return (repo == self.root and commitish == ROOT_SHA) or (repo == self.submodule and commitish == CHILD_SHA)

    def resolve_commit(self, repo_path: Path, commitish: str) -> str:
        if self._repo_key(repo_path) == self.root and commitish == "HEAD":
            return ROOT_SHA
        raise MergeAutomationError(f"unexpected resolve_commit: {repo_path} {commitish}")

    def run_git(self, repo_path: Path, args: list[str], **kwargs: Any) -> FakeGitResult:
        repo = self._repo_key(repo_path)
        self.calls.append((repo, tuple(args), kwargs))

        if args == ["check-ref-format", "--branch", "publish-target"]:
            return FakeGitResult(args)
        if args == ["check-ref-format", "refs/heads/publish-target"]:
            return FakeGitResult(args)
        if args[:1] == ["check-ref-format"]:
            return FakeGitResult(args, stderr="invalid ref", returncode=1)
        if args == ["rev-parse", "--show-toplevel"]:
            return FakeGitResult(args, stdout=f"{repo}\n")
        if args[:2] == ["rev-parse", "--git-path"]:
            return FakeGitResult(args, stdout=f".git/{args[2]}\n")
        if args == ["submodule", "update", "--init", "--recursive"]:
            return FakeGitResult(args)
        if args[:2] == ["config", "--blob"]:
            if repo == self.root and args[2] == f"{ROOT_SHA}:.gitmodules":
                return FakeGitResult(args, stdout="submodule.sub.path sub\n")
            return FakeGitResult(args, returncode=1)
        if args == ["ls-tree", ROOT_SHA, "--", "sub"]:
            return FakeGitResult(args, stdout=f"160000 commit {CHILD_SHA}\tsub\n")
        if args[:2] == ["rev-parse", "--verify"]:
            commitish = args[2]
            if commitish.startswith("refs/heads/"):
                branch = commitish.removeprefix("refs/heads/").removesuffix("^{commit}")
                sha = self.branches[repo].get(branch)
                if sha:
                    return FakeGitResult(args, stdout=f"{sha}\n")
                return FakeGitResult(args, stderr="unknown revision", returncode=1)
            if commitish == "FETCH_HEAD":
                return FakeGitResult(args, stdout=f"{self.remote_heads[repo] or ''}\n")
        if args == ["rev-parse", "FETCH_HEAD"]:
            return FakeGitResult(args, stdout=f"{self.remote_heads[repo] or ''}\n")
        if args[:1] == ["branch"]:
            _branch_cmd, branch, sha = args
            if branch in self.branches[repo]:
                return FakeGitResult(args, stderr="already exists", returncode=1)
            self.branches[repo][branch] = sha
            return FakeGitResult(args)
        if args[:1] == ["ls-remote"]:
            ref = args[-1]
            sha = self.remote_heads[repo]
            return FakeGitResult(args, stdout=(f"{sha}\t{ref}\n" if sha else ""))
        if args[:1] == ["fetch"]:
            return FakeGitResult(args)
        if args[:1] == ["merge-base"]:
            _cmd, _flag, ancestor, descendant = args
            ok = self.ancestor_result.get((repo, ancestor, descendant), False)
            return FakeGitResult(args, returncode=0 if ok else 1)
        if args[:1] == ["push"]:
            return FakeGitResult(args)
        return FakeGitResult(args)


class ManualPublishWorkflowTests(unittest.TestCase):
    def make_ops(self, temp_dir: str) -> tuple[Path, Path, FakePublishOps]:
        root = Path(temp_dir) / "root"
        submodule = root / "sub"
        (root / ".git").mkdir(parents=True)
        (submodule / ".git").mkdir(parents=True)
        return root, submodule, FakePublishOps(root, submodule)

    def test_publish_pushes_submodule_before_root_without_checkout_or_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)

            with redirect_stdout(StringIO()):
                result = run_publish_workflow(
                    root,
                    remote="origin",
                    push_target="publish-target",
                    check_only=False,
                    sync_local_branches=True,
                    allow_suffix_branch=True,
                    ops=ops,  # type: ignore[arg-type]
                )

            self.assertTrue(result.success)
            self.assertEqual([item.repo_path for item in result.items], [submodule.resolve(), root.resolve()])
            call_args = [call[1] for call in ops.calls]
            self.assertIn(("submodule", "update", "--init", "--recursive"), call_args)
            self.assertIn(("check-ref-format", "--branch", "publish-target"), call_args)
            self.assertIn(("check-ref-format", "refs/heads/publish-target"), call_args)
            self.assertIn(("branch", "publish-target", CHILD_SHA), call_args)
            self.assertIn(("branch", "publish-target", ROOT_SHA), call_args)
            self.assertFalse(any(args and args[0] in {"checkout", "switch"} for args in call_args))
            self.assertFalse(any(part.startswith("--force") for args in call_args for part in args))

            pushes = [
                (repo, args)
                for repo, args, _kwargs in ops.calls
                if args[:1] == ("push",) and "--dry-run" not in args
            ]
            self.assertEqual([repo for repo, _args in pushes], [submodule.resolve(), root.resolve()])
            self.assertEqual(pushes[0][1][-1], f"{CHILD_SHA}:refs/heads/publish-target")
            self.assertEqual(pushes[1][1][-1], f"{ROOT_SHA}:refs/heads/publish-target")

    def test_publish_check_only_does_not_create_branches_or_push(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, _submodule, ops = self.make_ops(temp_dir)

            with redirect_stdout(StringIO()):
                run_publish_workflow(
                    root,
                    remote="origin",
                    push_target="publish-target",
                    check_only=True,
                    sync_local_branches=True,
                    allow_suffix_branch=True,
                    ops=ops,  # type: ignore[arg-type]
                )

            call_args = [call[1] for call in ops.calls]
            self.assertFalse(any(args[:1] == ("branch",) for args in call_args))
            self.assertFalse(any(args[:1] == ("push",) and "--dry-run" not in args for args in call_args))
            self.assertTrue(any(args[:2] == ("push", "--dry-run") for args in call_args))

    def test_publish_rejects_non_fast_forward_before_push(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, _submodule, ops = self.make_ops(temp_dir)
            ops.remote_heads[ops.root] = REMOTE_SHA
            ops.ancestor_result[(ops.root, REMOTE_SHA, ROOT_SHA)] = False

            with self.assertRaises(MergeAutomationError):
                with redirect_stdout(StringIO()):
                    run_publish_workflow(
                        root,
                        remote="origin",
                        push_target="publish-target",
                        check_only=False,
                        sync_local_branches=True,
                        allow_suffix_branch=True,
                        ops=ops,  # type: ignore[arg-type]
                    )

            call_args = [call[1] for call in ops.calls]
            self.assertFalse(any(args[:1] == ("branch",) for args in call_args))
            self.assertFalse(any(args[:1] == ("push",) for args in call_args))

    def test_publish_rejects_full_ref_push_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, _submodule, ops = self.make_ops(temp_dir)

            with self.assertRaises(MergeAutomationError) as raised:
                with redirect_stdout(StringIO()):
                    run_publish_workflow(
                        root,
                        remote="origin",
                        push_target="refs/heads/publish-target",
                        check_only=False,
                        sync_local_branches=True,
                        allow_suffix_branch=True,
                        ops=ops,  # type: ignore[arg-type]
                    )

            self.assertIn("not a full ref", str(raised.exception))
            call_args = [call[1] for call in ops.calls]
            self.assertFalse(any(args[:1] == ("push",) for args in call_args))


if __name__ == "__main__":
    unittest.main()
