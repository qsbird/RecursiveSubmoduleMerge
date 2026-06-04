import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ci_merge_split import commands as ci_commands
from ci_merge_split import models as ci_models
from ci_merge_split import runtime as ci_runtime
from ci_merge_split.publish import classify_publish_command_failure
from split_gitlink_merge import GitActionExecutor, GitCommandResult, RequiredCommitFetchError, RequiredCommitMissingError


def completed(args: list[str], returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class FakeBackend:
    def __init__(self, results: list[GitCommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[Path, tuple[str, ...], bool]] = []

    def run(self, repo_path: Path, args: list[str], *, check: bool = True) -> GitCommandResult:
        self.calls.append((repo_path, tuple(args), check))
        if not self.results:
            raise AssertionError("no fake result left")
        return self.results.pop(0)


def fake_context() -> object:
    return types.SimpleNamespace(
        root_repo=Path("repo").resolve(strict=False),
        report=types.SimpleNamespace(data={"state": ci_models.Phase.MERGING_SUBMODULE}),
    )


class CiNetworkResilienceTests(unittest.TestCase):
    def test_git_runner_retries_network_sensitive_command_and_records_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            args = ci_runtime.parse_arguments(
                [
                    "--source",
                    "feature",
                    "--target",
                    "main",
                    "--repo_path",
                    str(repo),
                    "--output_dir",
                    str(repo / "out"),
                    "--git-network-retries",
                    "2",
                    "--git-network-retry-delay",
                    "0",
                ]
            )
            ctx = ci_runtime.create_execution_context(args, "network-test")
            side_effect = [
                completed(["git"], 128, stderr="fatal: unable to access remote: Failed to connect"),
                completed(["git"], 0, stdout="ok\n"),
            ]

            with mock.patch("ci_merge_split.runtime.subprocess.run", side_effect=side_effect) as run:
                result = ctx.runner.run(repo, ["fetch", "origin", "--prune"])

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(ctx.report.data["commands"][0]["network_failure_reason"], "GIT_NETWORK_CONNECT_FAILED")
            self.assertEqual(ctx.report.data["commands"][0]["attempt"], 1)
            self.assertEqual(ctx.report.data["commands"][0]["max_attempts"], 3)
            self.assertEqual(ctx.report.data["commands"][1]["attempt"], 2)

    def test_git_runner_classifies_exhausted_network_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            args = ci_runtime.parse_arguments(
                [
                    "--source",
                    "feature",
                    "--target",
                    "main",
                    "--repo_path",
                    str(repo),
                    "--output_dir",
                    str(repo / "out"),
                    "--git-network-retries",
                    "1",
                    "--git-network-retry-delay",
                    "0",
                ]
            )
            ctx = ci_runtime.create_execution_context(args, "network-test")

            with mock.patch(
                "ci_merge_split.runtime.subprocess.run",
                return_value=completed(["git"], 128, stderr="fatal: unable to access remote: Could not resolve host: gitlab"),
            ) as run:
                with self.assertRaises(ci_models.CiMergeError) as raised:
                    ctx.runner.run(repo, ["ls-remote", "origin", "refs/heads/main"])

            self.assertEqual(run.call_count, 2)
            self.assertEqual(raised.exception.reason, "GIT_NETWORK_FAILED")
            self.assertEqual(raised.exception.details["command"]["network_failure_reason"], "GIT_NETWORK_DNS_FAILED")

    def test_commit_fetch_failure_and_missing_commit_are_distinct_ci_errors(self) -> None:
        repo = Path("repo")
        check_missing = GitCommandResult(["git", "cat-file"], repo, 1, stderr="missing")
        fetch_failed = GitCommandResult(["git", "fetch"], repo, 128, stderr="Could not resolve host: gitlab")
        actions = GitActionExecutor(FakeBackend([check_missing, fetch_failed]))

        with self.assertRaises(RequiredCommitFetchError) as fetch_error:
            actions.ensure_commit_available(repo, "abc123", fetch_missing=True)
        converted_fetch = ci_commands.convert_merge_exception(fake_context(), fetch_error.exception)
        self.assertIsNotNone(converted_fetch)
        assert converted_fetch is not None
        self.assertEqual(converted_fetch.reason, "SUBMODULE_COMMIT_FETCH_FAILED")
        self.assertEqual(converted_fetch.exit_code, ci_models.ExitCode.GIT_COMMAND_FAILED)

        actions = GitActionExecutor(
            FakeBackend(
                [
                    GitCommandResult(["git", "cat-file"], repo, 1, stderr="missing"),
                    GitCommandResult(["git", "fetch"], repo, 0),
                    GitCommandResult(["git", "cat-file"], repo, 1, stderr="missing"),
                ]
            )
        )
        with self.assertRaises(RequiredCommitMissingError) as missing_error:
            actions.ensure_commit_available(repo, "def456", fetch_missing=True)
        converted_missing = ci_commands.convert_merge_exception(fake_context(), missing_error.exception)
        self.assertIsNotNone(converted_missing)
        assert converted_missing is not None
        self.assertEqual(converted_missing.reason, "SUBMODULE_COMMIT_NOT_FOUND")
        self.assertEqual(converted_missing.exit_code, ci_models.ExitCode.SUBMODULE_CONFLICT)

    def test_publish_failure_uses_runner_network_classification(self) -> None:
        result = types.SimpleNamespace(stderr="", network_failure_reason="GIT_NETWORK_DNS_FAILED")

        self.assertEqual(classify_publish_command_failure(result), "PUBLISH_NETWORK_FAILED")


if __name__ == "__main__":
    unittest.main()
