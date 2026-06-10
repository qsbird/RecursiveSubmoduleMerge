from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

from ci_merge_split import commands as ci_commands
from ci_merge_split import recovery as ci_recovery
from ci_merge_split import runtime as ci_runtime
from ci_merge_split.models import ExitCode
from ci_merge_split.workflow import CiWorkflowServices
from tests_split_gitlink_merge.test_remaining_split_facades import (
    FakeCiCommandResult,
    FakeCiContext,
    FakeCiRunner,
)


def run_git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=git_test_env(),
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


def git_test_env() -> dict[str, str]:
    env = os.environ.copy()
    git_exe = shutil.which("git")
    if git_exe:
        git_root = Path(git_exe).resolve(strict=False).parent.parent
        extra_paths = [
            git_root / "usr" / "bin",
            git_root / "mingw64" / "bin",
            git_root / "mingw64" / "libexec" / "git-core",
        ]
        env["PATH"] = os.pathsep.join([*(str(path) for path in extra_paths if path.exists()), env.get("PATH", "")])
    return env


def configure_identity(repo: Path) -> None:
    run_git(repo, ["config", "user.email", "test@example.com"])
    run_git(repo, ["config", "user.name", "Test User"])
    run_git(repo, ["config", "protocol.file.allow", "always"])


class CiBatchCleanupIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git executable is not available")

    def test_batch_step_cleanup_removes_nested_git_repo_residue_before_next_step(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ci-batch-cleanup-") as temp_dir:
            base = Path(temp_dir)
            child_source = base / "child-source"
            root = base / "root"

            run_git(base, ["init", "-b", "main", str(child_source)])
            configure_identity(child_source)
            (child_source / "child.txt").write_text("child\n", encoding="utf-8")
            run_git(child_source, ["add", "child.txt"])
            run_git(child_source, ["commit", "-m", "child base"])

            run_git(base, ["init", "-b", "main", str(root)])
            configure_identity(root)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            run_git(root, ["add", "base.txt"])
            run_git(root, ["commit", "-m", "root base"])
            run_git(
                root,
                [
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(child_source),
                    "libs/child",
                ],
            )
            run_git(root, ["commit", "-m", "add child submodule"])

            for branch in ("source-one", "source-two"):
                run_git(root, ["checkout", "-B", branch, "main"])
                (root / f"{branch}.txt").write_text(f"{branch}\n", encoding="utf-8")
                run_git(root, ["add", f"{branch}.txt"])
                run_git(root, ["commit", "-m", branch])
            run_git(root, ["checkout", "main"])

            for ref in ("main", "source-one", "source-two"):
                sha = run_git(root, ["rev-parse", ref]).stdout.strip()
                run_git(root, ["update-ref", f"refs/remotes/origin/{ref}", sha])

            args = ci_runtime.parse_arguments(
                [
                    "--source",
                    "source-one",
                    "--source",
                    "source-two",
                    "--target",
                    "main",
                    "--repo_path",
                    str(root),
                    "--output_dir",
                    str(base / "out"),
                    "--job_id",
                    "batch-cleanup",
                    "--timeout",
                    "60",
                    "--git_network_retries",
                    "0",
                ]
            )
            ctx = ci_runtime.create_execution_context(args, "batch-cleanup")
            ctx.report.progress = lambda _message: None  # type: ignore[method-assign]
            nested_residue = root / "libs" / "child" / "nested-residue"
            merge_calls: list[int] = []

            def merge_repo(_ctx: object, repo_path: Path, **kwargs: object) -> object:
                step = len(merge_calls) + 1
                merge_calls.append(step)
                if step == 2:
                    self.assertFalse(
                        nested_residue.exists(),
                        "step 2 must start after cleanup removed the nested git repository residue",
                    )
                run_git(repo_path, ["checkout", "--detach", str(kwargs["target_sha"])])
                result_file = repo_path / f"result-step-{step}.txt"
                result_file.write_text(f"result {step}\n", encoding="utf-8")
                run_git(repo_path, ["add", result_file.name])
                run_git(repo_path, ["commit", "-m", f"result step {step}"])
                result_sha = run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()
                if step == 1:
                    nested_residue.mkdir(parents=True)
                    run_git(nested_residue, ["init", "-b", "main"])
                    configure_identity(nested_residue)
                    (nested_residue / "residue.txt").write_text("residue\n", encoding="utf-8")
                    run_git(nested_residue, ["add", "residue.txt"])
                    run_git(nested_residue, ["commit", "-m", "nested residue"])
                return types.SimpleNamespace(result_sha=result_sha, strategy="test_merge")

            with mock.patch.dict(os.environ, {"PATH": git_test_env()["PATH"]}):
                ci_commands.run_batch_workflow(ctx, CiWorkflowServices.from_local(), merge_repo)

            batch = ctx.report.data["batch"]
            self.assertEqual(batch["status"], "success")
            self.assertEqual(merge_calls, [1, 2])
            self.assertFalse(nested_residue.exists())
            self.assertEqual(ctx.report.data["summary"]["fallbacks"], len(ctx.report.data["fallbacks"]))

            step_two_cleanup = batch["steps"][1]["pre_step_cleanup"]
            self.assertTrue(step_two_cleanup["success"])
            fallback_slice = ctx.report.data["fallbacks"][
                step_two_cleanup["fallback_range"]["start"] : step_two_cleanup["fallback_range"]["end"] + 1
            ]
            self.assertEqual(
                [item["level"] for item in fallback_slice],
                ["level_1_basic_cleanup", "level_2_abort_and_recursive_cleanup"],
            )
            self.assertEqual([item["success"] for item in fallback_slice], [False, True])
            self.assertEqual(fallback_slice[0]["scope"]["step_index"], 2)
            self.assertIn("working tree not clean", "\n".join(fallback_slice[0]["verify_errors"]))


class CiCleanupFallbackChainTests(unittest.TestCase):
    def test_ensure_ready_after_state_change_cleans_and_replays_checkout_when_dirty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ci-post-checkout-cleanup-") as temp_dir:
            parent = Path(temp_dir)
            repo = parent / "repo"
            repo.mkdir()

            class DirtyAfterCheckoutRunner(FakeCiRunner):
                def __init__(self) -> None:
                    super().__init__()
                    self.status_checks = 0

                def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
                    if args == ["status", "--porcelain=v1"]:
                        self.calls.append((repo_path, tuple(args), allow_failure))
                        self.status_checks += 1
                        if self.status_checks == 1:
                            return FakeCiCommandResult(args, stdout=" M libs/child\n")
                        return FakeCiCommandResult(args)
                    return super().run(repo_path, args, allow_failure=allow_failure)

            runner = DirtyAfterCheckoutRunner()
            ctx = FakeCiContext(parent, runner)
            ctx.root_repo = repo

            ci_recovery.ensure_ready_after_state_change(
                ctx,
                repo,
                scope={"type": "test_dirty_checkout"},
                checkout_args=["checkout", "--detach", "targetsha"],
            )

            self.assertEqual(len(ctx.report.fallbacks), 1)
            fallback = ctx.report.fallbacks[0]
            self.assertEqual(fallback["scope"]["type"], "test_dirty_checkout")
            self.assertEqual(fallback["scope"]["post_state_change_attempt"], 1)
            self.assertIn("working tree not clean", "\n".join(fallback["scope"]["post_state_change_verify_errors"]))
            runner_args = [call[1] for call in runner.calls]
            self.assertIn(("checkout", "--detach", "targetsha"), runner_args)
            self.assertGreaterEqual(runner.status_checks, 3)

    def test_clean_and_prepare_repo_reaches_fresh_clone_after_all_cleanup_levels_fail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ci-fresh-clone-chain-") as temp_dir:
            parent = Path(temp_dir)
            repo = parent / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            (repo / ".gitmodules").write_text(
                '[submodule "libs/child"]\n\tpath = libs/child\n',
                encoding="utf-8",
            )

            class FreshCloneChainRunner(FakeCiRunner):
                def __init__(self) -> None:
                    super().__init__()
                    self.staging: Path | None = None

                def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
                    self.calls.append((repo_path, tuple(args), allow_failure))
                    if args == [
                        "config",
                        "-f",
                        ".gitmodules",
                        "--get-regexp",
                        r"^submodule\..*\.path$",
                    ]:
                        return FakeCiCommandResult(args, stdout="submodule.libs/child.path libs/child\n")
                    if args == ["remote", "get-url", "origin"]:
                        return FakeCiCommandResult(args, stdout=str(repo) + "\n")
                    if args[:4] == ["clone", "--recurse-submodules", "--origin", "origin"]:
                        self.staging = repo_path / args[-1]
                        self.staging.mkdir()
                        (self.staging / ".git").mkdir()
                        (self.staging / "fresh.txt").write_text("fresh\n", encoding="utf-8")
                        return FakeCiCommandResult(args)
                    if self.staging is not None and repo_path == self.staging:
                        if args in (
                            ["submodule", "sync", "--recursive"],
                            ["submodule", "update", "--init", "--recursive", "--force"],
                        ):
                            return FakeCiCommandResult(args)
                    if repo_path == repo or repo_path == repo / "libs" / "child":
                        if args in (
                            ["reset", "--hard"],
                            ["submodule", "sync", "--recursive"],
                            ["submodule", "update", "--init", "--recursive"],
                            ["submodule", "update", "--init", "--recursive", "--force"],
                        ):
                            return FakeCiCommandResult(args, exit_code=1, stderr="forced cleanup failure\n")
                    return FakeCiCommandResult(args)

            ctx = FakeCiContext(parent, FreshCloneChainRunner())
            ctx.root_repo = repo

            ci_recovery.clean_and_prepare_repo(ctx, repo, scope={"type": "test_chain"})

            self.assertEqual(
                [fallback["level"] for fallback in ctx.report.fallbacks],
                [
                    "level_1_basic_cleanup",
                    "level_2_abort_and_recursive_cleanup",
                    "level_3_sync_and_force_update",
                    "level_4_deep_submodule_cleanup",
                    "level_5_fresh_clone_workspace",
                ],
            )
            self.assertEqual([fallback["success"] for fallback in ctx.report.fallbacks], [False, False, False, False, True])
            self.assertTrue((repo / "fresh.txt").exists())
            self.assertTrue(Path(ctx.report.fallbacks[-1]["backup_path"]).exists())
            self.assertEqual(ctx.report.fallbacks[-1]["scope"], {"type": "test_chain"})

    def test_clean_and_prepare_repo_reports_clean_failed_when_fresh_clone_also_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ci-fresh-clone-fails-") as temp_dir:
            parent = Path(temp_dir)
            repo = parent / "repo"
            repo.mkdir()
            (repo / ".gitmodules").write_text(
                '[submodule "libs/child"]\n\tpath = libs/child\n',
                encoding="utf-8",
            )

            class FullyFailingCleanupRunner(FakeCiRunner):
                def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
                    self.calls.append((repo_path, tuple(args), allow_failure))
                    if args == [
                        "config",
                        "-f",
                        ".gitmodules",
                        "--get-regexp",
                        r"^submodule\..*\.path$",
                    ]:
                        return FakeCiCommandResult(args, stdout="submodule.libs/child.path libs/child\n")
                    if args == ["remote", "get-url", "origin"]:
                        return FakeCiCommandResult(args, exit_code=1, stderr="no remote\n")
                    if not allow_failure:
                        return FakeCiCommandResult(args, exit_code=1, stderr="forced cleanup failure\n")
                    return FakeCiCommandResult(args)

            ctx = FakeCiContext(parent, FullyFailingCleanupRunner())
            ctx.root_repo = repo

            with self.assertRaisesRegex(Exception, "repository cleanup failed after all fallback levels") as raised:
                ci_recovery.clean_and_prepare_repo(ctx, repo, scope={"type": "test_failure"})

            self.assertEqual(getattr(raised.exception, "exit_code", None), ExitCode.CLEAN_OR_RESTORE_FAILED)
            self.assertEqual(getattr(raised.exception, "reason", None), "CLEAN_FAILED")
            self.assertEqual(ctx.report.fallbacks[-1]["level"], "level_5_fresh_clone_workspace")
            self.assertEqual(ctx.report.fallbacks[-1]["failure_reason"], "FRESH_CLONE_REMOTE_URL_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
