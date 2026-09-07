from __future__ import annotations

import tempfile
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from manual_merge_split.models import MergeAutomationError, PublishCandidate
from manual_merge_split.publish import (
    PublishPlanItem,
    apply_candidate_toggle,
    gitlink_reachable_on_any_remote,
    missing_required_selection_messages,
    parse_missing_submodule_paths,
    push_item,
    run_publish_check,
    run_publish_execute,
    run_publish_workflow,
)
from manual_merge_split.repository_ops import ManualRepositoryOps


ROOT_SHA = "a" * 40
CHILD_SHA = "b" * 40
REMOTE_SHA = "c" * 40
ROOT_BASE_SHA = "d" * 40
CHILD_BASE_SHA = "e" * 40


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
        self.root_base_child_sha: str | None = CHILD_BASE_SHA
        self.remote_object_shas: dict[Path, set[str]] = {
            self.root: set(),
            self.submodule: set(),
        }

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
        if args == ["ls-tree", ROOT_BASE_SHA, "--", "sub"]:
            if self.root_base_child_sha:
                return FakeGitResult(args, stdout=f"160000 commit {self.root_base_child_sha}\tsub\n")
            return FakeGitResult(args, returncode=1)
        if args[:2] == ["rev-parse", "--verify"]:
            commitish = args[2]
            if repo == self.root and commitish == f"{ROOT_SHA}^1^{{commit}}":
                return FakeGitResult(args, stdout=f"{ROOT_BASE_SHA}\n")
            if repo == self.submodule and commitish == f"{CHILD_SHA}^1^{{commit}}":
                return FakeGitResult(args, stdout=f"{CHILD_BASE_SHA}\n")
            if commitish.startswith("refs/heads/"):
                branch = commitish.removeprefix("refs/heads/").removesuffix("^{commit}")
                sha = self.branches[repo].get(branch)
                if sha:
                    return FakeGitResult(args, stdout=f"{sha}\n")
                return FakeGitResult(args, stderr="unknown revision", returncode=1)
            if commitish == "FETCH_HEAD":
                return FakeGitResult(args, stdout=f"{self.remote_heads[repo] or ''}\n")
            return FakeGitResult(args, stderr="unknown revision", returncode=1)
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
        if len(args) == 5 and args[:2] == ["rev-list", "--max-count=1"] and args[3:] == ["--not", "--remotes"]:
            sha = args[2]
            if self._sha_on_remote(repo, sha):
                return FakeGitResult(args)
            return FakeGitResult(args, stdout=f"{sha}\n")
        if args[:1] == ["push"]:
            return self._handle_push(repo, args)
        return FakeGitResult(args)

    def _sha_on_remote(self, repo: Path, sha: str) -> bool:
        if sha in self.remote_object_shas.get(repo, set()):
            return True
        return self.remote_heads.get(repo) == sha

    def _handle_push(self, repo: Path, args: list[str]) -> FakeGitResult:
        dry_run = "--dry-run" in args
        use_check = "--recurse-submodules=check" in args
        spec = args[-1]
        sha = spec.split(":", 1)[0]
        if use_check and repo == self.root and not self._sha_on_remote(self.submodule, CHILD_SHA):
            stderr = (
                "The following submodule paths contain changes that can not be found on any remote:\n"
                "sub\n"
                "fatal: Aborting.\n"
            )
            return FakeGitResult(args, stderr=stderr, returncode=128)
        if not dry_run:
            self.remote_object_shas.setdefault(repo, set()).add(sha)
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
            dry_runs = [
                args
                for _repo, args, _kwargs in ops.calls
                if args[:1] == ("push",) and "--dry-run" in args
            ]
            self.assertTrue(all("--recurse-submodules=no" in args for args in dry_runs))
            self.assertFalse(any("--recurse-submodules=check" in args and "--dry-run" in args for args in dry_runs))

    def test_publish_check_succeeds_when_child_commit_is_not_yet_on_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)

            with redirect_stdout(StringIO()):
                result = run_publish_check(
                    root,
                    remote="origin",
                    push_target="publish-target",
                    sync_local_branches=True,
                    allow_suffix_branch=True,
                    ops=ops,  # type: ignore[arg-type]
                )

            self.assertTrue(result.success)
            self.assertEqual([item.repo_path for item in result.items], [submodule.resolve(), root.resolve()])
            self.assertFalse(any(item.reachable_on_remote for item in result.items))
            self.assertTrue(all(item.dry_run_ok or item.skipped for item in result.items))

    def test_publish_skips_submodule_when_gitlink_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)
            ops.root_base_child_sha = CHILD_SHA
            ops.remote_object_shas[ops.submodule].add(CHILD_SHA)

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
            self.assertEqual([item.repo_path for item in result.items], [root.resolve()])
            call_args = [(repo, args) for repo, args, _kwargs in ops.calls]
            self.assertNotIn((submodule.resolve(), ("branch", "publish-target", CHILD_SHA)), call_args)
            self.assertFalse(
                any(repo == submodule.resolve() and args[:1] == ("push",) for repo, args in call_args)
            )
            self.assertIn((root.resolve(), ("branch", "publish-target", ROOT_SHA)), call_args)

    def test_publish_includes_unchanged_gitlink_when_not_on_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)
            ops.root_base_child_sha = CHILD_SHA

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
            pushes = [
                (repo, args)
                for repo, args, _kwargs in ops.calls
                if args[:1] == ("push",) and "--dry-run" not in args
            ]
            self.assertEqual([repo for repo, _args in pushes], [submodule.resolve(), root.resolve()])

    def test_publish_execute_rejects_parent_without_unpushed_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)

            with redirect_stdout(StringIO()):
                check_result = run_publish_check(
                    root,
                    remote="origin",
                    push_target="publish-target",
                    sync_local_branches=True,
                    allow_suffix_branch=True,
                    ops=ops,  # type: ignore[arg-type]
                )

            with self.assertRaises(MergeAutomationError) as raised:
                with redirect_stdout(StringIO()):
                    run_publish_execute(
                        check_result,
                        remote="origin",
                        push_target="publish-target",
                        sync_local_branches=True,
                        allow_suffix_branch=True,
                        selected_display_paths={"."},
                        ops=ops,  # type: ignore[arg-type]
                    )
            self.assertIn("requires sub", str(raised.exception))

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

    def test_push_refuses_submodule_missing_from_checked_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root, submodule, ops = self.make_ops(temp_dir)
            root_item = PublishPlanItem(
                repo_path=root.resolve(),
                display_path=".",
                desired_sha=ROOT_SHA,
                depth=0,
            )

            with self.assertRaises(MergeAutomationError) as raised:
                push_item(
                    root_item,
                    "origin",
                    "publish-target",
                    ops,  # type: ignore[arg-type]
                    plan_items=[root_item],
                )

            self.assertIn("not included in the checked publish plan", str(raised.exception))
            self.assertFalse(
                any(
                    repo == submodule.resolve() and args[:1] == ("push",)
                    for repo, args, _kwargs in ops.calls
                )
            )

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


class MissingSubmodulePathParseTests(unittest.TestCase):
    def test_parse_git_check_abort_paths(self) -> None:
        output = (
            "git push --dry-run --recurse-submodules=check origin abc:refs/heads/temp\n"
            "The following submodule paths contain changes that can not be found on any remote:\n"
            "ArcherPre\n"
            "fatal: Aborting.\n"
            "fatal: the remote end hung up unexpectedly\n"
        )
        self.assertEqual(parse_missing_submodule_paths(output), ["ArcherPre"])

    def test_parse_stops_before_git_recovery_instructions(self) -> None:
        output = (
            "The following submodule paths contain changes that can not be found on any remote:\n"
            "  ArcherPre\n"
            "\n"
            "Please try\n"
            "\n"
            "\tgit push --recurse-submodules=on-demand\n"
            "\n"
            "or cd to the path and use\n"
            "\n"
            "\tgit push\n"
            "\n"
            "to push them to a remote.\n"
            "\n"
            "fatal: Aborting.\n"
        )

        self.assertEqual(parse_missing_submodule_paths(output), ["ArcherPre"])

    def test_parse_accepts_wrapped_git_marker(self) -> None:
        output = (
            "The following submodule paths contain changes that can\n"
            "not be found on any remote:\n"
            "  ArcherPre\n"
            "\n"
            "Please try\n"
        )

        self.assertEqual(parse_missing_submodule_paths(output), ["ArcherPre"])


class GitlinkReachabilityIntegrationTests(unittest.TestCase):
    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-c", "safe.directory=*", "-c", "core.longpaths=true", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    def test_local_only_commit_is_not_reachable_from_remote_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.name", "Publish Test")
            self._git(repo, "config", "user.email", "publish-test@example.invalid")
            self._git(repo, "commit", "--allow-empty", "--quiet", "-m", "base")
            base_sha = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "update-ref", "refs/remotes/origin/main", base_sha)
            self._git(repo, "commit", "--allow-empty", "--quiet", "-m", "local-only")
            local_sha = self._git(repo, "rev-parse", "HEAD")
            ops = ManualRepositoryOps()

            self.assertFalse(gitlink_reachable_on_any_remote(repo, local_sha, ops))

            self._git(repo, "update-ref", "refs/remotes/origin/main", local_sha)
            self.assertTrue(gitlink_reachable_on_any_remote(repo, local_sha, ops))


class PublishSelectionDependencyTests(unittest.TestCase):
    def _candidates(self) -> list[PublishCandidate]:
        return [
            PublishCandidate(
                repo_path=Path("root/sub"),
                display_path="sub",
                commit_sha=CHILD_SHA,
                depth=1,
                selected=False,
                reachable_on_remote=False,
            ),
            PublishCandidate(
                repo_path=Path("root"),
                display_path=".",
                commit_sha=ROOT_SHA,
                depth=0,
                selected=True,
                reachable_on_remote=False,
            ),
        ]

    def test_missing_required_child_is_reported(self) -> None:
        messages = missing_required_selection_messages(self._candidates())
        self.assertEqual(messages, [". requires sub"])

    def test_selecting_parent_selects_required_child(self) -> None:
        candidates = self._candidates()
        candidates[1].selected = False
        apply_candidate_toggle(candidates, 1)
        self.assertTrue(candidates[1].selected)
        self.assertTrue(candidates[0].selected)

    def test_deselecting_required_child_deselects_parent(self) -> None:
        candidates = self._candidates()
        candidates[0].selected = True
        apply_candidate_toggle(candidates, 0)
        self.assertFalse(candidates[0].selected)
        self.assertFalse(candidates[1].selected)


if __name__ == "__main__":
    unittest.main()
