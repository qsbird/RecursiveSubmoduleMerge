from __future__ import annotations

import types
import unittest
import tempfile
import json
import io
import os
import sys
import subprocess
from unittest import mock
from pathlib import Path

import subprocess_window
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
from merge_cleanup_core import steps as cleanup_steps
from manual_merge_split import ManualWorkflowServices
from manual_merge_split import common as manual_common
from manual_merge_split import execution as manual_execution
from manual_merge_split import messages as manual_messages
from manual_merge_split import models as manual_models
from manual_merge_split import runtime as manual_runtime
from manual_merge_split.commands import ManualCommandService, process_repository, write_report
from manual_merge_split.cross_submodule_moves import CROSS_SUBMODULE_MOVE_FUNCTIONS, apply_cross_submodule_moved_files, collect_cross_submodule_moves, describe_cross_submodule_move, merge_cross_submodule_file_contents
from manual_merge_split.file_conflicts import FILE_CONFLICT_FUNCTIONS, prepare_conflict_tool_command, record_file_conflicts, render_conflict_tool_command, resolve_file_conflicts, resolve_moved_away_file_conflicts, resolve_single_file_conflict, vscode_command_candidates
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
    def __init__(self, args: list[str], stdout: str = "", exit_code: int = 0, stderr: str = "") -> None:
        self.args = args
        self.cmd = ["git", *args]
        self.cwd = "repo"
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

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
        if args[:2] == ["rev-parse", "--verify"] and len(args) == 3:
            refspec = args[2]
            prefix = "refs/remotes/origin/"
            suffix = "^{commit}"
            if refspec.startswith(prefix + "result") and refspec.endswith(suffix):
                return FakeCiCommandResult(args, stdout=refspec[len(prefix) : -len(suffix)] + "\n")
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
            "warnings": [],
            "restore": {"attempted": False, "success": None, "errors": [], "results": []},
        }
        self.conflicts: list[dict[str, object]] = []
        self.repositories: list[dict[str, object]] = []
        self.fallbacks: list[dict[str, object]] = []
        self.snapshots: list[dict[str, object]] = []
        self.log: list[str] = []
        self.console_messages: list[str] = []
        self.progress_messages: list[str] = []

    def add_conflicts(self, conflicts: list[dict[str, object]]) -> None:
        self.conflicts.extend(conflicts)
        self.data.setdefault("conflicts", []).extend(conflicts)  # type: ignore[union-attr]
        self.data.setdefault("summary", {})["conflicts"] = len(self.data["conflicts"])  # type: ignore[index]

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

    def progress(self, message: str) -> None:
        self.progress_messages.append(message)

    def set_success(self) -> None:
        self.data["status"] = "success"

    def set_state(self, state: str) -> None:
        self.data["state"] = state

    def add_error(self, error: object) -> None:
        to_report = getattr(error, "to_report_error", None)
        self.data["errors"].append(to_report() if callable(to_report) else error)

    def add_warning(self, reason: str, message: str, details: dict[str, object] | None = None) -> None:
        self.data["warnings"].append(
            {
                "reason": reason,
                "message": message,
                "details": details or {},
            }
        )

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
            expanded_diff_context_lines=20,
            git="git",
            remote="origin",
            target="main",
            publish=False,
            publish_only=False,
            publish_target=None,
            publish_mode="push",
            candidate_ref=None,
            allow_create_publish_target=False,
            from_report=None,
            allow_partial_publish=False,
            batch_failure_mode="continue-success",
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
            file_conflict_mode="tool",
            conflict_tool_command="tool",
            conflict_tool_timeout_seconds=1,
        )
        services.file_conflicts.record_file_conflicts(
            Path("repo"),
            ["demo.txt"],
            conflict_context,
            reason="manual merge required",
        )
        services.cross_submodule_moves.collect_cross_submodule_moves(Path("repo"), ["old-lib"])
        services.repository_ops.ensure_commit_available(Path("repo"), "abc123")
        branch_tokens = services.guided_commands.parse_branch_tokens("a b")

        self.assertEqual(branch_tokens, ["a b"])
        self.assertEqual(dirty_impact[0], True)
        self.assertEqual(conflict_context.report_entries[0].files, ["demo.txt"])
        self.assertIn("manual merge required", conflict_context.report_entries[0].reason)
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

    def test_manual_git_execution_hides_windows_child_console(self) -> None:
        fake_completed = types.SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")
        with mock.patch("manual_merge_split.execution.hidden_subprocess_kwargs", return_value={"creationflags": 1}):
            with mock.patch("manual_merge_split.execution.subprocess.run", return_value=fake_completed) as run:
                result = manual_execution.run_git(Path.cwd(), ["status"])

        self.assertEqual(result.stdout, "ok")
        self.assertEqual(run.call_args.kwargs["creationflags"], 1)

    def test_windows_subprocess_helper_is_platform_scoped(self) -> None:
        with mock.patch("subprocess_window.sys.platform", "linux"):
            self.assertEqual(subprocess_window.hidden_subprocess_kwargs(), {})
        if sys.platform == "win32":
            kwargs = subprocess_window.hidden_subprocess_kwargs()
            self.assertIn("startupinfo", kwargs)
            self.assertIn("creationflags", kwargs)


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
        self.assertEqual(report_value(nested, "a", "b"), 0)
        self.assertIsNone(report_value(nested, "x", "missing"))

        ctx = types.SimpleNamespace(args=types.SimpleNamespace(publish_target="main", remote="origin"))
        report = {"input": {"target": "main"}, "publish": {"target": None}, "resolved": {"target_ref": "refs/heads/main"}}
        self.assertEqual(infer_publish_target_from_report(ctx, report), "main")
        ctx.args.publish_target = None
        with self.assertRaises(ci_models.CiMergeError) as missing_target:
            infer_publish_target_from_report(ctx, report)
        self.assertEqual(missing_target.exception.reason, "PUBLISH_TARGET_REQUIRED")

    def test_ci_command_helpers_are_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            ctx = FakeCiContext(Path(temp_dir))
            ctx.root_repo = root

            resolved_fields = {
                "source_ref": "refs/remotes/origin/source",
                "source_sha": "sourcesha",
                "target_ref": "refs/remotes/origin/main",
                "target_sha": "targetsha",
                "merge_base_sha": "basesha",
                "result_sha": "resultsha",
            }
            ci_commands.set_resolved_fields(ctx, **resolved_fields)
            for key, value in resolved_fields.items():
                self.assertEqual(ctx.report.data["resolved"][key], value)

            scope = ci_commands.batch_step_artifact_scope(ctx, "batch/step-01")
            self.assertEqual(scope["prefix"], "batch/step-01")
            self.assertEqual(scope["patch_dir"], str(ctx.report.output_dir / "patches" / "batch/step-01"))

            ctx.report.data["commands"] = [{"cmd": ["git"]}, {"cmd": ["git"]}]
            self.assertEqual(ci_commands.command_range(ctx, 1), {"start": 1, "end": 1, "count": 1})

            publish_info = ci_commands.manual_publish_info(ctx, "targetsha", "resultsha")
            self.assertEqual(publish_info["remote"], "origin")
            self.assertEqual(publish_info["target"], "main")
            self.assertTrue(publish_info["supported"])
            self.assertIn("--force-with-lease=main:targetsha", publish_info["command"])
            self.assertTrue(ci_commands.can_attempt_cleanup(ctx, root))

        passed = ci_models.PreflightResult(True, [], [], source_sha="source", target_sha="target")
        self.assertIsNone(ci_commands.ensure_preflight_shas(passed))

        failed = ci_models.PreflightResult(False, [], [{"name": "working_tree_clean"}])
        with self.assertRaises(ci_models.CiMergeError) as error:
            ci_commands.fail_preflight(failed)
        self.assertEqual(error.exception.exit_code, ci_models.ExitCode.PREFLIGHT_FAILED)
        self.assertEqual(error.exception.phase, ci_models.Phase.PREFLIGHT)
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

            self.assertEqual(local_args.sources, [original_args.source])
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
            local_input = local_report.data["input"].copy()
            self.assertFalse(local_input.pop("allow_create_publish_target"))
            self.assertEqual(local_input.pop("submodule_fetch_scope"), "needed")
            self.assertEqual(local_input.pop("git_network_retries"), 2)
            self.assertEqual(local_input.pop("git_network_retry_delay"), 3.0)
            self.assertEqual(local_input.pop("expanded_diff_context_lines"), 20)
            self.assertEqual(local_input.pop("sources"), ["feature"])
            self.assertFalse(local_input.pop("allow_partial_publish"))
            self.assertIsNone(local_input.pop("from_report"))
            self.assertFalse(local_input.pop("publish_only"))
            self.assertEqual(local_input, original_report.data["input"])
            local_batch = local_report.data["batch"]
            self.assertEqual(local_batch["failure_mode"], "continue-success")
            self.assertFalse(local_batch["enabled"])
            self.assertEqual(local_batch["source_count"], 1)
            self.assertEqual(local_batch["steps"], [])
            local_publish = local_report.data["publish"].copy()
            self.assertFalse(local_publish.pop("allow_create_target"))
            self.assertFalse(local_publish.pop("target_created"))
            self.assertIsNone(local_publish.pop("target_publish_mode"))
            self.assertEqual(
                local_publish.pop("root"),
                {"candidate_ref_pushed": False, "target_pushed": False, "success": None},
            )
            self.assertEqual(
                local_publish.pop("submodules"),
                {"attempted": False, "success": None, "count": 0, "items": []},
            )
            self.assertEqual(local_publish, original_report.data["publish"])
            self.assertTrue(set(original_report.data).issubset(set(local_report.data)))

    def test_ci_runtime_publish_requires_explicit_publish_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            publish_args = ci_runtime.parse_arguments(
                [
                    "--source",
                    "feature",
                    "--target",
                    "main",
                    "--repo_path",
                    str(repo),
                    "--publish",
                    "true",
                ]
            )
            ci_runtime.normalize_args(publish_args, "job-42")
            with self.assertRaises(ci_models.CiMergeError) as publish_error:
                ci_runtime.validate_args(publish_args)
            self.assertEqual(publish_error.exception.reason, "PUBLISH_TARGET_REQUIRED")

            publish_only_args = ci_runtime.parse_arguments(
                [
                    "--publish_only",
                    "true",
                    "--from_report",
                    str(Path(temp_dir) / "merge-report.json"),
                    "--repo_path",
                    str(repo),
                ]
            )
            ci_runtime.normalize_args(publish_only_args, "job-42")
            with self.assertRaises(ci_models.CiMergeError) as publish_only_error:
                ci_runtime.validate_args(publish_only_args)
            self.assertEqual(publish_only_error.exception.reason, "PUBLISH_TARGET_REQUIRED")

    def test_ci_runtime_multi_source_argument_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()

            repeated = ci_runtime.parse_arguments(
                [
                    "--source",
                    "feature-a",
                    "--source",
                    "feature-b",
                    "--target",
                    "main",
                    "--repo_path",
                    str(repo),
                ]
            )
            ci_runtime.normalize_args(repeated, "job-42")
            ci_runtime.validate_args(repeated)
            self.assertEqual(repeated.sources, ["feature-a", "feature-b"])
            self.assertEqual(repeated.source, "feature-a")
            self.assertEqual(repeated.batch_failure_mode, "continue-success")

            repeated_publish = ci_runtime.parse_arguments(
                [
                    "--source",
                    "feature-a",
                    "--source",
                    "feature-b",
                    "--target",
                    "main",
                    "--repo_path",
                    str(repo),
                    "--publish",
                    "true",
                    "--publish_target",
                    "main",
                ]
            )
            ci_runtime.normalize_args(repeated_publish, "job-42")
            ci_runtime.validate_args(repeated_publish)
            self.assertTrue(repeated_publish.publish)
            self.assertEqual(repeated_publish.sources, ["feature-a", "feature-b"])

            comma_dash = ci_runtime.parse_arguments(
                [
                    "--multi-source",
                    "1",
                    "--source",
                    "feature-a, feature-b,feature-c",
                    "--target",
                    "main",
                    "--repo_path",
                    str(repo),
                ]
            )
            ci_runtime.normalize_args(comma_dash, "job-42")
            ci_runtime.validate_args(comma_dash)
            self.assertEqual(comma_dash.sources, ["feature-a", "feature-b", "feature-c"])
            self.assertEqual(comma_dash.source, "feature-a")

            comma_underscore = ci_runtime.parse_arguments(
                [
                    "--multi_source",
                    "true",
                    "--source",
                    "feature-a,feature-b",
                    "--source",
                    "feature-c",
                    "--target",
                    "main",
                    "--repo_path",
                    str(repo),
                ]
            )
            ci_runtime.normalize_args(comma_underscore, "job-42")
            ci_runtime.validate_args(comma_underscore)
            self.assertEqual(comma_underscore.sources, ["feature-a", "feature-b", "feature-c"])

            default_single = ci_runtime.parse_arguments(
                [
                    "--source",
                    "feature-a,feature-b",
                    "--target",
                    "main",
                    "--repo_path",
                    str(repo),
                ]
            )
            ci_runtime.normalize_args(default_single, "job-42")
            ci_runtime.validate_args(default_single)
            self.assertEqual(default_single.sources, ["feature-a,feature-b"])

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

    def _make_batch_context(self, output_dir: Path, *, failure_mode: str = "continue-success") -> FakeCiContext:
        ctx = FakeCiContext(output_dir)
        ctx.args.sources = ["source-one", "source-two"]
        ctx.args.source = "source-one"
        ctx.args.batch_failure_mode = failure_mode
        ctx.report.data["resolved"] = {
            "source_ref": None,
            "source_sha": None,
            "target_ref": None,
            "target_sha": None,
            "merge_base_sha": None,
            "result_sha": None,
        }
        ctx.report.data["batch"] = {
            "enabled": True,
            "source_count": 2,
            "initial_target": ctx.args.target,
            "original_target_ref": None,
            "original_target_sha": None,
            "final_result_sha": None,
            "failure_step": None,
            "steps": [],
            "manual_publish": None,
            "failure_mode": failure_mode,
            "status": None,
            "successful_steps": [],
            "failed_steps": [],
            "errors": [],
        }
        return ctx

    def _batch_services(self, publish_calls: list[dict[str, object]] | None = None) -> object:
        class BatchGuards:
            def clean_and_prepare_repo(
                self,
                ctx: object,
                repo_path: Path,
                *,
                scope: dict[str, object] | None = None,
            ) -> None:
                report = getattr(ctx, "report")
                report.add_fallback(
                    {
                        "level": "test_batch_step_cleanup",
                        "repo_path": str(repo_path),
                        "success": True,
                        "steps": [],
                        "verify_errors": [],
                        "scope": scope or {},
                    }
                )

            def preflight_check(
                self,
                _ctx: object,
                _repo_path: Path,
                source: str | None,
                target: str | None,
                label: str,
                resolve_refs: bool,
            ) -> ci_models.PreflightResult:
                if not resolve_refs:
                    return ci_models.PreflightResult(passed=True, checks=[], failed_checks=[])
                return ci_models.PreflightResult(
                    passed=True,
                    checks=[],
                    failed_checks=[],
                    source_ref=f"refs/remotes/origin/{source}",
                    source_sha=f"{source}-sha",
                    target_ref=str(target),
                    target_sha=f"{target}-sha",
                    merge_base_sha=f"{source}-base",
                )

            def verify_repo_ready(self, _ctx: object, _repo_path: Path) -> tuple[bool, list[str]]:
                return True, []

            def ensure_ready_after_state_change(
                self,
                ctx: object,
                repo_path: Path,
                *,
                scope: dict[str, object] | None = None,
                checkout_args: list[str] | tuple[str, ...] | None = None,
                update_submodules: bool = True,
                max_cleanup_attempts: int = 2,
            ) -> None:
                del checkout_args, update_submodules, max_cleanup_attempts
                ready, reasons = self.verify_repo_ready(ctx, repo_path)
                if not ready:
                    cleanup_scope = dict(scope or {})
                    cleanup_scope["post_state_change_verify_errors"] = reasons
                    self.clean_and_prepare_repo(ctx, repo_path, scope=cleanup_scope)

        class BatchPublish:
            def publish_result(self, ctx: object, repo_path: Path, target_sha: str, result_sha: str) -> dict[str, object]:
                item = {
                    "ctx": ctx,
                    "repo_path": repo_path,
                    "target_sha": target_sha,
                    "result_sha": result_sha,
                }
                if publish_calls is not None:
                    publish_calls.append(item)
                report = getattr(ctx, "report")
                report.data["publish"]["attempted"] = True
                report.data["publish"]["success"] = True
                report.data["publish"]["expected_result_sha"] = result_sha
                return report.data["publish"]

        return types.SimpleNamespace(guards=BatchGuards(), publish=BatchPublish())

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
            self.assertIn("SINGLE start source=source target=main", ctx.report.progress_messages)
            self.assertIn("SINGLE success result=resultsha", ctx.report.progress_messages)

    def test_ci_single_workflow_converts_mixed_conflict_to_root_conflict(self) -> None:
        from split_gitlink_merge import Conflict, NonGitlinkConflictError

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            ctx = FakeCiContext(Path(temp_dir))
            ctx.root_repo = root

            def merge_repo(_ctx_arg: object, repo_path: Path, **_kwargs: object) -> object:
                conflicts = [
                    Conflict(repo_path=repo_path, path="file.txt", stages=(), kind="file"),
                    Conflict(repo_path=repo_path, path=".gitmodules", stages=(), kind="gitmodules"),
                    Conflict(repo_path=repo_path, path="unsupported.txt", stages=(), kind="unsupported_stage"),
                    Conflict(repo_path=repo_path, path="submodule", stages=(), kind="gitlink"),
                ]
                raise NonGitlinkConflictError(repo_path, conflicts)

            with self.assertRaises(ci_models.CiMergeError) as raised:
                ci_commands.run_single_workflow(ctx, CiWorkflowServices.from_local(), merge_repo)

            error = raised.exception
            self.assertEqual(error.exit_code, ci_models.ExitCode.ROOT_CONFLICT)
            self.assertEqual(error.phase, ci_models.Phase.MERGING_ROOT)
            self.assertEqual(error.reason, "NON_GITLINK_CONFLICT")
            self.assertTrue(error.restore_needed)
            self.assertEqual(error.details["type"], "NonGitlinkConflictError")
            self.assertEqual(
                [item["type"] for item in error.details["conflicts"]],
                ["file_conflict", "gitmodules_conflict", "unsupported_conflict_stage"],
            )
            self.assertEqual(error.details["conflict_paths"], ["file.txt", ".gitmodules", "unsupported.txt"])
            self.assertEqual(error.details["conflict_count"], 3)
            self.assertEqual(
                [item["path"] for item in ctx.report.data["conflicts"]],
                ["file.txt", ".gitmodules", "unsupported.txt"],
            )
            self.assertEqual(ctx.report.data["summary"]["conflicts"], 3)
            self.assertIn("SINGLE failed reason=NON_GITLINK_CONFLICT", ctx.report.progress_messages)

    def test_ci_batch_progress_reports_minimal_step_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = self._make_batch_context(Path(temp_dir))
            calls = 0

            def merge_repo(_ctx: object, _repo_path: Path, **_kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return types.SimpleNamespace(result_sha=f"result-step-{calls}", strategy="recursive_gitlink_merge")

            ci_commands.run_batch_workflow(ctx, self._batch_services(), merge_repo)

            self.assertEqual(
                ctx.report.progress_messages,
                [
                    "BATCH start sources=2 target=main mode=continue-success",
                    "STEP 1/2 start source=source-one target=main",
                    "STEP 1/2 success result=result-step-1",
                    "STEP 2/2 start source=source-two target=result-step-1",
                    "STEP 2/2 success result=result-step-2",
                    "BATCH success steps=[1, 2] result=result-step-2",
                ],
            )

    def test_ci_batch_prepares_target_state_before_each_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = self._make_batch_context(Path(temp_dir))
            calls = 0

            def merge_repo(_ctx: object, _repo_path: Path, **_kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return types.SimpleNamespace(result_sha=f"result-step-{calls}", strategy="recursive_gitlink_merge")

            ci_commands.run_batch_workflow(ctx, self._batch_services(), merge_repo)

            steps = ctx.report.data["batch"]["steps"]
            self.assertEqual([step["pre_step_cleanup"]["success"] for step in steps], [True, True])
            self.assertEqual(
                [step["pre_step_cleanup"]["fallback_range"] for step in steps],
                [
                    {"start": 0, "end": None, "count": 0},
                    {"start": 0, "end": None, "count": 0},
                ],
            )
            runner_args = [call[1] for call in ctx.runner.calls]
            self.assertIn(("checkout", "--detach", "targetsha"), runner_args)
            self.assertIn(("checkout", "--detach", "result-step-1"), runner_args)
            self.assertEqual(ctx.report.fallbacks, [])

    def test_ci_batch_publish_pushes_final_success_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = self._make_batch_context(Path(temp_dir))
            ctx.args.publish = True
            ctx.args.publish_target = "main"
            publish_calls: list[dict[str, object]] = []
            calls = 0

            def merge_repo(_ctx: object, _repo_path: Path, **_kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return types.SimpleNamespace(result_sha=f"result-step-{calls}", strategy="recursive_gitlink_merge")

            ci_commands.run_batch_workflow(ctx, self._batch_services(publish_calls), merge_repo)

            self.assertEqual(ctx.report.data["status"], "success")
            self.assertEqual(ctx.report.data["batch"]["status"], "success")
            self.assertEqual(len(publish_calls), 1)
            self.assertEqual(publish_calls[0]["target_sha"], "main-sha")
            self.assertEqual(publish_calls[0]["result_sha"], "result-step-2")
            self.assertTrue(ctx.report.data["publish"]["success"])
            self.assertIn("BATCH publish success result=result-step-2", ctx.report.progress_messages)

    def test_ci_batch_publish_skips_partial_success_without_allow_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = self._make_batch_context(Path(temp_dir), failure_mode="continue-success")
            ctx.args.publish = True
            ctx.args.publish_target = "main"
            publish_calls: list[dict[str, object]] = []
            calls = 0

            def merge_repo(_ctx: object, _repo_path: Path, **_kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ci_models.CiMergeError(
                        ci_models.ExitCode.ROOT_CONFLICT,
                        ci_models.Phase.MERGING_ROOT,
                        "TEST_CONFLICT",
                        "synthetic conflict",
                        restore_needed=False,
                    )
                return types.SimpleNamespace(result_sha="resultsha", strategy="clean_merge")

            ci_commands.run_batch_workflow(ctx, self._batch_services(publish_calls), merge_repo)

            self.assertEqual(ctx.report.data["status"], "partial_success")
            self.assertEqual(ctx.report.data["batch"]["status"], "partial_success")
            self.assertEqual(publish_calls, [])
            self.assertEqual(ctx.report.data["warnings"][0]["reason"], "BATCH_PARTIAL_PUBLISH_SKIPPED")
            self.assertIn("BATCH publish skipped reason=BATCH_PARTIAL_PUBLISH_SKIPPED", ctx.report.progress_messages)

    def test_ci_batch_publish_allows_partial_success_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = self._make_batch_context(Path(temp_dir), failure_mode="continue-success")
            ctx.args.publish = True
            ctx.args.publish_target = "main"
            ctx.args.allow_partial_publish = True
            publish_calls: list[dict[str, object]] = []
            calls = 0

            def merge_repo(_ctx: object, _repo_path: Path, **_kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ci_models.CiMergeError(
                        ci_models.ExitCode.ROOT_CONFLICT,
                        ci_models.Phase.MERGING_ROOT,
                        "TEST_CONFLICT",
                        "synthetic conflict",
                        restore_needed=False,
                    )
                return types.SimpleNamespace(result_sha="resultsha", strategy="clean_merge")

            ci_commands.run_batch_workflow(ctx, self._batch_services(publish_calls), merge_repo)

            self.assertEqual(ctx.report.data["status"], "partial_success")
            self.assertEqual(ctx.report.data["batch"]["status"], "partial_success")
            self.assertEqual(len(publish_calls), 1)
            self.assertEqual(publish_calls[0]["target_sha"], "main-sha")
            self.assertEqual(publish_calls[0]["result_sha"], "resultsha")
            self.assertTrue(ctx.report.data["publish"]["success"])

    def test_ci_batch_runtime_exception_marks_current_step_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = self._make_batch_context(Path(temp_dir))

            def merge_repo(_ctx: object, _repo_path: Path, **_kwargs: object) -> object:
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                ci_commands.run_batch_workflow(ctx, self._batch_services(), merge_repo)

            batch = ctx.report.data["batch"]
            step = batch["steps"][0]
            self.assertEqual(batch["status"], "failed")
            self.assertEqual(batch["failure_step"], 1)
            self.assertEqual(batch["successful_steps"], [])
            self.assertEqual(batch["failed_steps"], [1])
            self.assertEqual(step["status"], "failed")
            self.assertIsNotNone(step["ended_at"])
            self.assertEqual(step["failure"]["reason"], "SYSTEM_EXCEPTION")
            self.assertEqual(step["failure"]["details"]["type"], "RuntimeError")
            self.assertEqual(batch["errors"], [step["failure"]])

    def test_ci_batch_ci_error_fail_fast_marks_batch_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = self._make_batch_context(Path(temp_dir), failure_mode="fail-fast")

            def merge_repo(_ctx: object, _repo_path: Path, **_kwargs: object) -> object:
                raise ci_models.CiMergeError(
                    ci_models.ExitCode.ROOT_CONFLICT,
                    ci_models.Phase.MERGING_ROOT,
                    "TEST_CONFLICT",
                    "synthetic conflict",
                    restore_needed=False,
                )

            with self.assertRaises(ci_models.CiMergeError):
                ci_commands.run_batch_workflow(ctx, self._batch_services(), merge_repo)

            batch = ctx.report.data["batch"]
            step = batch["steps"][0]
            self.assertEqual(batch["status"], "failed")
            self.assertEqual(batch["failure_step"], 1)
            self.assertEqual(batch["failed_steps"], [1])
            self.assertEqual(step["status"], "failed")
            self.assertIsNotNone(step["ended_at"])
            self.assertEqual(step["failure"]["reason"], "TEST_CONFLICT")
            self.assertEqual(batch["errors"], [step["failure"]])

    def test_ci_batch_continue_success_still_continues_after_ci_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = self._make_batch_context(Path(temp_dir), failure_mode="continue-success")
            calls = 0

            def merge_repo(_ctx: object, _repo_path: Path, **_kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ci_models.CiMergeError(
                        ci_models.ExitCode.ROOT_CONFLICT,
                        ci_models.Phase.MERGING_ROOT,
                        "TEST_CONFLICT",
                        "synthetic conflict",
                        restore_needed=False,
                    )
                return types.SimpleNamespace(result_sha="resultsha", strategy="clean_merge")

            ci_commands.run_batch_workflow(ctx, self._batch_services(), merge_repo)

            batch = ctx.report.data["batch"]
            self.assertEqual(calls, 2)
            self.assertEqual(batch["status"], "partial_success")
            self.assertEqual(batch["failed_steps"], [1])
            self.assertEqual(batch["successful_steps"], [2])
            self.assertEqual(batch["steps"][0]["status"], "failed")
            self.assertTrue(batch["steps"][0]["continued_after_failure"])
            self.assertEqual(batch["steps"][1]["status"], "success")
            self.assertEqual(ctx.report.data["status"], "partial_success")
            self.assertEqual(ctx.report.data["exit_code"], ci_models.ExitCode.PARTIAL_SUCCESS)
            self.assertIn("STEP 1/2 failed reason=TEST_CONFLICT", ctx.report.progress_messages)
            self.assertIn("STEP 1/2 skipped continue_from=main-sha", ctx.report.progress_messages)
            self.assertIn(
                "BATCH partial_success success=[2] failed=[1] result=resultsha",
                ctx.report.progress_messages,
            )

    def test_ci_batch_continue_success_skips_file_conflict_and_merges_later_gitlink_steps(self) -> None:
        from split_gitlink_merge import Conflict, NonGitlinkConflictError

        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = self._make_batch_context(Path(temp_dir), failure_mode="continue-success")
            ctx.args.sources = [
                "branch-1-gitlink",
                "branch-2-gitlink",
                "branch-3-file-conflict",
                "branch-4-gitlink",
                "branch-5-gitlink",
            ]
            ctx.args.source = ctx.args.sources[0]
            ctx.report.data["batch"]["source_count"] = len(ctx.args.sources)
            calls: list[dict[str, object]] = []

            def merge_repo(_ctx: object, repo_path: Path, **kwargs: object) -> object:
                step = len(calls) + 1
                calls.append({"step": step, **kwargs})
                if step == 3:
                    conflicts = [
                        Conflict(repo_path=repo_path, path="root-file.txt", stages=(), kind="file"),
                        Conflict(repo_path=repo_path, path="second-root-file.txt", stages=(), kind="file"),
                    ]
                    raise NonGitlinkConflictError(repo_path, conflicts)
                return types.SimpleNamespace(result_sha=f"result-step-{step}", strategy="recursive_gitlink_merge")

            ci_commands.run_batch_workflow(ctx, self._batch_services(), merge_repo)

            batch = ctx.report.data["batch"]
            steps = batch["steps"]
            self.assertEqual(len(calls), 5)
            self.assertEqual(batch["status"], "partial_success")
            self.assertEqual(batch["successful_steps"], [1, 2, 4, 5])
            self.assertEqual(batch["failed_steps"], [3])
            self.assertEqual(batch["failure_step"], 3)
            self.assertEqual(batch["final_result_sha"], "result-step-5")
            self.assertEqual(ctx.report.data["resolved"]["result_sha"], "result-step-5")
            self.assertEqual(ctx.report.data["exit_code"], ci_models.ExitCode.PARTIAL_SUCCESS)

            self.assertEqual(steps[2]["status"], "failed")
            self.assertTrue(steps[2]["continued_after_failure"])
            self.assertEqual(steps[2]["failure"]["exit_code"], ci_models.ExitCode.ROOT_CONFLICT)
            self.assertEqual(steps[2]["failure"]["reason"], "NON_GITLINK_CONFLICT")
            self.assertEqual(steps[2]["failure"]["details"]["type"], "NonGitlinkConflictError")
            self.assertEqual(
                steps[2]["failure"]["details"]["conflict_paths"],
                ["root-file.txt", "second-root-file.txt"],
            )
            self.assertEqual(steps[2]["failure"]["details"]["conflict_count"], 2)
            self.assertEqual(
                [item["path"] for item in steps[2]["failure"]["details"]["conflicts"]],
                ["root-file.txt", "second-root-file.txt"],
            )
            self.assertEqual(
                [item["path"] for item in ctx.report.data["conflicts"]],
                ["root-file.txt", "second-root-file.txt"],
            )
            self.assertEqual(ctx.report.data["summary"]["conflicts"], 2)
            self.assertEqual(steps[3]["target"], "result-step-2")
            self.assertEqual(steps[4]["target"], "result-step-4")
            self.assertIn("STEP 3/5 failed reason=NON_GITLINK_CONFLICT", ctx.report.progress_messages)
            self.assertIn("STEP 3/5 skipped continue_from=result-step-2", ctx.report.progress_messages)
            self.assertIn(
                "BATCH partial_success success=[1, 2, 4, 5] failed=[3] result=result-step-5",
                ctx.report.progress_messages,
            )

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
            self.assertIn(("fetch", "--no-recurse-submodules", "origin", "--prune"), runner_args)
            self.assertIn(("checkout", "--detach", "targetsha"), runner_args)
            root_fetch_index = runner_args.index(("fetch", "--no-recurse-submodules", "origin", "--prune"))
            target_checkout_index = runner_args.index(("checkout", "--detach", "targetsha"))
            submodule_sync_index = runner_args.index(("submodule", "sync", "--recursive"))
            self.assertLess(root_fetch_index, target_checkout_index)
            self.assertLess(target_checkout_index, submodule_sync_index)
            self.assertIn("CI start mode=single sources=1 target=main", ctx.report.progress_messages)
            self.assertIn("PREPARE pre-checkout repository start", ctx.report.progress_messages)
            self.assertIn("PREPARE pre-checkout repository done", ctx.report.progress_messages)
            self.assertIn("SYNC root refs for target checkout start", ctx.report.progress_messages)
            self.assertIn("CHECKOUT target repository start", ctx.report.progress_messages)
            self.assertIn("SYNC remote refs start", ctx.report.progress_messages)
            self.assertIn("SYNC remote refs done", ctx.report.progress_messages)
            self.assertIn("CHECKOUT target after sync done", ctx.report.progress_messages)

    def test_ci_auto_merge_cleans_when_post_merge_sync_dirties_repo(self) -> None:
        from merge_tool_adapters import CiAutoGitlinkMergeService
        from split_gitlink_merge import GitCommandResult

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()

            class PostMergeDirtyRunner(FakeCiRunner):
                def __init__(self) -> None:
                    super().__init__()
                    self.status_checks = 0

                def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
                    if args == ["status", "--porcelain=v1"]:
                        self.calls.append((repo_path, tuple(args), allow_failure))
                        self.status_checks += 1
                        if self.status_checks == 2:
                            return FakeCiCommandResult(args, stdout=" M sub\n")
                        return FakeCiCommandResult(args)
                    return super().run(repo_path, args, allow_failure=allow_failure)

            class CleanMergeBackend:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, ...]] = []

                def run(self, repo_path: Path, args: list[str], *, check: bool = True) -> GitCommandResult:
                    del check
                    self.calls.append(tuple(args))
                    if args == ["merge", "--no-ff", "--no-commit", "sourcesha"]:
                        return GitCommandResult(args=args, cwd=repo_path, returncode=0)
                    if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                        return GitCommandResult(args=args, cwd=repo_path, returncode=0, stdout="sourcesha\n")
                    if args == ["diff", "--cached", "--quiet", "--ignore-submodules=none"]:
                        return GitCommandResult(args=args, cwd=repo_path, returncode=1)
                    if args[:1] == ["commit"]:
                        return GitCommandResult(args=args, cwd=repo_path, returncode=0)
                    if args == ["rev-parse", "HEAD"]:
                        return GitCommandResult(args=args, cwd=repo_path, returncode=0, stdout="resultsha\n")
                    return GitCommandResult(args=args, cwd=repo_path, returncode=0)

            runner = PostMergeDirtyRunner()
            ctx = FakeCiContext(Path(temp_dir), runner)
            ctx.root_repo = root
            backend = CleanMergeBackend()
            service = CiAutoGitlinkMergeService(backend, ctx=ctx)
            service._collect_repository_details = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

            result = service.merge_repo(
                root,
                source_sha="sourcesha",
                target_sha="targetsha",
                base_sha="basesha",
                depth=0,
                display_path=".",
            )

            self.assertEqual(result.result_sha, "resultsha")
            self.assertEqual(len(ctx.report.fallbacks), 1)
            fallback_scope = ctx.report.fallbacks[0]["scope"]
            self.assertEqual(fallback_scope["type"], "merge_repo_result_checkout")
            self.assertEqual(fallback_scope["preserve_refs"], ["resultsha"])
            self.assertIn("working tree not clean", "\n".join(fallback_scope["post_state_change_verify_errors"]))
            runner_args = [call[1] for call in runner.calls]
            self.assertIn(("checkout", "--detach", "resultsha"), runner_args)
            self.assertIn(("submodule", "update", "--init", "--recursive", "--force"), runner_args)

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
            self.assertTrue(Path(patches["repository_expanded"]["path"]).exists())
            self.assertEqual(patches["repository"]["context_lines"], 3)
            self.assertEqual(patches["repository_expanded"]["context_lines"], 20)
            self.assertEqual(changed_files[0]["patch_relative_path"], "patches/root/files/0001-file.txt.patch")
            self.assertEqual(changed_files[0]["expanded_patch_relative_path"], "patches/root/files/0001-file.txt.expanded.patch")
            self.assertEqual(changed_files[0]["expanded_patch_context_lines"], 20)

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

    def test_ci_artifacts_export_clean_gitlink_submodule_diffs_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            repo = output_dir / "repo"
            sub_repo = repo / "libs" / "sub"
            sub_repo.mkdir(parents=True)

            class GitlinkDiffRunner(FakeCiRunner):
                def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
                    if repo_path == repo.resolve(strict=False) and args[:1] == ["diff"] and "--name-status" in args:
                        return FakeCiCommandResult(args, stdout="M\tlibs/sub\n")
                    if repo_path == repo.resolve(strict=False) and args == [
                        "diff",
                        "--raw",
                        "--no-abbrev",
                        "ours",
                        "result",
                        "--",
                    ]:
                        return FakeCiCommandResult(
                            args,
                            stdout=":160000 160000 oldsub newsub M\tlibs/sub\n",
                        )
                    if repo_path == sub_repo.resolve(strict=False) and args == [
                        "diff",
                        "--raw",
                        "--no-abbrev",
                        "oldsub",
                        "newsub",
                        "--",
                    ]:
                        return FakeCiCommandResult(args, stdout="")
                    return super().run(repo_path, args, allow_failure=allow_failure)

            ctx = FakeCiContext(output_dir, GitlinkDiffRunner())
            ctx.root_repo = repo
            ci_artifacts.collect_repository_details(
                ctx,
                repo,
                ".",
                "base",
                "ours",
                "theirs",
                "result",
                "clean_merge",
            )

            self.assertEqual([item["path"] for item in ctx.report.repositories], [".", "libs/sub"])
            root_repository = ctx.report.repositories[0]
            self.assertEqual(root_repository["changed_files"][0]["path"], "libs/sub")
            self.assertEqual(root_repository["changed_files"][0]["kind"], "gitlink")
            self.assertEqual(root_repository["changed_files"][0]["old_sha"], "oldsub")
            self.assertEqual(root_repository["changed_files"][0]["new_sha"], "newsub")
            submodule_repository = ctx.report.repositories[1]
            self.assertEqual(submodule_repository["base"], "oldsub")
            self.assertEqual(submodule_repository["ours"], "oldsub")
            self.assertEqual(submodule_repository["theirs"], "newsub")
            self.assertEqual(submodule_repository["result"], "newsub")
            self.assertEqual(submodule_repository["strategy"], "gitlink_update")

    def test_ci_artifacts_deduplicates_gitlink_event_and_scan_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            repo = output_dir / "repo"
            sub_repo = repo / "libs" / "sub"
            sub_repo.mkdir(parents=True)

            class GitlinkDiffRunner(FakeCiRunner):
                def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
                    if repo_path == repo.resolve(strict=False) and args[:1] == ["diff"] and "--name-status" in args:
                        return FakeCiCommandResult(args, stdout="M\tlibs/sub\n")
                    if repo_path == repo.resolve(strict=False) and args == [
                        "diff",
                        "--raw",
                        "--no-abbrev",
                        "ours",
                        "result",
                        "--",
                    ]:
                        return FakeCiCommandResult(
                            args,
                            stdout=":160000 160000 oldsub newsub M\tlibs/sub\n",
                        )
                    if repo_path == sub_repo.resolve(strict=False) and args == [
                        "diff",
                        "--raw",
                        "--no-abbrev",
                        "oldsub",
                        "newsub",
                        "--",
                    ]:
                        return FakeCiCommandResult(args, stdout="")
                    return super().run(repo_path, args, allow_failure=allow_failure)

            ctx = FakeCiContext(output_dir, GitlinkDiffRunner())
            ctx.root_repo = repo
            ci_artifacts.collect_repository_details(
                ctx,
                sub_repo,
                "libs/sub",
                "oldsub",
                "oldsub",
                "newsub",
                "newsub",
                "use_theirs",
            )
            ci_artifacts.collect_repository_details(
                ctx,
                repo,
                ".",
                "base",
                "ours",
                "theirs",
                "result",
                "clean_merge",
            )

            paths = [item["path"] for item in ctx.report.repositories]
            self.assertEqual(paths, ["libs/sub", "."])
            self.assertEqual(paths.count("libs/sub"), 1)

    def test_ci_recovery_helpers_are_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = FakeCiContext(Path(temp_dir))
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            self.assertEqual(ci_recovery.clean_flag(ctx), "-fd")
            self.assertEqual(ci_recovery.clean_flag(ctx, remove_nested_git_repos=True), "-ffd")
            ctx.allow_clean_x = True
            self.assertEqual(ci_recovery.clean_flag(ctx), "-fdx")
            self.assertEqual(ci_recovery.clean_flag(ctx, remove_nested_git_repos=True), "-ffdx")
            ctx.allow_clean_x = False

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
            calls = [(call[0], call[1], call[2]) for call in ctx.runner.calls]
            self.assertIn((repo, ("clean", "-ffd"), False), calls)
            self.assertIn((repo, ("submodule", "foreach", "--recursive", "git clean -ffd"), True), calls)

    def test_shared_cleanup_core_builds_ci_cleanup_sequences(self) -> None:
        self.assertEqual(cleanup_steps.git_clean_flag(False), "-fd")
        self.assertEqual(cleanup_steps.git_clean_flag(True), "-fdx")
        self.assertEqual(cleanup_steps.git_clean_flag(False, remove_nested_git_repos=True), "-ffd")
        self.assertEqual(cleanup_steps.git_clean_flag(True, remove_nested_git_repos=True), "-ffdx")

        levels = cleanup_steps.ci_prepare_cleanup_levels(False)
        self.assertEqual([level.name for level in levels], [
            "level_1_basic_cleanup",
            "level_2_abort_and_recursive_cleanup",
            "level_3_sync_and_force_update",
        ])
        self.assertIn(("clean", "-ffd"), [step.args for step in levels[1].steps])
        self.assertIn(
            ("submodule", "foreach", "--recursive", "git clean -ffd"),
            [step.args for step in levels[1].steps],
        )

        batch_args = [step.args for step in cleanup_steps.ci_batch_continue_restore_steps("targetsha", False)]
        self.assertEqual(batch_args[0], ("merge", "--abort"))
        self.assertIn(("reset", "--hard", "targetsha"), batch_args)
        self.assertIn(("submodule", "update", "--init", "--recursive", "--force"), batch_args)

    def test_ci_cleanup_runner_accepts_shared_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = FakeCiContext(Path(temp_dir))
            repo = Path(temp_dir) / "repo"
            repo.mkdir()

            cleanup = ci_recovery.run_cleanup_steps(
                ctx,
                repo,
                "shared",
                [cleanup_steps.reset_hard_step()],
            )

            self.assertTrue(cleanup["success"])
            self.assertEqual(cleanup["steps"][0]["cmd"], ["git", "reset", "--hard"])

    def test_ci_fresh_clone_fallback_swaps_workspace_and_preserves_batch_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            repo = parent / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            (repo / "old.txt").write_text("old workspace", encoding="utf-8")

            class FreshCloneRunner(FakeCiRunner):
                def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
                    if args == ["remote", "get-url", "origin"]:
                        self.calls.append((repo_path, tuple(args), allow_failure))
                        return FakeCiCommandResult(args, stdout=str(repo) + "\n")
                    if args[:4] == ["clone", "--recurse-submodules", "--origin", "origin"]:
                        self.calls.append((repo_path, tuple(args), allow_failure))
                        clone_dir = repo_path / args[-1]
                        clone_dir.mkdir()
                        (clone_dir / ".git").mkdir()
                        (clone_dir / "new.txt").write_text("fresh workspace", encoding="utf-8")
                        return FakeCiCommandResult(args)
                    if args == ["fetch", "--no-tags", str(repo), "intermediate-sha"]:
                        self.calls.append((repo_path, tuple(args), allow_failure))
                        return FakeCiCommandResult(args)
                    if args == ["rev-parse", "--verify", "intermediate-sha^{commit}"]:
                        self.calls.append((repo_path, tuple(args), allow_failure))
                        return FakeCiCommandResult(args, stdout="intermediate-sha\n")
                    return super().run(repo_path, args, allow_failure=allow_failure)

            ctx = FakeCiContext(parent, FreshCloneRunner())
            fallback = ci_recovery.run_fresh_clone_fallback(
                ctx,
                repo,
                scope={"type": "batch_step_start", "preserve_refs": ["intermediate-sha"]},
            )

            self.assertTrue(fallback["success"])
            self.assertEqual(fallback["level"], "level_5_fresh_clone_workspace")
            self.assertEqual(fallback["preserved_refs"], ["intermediate-sha"])
            self.assertTrue((repo / ".git").exists())
            self.assertTrue((repo / "new.txt").exists())
            self.assertFalse((repo / "old.txt").exists())
            self.assertTrue(Path(fallback["backup_path"]).exists())
            self.assertIn(
                ("fetch", "--no-tags", str(repo), "intermediate-sha"),
                [call[1] for call in ctx.runner.calls],
            )

    def test_ci_fresh_clone_fallback_refuses_output_inside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            repo = parent / "repo"
            repo.mkdir()
            ctx = FakeCiContext(parent)
            ctx.report.data["output"] = {"generated_file_inside_repo": True}

            fallback = ci_recovery.run_fresh_clone_fallback(ctx, repo)

            self.assertFalse(fallback["success"])
            self.assertEqual(fallback["failure_reason"], "FRESH_CLONE_UNSAFE_OUTPUT_INSIDE_REPO")
            self.assertTrue(repo.exists())
            self.assertEqual(ctx.runner.calls, [])

    def test_ci_publish_result_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = FakePublishRunner()
            ctx = FakeCiContext(Path(temp_dir), runner)
            ctx.args.publish_target = "main"
            repo = Path(temp_dir) / "repo"
            repo.mkdir()

            publish = publish_result(ctx, repo, "targetsha", "resultsha")

            self.assertTrue(publish["success"])
            self.assertFalse(publish["target_created"])
            self.assertEqual(publish["target_publish_mode"], "force-with-lease")
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

    def test_ci_publish_result_creates_missing_target_only_when_allowed(self) -> None:
        class MissingTargetRunner(FakePublishRunner):
            def __init__(self) -> None:
                super().__init__()
                self.target_fetches = 0

            def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCiCommandResult:
                self.calls.append((repo_path, tuple(args), allow_failure))
                if args == ["fetch", "origin", "new-target"]:
                    self.target_fetches += 1
                    if self.target_fetches == 1:
                        return FakeCiCommandResult(
                            args,
                            exit_code=128,
                            stderr="fatal: couldn't find remote ref new-target\n",
                        )
                    return FakeCiCommandResult(args)
                if args == ["rev-parse", "FETCH_HEAD"]:
                    return FakeCiCommandResult(args, stdout="resultsha\n")
                return FakeCiCommandResult(args)

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            blocked = FakeCiContext(Path(temp_dir), MissingTargetRunner())
            blocked.args.publish_target = "new-target"

            with self.assertRaises(ci_models.CiMergeError) as blocked_error:
                publish_result(blocked, repo, "targetsha", "resultsha")
            self.assertEqual(blocked_error.exception.reason, "PUBLISH_TARGET_NOT_FOUND")

            runner = MissingTargetRunner()
            ctx = FakeCiContext(Path(temp_dir), runner)
            ctx.args.publish_target = "new-target"
            ctx.args.allow_create_publish_target = True

            publish = publish_result(ctx, repo, "targetsha", "resultsha")

            self.assertTrue(publish["success"])
            self.assertTrue(publish["target_created"])
            self.assertEqual(publish["target_publish_mode"], "create")
            self.assertIsNone(publish["remote_target_before_sha"])
            self.assertEqual(publish["remote_target_after_sha"], "resultsha")
            pushed = [call[1] for call in runner.calls if call[1] and call[1][0] == "push"]
            self.assertIn(
                (
                    "push",
                    "--recurse-submodules=check",
                    "origin",
                    "resultsha:refs/heads/new-target",
                ),
                pushed,
            )
            self.assertNotIn(
                (
                    "push",
                    "--recurse-submodules=check",
                    "--force-with-lease=new-target:targetsha",
                    "origin",
                    "resultsha:new-target",
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
            ctx.args.publish_target = "main"

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
        local_command_dict = local_command.to_dict()
        legacy_command_dict = dict(local_command_dict)
        self.assertEqual(legacy_command_dict.pop("attempt"), 1)
        self.assertEqual(legacy_command_dict.pop("max_attempts"), 1)
        self.assertIsNone(legacy_command_dict.pop("network_failure_reason"))
        original_command = self.ci_merge.CommandResult(**legacy_command_dict)
        self.assertEqual(legacy_command_dict, original_command.to_dict())

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

    def test_manual_commit_messages_include_ci_style_metadata(self) -> None:
        root_message = manual_messages.build_root_commit_message(
            repo_path=Path("repo"),
            source_ref="origin/feature",
            target_ref="origin/main",
            source_sha="a" * 40,
            target_sha="b" * 40,
            work_branch="work",
            resolved_submodules=["libs/core"],
        )
        self.assertEqual(root_message.title, "Manual merge: origin/feature into origin/main")
        self.assertIn("Source SHA: " + "a" * 40, root_message.body)
        self.assertIn("Target SHA: " + "b" * 40, root_message.body)
        self.assertIn("Resolved submodules:\n    libs/core", root_message.body)

        submodule_message = manual_messages.build_submodule_commit_message(
            repo_path=Path("repo/libs/core"),
            parent_repo=Path("repo"),
            display_path="libs/core",
            source_ref="c" * 40,
            target_ref="d" * 40,
            source_sha="c" * 40,
            target_sha="d" * 40,
            base_sha="e" * 40,
            work_branch="work",
        )
        self.assertEqual(submodule_message.title, "Manual submodule merge: libs/core")
        self.assertIn("Parent Repository: repo", submodule_message.body)
        self.assertIn("Submodule Path: libs/core", submodule_message.body)
        self.assertIn("Base SHA: " + "e" * 40, submodule_message.body)
        self.assertIn("Ours SHA: " + "d" * 40, submodule_message.body)
        self.assertIn("Theirs SHA: " + "c" * 40, submodule_message.body)

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
            self.assertIn(
                ("fetch", "--no-recurse-submodules", "origin", "+refs/heads/main:refs/remotes/origin/main"),
                calls,
            )
            self.assertIn(
                ("fetch", "--no-recurse-submodules", "origin", "+refs/heads/feature:refs/remotes/origin/feature"),
                calls,
            )
            self.assertNotIn(("fetch", "--all", "--prune"), calls)
            self.assertNotIn(("fetch", "--no-recurse-submodules", "--all", "--prune"), calls)

    def test_manual_preflight_dirty_root_continues_after_risk_confirmation(self) -> None:
        from manual_merge_split.preflight import ManualPreflightService

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []
            confirmations: list[dict[str, object]] = []

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
                    return FakeManualGitResult(args, stdout=" M local.txt\n?? scratch.txt\n")
                if args == ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["remote"]:
                    return FakeManualGitResult(args, stdout="origin\n")
                return FakeManualGitResult(args)

            def confirm_risky_operation(**kwargs: object) -> bool:
                confirmations.append(dict(kwargs))
                return True

            module = types.SimpleNamespace(
                run_git=run_git,
                confirm_risky_operation=confirm_risky_operation,
                run_git_binary=lambda _repo_path, args, **_kwargs: FakeManualBinaryResult(args, b""),
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            service = ManualPreflightService(module)  # type: ignore[arg-type]

            result = service.preflight_repository(repo, "origin/main", ["origin/feature"])

            self.assertEqual(result.allowed_dirty_submodules, [])
            self.assertEqual(confirmations[0]["reason"], "ROOT_UNCOMMITTED_CHANGES_BEFORE_PREFLIGHT")
            self.assertEqual(confirmations[0]["details"]["status"], [" M local.txt", "?? scratch.txt"])  # type: ignore[index]
            self.assertIn(
                ("fetch", "--no-recurse-submodules", "origin", "+refs/heads/feature:refs/remotes/origin/feature"),
                calls,
            )

    def test_manual_preflight_dirty_touched_submodule_continues_after_risk_confirmation(self) -> None:
        from manual_merge_split.preflight import ManualPreflightService

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            sub_repo = repo / "sub"
            sub_repo.mkdir()
            (repo / ".gitmodules").write_text("[submodule \"sub\"]\n\tpath = sub\n", encoding="utf-8")
            (sub_repo / ".git").mkdir()
            calls: list[tuple[Path, tuple[str, ...]]] = []
            confirmations: list[dict[str, object]] = []

            def run_git(repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                calls.append((repo_path, tuple(args)))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo_path) + "\n")
                if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["config", "--get", "user.name"]:
                    return FakeManualGitResult(args, stdout="User\n")
                if args == ["config", "--get", "user.email"]:
                    return FakeManualGitResult(args, stdout="user@example.com\n")
                if args == ["status", "--porcelain", "--ignore-submodules=dirty"]:
                    if repo_path == sub_repo:
                        return FakeManualGitResult(args, stdout=" M local-sub.txt\n")
                    return FakeManualGitResult(args)
                if args == ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"]:
                    return FakeManualGitResult(args, stdout="submodule.sub.path sub\n")
                if args == ["remote"]:
                    return FakeManualGitResult(args, stdout=("origin\n" if repo_path == repo else ""))
                if args == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
                    return FakeManualGitResult(args, stdout="base-commit\n")
                if args == ["rev-parse", "--verify", "refs/remotes/origin/feature^{commit}"]:
                    return FakeManualGitResult(args, stdout="feature-commit\n")
                if args == ["ls-tree", "base-commit", "--", "sub"]:
                    return FakeManualGitResult(args, stdout="160000 commit sub-base\tsub\n")
                if args == ["ls-tree", "feature-commit", "--", "sub"]:
                    return FakeManualGitResult(args, stdout="160000 commit sub-feature\tsub\n")
                return FakeManualGitResult(args)

            def confirm_risky_operation(**kwargs: object) -> bool:
                confirmations.append(dict(kwargs))
                return True

            module = types.SimpleNamespace(
                run_git=run_git,
                confirm_risky_operation=confirm_risky_operation,
                run_git_binary=lambda _repo_path, args, **_kwargs: FakeManualBinaryResult(args, b""),
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            service = ManualPreflightService(module)  # type: ignore[arg-type]

            result = service.preflight_repository(repo, "origin/main", ["origin/feature"])

            self.assertEqual([entry.lineage for entry in result.allowed_dirty_submodules], [("sub",)])
            self.assertTrue(result.allowed_dirty_submodules[0].risk_confirmed)
            self.assertEqual(confirmations[0]["reason"], "DIRTY_SUBMODULE_TOUCHED_BY_MERGE")
            self.assertIn("sub", confirmations[0]["details"]["submodules"][0])  # type: ignore[index]
            self.assertIn(
                (
                    repo,
                    ("fetch", "--no-recurse-submodules", "origin", "+refs/heads/feature:refs/remotes/origin/feature"),
                ),
                calls,
            )

    def test_manual_guided_prompts_and_cleanup_path_are_local(self) -> None:
        self.assertEqual(prompt_with_default("Prompt", "default", input_func=lambda _prompt: ""), "default")
        self.assertEqual(prompt_optional("Prompt", "default", input_func=lambda _prompt: "value"), "value")
        self.assertIsNone(prompt_optional("Prompt", input_func=lambda _prompt: ""))
        self.assertTrue(prompt_yes_no("Prompt", True, input_func=lambda _prompt: ""))
        self.assertTrue(prompt_yes_no("Prompt", False, input_func=lambda _prompt: "yes"))

        answers = iter(["", "cleanup", "n", "", "", "", ""])
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
            "tool",
            "--ignore-root-untracked-path",
            "generated.txt",
        ]
        local_merge = manual_runtime.parse_args(merge_argv)
        original_merge = self.manual_merge.parse_args(merge_argv)
        local_merge_values = vars(local_merge).copy()
        self.assertFalse(local_merge_values.pop("no_confirm_after_conflict_tool"))
        self.assertFalse(local_merge_values.pop("enable_cross_submodule_moves"))
        self.assertEqual(local_merge_values, vars(original_merge))
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                manual_runtime.parse_args(["feature", "--file-conflict-mode", "preserve"])

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
        local_cleanup_values = vars(local_cleanup).copy()
        self.assertFalse(local_cleanup_values.pop("allow_clean_x"))
        self.assertEqual(local_cleanup_values, vars(original_cleanup))

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

    def test_manual_merge_creates_missing_work_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []
            branch_created = False
            merge_pending = False

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                nonlocal branch_created, merge_pending
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo) + "\n")
                if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                    if merge_pending:
                        return FakeManualGitResult(args, stdout="feature-commit\n")
                    return FakeManualGitResult(args, returncode=1)
                if args == ["config", "--get", "user.name"]:
                    return FakeManualGitResult(args, stdout="User\n")
                if args == ["config", "--get", "user.email"]:
                    return FakeManualGitResult(args, stdout="user@example.com\n")
                if args == ["status", "--porcelain", "--ignore-submodules=dirty"]:
                    return FakeManualGitResult(args)
                if args == ["status", "--porcelain"]:
                    return FakeManualGitResult(args, stdout=(" M merged.txt\n" if merge_pending else ""))
                if args == ["remote"]:
                    return FakeManualGitResult(args, stdout="origin\n")
                if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                    return FakeManualGitResult(args, stdout=("work\n" if branch_created else "main\n"))
                if args == ["show-ref", "--verify", "--quiet", "refs/heads/work"]:
                    if branch_created:
                        return FakeManualGitResult(args, stdout="base-commit refs/heads/work\n")
                    return FakeManualGitResult(args, returncode=1)
                if args == ["rev-parse", "--verify", "refs/heads/work^{commit}"]:
                    if branch_created:
                        return FakeManualGitResult(args, stdout="base-commit\n")
                    return FakeManualGitResult(
                        args,
                        stderr="fatal: Needed a single revision\n",
                        returncode=128,
                    )
                if args == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
                    return FakeManualGitResult(args, stdout="base-commit\n")
                if args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                    return FakeManualGitResult(args, stdout="base-commit\n")
                if args == ["rev-parse", "--verify", "refs/remotes/origin/feature^{commit}"]:
                    return FakeManualGitResult(args, stdout="feature-commit\n")
                if args == ["rev-parse", "HEAD"]:
                    return FakeManualGitResult(args, stdout="base-commit\n")
                if args == ["checkout", "-B", "work", "refs/remotes/origin/main"]:
                    branch_created = True
                    return FakeManualGitResult(args)
                if args == ["merge", "--no-ff", "--no-commit", "refs/remotes/origin/feature"]:
                    merge_pending = True
                    return FakeManualGitResult(args)
                if args[:1] == ["commit"]:
                    merge_pending = False
                    return FakeManualGitResult(args)
                if args == ["diff", "--name-only", "--diff-filter=U"]:
                    return FakeManualGitResult(args)
                return FakeManualGitResult(args)

            module = types.SimpleNamespace(
                run_git=run_git,
                run_git_binary=lambda _repo_path, args, **_kwargs: FakeManualBinaryResult(args, b""),
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            service = ManualCommandService(module)  # type: ignore[arg-type]

            result = service.run_merge(
                types.SimpleNamespace(
                    repo=str(repo),
                    base_remote_branch="origin/main",
                    merge_branches=["origin/feature"],
                    continue_current_branch=False,
                    work_branch="work",
                    clone_branch=None,
                    clone_url=None,
                    clone_path=None,
                    report_file="merge_manual_todo.txt",
                    verbose=False,
                    ignore_root_untracked_path=[],
                    allow_dirty_submodules=False,
                    no_prompt_dirty_submodules=True,
                    file_conflict_mode="tool",
                    conflict_tool_command="tool",
                    conflict_tool_timeout_seconds=1,
                    no_confirm_after_conflict_tool=False,
                    enable_cross_submodule_moves=False,
                )
            )

            self.assertEqual(result, 0)
            self.assertTrue(branch_created)
            self.assertIn(("checkout", "-B", "work", "refs/remotes/origin/main"), calls)
            commit_calls = [args for args in calls if args[:1] == ("commit",)]
            self.assertEqual(len(commit_calls), 1)
            commit_args = commit_calls[0]
            self.assertEqual(commit_args[:3], ("commit", "-m", "Manual merge: origin/feature into origin/main"))
            self.assertIn("Source SHA: feature-commit", commit_args[-1])
            self.assertIn("Target SHA: base-commit", commit_args[-1])
            self.assertIn("Work Branch: work", commit_args[-1])

    def test_manual_merge_does_not_reject_dirty_submodule_after_risk_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            sub_repo = repo / "sub"
            sub_repo.mkdir()
            (repo / ".gitmodules").write_text("[submodule \"sub\"]\n\tpath = sub\n", encoding="utf-8")
            (sub_repo / ".git").mkdir()
            calls: list[tuple[Path, tuple[str, ...]]] = []
            confirmations: list[dict[str, object]] = []
            branch_created = False

            def run_git(repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                nonlocal branch_created
                calls.append((repo_path, tuple(args)))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo_path) + "\n")
                if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["config", "--get", "user.name"]:
                    return FakeManualGitResult(args, stdout="User\n")
                if args == ["config", "--get", "user.email"]:
                    return FakeManualGitResult(args, stdout="user@example.com\n")
                if args == ["status", "--porcelain", "--ignore-submodules=dirty"]:
                    if repo_path == sub_repo:
                        return FakeManualGitResult(args, stdout=" M local-sub.txt\n")
                    return FakeManualGitResult(args)
                if args == ["status", "--porcelain"]:
                    return FakeManualGitResult(args)
                if args == ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"]:
                    if repo_path == repo:
                        return FakeManualGitResult(args, stdout="submodule.sub.path sub\n")
                    return FakeManualGitResult(args, returncode=1)
                if args == ["remote"]:
                    return FakeManualGitResult(args, stdout=("origin\n" if repo_path == repo else ""))
                if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                    return FakeManualGitResult(args, stdout=("work\n" if branch_created else "main\n"))
                if args == ["show-ref", "--verify", "--quiet", "refs/heads/work"]:
                    if branch_created:
                        return FakeManualGitResult(args, stdout="base-commit refs/heads/work\n")
                    return FakeManualGitResult(args, returncode=1)
                if args == ["rev-parse", "--verify", "refs/heads/work^{commit}"]:
                    if branch_created:
                        return FakeManualGitResult(args, stdout="base-commit\n")
                    return FakeManualGitResult(
                        args,
                        stderr="fatal: Needed a single revision\n",
                        returncode=128,
                    )
                if args == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
                    return FakeManualGitResult(args, stdout="base-commit\n")
                if args == ["rev-parse", "--verify", "refs/remotes/origin/feature^{commit}"]:
                    return FakeManualGitResult(args, stdout="feature-commit\n")
                if args == ["ls-tree", "base-commit", "--", "sub"]:
                    return FakeManualGitResult(args, stdout="160000 commit sub-base\tsub\n")
                if args == ["ls-tree", "feature-commit", "--", "sub"]:
                    return FakeManualGitResult(args, stdout="160000 commit sub-feature\tsub\n")
                if args == ["rev-parse", "HEAD"]:
                    return FakeManualGitResult(args, stdout="base-commit\n")
                if args == ["checkout", "-B", "work", "refs/remotes/origin/main"]:
                    branch_created = True
                    return FakeManualGitResult(args)
                if args == ["merge", "--no-ff", "--no-commit", "refs/remotes/origin/feature"]:
                    return FakeManualGitResult(args)
                return FakeManualGitResult(args)

            def confirm_risky_operation(**kwargs: object) -> bool:
                confirmations.append(dict(kwargs))
                return True

            module = types.SimpleNamespace(
                run_git=run_git,
                confirm_risky_operation=confirm_risky_operation,
                run_git_binary=lambda _repo_path, args, **_kwargs: FakeManualBinaryResult(args, b""),
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            service = ManualCommandService(module)  # type: ignore[arg-type]

            result = service.run_merge(
                types.SimpleNamespace(
                    repo=str(repo),
                    base_remote_branch="origin/main",
                    merge_branches=["origin/feature"],
                    continue_current_branch=False,
                    work_branch="work",
                    clone_branch=None,
                    clone_url=None,
                    clone_path=None,
                    report_file="merge_manual_todo.txt",
                    verbose=False,
                    ignore_root_untracked_path=[],
                    allow_dirty_submodules=False,
                    no_prompt_dirty_submodules=True,
                    file_conflict_mode="tool",
                    conflict_tool_command="tool",
                    conflict_tool_timeout_seconds=1,
                    no_confirm_after_conflict_tool=False,
                    enable_cross_submodule_moves=False,
                )
            )

            self.assertEqual(result, 0)
            self.assertEqual(confirmations[0]["reason"], "DIRTY_SUBMODULE_TOUCHED_BY_MERGE")
            self.assertTrue(branch_created)

    def test_manual_cleanup_rebuild_creates_missing_work_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
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
                    return FakeManualGitResult(args)
                if args == ["remote"]:
                    return FakeManualGitResult(args, stdout="origin\n")
                if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                    return FakeManualGitResult(args, stdout="main\n")
                if args == ["show-ref", "--verify", "--quiet", "refs/heads/work"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["rev-parse", "--verify", "refs/heads/work^{commit}"]:
                    return FakeManualGitResult(
                        args,
                        stderr="fatal: Needed a single revision\n",
                        returncode=128,
                    )
                if args == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
                    return FakeManualGitResult(args, stdout="base-commit\n")
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
                    skip_submodule_update=True,
                    rebuild_work_branch=True,
                    base_remote_branch="origin/main",
                    work_branch="work",
                    verbose=False,
                )
            )

            self.assertEqual(result, 0)
            self.assertIn(("checkout", "-B", "work", "refs/remotes/origin/main"), calls)

    def test_manual_submodule_branch_creation_allows_missing_work_branch(self) -> None:
        from manual_merge_split.repository_ops import ManualRepositoryOps

        repo = Path("repo/sub")
        calls: list[tuple[str, ...]] = []

        def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
            calls.append(tuple(args))
            if args == ["show-ref", "--verify", "--quiet", "refs/heads/work"]:
                return FakeManualGitResult(args, returncode=1)
            if args == ["rev-parse", "--verify", "refs/heads/work^{commit}"]:
                return FakeManualGitResult(
                    args,
                    stderr="fatal: Needed a single revision\n",
                    returncode=128,
                )
            if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                return FakeManualGitResult(args, stdout="main\n")
            if args == ["rev-parse", "--verify", "startsha^{commit}"]:
                return FakeManualGitResult(args, stdout="startsha\n")
            return FakeManualGitResult(args)

        module = types.SimpleNamespace(
            run_git=run_git,
            MergeAutomationError=manual_models.MergeAutomationError,
            VERBOSE=False,
        )
        ops = ManualRepositoryOps(module)  # type: ignore[arg-type]

        ops.create_submodule_branch(repo, "work", start_point="startsha")

        self.assertIn(("checkout", "-B", "work", "startsha"), calls)

    def test_manual_branch_exists_treats_invalid_ref_as_missing(self) -> None:
        from manual_merge_split.repository_ops import ManualRepositoryOps

        repo = Path("repo")
        calls: list[tuple[str, ...]] = []

        def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
            calls.append(tuple(args))
            if args == ["show-ref", "--verify", "--quiet", "refs/heads/liuheng/2026/temp0604/bugfix"]:
                return FakeManualGitResult(args, stderr="fatal: not a valid ref\n", returncode=128)
            return FakeManualGitResult(args)

        module = types.SimpleNamespace(
            run_git=run_git,
            MergeAutomationError=manual_models.MergeAutomationError,
            VERBOSE=False,
        )
        ops = ManualRepositoryOps(module)  # type: ignore[arg-type]

        self.assertFalse(ops.branch_exists(repo, "liuheng/2026/temp0604/bugfix"))
        self.assertEqual(calls, [("show-ref", "--verify", "--quiet", "refs/heads/liuheng/2026/temp0604/bugfix")])

    def test_manual_risk_confirmation_protocol_allows_non_tty_pipe_confirmation(self) -> None:
        from manual_merge_split.repository_ops import (
            ManualRepositoryOps,
            RISK_CONFIRMATION_EVENT_PREFIX,
            RISK_CONFIRMATION_PROTOCOL_ENV,
            RISK_CONFIRMATION_PROTOCOL_VERSION,
        )

        ops = ManualRepositoryOps()
        stdin = io.StringIO("y\n")
        stderr = io.StringIO()

        with mock.patch.dict(os.environ, {RISK_CONFIRMATION_PROTOCOL_ENV: RISK_CONFIRMATION_PROTOCOL_VERSION}):
            with mock.patch.object(sys, "stdin", stdin):
                with mock.patch.object(sys, "stderr", stderr):
                    confirmed = ops.confirm_risky_operation(
                        reason="LOCAL_BRANCH_RESET_RISK",
                        message="Continuing will reset a branch.",
                        details={"branch": "work", "affected_files": ["a.txt"]},
                    )

        output = stderr.getvalue()
        event_line = next(line for line in output.splitlines() if line.startswith(RISK_CONFIRMATION_EVENT_PREFIX))
        payload = json.loads(event_line.removeprefix(RISK_CONFIRMATION_EVENT_PREFIX))

        self.assertTrue(confirmed)
        self.assertEqual(payload["protocol"], RISK_CONFIRMATION_PROTOCOL_VERSION)
        self.assertEqual(payload["reason"], "LOCAL_BRANCH_RESET_RISK")
        self.assertEqual(payload["details"]["affected_files"], ["a.txt"])

    def test_manual_branch_reset_continues_after_risk_confirmation(self) -> None:
        from manual_merge_split.repository_ops import ManualRepositoryOps

        repo = Path("repo")
        calls: list[tuple[str, ...]] = []
        confirmations: list[dict[str, object]] = []

        def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
            calls.append(tuple(args))
            if args == ["remote"]:
                return FakeManualGitResult(args, stdout="origin\n")
            if args == ["show-ref", "--verify", "--quiet", "refs/heads/work"]:
                return FakeManualGitResult(args)
            if args == ["rev-parse", "--verify", "refs/heads/work^{commit}"]:
                return FakeManualGitResult(args, stdout="local-only-commit\n")
            if args == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
                return FakeManualGitResult(args, stdout="base-commit\n")
            if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                return FakeManualGitResult(args, stdout="main\n")
            if args == ["diff", "--name-only", "base-commit", "local-only-commit", "--"]:
                return FakeManualGitResult(args, stdout="local.txt\n")
            return FakeManualGitResult(args)

        def confirm_risky_operation(**kwargs: object) -> bool:
            confirmations.append(dict(kwargs))
            return True

        module = types.SimpleNamespace(
            run_git=run_git,
            confirm_risky_operation=confirm_risky_operation,
            MergeAutomationError=manual_models.MergeAutomationError,
            VERBOSE=False,
        )
        ops = ManualRepositoryOps(module)  # type: ignore[arg-type]

        ops.create_root_branch(repo, "origin/main", "work")

        self.assertIn(("checkout", "-B", "work", "refs/remotes/origin/main"), calls)
        self.assertEqual(confirmations[0]["reason"], "LOCAL_BRANCH_RESET_RISK")
        self.assertEqual(confirmations[0]["details"]["affected_files"], ["local.txt"])  # type: ignore[index]

    def test_manual_commit_availability_fetches_exact_commit_without_recursing_submodules(self) -> None:
        from manual_merge_split.repository_ops import ManualRepositoryOps

        repo = Path("repo/sub")
        calls: list[tuple[str, ...]] = []
        exact_fetch_seen = False

        def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
            nonlocal exact_fetch_seen
            calls.append(tuple(args))
            if args == ["rev-parse", "--verify", "missing-commit^{commit}"]:
                if exact_fetch_seen:
                    return FakeManualGitResult(args, stdout="missing-commit\n")
                return FakeManualGitResult(args, stderr="fatal: Needed a single revision\n", returncode=128)
            if args == ["remote"]:
                return FakeManualGitResult(args, stdout="origin\n")
            if args == ["fetch", "--no-recurse-submodules", "origin", "missing-commit"]:
                exact_fetch_seen = True
                return FakeManualGitResult(args)
            return FakeManualGitResult(args)

        module = types.SimpleNamespace(
            run_git=run_git,
            MergeAutomationError=manual_models.MergeAutomationError,
            VERBOSE=False,
        )
        ops = ManualRepositoryOps(module)  # type: ignore[arg-type]

        self.assertTrue(ops.ensure_commit_available(repo, "missing-commit"))
        self.assertIn(("fetch", "--no-recurse-submodules", "origin", "missing-commit"), calls)
        self.assertNotIn(("fetch", "origin"), calls)
        self.assertNotIn(("fetch", "--no-recurse-submodules", "origin"), calls)

    def test_manual_merge_refuses_to_reset_existing_work_branch_with_local_commits(self) -> None:
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
                if args == ["status", "--porcelain"]:
                    return FakeManualGitResult(args)
                if args == ["remote"]:
                    return FakeManualGitResult(args, stdout="origin\n")
                if args == ["rev-parse", "--verify", "refs/heads/work^{commit}"]:
                    return FakeManualGitResult(args, stdout="local-only-commit\n")
                if args == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
                    return FakeManualGitResult(args, stdout="base-commit\n")
                return FakeManualGitResult(args)

            module = types.SimpleNamespace(
                run_git=run_git,
                run_git_binary=lambda _repo_path, args, **_kwargs: FakeManualBinaryResult(args, b""),
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            service = ManualCommandService(module)  # type: ignore[arg-type]

            result = service.run_merge(
                types.SimpleNamespace(
                    repo=str(repo),
                    base_remote_branch="origin/main",
                    merge_branches=["origin/feature"],
                    continue_current_branch=False,
                    work_branch="work",
                    clone_branch=None,
                    clone_url=None,
                    clone_path=None,
                    report_file="merge_manual_todo.txt",
                    verbose=False,
                    ignore_root_untracked_path=[],
                    allow_dirty_submodules=False,
                    no_prompt_dirty_submodules=True,
                    file_conflict_mode="tool",
                    conflict_tool_command="tool",
                    conflict_tool_timeout_seconds=1,
                    no_confirm_after_conflict_tool=False,
                    enable_cross_submodule_moves=False,
                )
            )

            self.assertEqual(result, 1)
            self.assertNotIn(("checkout", "-B", "work", "refs/remotes/origin/main"), calls)

    def test_manual_cleanup_rebuild_refuses_to_reset_existing_work_branch_with_local_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
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
                    return FakeManualGitResult(args)
                if args == ["remote"]:
                    return FakeManualGitResult(args, stdout="origin\n")
                if args == ["rev-parse", "--verify", "refs/heads/work^{commit}"]:
                    return FakeManualGitResult(args, stdout="local-only-commit\n")
                if args == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
                    return FakeManualGitResult(args, stdout="base-commit\n")
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
                    skip_submodule_update=True,
                    rebuild_work_branch=True,
                    base_remote_branch="origin/main",
                    work_branch="work",
                    verbose=False,
                )
            )

            self.assertEqual(result, 1)
            self.assertNotIn(("checkout", "-B", "work", "refs/remotes/origin/main"), calls)

    def test_manual_merge_updates_submodules_after_root_checkout_before_clean_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []
            checked_out_target = False
            updated_after_checkout = False

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                nonlocal checked_out_target, updated_after_checkout
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
                if args == ["status", "--porcelain"]:
                    return FakeManualGitResult(args, stdout=(" M sub\n" if checked_out_target and not updated_after_checkout else ""))
                if args == ["remote"]:
                    return FakeManualGitResult(args, stdout="origin\n")
                if args == ["checkout", "-B", "work", "refs/remotes/origin/main"]:
                    checked_out_target = True
                    return FakeManualGitResult(args)
                if args == ["submodule", "update", "--init", "--recursive"]:
                    if checked_out_target:
                        updated_after_checkout = True
                    return FakeManualGitResult(args)
                if args == ["rev-parse", "HEAD"]:
                    return FakeManualGitResult(args, stdout="headsha\n")
                if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                    return FakeManualGitResult(args, stdout="work\n")
                if args == ["merge", "--no-ff", "--no-commit", "refs/remotes/origin/feature"]:
                    return FakeManualGitResult(args)
                if args == ["diff", "--name-only", "--diff-filter=U"]:
                    return FakeManualGitResult(args)
                return FakeManualGitResult(args)

            module = types.SimpleNamespace(
                run_git=run_git,
                run_git_binary=lambda _repo_path, args, **_kwargs: FakeManualBinaryResult(args, b""),
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            service = ManualCommandService(module)  # type: ignore[arg-type]

            result = service.run_merge(
                types.SimpleNamespace(
                    repo=str(repo),
                    base_remote_branch="origin/main",
                    merge_branches=["origin/feature"],
                    continue_current_branch=False,
                    work_branch="work",
                    clone_branch=None,
                    clone_url=None,
                    clone_path=None,
                    report_file="merge_manual_todo.txt",
                    verbose=False,
                    ignore_root_untracked_path=[],
                    allow_dirty_submodules=False,
                    no_prompt_dirty_submodules=True,
                    file_conflict_mode="tool",
                    conflict_tool_command="tool",
                    conflict_tool_timeout_seconds=1,
                    no_confirm_after_conflict_tool=False,
                    enable_cross_submodule_moves=False,
                )
            )

            self.assertEqual(result, 0)
            checkout_index = calls.index(("checkout", "-B", "work", "refs/remotes/origin/main"))
            update_index = calls.index(("submodule", "update", "--init", "--recursive"))
            clean_check_index = calls.index(("status", "--porcelain"))
            self.assertLess(checkout_index, update_index)
            self.assertLess(update_index, clean_check_index)

    def test_manual_process_repository_file_conflict_is_local(self) -> None:
        from manual_merge_split.cross_submodule_moves import CrossSubmoduleMoveService
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
                if args == ["merge", "--no-ff", "--no-commit", "feature"]:
                    return FakeManualGitResult(args)
                if args == ["ls-files", "-u"]:
                    return FakeManualGitResult(
                        args,
                        stdout=(
                            "100644 base 1\tfile.txt\n"
                            "100644 ours 2\tfile.txt\n"
                            "100644 theirs 3\tfile.txt\n"
                        ),
                    )
                if args == ["diff", "--name-only", "--diff-filter=U"]:
                    return FakeManualGitResult(args, stdout="file.txt\n")
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
                file_conflict_mode="tool",
                conflict_tool_command="tool",
                conflict_tool_timeout_seconds=1,
            )

            class FakeFileConflictService:
                def resolve_moved_away_file_conflicts(self, *_args: object) -> list[str]:
                    return []

                def resolve_file_conflicts(
                    self,
                    _repo_path: Path,
                    file_conflicts: list[str],
                    _context: manual_models.MergeContext,
                ) -> tuple[list[str], str, bool]:
                    return file_conflicts, "manual file conflict requires hand merge", False

                def record_file_conflicts(
                    self,
                    repo_path: Path,
                    file_conflicts: list[str],
                    merge_context: manual_models.MergeContext,
                    *,
                    reason: str,
                    category: str = "manual_conflict",
                ) -> None:
                    record_file_conflicts(repo_path, file_conflicts, merge_context, reason=reason, category=category)

            completed = process_repository(
                repo,
                context,
                root_base_remote_branch=None,
                is_root=True,
                prepare_branch=False,
                ops=ManualRepositoryOps(module),  # type: ignore[arg-type]
                file_conflicts_service=FakeFileConflictService(),  # type: ignore[arg-type]
                cross_submodule_moves_service=CrossSubmoduleMoveService(module),  # type: ignore[arg-type]
            )

            self.assertFalse(completed)
            self.assertEqual(context.report_entries[0].files, ["file.txt"])
            self.assertIn(("merge", "--no-ff", "--no-commit", "feature"), calls)

            report_path = repo / "merge_manual_todo.txt"
            write_report(report_path, context)
            self.assertIn("file.txt", report_path.read_text(encoding="utf-8"))

    def test_manual_repository_ops_classifies_incomplete_stage_conflicts(self) -> None:
        from manual_merge_split.repository_ops import ManualRepositoryOps

        def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
            if args == ["ls-files", "-u"]:
                return FakeManualGitResult(
                    args,
                    stdout=(
                        "100644 base 1\tcomplete-file.txt\n"
                        "100644 ours 2\tcomplete-file.txt\n"
                        "100644 theirs 3\tcomplete-file.txt\n"
                        "160000 base 1\tcomplete-submodule\n"
                        "160000 ours 2\tcomplete-submodule\n"
                        "160000 theirs 3\tcomplete-submodule\n"
                        "100644 base 1\tdeleted-by-them.txt\n"
                        "100644 ours 2\tdeleted-by-them.txt\n"
                        "160000 base 1\tdeleted-submodule\n"
                        "160000 theirs 3\tdeleted-submodule\n"
                    ),
                )
            return FakeManualGitResult(args)

        module = types.SimpleNamespace(
            run_git=run_git,
            MergeAutomationError=manual_models.MergeAutomationError,
            VERBOSE=False,
        )
        ops = ManualRepositoryOps(module)  # type: ignore[arg-type]

        submodule_conflicts, file_conflicts, incomplete = ops.split_conflicts_by_stage(Path("repo"))

        self.assertEqual(submodule_conflicts, ["complete-submodule"])
        self.assertEqual(file_conflicts, ["complete-file.txt"])
        self.assertEqual([item.path for item in incomplete], ["deleted-by-them.txt", "deleted-submodule"])
        self.assertFalse(incomplete[0].is_gitlink)
        self.assertTrue(incomplete[1].is_gitlink)
        self.assertEqual(incomplete[0].stages, (1, 2))
        self.assertEqual(incomplete[1].missing_stages, (2,))

    def test_manual_process_repository_stops_on_incomplete_stage_conflicts(self) -> None:
        from manual_merge_split.cross_submodule_moves import CrossSubmoduleMoveService
        from manual_merge_split.file_conflicts import ManualFileConflictService
        from manual_merge_split.repository_ops import ManualRepositoryOps

        cases = [
            (
                "file.txt",
                "100644 base 1\tfile.txt\n100644 ours 2\tfile.txt\n",
                "file",
                "missing=3",
            ),
            (
                "libs/sub",
                "160000 base 1\tlibs/sub\n160000 theirs 3\tlibs/sub\n",
                "gitlink",
                "missing=2",
            ),
        ]

        for path, unmerged_index, expected_type, expected_missing in cases:
            with self.subTest(path=path):
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
                        if args == ["merge", "--no-ff", "--no-commit", "feature"]:
                            return FakeManualGitResult(args)
                        if args == ["ls-files", "-u"]:
                            return FakeManualGitResult(args, stdout=unmerged_index)
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
                        file_conflict_mode="tool",
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
                    self.assertEqual(context.report_entries[0].category, "incomplete_stage_conflict")
                    self.assertEqual(context.report_entries[0].files, [path])
                    self.assertIn(expected_type, context.report_entries[0].reason)
                    self.assertIn(expected_missing, context.report_entries[0].reason)
                    self.assertNotIn(("diff", "--name-only", "--diff-filter=U"), calls)
                    self.assertNotIn(("ls-files", "-u", "--", path), calls)

    def test_conflict_tool_command_finds_or_explains_vscode_cli(self) -> None:
        with mock.patch("manual_merge_split.file_conflicts.os.name", "nt"):
            self.assertIn(Path("D:/vscode/bin/code.cmd"), vscode_command_candidates())

        with mock.patch("manual_merge_split.file_conflicts.shutil.which", return_value=r"C:\bin\code.cmd"):
            prepared = prepare_conflict_tool_command('code --wait "C:\\repo\\file.txt"')
        self.assertEqual(prepared.command, 'code --wait "C:\\repo\\file.txt"')
        self.assertIsNone(prepared.error)

        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = Path(temp_dir) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
            candidate.parent.mkdir(parents=True)
            candidate.write_text("", encoding="utf-8")
            with mock.patch("manual_merge_split.file_conflicts.shutil.which", return_value=None):
                with mock.patch("manual_merge_split.file_conflicts.os.name", "nt"):
                    with mock.patch.dict(
                        os.environ,
                        {"LOCALAPPDATA": temp_dir, "ProgramFiles": "", "ProgramFiles(x86)": ""},
                    ):
                        prepared = prepare_conflict_tool_command('code --wait "C:\\repo\\file.txt"')
        self.assertIn(str(candidate), prepared.command)
        self.assertIn("not found in PATH", prepared.message or "")
        self.assertIsNone(prepared.error)

        with mock.patch("manual_merge_split.file_conflicts.shutil.which", return_value=None):
            with mock.patch("manual_merge_split.file_conflicts.vscode_command_candidates", return_value=[]):
                prepared = prepare_conflict_tool_command('code --wait "C:\\repo\\file.txt"')
        self.assertIn("VS Code command 'code' was not found in PATH", prepared.error or "")

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
            conflict_tool_command="tool --wait {file}",
            conflict_tool_timeout_seconds=1,
            confirm_after_conflict_tool=False,
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

        class TimeoutProcess:
            returncode = None

            def __init__(self) -> None:
                self.wait_calls = 0
                self.terminated = False

            def wait(self, timeout: int | None = None) -> None:
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("tool", timeout)
                return None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                return None

        timeout_process = TimeoutProcess()
        unresolved, reason, rollback = resolve_file_conflicts(
            Path("repo"),
            ["a.txt"],
            tool_context,
            run_git=run_git_for_tool,
            popen_factory=lambda *_args, **_kwargs: timeout_process,
        )
        self.assertEqual(unresolved, ["a.txt"])
        self.assertIn("leaving the working tree unchanged", reason)
        self.assertFalse(rollback)
        self.assertTrue(timeout_process.terminated)

        confirming_context = manual_models.MergeContext(
            work_branch="work",
            merge_branch="merge",
            file_conflict_mode="tool",
            conflict_tool_command="tool --wait {file}",
            conflict_tool_timeout_seconds=1,
        )
        prompted: list[str] = []
        reopen_count = 0

        def popen_for_confirmation(*_args: object, **_kwargs: object) -> FakeProcess:
            nonlocal reopen_count
            reopen_count += 1
            return FakeProcess()

        def confirm_input(prompt: str) -> str:
            prompted.append(prompt)
            return "r" if len(prompted) == 1 else ""

        unresolved, reason, rollback = resolve_file_conflicts(
            Path("repo"),
            ["a.txt"],
            confirming_context,
            run_git=run_git_for_tool,
            popen_factory=popen_for_confirmation,
            input_func=confirm_input,
            output_func=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(reason, "")
        self.assertFalse(rollback)
        self.assertEqual(reopen_count, 2)
        self.assertEqual(len(prompted), 2)

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
                if args == ["show-ref", "--verify", "--quiet", "refs/heads/main"]:
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

    def test_restore_snapshot_refuses_to_rewind_branch_with_new_commits(self) -> None:
        from manual_merge_split.repository_ops import ManualRepositoryOps

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo) + "\n")
                if args == ["show-ref", "--verify", "--quiet", "refs/heads/main"]:
                    return FakeManualGitResult(args, stdout="newsha refs/heads/main\n")
                if args == ["rev-parse", "--verify", "refs/heads/main^{commit}"]:
                    return FakeManualGitResult(args, stdout="newsha\n")
                if args == ["rev-parse", "--verify", "oldsha^{commit}"]:
                    return FakeManualGitResult(args, stdout="oldsha\n")
                return FakeManualGitResult(args)

            module = types.SimpleNamespace(
                run_git=run_git,
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            ops = ManualRepositoryOps(module)  # type: ignore[arg-type]
            snapshot = manual_models.RepoSnapshot(repo.resolve(), "oldsha", "main")

            with self.assertRaises(manual_models.MergeAutomationError):
                ops.restore_snapshot(repo, {repo.resolve(): snapshot}, is_root=False)

            self.assertNotIn(("reset", "--hard", "oldsha"), calls)

    def test_restore_snapshot_refuses_to_leave_detached_local_commit(self) -> None:
        from manual_merge_split.repository_ops import ManualRepositoryOps

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo) + "\n")
                if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                    return FakeManualGitResult(args, stdout="detached-local\n")
                if args == ["rev-parse", "--verify", "oldsha^{commit}"]:
                    return FakeManualGitResult(args, stdout="oldsha\n")
                return FakeManualGitResult(args, returncode=1)

            module = types.SimpleNamespace(
                run_git=run_git,
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            ops = ManualRepositoryOps(module)  # type: ignore[arg-type]
            snapshot = manual_models.RepoSnapshot(repo.resolve(), "oldsha", None)

            with self.assertRaises(manual_models.MergeAutomationError):
                ops.restore_snapshot(repo, {repo.resolve(): snapshot}, is_root=False)

            self.assertNotIn(("checkout", "--detach", "oldsha"), calls)

    def test_restore_snapshot_detached_head_continues_after_risk_confirmation(self) -> None:
        from manual_merge_split.repository_ops import ManualRepositoryOps

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            calls: list[tuple[str, ...]] = []
            confirmations: list[dict[str, object]] = []

            def run_git(_repo_path: Path, args: list[str], **_kwargs: object) -> FakeManualGitResult:
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return FakeManualGitResult(args, stdout=str(repo) + "\n")
                if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                    return FakeManualGitResult(args, returncode=1)
                if args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                    return FakeManualGitResult(args, stdout="detached-local\n")
                if args == ["rev-parse", "--verify", "oldsha^{commit}"]:
                    return FakeManualGitResult(args, stdout="oldsha\n")
                if args == ["diff", "--name-only", "oldsha", "detached-local", "--"]:
                    return FakeManualGitResult(args, stdout="detached.txt\n")
                if args == ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"]:
                    return FakeManualGitResult(args, returncode=1)
                return FakeManualGitResult(args)

            def confirm_risky_operation(**kwargs: object) -> bool:
                confirmations.append(dict(kwargs))
                return True

            module = types.SimpleNamespace(
                run_git=run_git,
                confirm_risky_operation=confirm_risky_operation,
                MergeAutomationError=manual_models.MergeAutomationError,
                VERBOSE=False,
            )
            ops = ManualRepositoryOps(module)  # type: ignore[arg-type]
            snapshot = manual_models.RepoSnapshot(repo.resolve(), "oldsha", None)

            ops.restore_snapshot(repo, {repo.resolve(): snapshot}, is_root=False)

            self.assertIn(("checkout", "--detach", "oldsha"), calls)
            self.assertEqual(confirmations[0]["reason"], "DETACHED_HEAD_CHECKOUT_RISK")
            self.assertEqual(confirmations[0]["details"]["affected_files"], ["detached.txt"])  # type: ignore[index]

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
