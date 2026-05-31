from __future__ import annotations

import types
import unittest
import tempfile
import json
from pathlib import Path

from ci_merge_split import CiWorkflowServices
from ci_merge_split import artifacts as ci_artifacts
from ci_merge_split import common as ci_common
from ci_merge_split import commands as ci_commands
from ci_merge_split import message_builder
from ci_merge_split import models as ci_models
from ci_merge_split import recovery as ci_recovery
from ci_merge_split import repository_ops as ci_repository_ops
from ci_merge_split import runtime as ci_runtime
from ci_merge_split.legacy import load_ci_merge_module
from ci_merge_split.publish import classify_publish_failure, infer_publish_target_from_report, publish_result, report_value, run_publish_only_workflow, target_requires_explicit_publish_target
from ci_merge_split.publish import PUBLISH_FUNCTIONS
from ci_merge_split.report_artifacts import REPORT_ARTIFACT_FUNCTIONS
from ci_merge_split.repository_guards import REPOSITORY_GUARD_FUNCTIONS
from ci_merge_split.snapshot_restore import SNAPSHOT_RESTORE_FUNCTIONS
from manual_merge_split import ManualWorkflowServices
from manual_merge_split import common as manual_common
from manual_merge_split import execution as manual_execution
from manual_merge_split import models as manual_models
from manual_merge_split import runtime as manual_runtime
from manual_merge_split.commands import ManualCommandService, process_repository, write_report
from manual_merge_split.cross_submodule_moves import CROSS_SUBMODULE_MOVE_FUNCTIONS, apply_cross_submodule_moved_files, collect_cross_submodule_moves, describe_cross_submodule_move, merge_cross_submodule_file_contents
from manual_merge_split.file_conflicts import FILE_CONFLICT_FUNCTIONS, record_file_conflicts, render_conflict_tool_command, resolve_file_conflicts, resolve_moved_away_file_conflicts, resolve_single_file_conflict
from manual_merge_split.guided_commands import GUIDED_COMMAND_FUNCTIONS, prompt_optional, prompt_with_default, prompt_yes_no, run_guided
from manual_merge_split.legacy import load_manual_merge_module
from manual_merge_split.preflight import PREFLIGHT_FUNCTIONS, build_preserved_dirty_path_map, format_dirty_submodule_list
from manual_merge_split.repository_ops import REPOSITORY_OP_FUNCTIONS


def fake_module(function_names: set[str]) -> tuple[types.SimpleNamespace, list[tuple[str, tuple[object, ...], dict[str, object]]]]:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def make(name: str):
        def func(*args: object, **kwargs: object) -> object:
            calls.append((name, args, kwargs))
            if name == "branch_for_depth":
                return "branch-from-fake"
            if name == "root_merge_title":
                return "title-from-fake"
            if name == "commit_body":
                return "body-from-fake"
            if name in {"verify_repo_ready", "resolve_commit_variants_for_refs"}:
                return True, []
            if name in {"split_conflicts", "resolve_file_conflicts", "apply_cross_submodule_moved_files"}:
                return [], "", False
            if name in {"get_submodule_paths", "collect_submodule_move_candidate_info", "collect_cross_submodule_moves"}:
                return []
            if name in {"collect_submodule_lineages"}:
                return set()
            if name == "assess_dirty_submodule_impact":
                return False, "safe"
            if name == "ensure_commit_available":
                return True
            if name == "publish_remote_ref_sha":
                return "remote-sha"
            if name == "analyze_conflicts":
                return types.SimpleNamespace(conflicts=[])
            if name == "export_repository_patches":
                return {"repository": None, "files": []}
            if name == "export_changed_file_artifacts":
                return {"files": []}
            if name == "artifact_category_dir":
                return Path("artifacts")
            if name == "relative_output_path":
                return "relative"
            return {"called": name}

        return func

    return types.SimpleNamespace(**{name: make(name) for name in function_names}), calls


class FakeCiCommandResult:
    def __init__(self, args: list[str], stdout: str = "", exit_code: int = 0) -> None:
        self.args = args
        self.cmd = ["git", *args]
        self.cwd = "repo"
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "cmd": self.cmd,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class FakeCiRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...], bool]] = []

    def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
        self.calls.append((repo_path, tuple(args), allow_failure))
        if args == ["rev-parse", "--verify", "HEAD^{commit}"]:
            return FakeCiCommandResult(args, stdout="headsha\n")
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return FakeCiCommandResult(args, stdout="true\n")
        if args == ["rev-parse", "--show-toplevel"]:
            return FakeCiCommandResult(args, stdout=str(repo_path.resolve(strict=False)) + "\n")
        if args == ["rev-parse", "--verify", "refs/remotes/origin/source^{commit}"]:
            return FakeCiCommandResult(args, stdout="sourcesha\n")
        if args in (
            ["rev-parse", "--verify", "refs/remotes/origin/target^{commit}"],
            ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
        ):
            return FakeCiCommandResult(args, stdout="targetsha\n")
        if args == ["merge-base", "sourcesha", "targetsha"]:
            return FakeCiCommandResult(args, stdout="basesha\n")
        if args == ["rev-parse", "HEAD"]:
            return FakeCiCommandResult(args, stdout="headsha\n")
        if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
            return FakeCiCommandResult(args, stdout="main\n")
        if args == ["rev-parse", "FETCH_HEAD"]:
            return FakeCiCommandResult(args, stdout="targetsha\n")
        if args[:1] == ["ls-remote"]:
            return FakeCiCommandResult(args, stdout=f"resultsha\t{args[-1]}\n")
        if args == ["cat-file", "-t", "blobsha"]:
            return FakeCiCommandResult(args, stdout="blob\n")
        if args[:1] == ["diff"] and "--name-status" in args:
            return FakeCiCommandResult(args, stdout="M\tfile.txt\n")
        if args[:1] == ["diff"] and "--stat" in args:
            return FakeCiCommandResult(args, stdout=" file.txt | 1 +\n")
        if args[:1] == ["diff"] and "--no-ext-diff" in args:
            return FakeCiCommandResult(args, stdout="diff --git a/file.txt b/file.txt\n")
        if args[:1] == ["log"]:
            return FakeCiCommandResult(args, stdout="abc\tAlice\ta@example.com\t2026-05-30T00:00:00+08:00\tSubject\n")
        return FakeCiCommandResult(args)

    def run_binary(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> tuple[FakeCiCommandResult, bytes]:
        self.calls.append((repo_path, tuple(args), allow_failure))
        return FakeCiCommandResult(args), b"content"


class FakeCiReport:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.data: dict[str, object] = {
            "status": "running",
            "exit_code": None,
            "publish": {
                "root": {"candidate_ref_pushed": False, "target_pushed": False, "success": False},
            },
            "publish_only": {},
            "input": {"remote": "origin", "target": "main"},
            "resolved": {},
            "batch": {},
            "conflicts": [],
            "summary": {},
            "repositories": [],
            "preflight_runs": [],
            "preflight": {},
            "remote_sync": {"attempted": False, "success": None, "started_at": None, "ended_at": None},
            "commands": [],
            "errors": [],
            "restore": {"attempted": False, "success": None, "errors": [], "results": []},
        }
        self.conflicts: list[dict[str, object]] = []
        self.repositories: list[dict[str, object]] = []
        self.fallbacks: list[dict[str, object]] = []
        self.snapshots: list[dict[str, object]] = []
        self.log: list[str] = []
        self.console_messages: list[str] = []

    def add_conflicts(self, conflicts: list[dict[str, object]]) -> None:
        self.conflicts.extend(conflicts)

    def add_repository(self, repository: dict[str, object]) -> None:
        self.repositories.append(repository)

    def add_fallback(self, fallback: dict[str, object]) -> None:
        self.fallbacks.append(fallback)

    def add_snapshot(self, snapshot: dict[str, object]) -> None:
        self.snapshots.append(snapshot)

    def append_log(self, message: str) -> None:
        self.log.append(message)

    def console(self, message: str) -> None:
        self.console_messages.append(message)

    def set_success(self) -> None:
        self.data["status"] = "success"

    def set_state(self, state: str) -> None:
        self.data["state"] = state

    def add_error(self, error: object) -> None:
        to_report = getattr(error, "to_report_error", None)
        self.data["errors"].append(to_report() if callable(to_report) else error)

    def set_failure(self, error: object) -> None:
        self.data["status"] = "failed"
        self.data["exit_code"] = getattr(error, "exit_code", 1)


class FakeCiContext:
    def __init__(self, output_dir: Path, runner: FakeCiRunner | None = None) -> None:
        self.args = types.SimpleNamespace(
            source="source",
            sources=["source"],
            repo_path=str(Path("repo").resolve(strict=False)),
            work_branch="root-work",
            timeout=1800,
            max_depth=16,
            diff_context_lines=3,
            git="git",
            remote="origin",
            target="main",
            publish=False,
            publish_only=False,
            publish_target=None,
            publish_mode="push",
            candidate_ref=None,
            from_report=None,
            allow_partial_publish=False,
            batch_failure_mode="fail-fast",
        )
        self.job_id = "job 42"
        self.runner = runner or FakeCiRunner()
        self.root_repo = Path("repo").resolve(strict=False)
        self.report = FakeCiReport(output_dir)
        self.merge_step = None
        self.snapshots: list[dict[str, object]] = []
        self.allow_clean_x = False

    def set_state(self, state: str) -> None:
        self.report.data["state"] = state


class FakeManualGitResult:
    def __init__(self, args: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.args = args
        self.cwd = Path("repo")
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeManualGitRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...], dict[str, object]]] = []

    def run(self, repo_path: Path, args: list[str], **kwargs: object) -> FakeManualGitResult:
        self.calls.append((repo_path, tuple(args), kwargs))
        if args[:2] == ["rev-parse", "--verify"]:
            return FakeManualGitResult(args, stdout="abc123\n")
        return FakeManualGitResult(args)


class FakeManualBinaryResult:
    def __init__(self, args: list[str], stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self.args = args
        self.cwd = Path("repo")
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakePublishRunner(FakeCiRunner):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_head_reads = 0

    def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
        self.calls.append((repo_path, tuple(args), allow_failure))
        if args == ["rev-parse", "FETCH_HEAD"]:
            self.fetch_head_reads += 1
            return FakeCiCommandResult(args, stdout=("targetsha\n" if self.fetch_head_reads == 1 else "resultsha\n"))
        return FakeCiCommandResult(args)


class RemainingSplitFacadeTests(unittest.TestCase):
    def test_ci_workflow_services_group_original_ci_responsibilities(self) -> None:
        names = set(REPORT_ARTIFACT_FUNCTIONS + REPOSITORY_GUARD_FUNCTIONS + SNAPSHOT_RESTORE_FUNCTIONS + PUBLISH_FUNCTIONS)
        module, calls = fake_module(names)
        runner = FakeCiRunner()
        ctx = FakeCiContext(Path("out"), runner)
        repo_path = Path(".")

        services = CiWorkflowServices.from_module(module)  # type: ignore[arg-type]
        preflight_result = services.guards.preflight_check(
            ctx,
            repo_path,
            "source",
            "target",
            label="initial",
            resolve_refs=True,
        )
        services.guards.sync_remote_refs(ctx, repo_path)
        services.guards.ensure_clean_after_merge(ctx, repo_path)
        snapshot = services.snapshots.record_snapshot(ctx, repo_path, "source", "target", 0)
        remote_sha = services.publish.publish_remote_ref_sha(ctx, repo_path, "origin", "refs/heads/main", "fallback")
        branch = services.messages.branch_for_depth(ctx, 2)
        conflicts = services.report_artifacts.analyze_conflicts(ctx, Path("repo")).conflicts
        remote_ref = services.guards.remote_first_ref("feature", "origin")

        self.assertEqual(branch, "ci-submodule-merge/job-42-d2")
        self.assertTrue(preflight_result.passed)
        self.assertEqual(preflight_result.source_sha, "sourcesha")
        self.assertEqual(ctx.report.data["preflight"]["passed"], True)
        self.assertEqual(ctx.report.data["remote_sync"]["success"], True)
        self.assertEqual(snapshot["pre_merge_head"], "headsha")
        self.assertEqual(snapshot["current_branch"], "main")
        self.assertEqual(remote_sha, "resultsha")
        self.assertEqual(conflicts[0]["type"], "unknown_conflict_state")
        self.assertEqual(remote_ref, ("refs/remotes/origin/feature", "remote_tracking"))
        runner_args = [call[1] for call in runner.calls]
        self.assertIn(("diff", "--name-only", "--diff-filter=U"), runner_args)
        self.assertIn(("diff", "--cached", "--raw", "--"), runner_args)
        called_names = [item[0] for item in calls]
        self.assertNotIn("preflight_check", called_names)
        self.assertNotIn("sync_remote_refs", called_names)
        self.assertNotIn("publish_remote_ref_sha", called_names)
        self.assertNotIn("analyze_conflicts", called_names)

    def test_manual_workflow_services_group_original_manual_responsibilities(self) -> None:
        names = set(
            PREFLIGHT_FUNCTIONS
            + FILE_CONFLICT_FUNCTIONS
            + CROSS_SUBMODULE_MOVE_FUNCTIONS
            + REPOSITORY_OP_FUNCTIONS
            + GUIDED_COMMAND_FUNCTIONS
        )
        module, calls = fake_module(names)
        manual_git = FakeManualGitRunner()
        module.run_git = manual_git.run

        services = ManualWorkflowServices.from_module(module)  # type: ignore[arg-type]
        dirty_impact = services.preflight.assess_dirty_submodule_impact(Path("repo"), ("sub",), ["base", "feature"])
        conflict_context = manual_models.MergeContext(
            work_branch="work",
            merge_branch="merge",
            file_conflict_mode="preserve",
            conflict_tool_command="tool",
            conflict_tool_timeout_seconds=1,
        )
        unresolved, reason, rollback = services.file_conflicts.resolve_file_conflicts(Path("repo"), ["demo.txt"], conflict_context)
        services.cross_submodule_moves.collect_cross_submodule_moves(Path("repo"), ["old-lib"])
        services.repository_ops.ensure_commit_available(Path("repo"), "abc123")
        branch_tokens = services.guided_commands.parse_branch_tokens("a b")

        self.assertEqual(branch_tokens, ["a b"])
        self.assertEqual(dirty_impact[0], True)
        self.assertEqual(unresolved, ["demo.txt"])
        self.assertIn("preserve conflicts", reason)
        self.assertFalse(rollback)
        called_names = [item[0] for item in calls]
        self.assertNotIn("resolve_file_conflicts", called_names)
        self.assertNotIn("collect_cross_submodule_moves", called_names)
        self.assertIn(("ls-files", "-u", "--", "old-lib"), [item[1] for item in manual_git.calls])
        self.assertIn(("rev-parse", "--verify", "abc123^{commit}"), [item[1] for item in manual_git.calls])

    def test_manual_services_can_use_local_execution_defaults(self) -> None:
        from manual_merge_split.commands import ManualCommandService
        from manual_merge_split.repository_ops import ManualRepositoryOps

        module, _calls = fake_module(set(REPOSITORY_OP_FUNCTIONS))

        self.assertIsNotNone(ManualRepositoryOps(module))  # type: ignore[arg-type]
        self.assertIsNotNone(ManualRepositoryOps())
        self.assertIsNotNone(ManualCommandService(module))  # type: ignore[arg-type]
        self.assertIsNotNone(ManualCommandService())
        self.assertIsNotNone(ManualWorkflowServices.from_local())

    def test_manual_local_git_execution_is_available(self) -> None:
        result = manual_execution.run_git(Path.cwd(), ["--version"])
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("git version"))
        failure = manual_execution.format_git_failure(
            manual_models.GitResult(
                args=["git", "demo"],
                cwd=Path("repo"),
                returncode=1,
                stdout="out",
                stderr="err",
            )
        )
        self.assertEqual(failure, "Command failed in repo: git demo\nstdout:\nout\nstderr:\nerr")


class ExtractedImplementationParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ci_merge = load_ci_merge_module()
        cls.manual_merge = load_manual_merge_module()

    def test_ci_report_low_coupling_helpers_match_original(self) -> None:
        commit_log = "abc\tAlice\ta@example.com\t2026-05-30\tSubject\nbroken line\n"
        name_status = "M\tfile.txt\nR100\told.txt\tnew.txt\nD\tgone.txt\n"
        unmerged_index = "160000 abc 1\tlibs/a\n160000 def 2\tlibs/a\n160000 123 3\tlibs/a\nbad\n"
        conflict = {"stages": ci_common.parse_unmerged_index(unmerged_index)["libs/a"]}

        self.assertEqual(ci_common.parse_commit_log(commit_log), self.ci_merge.parse_commit_log(commit_log))
        self.assertEqual(ci_common.parse_name_status(name_status), self.ci_merge.parse_name_status(name_status))
        self.assertEqual(ci_common.sanitize_patch_name("a/b:c*.txt", "fallback"), self.ci_merge.sanitize_patch_name("a/b:c*.txt", "fallback"))
        self.assertEqual(ci_common.parse_unmerged_index(unmerged_index), self.ci_merge.parse_unmerged_index(unmerged_index))
        self.assertEqual(ci_common.stage_sha(conflict, "2"), self.ci_merge.stage_sha(conflict, "2"))
        self.assertEqual(ci_repository_ops.remote_first_ref("feature", "origin"), self.ci_merge.remote_first_ref("feature", "origin"))
        self.assertEqual(ci_repository_ops.remote_first_ref("refs/heads/main", "origin"), self.ci_merge.remote_first_ref("refs/heads/main", "origin"))
        self.assertEqual(ci_repository_ops.should_resolve_ref_directly("HEAD~1", "origin"), self.ci_merge.should_resolve_ref_directly("HEAD~1", "origin"))
        self.assertEqual(classify_publish_failure("non-fast-forward"), self.ci_merge.classify_publish_failure("non-fast-forward"))
        for target in (
            "HEAD",
            "FETCH_HEAD",
            "abc1234",
            "origin/main",
            "refs/remotes/origin/main",
            "refs/tags/v1",
            "refs/merge/foo",
            "refs/heads/main",
            "main",
            "feature~1",
            "feature:path",
        ):
            self.assertEqual(
                target_requires_explicit_publish_target(target, "origin"),
                self.ci_merge.target_requires_explicit_publish_target(target, "origin"),
            )

        nested = {"a": {"b": 0}, "x": 1}
        self.assertEqual(report_value(nested, "a", "b"), self.ci_merge.report_value(nested, "a", "b"))
        self.assertEqual(report_value(nested, "x", "missing"), self.ci_merge.report_value(nested, "x", "missing"))

        ctx = types.SimpleNamespace(args=types.SimpleNamespace(publish_target=None, remote="origin"))
        report = {"input": {"target": "main"}, "publish": {"target": None}, "resolved": {"target_ref": "refs/heads/main"}}
        self.assertEqual(infer_publish_target_from_report(ctx, report), self.ci_merge.infer_publish_target_from_report(ctx, report))

    def test_ci_command_helpers_match_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            local_ctx = FakeCiContext(Path(temp_dir))
            original_ctx = FakeCiContext(Path(temp_dir))
            local_ctx.root_repo = root
            original_ctx.root_repo = root

            kwargs = {
                "source_ref": "refs/remotes/origin/source",
                "source_sha": "sourcesha",
                "target_ref": "refs/remotes/origin/main",
                "target_sha": "targetsha",
                "merge_base_sha": "basesha",
                "result_sha": "resultsha",
            }
            ci_commands.set_resolved_fields(local_ctx, **kwargs)
            self.ci_merge.set_resolved_fields(original_ctx, **kwargs)
            self.assertEqual(local_ctx.report.data["resolved"], original_ctx.report.data["resolved"])

            self.assertEqual(
                ci_commands.batch_step_artifact_scope(local_ctx, "batch/step-01"),
                self.ci_merge.batch_step_artifact_scope(original_ctx, "batch/step-01"),
            )
            local_ctx.report.data["commands"] = [{"cmd": ["git"]}, {"cmd": ["git"]}]
            original_ctx.report.data["commands"] = [{"cmd": ["git"]}, {"cmd": ["git"]}]
            self.assertEqual(ci_commands.command_range(local_ctx, 1), self.ci_merge.command_range(original_ctx, 1))
            self.assertEqual(
                ci_commands.manual_publish_info(local_ctx, "targetsha", "resultsha"),
                self.ci_merge.manual_publish_info(original_ctx, "targetsha", "resultsha"),
            )
            self.assertEqual(
                ci_commands.can_attempt_cleanup(local_ctx, root),
                self.ci_merge.can_attempt_cleanup(original_ctx, root),
            )

        passed = ci_models.PreflightResult(True, [], [], source_sha="source", target_sha="target")
        self.assertIsNone(ci_commands.ensure_preflight_shas(passed))
        self.assertIsNone(self.ci_merge.ensure_preflight_shas(passed))

        failed = ci_models.PreflightResult(False, [], [{"name": "working_tree_clean"}])
        with self.assertRaises(ci_models.CiMergeError) as local_error:
            ci_commands.fail_preflight(failed)
        with self.assertRaises(self.ci_merge.CiMergeError) as original_error:
            self.ci_merge.fail_preflight(failed)
        self.assertEqual(local_error.exception.exit_code, original_error.exception.exit_code)
        self.assertEqual(local_error.exception.phase, original_error.exception.phase)
        self.assertEqual(local_error.exception.reason, original_error.exception.reason)

    def test_ci_runtime_args_and_report_match_original_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            argv = [
                "--source",
                "feature",
                "--target",
                "main",
                "--repo_path",
                str(repo),
                "--output_dir",
                str(Path(temp_dir) / "out"),
                "--publish",
                "true",
                "--publish_target",
                "main",
                "--job_id",
                "job 42",
            ]
            local_args = ci_runtime.parse_arguments(argv)
            original_args = self.ci_merge.parse_arguments(argv)
            ci_runtime.normalize_args(local_args, "job-42")
            self.ci_merge.normalize_args(original_args, "job-42")

            self.assertEqual(local_args.sources, original_args.sources)
            self.assertEqual(local_args.work_branch, original_args.work_branch)
            self.assertEqual(local_args.candidate_ref, original_args.candidate_ref)
            ci_runtime.validate_args(local_args)
            self.ci_merge.validate_args(original_args)

            local_paths = ci_runtime.resolve_output_paths(local_args, "job-42")
            original_paths = self.ci_merge.resolve_output_paths(original_args, "job-42")
            self.assertEqual(tuple(map(str, local_paths)), tuple(map(str, original_paths)))

            local_report = ci_runtime.JsonReport(local_args, "job-42", *local_paths)
            original_report = self.ci_merge.JsonReport(original_args, "job-42", *original_paths)
            for data in (local_report.data, original_report.data):
                data.pop("started_at", None)
            self.assertEqual(local_report.data["input"], original_report.data["input"])
            self.assertEqual(local_report.data["batch"], original_report.data["batch"])
            self.assertEqual(local_report.data["publish"], original_report.data["publish"])
            self.assertEqual(set(local_report.data), set(original_report.data))

    def test_ci_runtime_git_runner_is_local(self) -> None:
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
                    "--job_id",
                    "runtime-test",
                    "--timeout",
                    "30",
                ]
            )
            ctx = ci_runtime.create_execution_context(args, "runtime-test")
            result = ctx.runner.run(repo, ["--version"])
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.stdout.startswith("git version"))
            self.assertEqual(ctx.report.data["commands"][0]["cmd"][0], "git")

    def test_ci_command_single_workflow_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            ctx = FakeCiContext(Path(temp_dir))
            ctx.root_repo = root
            merge_calls: list[dict[str, object]] = []

            def merge_repo(ctx_arg: object, repo_path: Path, **kwargs: object) -> object:
                merge_calls.append({"ctx": ctx_arg, "repo_path": repo_path, **kwargs})
                return types.SimpleNamespace(result_sha="resultsha", strategy="clean_merge")

            ci_commands.run_single_workflow(ctx, CiWorkflowServices.from_local(), merge_repo)

            self.assertEqual(ctx.report.data["status"], "success")
            self.assertEqual(ctx.report.data["resolved"]["source_sha"], "sourcesha")
            self.assertEqual(ctx.report.data["resolved"]["target_sha"], "targetsha")
            self.assertEqual(ctx.report.data["resolved"]["merge_base_sha"], "basesha")
            self.assertEqual(ctx.report.data["resolved"]["result_sha"], "resultsha")
            self.assertEqual([run["label"] for run in ctx.report.data["preflight_runs"]], ["initial", "final"])
            self.assertEqual(merge_calls[0]["repo_path"], root)
            self.assertEqual(merge_calls[0]["source_sha"], "sourcesha")
            self.assertEqual(merge_calls[0]["target_sha"], "targetsha")

    def test_ci_command_run_workflow_uses_local_cleanup_and_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            runner = FakeCiRunner()
            ctx = FakeCiContext(Path(temp_dir), runner)
            ctx.root_repo = root
            merge_calls: list[dict[str, object]] = []

            def merge_repo(ctx_arg: object, repo_path: Path, **kwargs: object) -> object:
                merge_calls.append({"ctx": ctx_arg, "repo_path": repo_path, **kwargs})
                return types.SimpleNamespace(result_sha="resultsha", strategy="clean_merge")

            ci_commands.run_workflow(ctx, CiWorkflowServices.from_local(), merge_repo)

            self.assertEqual(ctx.report.data["status"], "success")
            self.assertEqual(ctx.report.data["remote_sync"]["success"], True)
            self.assertEqual(len(merge_calls), 1)
            runner_args = [call[1] for call in runner.calls]
            self.assertIn(("reset", "--hard"), runner_args)
            self.assertIn(("fetch", "origin", "--prune"), runner_args)

    def test_ci_artifacts_write_patch_and_repository_details_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            repo = output_dir / "repo"
            repo.mkdir()
            ctx = FakeCiContext(output_dir)
            changed_files = [{"status": "M", "path": "file.txt"}]
            warnings: list[dict[str, object]] = []

            patches = ci_artifacts.export_repository_patches(
                ctx,
                repo,
                ".",
                "ours",
                "result",
                changed_files,
                warnings,
            )
            self.assertTrue(Path(patches["repository"]["path"]).exists())
            self.assertEqual(changed_files[0]["patch_relative_path"], "patches/root/files/0001-file.txt.patch")

            repository = ci_artifacts.collect_repository_details(
                ctx,
                repo,
                ".",
                "base",
                "ours",
                "theirs",
                "result",
                "strategy",
            )
            self.assertEqual(repository["path"], ".")
            self.assertEqual(ctx.report.repositories[-1]["result"], "result")

    def test_ci_recovery_helpers_are_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = FakeCiContext(Path(temp_dir))
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            cleanup = ci_recovery.run_cleanup_steps(
                ctx,
                repo,
                "level",
                [{"args": ["reset", "--hard"]}],
            )
            self.assertTrue(cleanup["success"])
            self.assertEqual(cleanup["steps"][0]["cmd"], ["git", "reset", "--hard"])

            ci_recovery.clean_and_prepare_repo(ctx, repo)
            self.assertEqual(ctx.report.fallbacks[-1]["success"], True)

            snapshot = ci_recovery.record_snapshot(ctx, repo, "source", "target", 0)
            self.assertEqual(snapshot["pre_merge_head"], "headsha")
            success = ci_recovery.restore_all_snapshots(ctx)
            self.assertTrue(success)
            restore_data = ctx.report.data["restore"]
            self.assertTrue(restore_data["attempted"])
            self.assertTrue(restore_data["success"])

    def test_ci_publish_result_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = FakePublishRunner()
            ctx = FakeCiContext(Path(temp_dir), runner)
            repo = Path(temp_dir) / "repo"
            repo.mkdir()

            publish = publish_result(ctx, repo, "targetsha", "resultsha")

            self.assertTrue(publish["success"])
            self.assertEqual(publish["remote_target_before_sha"], "targetsha")
            self.assertEqual(publish["remote_target_after_sha"], "resultsha")
            pushed = [call[1] for call in runner.calls if call[1] and call[1][0] == "push"]
            self.assertIn(
                (
                    "push",
                    "--recurse-submodules=check",
                    "--force-with-lease=main:targetsha",
                    "origin",
                    "resultsha:main",
                ),
                pushed,
            )

    def test_ci_publish_only_workflow_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "merge-report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "input": {"remote": "upstream", "target": "main"},
                        "publish": {"remote": "upstream", "target": "main"},
                        "resolved": {
                            "target_ref": "main",
                            "target_sha": "targetsha",
                            "result_sha": "resultsha",
                        },
                        "batch": {},
                        "repositories": [],
                        "conflicts": [],
                        "summary": {"repositories_visited": 1},
                    }
                ),
                encoding="utf-8",
            )
            runner = FakePublishRunner()
            ctx = FakeCiContext(root, runner)
            ctx.root_repo = root / "repo"
            ctx.root_repo.mkdir()
            ctx.args.from_report = str(report_path)

            run_publish_only_workflow(ctx)

            self.assertEqual(ctx.report.data["status"], "success")
            self.assertEqual(ctx.args.remote, "upstream")
            self.assertEqual(ctx.args.publish_target, "main")
            self.assertEqual(ctx.report.data["publish_only"]["source_report_status"], "success")
            self.assertTrue(ctx.report.data["publish"]["success"])
            pushed = [call[1] for call in runner.calls if call[1] and call[1][0] == "push"]
            self.assertIn(
                (
                    "push",
                    "--recurse-submodules=check",
                    "--force-with-lease=main:targetsha",
                    "upstream",
                    "resultsha:main",
                ),
                pushed,
            )

    def test_ci_models_match_original_shapes(self) -> None:
        local_command = ci_models.CommandResult(
            cmd=["git", "status"],
            cwd="repo",
            exit_code=1,
            stdout="out",
            stderr="err",
            started_at="start",
            ended_at="end",
            duration_ms=7,
            allow_failure=True,
            timed_out=False,
        )
        original_command = self.ci_merge.CommandResult(**local_command.to_dict())
        self.assertEqual(local_command.to_dict(), original_command.to_dict())

        local_error = ci_models.CiMergeError(
            ci_models.ExitCode.ARGUMENT_ERROR,
            ci_models.Phase.INIT,
            "REASON",
            "message",
            details={"path": "demo"},
        ).to_report_error()
        original_error = self.ci_merge.CiMergeError(
            self.ci_merge.ExitCode.ARGUMENT_ERROR,
            self.ci_merge.Phase.INIT,
            "REASON",
            "message",
            details={"path": "demo"},
        ).to_report_error()
        local_error.pop("timestamp")
        original_error.pop("timestamp")
        self.assertEqual(local_error, original_error)

    def test_ci_message_builder_matches_original(self) -> None:
        ctx = types.SimpleNamespace(
            args=types.SimpleNamespace(source="feature/ref", target="main/ref", work_branch="work/ref"),
            job_id="job 42",
            merge_step=None,
            report=types.SimpleNamespace(
                data={
                    "resolved": {
                        "source_ref": "refs/remotes/origin/feature/ref",
                        "target_ref": "refs/remotes/origin/main/ref",
                        "source_sha": "a" * 40,
                        "target_sha": "b" * 40,
                    }
                }
            ),
        )
        kwargs = {
            "repo_path": Path("repo"),
            "display_path": ".",
            "source_sha": "a" * 40,
            "target_sha": "b" * 40,
            "base_sha": "c" * 40,
            "depth": 0,
            "strategy": "recursive_gitlink_merge",
            "resolved_gitlinks": [{"path": "libs/core"}],
        }

        local_message = message_builder.build_merge_commit_message(ctx, **kwargs)
        original_message = self.ci_merge.build_merge_commit_message(ctx, **kwargs)
        self.assertEqual((local_message.title, local_message.body), (original_message.title, original_message.body))
        self.assertEqual(message_builder.branch_for_depth(ctx, 2), self.ci_merge.branch_for_depth(ctx, 2))

    def test_manual_low_coupling_helpers_match_original(self) -> None:
        self.assertEqual(manual_common.infer_clone_directory_name("git@example.com:org/repo.git"), self.manual_merge.infer_clone_directory_name("git@example.com:org/repo.git"))
        self.assertEqual(manual_common.normalize_status_relative_path("old/name -> new/name/"), self.manual_merge.normalize_status_relative_path("old/name -> new/name/"))
        self.assertEqual(manual_common.looks_like_commit_id("abcdef1"), self.manual_merge.looks_like_commit_id("abcdef1"))
        self.assertEqual(manual_common.normalize_user_merge_ref("feature"), self.manual_merge.normalize_user_merge_ref("feature"))
        self.assertEqual(manual_common.remote_tracking_ref("origin/feature"), self.manual_merge.remote_tracking_ref("origin/feature"))
        self.assertEqual(manual_common.parse_branch_tokens("a,b c"), self.manual_merge.parse_branch_tokens("a,b c"))
        self.assertEqual(manual_common.normalize_argv(["--repo", "x"]), self.manual_merge.normalize_argv(["--repo", "x"]))
        self.assertEqual(manual_common.unique_preserving_order(["a", "b", "a"]), self.manual_merge.unique_preserving_order(["a", "b", "a"]))
        self.assertEqual(manual_common.compute_work_branch("custom"), self.manual_merge.compute_work_branch("custom"))
        self.assertEqual(
            render_conflict_tool_command(Path("repo"), "src/file.txt", 'tool "{file}" --repo "{repo}" --rel "{relative}"'),
            self.manual_merge.render_conflict_tool_command(Path("repo"), "src/file.txt", 'tool "{file}" --repo "{repo}" --rel "{relative}"'),
        )
        move = manual_models.CrossSubmoduleMove(
            old_submodule_path="old",
            old_repo_path=Path("old"),
            old_file_path="a.txt",
            old_base_commit="base",
            old_other_commit="other",
            new_submodule_path="new",
            new_repo_path=Path("new"),
            new_file_path="a.txt",
            new_added_commit="added",
            move_side=3,
            score=1.0,
        )
        original_move = self.manual_merge.CrossSubmoduleMove(**move.__dict__)
        self.assertEqual(describe_cross_submodule_move(move), self.manual_merge.describe_cross_submodule_move(original_move))

        left = b"line one\nline two\n"
        right = b"line one\nline three\n"
        self.assertAlmostEqual(manual_common.file_content_similarity(left, right), self.manual_merge.file_content_similarity(left, right))
        self.assertAlmostEqual(manual_common.path_name_similarity("src/demo.txt", "dst/demo.txt"), self.manual_merge.path_name_similarity("src/demo.txt", "dst/demo.txt"))
        self.assertAlmostEqual(
            manual_common.score_cross_submodule_move_candidate("src/demo.txt", "dst/demo.txt", left, right),
            self.manual_merge.score_cross_submodule_move_candidate("src/demo.txt", "dst/demo.txt", left, right),
        )

    def test_manual_models_match_original_shapes(self) -> None:
        local_entry = manual_models.ConflictEntry(Path("repo"), ["a.txt"], " reason ", "missing_submodule_commit")
        original_entry = self.manual_merge.ConflictEntry(Path("repo"), ["a.txt"], " reason ", "missing_submodule_commit")
        self.assertEqual(local_entry.__dict__, original_entry.__dict__)

        local_context = manual_models.MergeContext(
            work_branch="work",
            merge_branch="merge",
            file_conflict_mode="tool",
            conflict_tool_command="code",
            conflict_tool_timeout_seconds=1,
            report_entries=[local_entry],
        )
        original_context = self.manual_merge.MergeContext(
            work_branch="work",
            merge_branch="merge",
            file_conflict_mode="tool",
            conflict_tool_command="code",
            conflict_tool_timeout_seconds=1,
            report_entries=[original_entry],
        )
        self.assertEqual(
            manual_common.summarize_conflict_entries([local_entry]),
            self.manual_merge.summarize_conflict_entries([original_entry]),
        )
        self.assertEqual(
            manual_common.can_skip_failed_branch(local_context, completed=False),
            self.manual_merge.can_skip_failed_branch(original_context, completed=False),
        )
        self.assertEqual(
            manual_models.DirtyRepoAssessment(Path("repo"), ("libs", "core"), "dirty").root_relative_path,
            self.manual_merge.DirtyRepoAssessment(Path("repo"), ("libs", "core"), "dirty").root_relative_path,
        )
        self.assertEqual(
            manual_models.SubmoduleConflictInfo("libs", Path("repo/libs"), "b", "o", "t").commit_for_stage(3),
            self.manual_merge.SubmoduleConflictInfo("libs", Path("repo/libs"), "b", "o", "t").commit_for_stage(3),
        )

    def test_manual_preflight_low_coupling_helpers_match_original(self) -> None:
        entries = [
            manual_models.DirtyRepoAssessment(Path("repo/libs/a"), ("libs", "a"), "safe"),
            manual_models.DirtyRepoAssessment(Path("repo/libs/b"), ("libs", "b"), ""),
        ]
        original_entries = [
            self.manual_merge.DirtyRepoAssessment(Path("repo/libs/a"), ("libs", "a"), "safe"),
            self.manual_merge.DirtyRepoAssessment(Path("repo/libs/b"), ("libs", "b"), ""),
        ]
        self.assertEqual(format_dirty_submodule_list(entries), self.manual_merge.format_dirty_submodule_list(original_entries))

        local_map = build_preserved_dirty_path_map(Path("repo"), entries)
        original_map = self.manual_merge.build_preserved_dirty_path_map(Path("repo"), original_entries)
        self.assertEqual(
            {str(key): value for key, value in local_map.items()},
            {str(key): value for key, value in original_map.items()},
        )

    def test_manual_preflight_repository_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo) + "\n")
                if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["config", "--get", "user.name"]:
                    return FakeManualGitResult(args, stdout="User\n")
                if args == ["config", "--get", "user.email"]:
                    return FakeManualGitResult(args, stdout="user@example.com\n")
                if args == ["status", "--porcelain", "--ignore-submodules=dirty"]:
                    return FakeManualGitResult(args)
                if args == ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["remote"]:
                    return FakeManualGitResult(args, stdout="origin\n")
                return FakeManualGitResult(args)

            module = types.SimpleNamespace(
                run_git=run_git,
                run_git_binary=lambda _repo_path, args, **_kwargs: FakeManualBinaryResult(args, b""),
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            from manual_merge_split.preflight import ManualPreflightService

            services = types.SimpleNamespace(preflight=ManualPreflightService(module))  # type: ignore[arg-type]

            result = services.preflight.preflight_repository(repo, "origin/main", ["origin/feature"])

            self.assertEqual(result.allowed_dirty_submodules, [])
            self.assertIn(("submodule", "sync", "--recursive"), calls)
            self.assertIn(("fetch", "origin", "main"), calls)
            self.assertIn(("fetch", "origin", "feature"), calls)

    def test_manual_guided_prompts_and_cleanup_path_are_local(self) -> None:
        self.assertEqual(prompt_with_default("Prompt", "default", input_func=lambda _prompt: ""), "default")
        self.assertEqual(prompt_optional("Prompt", "default", input_func=lambda _prompt: "value"), "value")
        self.assertIsNone(prompt_optional("Prompt", input_func=lambda _prompt: ""))
        self.assertTrue(prompt_yes_no("Prompt", True, input_func=lambda _prompt: ""))
        self.assertTrue(prompt_yes_no("Prompt", False, input_func=lambda _prompt: "yes"))

        answers = iter(["", "cleanup", "n", "", "", ""])
        captured: dict[str, object] = {}

        def fake_cleanup(args: object) -> int:
            captured["args"] = args
            return 7

        verbose_values: list[bool] = []
        result = run_guided(
            types.SimpleNamespace(repo=".", verbose=False),
            run_merge=lambda _args: 99,
            run_cleanup=fake_cleanup,
            set_verbose=verbose_values.append,
            input_func=lambda _prompt: next(answers),
        )

        self.assertEqual(result, 7)
        self.assertEqual(verbose_values, [False])
        cleanup_args = captured["args"]
        self.assertEqual(cleanup_args.command, "cleanup")
        self.assertFalse(cleanup_args.rebuild_work_branch)
        self.assertFalse(cleanup_args.skip_submodule_update)

    def test_manual_runtime_parser_and_main_are_local(self) -> None:
        merge_argv = [
            "feature",
            "--repo",
            "repo",
            "--continue-current-branch",
            "--file-conflict-mode",
            "preserve",
            "--ignore-root-untracked-path",
            "generated.txt",
        ]
        local_merge = manual_runtime.parse_args(merge_argv)
        original_merge = self.manual_merge.parse_args(merge_argv)
        self.assertEqual(vars(local_merge), vars(original_merge))

        cleanup_argv = [
            "cleanup",
            "--repo",
            "repo",
            "--skip-submodule-update",
            "--rebuild-work-branch",
            "--work-branch",
            "work",
        ]
        local_cleanup = manual_runtime.parse_args(cleanup_argv)
        original_cleanup = self.manual_merge.parse_args(cleanup_argv)
        self.assertEqual(vars(local_cleanup), vars(original_cleanup))

        calls: list[tuple[str, object]] = []
        command_service = types.SimpleNamespace(
            manual_module=None,
            run_merge=lambda args: calls.append(("merge", args)) or 11,
            run_cleanup=lambda args: calls.append(("cleanup", args)) or 22,
            set_verbose=lambda _enabled: None,
        )
        guided_service = types.SimpleNamespace(
            run_guided=lambda args: calls.append(("guided", args)) or 33,
        )

        self.assertEqual(manual_runtime.main(["feature"], command_service=command_service), 11)
        self.assertEqual(manual_runtime.main(["cleanup"], command_service=command_service), 22)
        self.assertEqual(
            manual_runtime.main(["guided"], command_service=command_service, guided_service=guided_service),
            33,
        )
        self.assertEqual([name for name, _args in calls], ["merge", "cleanup", "guided"])

    def test_manual_command_cleanup_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []
            status_reads = 0

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                nonlocal status_reads
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo) + "\n")
                if args == ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["diff", "--name-only", "--diff-filter=U"]:
                    return FakeManualGitResult(args)
                if args == ["rev-parse", "--verify", "HEAD"]:
                    return FakeManualGitResult(args, stdout="headsha\n")
                if args == ["status", "--porcelain"]:
                    status_reads += 1
                    return FakeManualGitResult(args, stdout=(" M file.txt\n" if status_reads == 1 else ""))
                return FakeManualGitResult(args)

            module = types.SimpleNamespace(
                run_git=run_git,
                run_git_binary=lambda _repo_path, args, **_kwargs: FakeManualBinaryResult(args, b""),
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            service = ManualCommandService(module)  # type: ignore[arg-type]

            result = service.run_cleanup(
                types.SimpleNamespace(
                    repo=str(repo),
                    report_file="merge_manual_todo.txt",
                    skip_submodule_update=False,
                    rebuild_work_branch=False,
                    base_remote_branch="origin/main",
                    work_branch=None,
                    verbose=False,
                )
            )

            self.assertEqual(result, 0)
            self.assertIn(("reset", "--hard", "HEAD"), calls)
            self.assertIn(("submodule", "update", "--init", "--recursive"), calls)

    def test_manual_process_repository_file_conflict_is_local(self) -> None:
        from manual_merge_split.cross_submodule_moves import CrossSubmoduleMoveService
        from manual_merge_split.file_conflicts import ManualFileConflictService
        from manual_merge_split.repository_ops import ManualRepositoryOps

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo) + "\n")
                if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["config", "--get", "user.name"]:
                    return FakeManualGitResult(args, stdout="User\n")
                if args == ["config", "--get", "user.email"]:
                    return FakeManualGitResult(args, stdout="user@example.com\n")
                if args == ["status", "--porcelain"]:
                    return FakeManualGitResult(args)
                if args == ["remote"]:
                    return FakeManualGitResult(args)
                if args == ["merge", "--no-ff", "feature"]:
                    return FakeManualGitResult(args)
                if args == ["diff", "--name-only", "--diff-filter=U"]:
                    return FakeManualGitResult(args, stdout="file.txt\n")
                if args == ["ls-files", "--stage", "--", "file.txt"]:
                    return FakeManualGitResult(args, stdout="100644 abc 1\tfile.txt\n")
                return FakeManualGitResult(args)

            module = types.SimpleNamespace(
                run_git=run_git,
                run_git_binary=lambda _repo_path, args, **_kwargs: FakeManualBinaryResult(args, b""),
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            context = manual_models.MergeContext(
                work_branch="work",
                merge_branch="feature",
                file_conflict_mode="preserve",
                conflict_tool_command="tool",
                conflict_tool_timeout_seconds=1,
            )

            completed = process_repository(
                repo,
                context,
                root_base_remote_branch=None,
                is_root=True,
                prepare_branch=False,
                ops=ManualRepositoryOps(module),  # type: ignore[arg-type]
                file_conflicts_service=ManualFileConflictService(module),  # type: ignore[arg-type]
                cross_submodule_moves_service=CrossSubmoduleMoveService(module),  # type: ignore[arg-type]
            )

            self.assertFalse(completed)
            self.assertEqual(context.report_entries[0].files, ["file.txt"])
            self.assertIn(("merge", "--no-ff", "feature"), calls)

            report_path = repo / "merge_manual_todo.txt"
            write_report(report_path, context)
            self.assertIn("file.txt", report_path.read_text(encoding="utf-8"))

    def test_manual_file_conflict_helpers_are_local(self) -> None:
        context = manual_models.MergeContext(
            work_branch="work",
            merge_branch="merge",
            file_conflict_mode="tool",
            conflict_tool_command="code",
            conflict_tool_timeout_seconds=1,
        )
        record_file_conflicts(Path("repo"), ["a.txt"], context, reason="reason", category="cat")
        self.assertEqual(context.report_entries[0].files, ["a.txt"])
        self.assertEqual(context.report_entries[0].category, "cat")

        preserve = manual_models.MergeContext(
            work_branch="work",
            merge_branch="merge",
            file_conflict_mode="preserve",
            conflict_tool_command="code",
            conflict_tool_timeout_seconds=1,
        )
        resolution = resolve_single_file_conflict(
            Path("repo"),
            "a.txt",
            preserve,
            run_git=lambda *_args, **_kwargs: FakeManualGitResult([]),
        )
        self.assertEqual(resolution.reason, "current run is configured to preserve conflicts")

        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def run_git(_repo_path: Path, args: list[str], **kwargs: object) -> FakeManualGitResult:
            calls.append((tuple(args), kwargs))
            return FakeManualGitResult(args)

        resolved = resolve_moved_away_file_conflicts(
            Path("repo"),
            ["gone.txt", "keep.txt"],
            {"gone.txt"},
            run_git=run_git,
            get_unmerged_paths=lambda _repo_path: ["keep.txt"],
        )
        self.assertEqual(resolved, ["gone.txt"])
        self.assertIn((("rm", "-f", "--", "gone.txt"), {"check": False}), calls)

        class FakeProcess:
            returncode = 0

            def wait(self, timeout: int | None = None) -> None:
                return None

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        unmerged_reads = 0
        git_calls: list[tuple[str, ...]] = []

        def run_git_for_tool(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
            nonlocal unmerged_reads
            git_calls.append(tuple(args))
            if args == ["diff", "--name-only", "--diff-filter=U"]:
                unmerged_reads += 1
                return FakeManualGitResult(args, stdout=("a.txt\n" if unmerged_reads == 1 else ""))
            if args == ["diff", "--check", "--cached", "--", "a.txt"]:
                return FakeManualGitResult(args)
            return FakeManualGitResult(args)

        tool_context = manual_models.MergeContext(
            work_branch="work",
            merge_branch="merge",
            file_conflict_mode="tool",
            conflict_tool_command="code --wait {file}",
            conflict_tool_timeout_seconds=1,
        )
        unresolved, reason, rollback = resolve_file_conflicts(
            Path("repo"),
            ["a.txt"],
            tool_context,
            run_git=run_git_for_tool,
            popen_factory=lambda *_args, **_kwargs: FakeProcess(),
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(reason, "")
        self.assertFalse(rollback)
        self.assertIn(("add", "--", "a.txt"), git_calls)

    def test_manual_repository_cleanup_and_restore_are_local(self) -> None:
        from manual_merge_split.repository_ops import ManualRepositoryOps

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []
            status_reads = 0

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                nonlocal status_reads
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo) + "\n")
                if args == ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["diff", "--name-only", "--diff-filter=U"]:
                    return FakeManualGitResult(args)
                if args == ["rev-parse", "--verify", "HEAD"]:
                    return FakeManualGitResult(args, stdout="headsha\n")
                if args == ["status", "--porcelain"] or args == ["status", "--porcelain", "--ignore-submodules=dirty"]:
                    status_reads += 1
                    return FakeManualGitResult(args, stdout=(" M file.txt\n" if status_reads == 1 else ""))
                if args == ["show-ref", "--verify", "refs/heads/main"]:
                    return FakeManualGitResult(args)
                return FakeManualGitResult(args)

            module = types.SimpleNamespace(
                run_git=run_git,
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            ops = ManualRepositoryOps(module)  # type: ignore[arg-type]
            cleanup = manual_models.CleanupResult()
            ops.cleanup_repository(repo, cleanup, skip_submodule_update=False)

            self.assertEqual(cleanup.hard_resets, [repo])
            self.assertEqual(cleanup.submodule_updates, [repo])
            self.assertIn(("reset", "--hard", "HEAD"), calls)

            snapshot = manual_models.RepoSnapshot(repo.resolve(), "headsha", "main")
            ops.restore_snapshot(repo, {repo.resolve(): snapshot}, is_root=True)

            self.assertIn(("checkout", "main"), calls)
            self.assertIn(("reset", "--hard", "headsha"), calls)
            self.assertIn(("submodule", "update", "--init", "--recursive"), calls)

    def test_manual_cross_submodule_merge_file_contents_is_local(self) -> None:
        move = manual_models.CrossSubmoduleMove(
            old_submodule_path="old",
            old_repo_path=Path("old"),
            old_file_path="a.txt",
            old_base_commit="base",
            old_other_commit="other",
            new_submodule_path="new",
            new_repo_path=Path("new"),
            new_file_path="a.txt",
            new_added_commit="added",
            move_side=3,
            score=1.0,
        )
        calls: list[list[str]] = []

        def run_git_binary(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualBinaryResult:
            calls.append(args)
            return FakeManualBinaryResult(args, b"merged\n", returncode=0)

        merged, had_conflicts, failure = merge_cross_submodule_file_contents(
            Path("repo"),
            move,
            b"current\n",
            b"base\n",
            b"other\n",
            run_git_binary=run_git_binary,
        )
        self.assertEqual(merged, b"merged\n")
        self.assertFalse(had_conflicts)
        self.assertIsNone(failure)
        self.assertEqual(calls[0][0], "merge-file")

    def test_manual_collect_cross_submodule_moves_is_local(self) -> None:
        repo = Path("repo")
        infos = [
            manual_models.SubmoduleConflictInfo("old", repo / "old", "base-old", "curr-old", "merge-old"),
            manual_models.SubmoduleConflictInfo("new", repo / "new", "base-new", "curr-new", "merge-new"),
        ]

        def run_git(repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
            if repo_path == repo / "old" and args == ["diff", "--name-only", "--diff-filter=D", "base-old", "merge-old", "--"]:
                return FakeManualGitResult(args, stdout="src/a.txt\n")
            if repo_path == repo / "new" and args == ["diff", "--name-only", "--diff-filter=A", "base-new", "merge-new", "--"]:
                return FakeManualGitResult(args, stdout="dst/a.txt\n")
            return FakeManualGitResult(args)

        def run_git_binary(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualBinaryResult:
            content = {
                "base-old:src/a.txt": b"line\n",
                "curr-old:src/a.txt": b"line edited on old side\n",
                "merge-new:dst/a.txt": b"line\n",
            }.get(args[1])
            if content is None:
                return FakeManualBinaryResult(args, b"", returncode=1)
            return FakeManualBinaryResult(args, content)

        moves = collect_cross_submodule_moves(
            repo,
            [],
            candidate_infos=infos,
            run_git=run_git,
            run_git_binary=run_git_binary,
        )

        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].old_submodule_path, "old")
        self.assertEqual(moves[0].new_submodule_path, "new")
        self.assertEqual(moves[0].move_side, 3)

    def test_manual_apply_cross_submodule_moved_files_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            move = manual_models.CrossSubmoduleMove(
                old_submodule_path="old",
                old_repo_path=repo / "old",
                old_file_path="a.txt",
                old_base_commit="base",
                old_other_commit="other",
                new_submodule_path="new",
                new_repo_path=repo / "new",
                new_file_path="new/a.txt",
                new_added_commit="added",
                move_side=3,
                score=1.0,
            )
            git_calls: list[tuple[str, ...]] = []

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                git_calls.append(tuple(args))
                return FakeManualGitResult(args)

            def run_git_binary(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualBinaryResult:
                if args[0] == "show":
                    return FakeManualBinaryResult(args, b"content\n")
                return FakeManualBinaryResult(args, b"merged\n")

            unresolved, reason, rollback = apply_cross_submodule_moved_files(
                repo,
                [move],
                object(),
                run_git=run_git,
                run_git_binary=run_git_binary,
                get_unmerged_paths=lambda _repo_path: [],
                resolve_single_file_conflict=lambda *_args: manual_models.FileConflictResolution(None),
            )
            self.assertEqual(unresolved, [])
            self.assertEqual(reason, "")
            self.assertFalse(rollback)
            self.assertEqual((repo / "new" / "a.txt").read_bytes(), b"merged\n")
            self.assertIn(("add", "--", "new/a.txt"), git_calls)

    def test_manual_text_merge_normalization_matches_original_shape(self) -> None:
        current = b"\xef\xbb\xbfone\r\ntwo\r\n"
        base = b"one\r\ntwo\r\n"
        other = b"one\r\nthree\r\n"

        local = manual_common.prepare_cross_submodule_merge_inputs(current, base, other)
        original = self.manual_merge.prepare_cross_submodule_merge_inputs(current, base, other)
        self.assertEqual(local[:3], original[:3])
        self.assertEqual(local[3].had_utf8_bom, original[3].had_utf8_bom)
        self.assertEqual(local[3].newline, original[3].newline)
        self.assertEqual(
            manual_common.restore_cross_submodule_merge_output(b"one\nmerged\n", local[3]),
            self.manual_merge.restore_cross_submodule_merge_output(b"one\nmerged\n", original[3]),
        )


if __name__ == "__main__":
    unittest.main()
