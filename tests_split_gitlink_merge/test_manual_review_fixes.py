"""Regression tests for the manual-workflow review findings.

Every test here drives real ``git`` repositories: the bugs they cover were all
invisible to the mock-based suites because they live in the exact place where the
tool hands a string to git.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from manual_merge_split import common, execution  # noqa: E402
from manual_merge_split.file_conflicts import ManualFileConflictService  # noqa: E402
from manual_merge_split.models import (  # noqa: E402
    CleanupResult,
    GitResult,
    ManualConflictDisposition,
    MergeAutomationError,
)
from manual_merge_split.preflight import ManualPreflightService  # noqa: E402
from manual_merge_split.repository_ops import ManualRepositoryOps  # noqa: E402
from manual_merge_split.commands import run_cleanup  # noqa: E402
from manual_merge_split.session_models import (  # noqa: E402
    MergeSession,
    RepositoryFrame,
    SessionPhase,
    SessionStatus,
)
from manual_merge_split.session_store import (  # noqa: E402
    MergeSessionStore,
    SessionLockError,
    process_is_alive,
)
from manual_merge_split.session_workflow import ManualMergeSessionService  # noqa: E402
from merge_cleanup_core import steps as cleanup_steps  # noqa: E402


GIT_AVAILABLE = shutil.which("git") is not None


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {repo}: {completed.stderr}")
    return completed


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Review Fixture")
    git(path, "config", "user.email", "review@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    return path


def build_conflicting_repo(base: Path, name: str, conflict_file: str = "conflict.txt") -> Path:
    repo = init_repo(base / name)
    target = repo / conflict_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feature")
    target.write_text("theirs\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "theirs")
    git(repo, "checkout", "-q", "main")
    target.write_text("ours\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "ours")
    return repo


def merge_args(repo: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "repo": str(repo),
        "base_remote_branch": "",
        "use_current_branch": True,
        "continue_current_branch": False,
        "work_branch": None,
        "merge_branch": "feature",
        "ignore_root_untracked_path": [],
        "conflict_tool_command": "",
        "conflict_tool_timeout_seconds": 0,
        "no_confirm_after_conflict_tool": True,
        "report_file": "merge_manual_todo.txt",
        "clone_branch": None,
        "clone_url": None,
        "clone_path": None,
        "yes": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def build_service() -> tuple[ManualRepositoryOps, ManualMergeSessionService]:
    ops = ManualRepositoryOps()
    return ops, ManualMergeSessionService(
        ops=ops,
        preflight_service=ManualPreflightService(),
        file_conflicts_service=ManualFileConflictService(),
    )


class UnquoteGitPathTests(unittest.TestCase):
    def test_plain_paths_are_untouched(self) -> None:
        self.assertEqual(common.unquote_git_path("src/app.py"), "src/app.py")
        self.assertEqual(common.unquote_git_path("dir with space/a.txt"), "dir with space/a.txt")

    def test_octal_escapes_decode_to_utf8(self) -> None:
        quoted = '"\\344\\270\\255\\346\\226\\207/\\345\\206\\262\\347\\252\\201.txt"'
        self.assertEqual(common.unquote_git_path(quoted), "中文/冲突.txt")

    def test_simple_escapes_decode(self) -> None:
        self.assertEqual(common.unquote_git_path('"a\\"b.txt"'), 'a"b.txt')
        self.assertEqual(common.unquote_git_path('"a\\\\b.txt"'), "a\\b.txt")
        self.assertEqual(common.unquote_git_path('"a\\tb.txt"'), "a\tb.txt")

    def test_unterminated_quote_is_returned_unchanged(self) -> None:
        self.assertEqual(common.unquote_git_path('"broken'), '"broken')

    def test_normalize_status_path_decodes_before_separator_rewrite(self) -> None:
        quoted = '"\\344\\270\\255\\346\\226\\207/a.txt"'
        self.assertEqual(common.normalize_status_relative_path(quoted), "中文/a.txt")


class GitTimeoutResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.pop(execution.NETWORK_TIMEOUT_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(execution.NETWORK_TIMEOUT_ENV, None)
        if self._saved is not None:
            os.environ[execution.NETWORK_TIMEOUT_ENV] = self._saved

    def test_local_commands_have_no_timeout(self) -> None:
        self.assertIsNone(execution.resolve_git_timeout(["status", "--porcelain"], None))

    def test_network_commands_get_the_default_backstop(self) -> None:
        self.assertEqual(
            execution.resolve_git_timeout(["push", "origin", "main"], None),
            float(execution.DEFAULT_NETWORK_TIMEOUT_SECONDS),
        )

    def test_leading_config_flags_do_not_hide_the_subcommand(self) -> None:
        args = ["-c", "protocol.file.allow=always", "clone", "--recurse-submodules", "url", "dest"]
        self.assertEqual(
            execution.resolve_git_timeout(args, None),
            float(execution.DEFAULT_NETWORK_TIMEOUT_SECONDS),
        )

    def test_environment_can_disable_the_backstop(self) -> None:
        os.environ[execution.NETWORK_TIMEOUT_ENV] = "0"
        self.assertIsNone(execution.resolve_git_timeout(["fetch"], None))

    def test_explicit_timeout_wins(self) -> None:
        self.assertEqual(execution.resolve_git_timeout(["status"], 12), 12)

    def test_credential_prompts_are_disabled_by_default(self) -> None:
        env = execution.git_env({})
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_MERGE_AUTOEDIT"], "no")

    def test_caller_can_opt_back_into_prompts(self) -> None:
        env = execution.git_env({"GIT_TERMINAL_PROMPT": "1"})
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "1")

    def test_env_overrides_win_over_defaults(self) -> None:
        env = execution.git_env({}, overrides={"LC_ALL": "C", "LANGUAGE": "C"})
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["LANGUAGE"], "C")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")


class CleanupStepCwdTests(unittest.TestCase):
    def test_relative_step_cwd_resolves_against_the_repository(self) -> None:
        step = cleanup_steps.reset_hard_step(cwd="sub")
        self.assertEqual(
            cleanup_steps.step_cwd(step, Path("/repo")),
            Path("/repo") / "sub",
        )

    def test_absolute_step_cwd_is_preserved(self) -> None:
        absolute = Path(tempfile.gettempdir()) / "elsewhere"
        step = cleanup_steps.reset_hard_step(cwd=absolute)
        self.assertEqual(cleanup_steps.step_cwd(step, Path("/repo")), absolute)

    def test_run_cleanup_step_executes_in_the_step_directory(self) -> None:
        recorded: list[tuple[Path, list[str]]] = []

        class RecordingOps(ManualRepositoryOps):
            def run_git(self, repo_path: Path, args: list[str], **kwargs: object) -> object:
                recorded.append((repo_path, list(args)))
                return argparse.Namespace(returncode=0, stdout="", stderr="")

        ops = RecordingOps()
        ops.run_cleanup_step(Path("/repo"), cleanup_steps.clean_step(False, cwd=Path("/repo/sub")))
        self.assertEqual(recorded, [(Path("/repo/sub"), ["clean", "-fd"])])


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class StagedConflictMarkerDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.ops = ManualRepositoryOps()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_staged_markers_are_reported(self) -> None:
        repo = build_conflicting_repo(self.base, "markers")
        git(repo, "merge", "--no-ff", "--no-commit", "feature", check=False)
        (repo / "conflict.txt").write_text(
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\n",
            encoding="utf-8",
        )
        git(repo, "add", "conflict.txt")
        self.assertEqual(self.ops.find_staged_conflict_marker_paths(repo), ["conflict.txt"])

    def test_resolved_content_is_not_reported(self) -> None:
        repo = build_conflicting_repo(self.base, "resolved")
        git(repo, "merge", "--no-ff", "--no-commit", "feature", check=False)
        (repo / "conflict.txt").write_text("merged\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        self.assertEqual(self.ops.find_staged_conflict_marker_paths(repo), [])

    def test_untouched_files_mentioning_markers_are_ignored(self) -> None:
        repo = build_conflicting_repo(self.base, "fixture")
        fixture = repo / "fixture.txt"
        fixture.write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> other\n", encoding="utf-8")
        git(repo, "add", "fixture.txt")
        git(repo, "commit", "-q", "-m", "committed fixture with markers")
        git(repo, "merge", "--no-ff", "--no-commit", "feature", check=False)
        (repo / "conflict.txt").write_text("merged\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        self.assertEqual(self.ops.find_staged_conflict_marker_paths(repo), [])

    def test_half_marker_alone_is_not_enough(self) -> None:
        repo = build_conflicting_repo(self.base, "half")
        git(repo, "merge", "--no-ff", "--no-commit", "feature", check=False)
        (repo / "conflict.txt").write_text("<<<<<<< HEAD\nours\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")
        self.assertEqual(self.ops.find_staged_conflict_marker_paths(repo), [])

    def test_typechange_staged_file_with_markers_is_reported(self) -> None:
        repo = init_repo(self.base / "typechange")
        git(repo, "config", "core.symlinks", "true")
        body = repo / "_symlink_body"
        body.write_bytes(b"target.txt")
        symlink_blob = git(repo, "hash-object", "-w", str(body)).stdout.strip()
        body.unlink()
        git(repo, "update-index", "--add", "--cacheinfo", f"120000,{symlink_blob},link")
        git(repo, "commit", "-q", "-m", "symlink at HEAD")
        head_mode = git(repo, "ls-tree", "HEAD", "link").stdout.split()[0]
        self.assertEqual(head_mode, "120000")

        (repo / "link").write_text(
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n",
            encoding="utf-8",
        )
        file_blob = git(repo, "hash-object", "-w", str(repo / "link")).stdout.strip()
        git(repo, "update-index", "--cacheinfo", f"100644,{file_blob},link")
        status = git(repo, "diff", "--cached", "--name-status").stdout.strip()
        self.assertTrue(status.startswith("T"), status)
        self.assertEqual(self.ops.find_staged_conflict_marker_paths(repo), ["link"])


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class ResumeConflictMarkerGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_resume_refuses_to_commit_staged_markers(self) -> None:
        repo = build_conflicting_repo(self.base, "resume-markers")
        ops, service = build_service()
        self.assertEqual(service.start_merge(merge_args(repo)), 2)

        (repo / "conflict.txt").write_text(
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\n",
            encoding="utf-8",
        )
        git(repo, "add", "conflict.txt")

        self.assertEqual(service.resume(merge_args(repo)), 2)
        self.assertTrue(ops.merge_in_progress(repo), "the merge must stay open for another attempt")

        session = service.store.load(repo)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.status, SessionStatus.PAUSED_MANUAL)
        self.assertEqual(
            [record.path for record in session.paused_conflicts],
            ["conflict.txt"],
        )

        report = (repo / "merge_manual_todo.txt").read_text(encoding="utf-8")
        self.assertIn("conflict.txt", report)

    def test_resume_commits_a_genuinely_resolved_conflict(self) -> None:
        repo = build_conflicting_repo(self.base, "resume-clean")
        ops, service = build_service()
        self.assertEqual(service.start_merge(merge_args(repo)), 2)

        (repo / "conflict.txt").write_text("merged by hand\n", encoding="utf-8")
        git(repo, "add", "conflict.txt")

        self.assertEqual(service.resume(merge_args(repo)), 0)
        self.assertFalse(ops.merge_in_progress(repo))
        committed = git(repo, "show", "HEAD:conflict.txt").stdout
        self.assertEqual(committed, "merged by hand\n")
        self.assertIsNone(service.store.load(repo))

    def test_pause_diagnostics_survive_a_failed_resume(self) -> None:
        repo = build_conflicting_repo(self.base, "resume-diagnostics")
        _, service = build_service()
        self.assertEqual(service.start_merge(merge_args(repo)), 2)
        paused = service.store.load(repo)
        self.assertIsNotNone(paused)
        assert paused is not None
        self.assertTrue(paused.paused_conflicts)

        # Resume from a branch that no longer matches the session.
        git(repo, "checkout", "-q", "-b", "unexpected")
        self.assertEqual(service.resume(merge_args(repo)), 1)

        after = service.store.load(repo)
        self.assertIsNotNone(after)
        assert after is not None
        self.assertEqual(
            [record.path for record in after.paused_conflicts],
            [record.path for record in paused.paused_conflicts],
        )


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class NonAsciiConflictPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.ops = ManualRepositoryOps()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_cjk_conflict_path_round_trips_through_git(self) -> None:
        repo = build_conflicting_repo(self.base, "cjk", conflict_file="中文/冲突.txt")
        git(repo, "merge", "--no-ff", "--no-commit", "feature", check=False)

        unmerged = self.ops.get_unmerged_paths(repo)
        self.assertEqual(unmerged, ["中文/冲突.txt"])

        classified = self.ops.classify_unmerged_conflicts(repo)
        self.assertIn("中文/冲突.txt", classified)
        self.assertEqual(
            classified["中文/冲突.txt"].disposition,
            ManualConflictDisposition.SUPPORTED_FILE,
        )

        resolved = repo / "中文/冲突.txt"
        self.assertTrue(resolved.exists(), "the reported path must exist on disk")

        resolved.write_text("merged\n", encoding="utf-8")
        self.ops.stage_path(repo, "中文/冲突.txt")
        self.assertEqual(self.ops.get_unmerged_paths(repo), [])


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class DeepSubmoduleCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.ops = ManualRepositoryOps()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _build_superproject(self) -> tuple[Path, Path]:
        child_origin = init_repo(self.base / "child-origin")
        (child_origin / "child.txt").write_text("child\n", encoding="utf-8")
        git(child_origin, "add", "-A")
        git(child_origin, "commit", "-q", "-m", "child base")

        root = init_repo(self.base / "root")
        (root / "root.txt").write_text("root\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "root base")
        git(
            root,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(child_origin).replace("\\", "/"),
            "sub",
        )
        git(root, "commit", "-q", "-m", "add submodule")
        return root, root / "sub"

    def test_level_four_deep_steps_target_the_submodule(self) -> None:
        root, sub = self._build_superproject()
        levels = self.ops._build_cleanup_levels(
            root, self.ops.initialized_submodule_paths(root), False, True, True,
        )
        level_four = dict(levels).get("level_4_deep_submodule_cleanup")
        self.assertIsNotNone(level_four)
        assert level_four is not None
        deep_cwds = {
            cleanup_steps.step_cwd(step, root)
            for step in level_four
            if step.cwd is not None
        }
        self.assertEqual(deep_cwds, {sub})

    def test_uninitialized_submodules_are_not_deep_cleaned(self) -> None:
        root, sub = self._build_superproject()
        self.assertEqual(self.ops.initialized_submodule_paths(root), ["sub"])
        shutil.rmtree(sub)
        sub.mkdir()
        self.assertEqual(self.ops.initialized_submodule_paths(root), [])

    def test_cleanup_restores_a_dirty_submodule_without_touching_the_root(self) -> None:
        root, sub = self._build_superproject()
        root_head = git(root, "rev-parse", "HEAD").stdout.strip()
        (sub / "child.txt").write_text("dirty\n", encoding="utf-8")
        (sub / "stray.txt").write_text("stray\n", encoding="utf-8")

        self.ops.cleanup_repository(root, CleanupResult(), skip_submodule_update=True)

        self.assertEqual((sub / "child.txt").read_text(encoding="utf-8"), "child\n")
        self.assertFalse((sub / "stray.txt").exists())
        self.assertEqual(git(root, "rev-parse", "HEAD").stdout.strip(), root_head)
        self.assertEqual(git(root, "status", "--porcelain").stdout.strip(), "")


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class CommitGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.ops = ManualRepositoryOps()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_commit_if_needed_refuses_while_paths_are_unmerged(self) -> None:
        repo = build_conflicting_repo(self.base, "unmerged")
        git(repo, "merge", "--no-ff", "--no-commit", "feature", check=False)
        self.assertTrue(self.ops.get_unmerged_paths(repo))
        head_before = git(repo, "rev-parse", "HEAD").stdout.strip()

        with self.assertRaises(MergeAutomationError) as caught:
            self.ops.commit_if_needed(repo, fallback_message="should not happen")

        self.assertIn("still unmerged", str(caught.exception))
        self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), head_before)

    def test_marker_detection_does_not_depend_on_git_language(self) -> None:
        repo = build_conflicting_repo(self.base, "localized")
        git(repo, "merge", "--no-ff", "--no-commit", "feature", check=False)
        (repo / "conflict.txt").write_text(
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\n",
            encoding="utf-8",
        )
        git(repo, "add", "conflict.txt")

        previous = os.environ.get("LC_ALL")
        os.environ["LC_ALL"] = "zh_CN.UTF-8"
        try:
            marked = common.indexed_paths_with_conflict_markers(
                repo,
                run_git=execution.run_git,
                pathspec=["conflict.txt"],
            )
        finally:
            if previous is None:
                os.environ.pop("LC_ALL", None)
            else:
                os.environ["LC_ALL"] = previous
        self.assertEqual(marked, {"conflict.txt"})


class StaleSessionLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.repo = Path(self._temp.name)
        self.store = MergeSessionStore(git_path_resolver=lambda repo, _name: repo / ".git")
        self.lock_path = self.store.command_lock_path(self.repo)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _dead_pid(self) -> int:
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait()
        return process.pid

    def test_a_lock_owned_by_a_dead_process_is_released(self) -> None:
        self.lock_path.write_text(f"{self._dead_pid()}\n", encoding="utf-8")
        with self.store.command_lock(self.repo):
            pass
        self.assertFalse(self.lock_path.exists())

    def test_a_lock_owned_by_this_process_is_respected(self) -> None:
        self.lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        with self.assertRaises(SessionLockError):
            with self.store.command_lock(self.repo):
                self.fail("a live lock owner must block the command")
        self.assertTrue(self.lock_path.exists())

    def test_a_lock_without_a_readable_pid_is_left_alone(self) -> None:
        self.lock_path.write_text("busy\n", encoding="utf-8")
        with self.assertRaises(SessionLockError):
            with self.store.command_lock(self.repo):
                self.fail("an unrecognised lock must not be broken")
        self.assertTrue(self.lock_path.exists())

    def test_this_process_is_reported_alive(self) -> None:
        self.assertTrue(process_is_alive(os.getpid()))

    def test_a_finished_process_is_reported_dead(self) -> None:
        self.assertFalse(process_is_alive(self._dead_pid()))

    @unittest.skipUnless(os.name == "nt", "OpenProcess ACCESS_DENIED is a Windows failure mode")
    def test_an_inaccessible_live_process_is_reported_alive(self) -> None:
        self.assertTrue(
            process_is_alive(4),
            "PID 4 is the Windows System process; ACCESS_DENIED must not look like death",
        )

    @unittest.skipUnless(os.name == "nt", "OpenProcess ACCESS_DENIED is a Windows failure mode")
    def test_a_lock_owned_by_an_inaccessible_live_process_is_respected(self) -> None:
        self.lock_path.write_text("4\n", encoding="utf-8")
        with self.assertRaises(SessionLockError):
            with self.store.command_lock(self.repo):
                self.fail("ACCESS_DENIED on a live owner must not break the lock")
        self.assertTrue(self.lock_path.exists())


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class CleanupSideEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.ops = ManualRepositoryOps()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _repo_with_untracked_report(self, name: str) -> Path:
        repo = init_repo(self.base / name)
        (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        (repo / "merge_manual_todo.txt").write_text("todo\n", encoding="utf-8")
        return repo

    def test_ignored_root_untracked_paths_are_not_counted_as_dirty(self) -> None:
        repo = self._repo_with_untracked_report("ignored-report")
        result = CleanupResult()

        self.ops.cleanup_repository(
            repo,
            result,
            skip_submodule_update=True,
            ignored_root_untracked_paths=("merge_manual_todo.txt",),
        )

        self.assertEqual(result.hard_resets, [])
        self.assertEqual(result.escalated_levels, [])

    def test_an_undeclared_untracked_file_still_counts_as_dirty(self) -> None:
        repo = self._repo_with_untracked_report("unlisted-report")
        result = CleanupResult()

        self.ops.cleanup_repository(repo, result, skip_submodule_update=True)

        self.assertEqual(result.hard_resets, [repo])

    def test_cleanup_still_removes_untracked_files(self) -> None:
        repo = self._repo_with_untracked_report("stray")
        stray = repo / "stray.txt"
        stray.write_text("stray\n", encoding="utf-8")

        self.ops.cleanup_repository(
            repo,
            CleanupResult(),
            skip_submodule_update=True,
            ignored_root_untracked_paths=("merge_manual_todo.txt",),
        )

        self.assertFalse(stray.exists())


class EngineFileConflictProgressTests(unittest.TestCase):
    def test_a_handler_that_makes_no_progress_raises_instead_of_looping(self) -> None:
        from split_gitlink_merge import (
            Conflict,
            GitlinkMergeError,
            MergePolicy,
            RecursiveGitlinkMergeEngine,
        )

        repo = Path(".").resolve(strict=False)
        conflict = Conflict(repo_path=repo, path="a.txt", kind="file", stages=())
        calls: list[int] = []

        class NoopPolicy(MergePolicy):
            def handle_file_conflicts(self, actions, repo_path, conflicts, depth) -> None:
                calls.append(depth)

        class StuckActions:
            def ensure_git_worktree(self, repo_path: Path) -> None:
                return None

            def read_conflicts(self, repo_path: Path) -> list[Conflict]:
                return [conflict]

        engine = RecursiveGitlinkMergeEngine(StuckActions(), policy=NoopPolicy())
        with self.assertRaises(GitlinkMergeError) as caught:
            engine.resolve_active_conflicts(Path("."))

        self.assertIn("made no progress", str(caught.exception))
        self.assertEqual(len(calls), 1, "the handler must be dispatched once per round")


class AbortRestoreScopeTests(unittest.TestCase):
    def test_root_and_touched_submodules_are_all_restored_deepest_first(self) -> None:
        from manual_merge_split.session_models import MergeSession, RepositoryFrame, SessionPhase

        def frame(path: str) -> RepositoryFrame:
            return RepositoryFrame(
                repo_path=path,
                display_path=path,
                depth=0,
                original_head="a" * 40,
                phase=SessionPhase.MERGE_PENDING,
            )

        root = str(Path("/repo"))
        child = str(Path("/repo/sub"))
        grandchild = str(Path("/repo/sub/nested"))
        session = MergeSession.new(
            root_repo_path=root,
            work_branch="work",
            target_ref="feature",
            base_ref="main",
            merge_sha="b" * 40,
            repo_stack=[frame(root)],
        )
        session = dataclasses.replace(
            session,
            touched_frames=(frame(child), frame(grandchild)),
        )

        restored = [
            item.repo_path
            for item in ManualMergeSessionService._frames_to_restore(session)
        ]
        self.assertEqual(restored, [grandchild, child, root])

    def test_pre_init_snapshot_wins_over_post_init_stack_frame(self) -> None:
        root = str(Path("/repo"))
        child = str(Path("/repo/sub"))
        stack_frame = RepositoryFrame(
            repo_path=child,
            display_path="sub",
            depth=1,
            parent_repo_path=root,
            parent_gitlink_path="sub",
            original_head="b" * 40,
            was_initialized=True,
            phase=SessionPhase.MERGE_PENDING,
        )
        touched_frame = RepositoryFrame(
            repo_path=child,
            display_path="sub",
            depth=1,
            parent_repo_path=root,
            parent_gitlink_path="sub",
            was_initialized=False,
            phase=SessionPhase.MERGE_PENDING,
        )
        session = MergeSession.new(
            root_repo_path=root,
            work_branch="work",
            target_ref="feature",
            repo_stack=[
                RepositoryFrame(repo_path=root, display_path=".", depth=0),
                stack_frame,
            ],
        )
        session = dataclasses.replace(session, touched_frames=(touched_frame,))
        restored = ManualMergeSessionService._frames_to_restore(session)
        child_frame = next(item for item in restored if item.repo_path == child)
        self.assertFalse(child_frame.was_initialized)


def cleanup_args(repo: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "repo": str(repo),
        "report_file": "merge_manual_todo.txt",
        "skip_submodule_update": True,
        "rebuild_work_branch": False,
        "base_remote_branch": "origin/main",
        "work_branch": None,
        "verbose": False,
        "yes": True,
        "force": False,
        "allow_clean_x": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class CleanupCommandLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.repo = init_repo(Path(self._temp.name) / "cleanup-lock")
        (self.repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        (self.repo / "stray.txt").write_text("stray\n", encoding="utf-8")
        self.ops = ManualRepositoryOps()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_cleanup_refuses_to_run_under_a_live_command_lock(self) -> None:
        store = MergeSessionStore(runner=lambda repo, cmd: self.ops.run_git(repo, cmd, check=False))
        with store.command_lock(self.repo):
            code = run_cleanup(cleanup_args(self.repo), manual_module=None, ops=self.ops)
        self.assertEqual(code, 1)
        self.assertTrue((self.repo / "stray.txt").exists(), "locked cleanup must not reset the tree")

    def test_unreadable_session_stops_cleanup_unless_forced(self) -> None:
        store = MergeSessionStore(runner=lambda repo, cmd: self.ops.run_git(repo, cmd, check=False))
        session_path = store.session_path(self.repo)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text("{not-json", encoding="utf-8")

        code = run_cleanup(cleanup_args(self.repo), manual_module=None, ops=self.ops)
        self.assertEqual(code, 1)
        self.assertTrue(session_path.exists())
        self.assertTrue((self.repo / "stray.txt").exists())

        code = run_cleanup(cleanup_args(self.repo, force=True), manual_module=None, ops=self.ops)
        self.assertEqual(code, 0)
        self.assertFalse(session_path.exists())
        self.assertFalse((self.repo / "stray.txt").exists())


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class AbortUninitializedSubmoduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_restore_deinits_a_submodule_that_was_not_initialized(self) -> None:
        sub_src = init_repo(self.base / "sub_src")
        (sub_src / "file.txt").write_text("sub\n", encoding="utf-8")
        git(sub_src, "add", "-A")
        git(sub_src, "commit", "-q", "-m", "sub base")

        parent = init_repo(self.base / "parent")
        git(parent, "config", "protocol.file.allow", "always")
        git(
            parent,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(sub_src).replace("\\", "/"),
            "sub",
        )
        git(parent, "commit", "-q", "-m", "add submodule")
        self.assertTrue((parent / "sub" / ".git").exists())

        ops, service = build_service()
        frame = RepositoryFrame(
            repo_path=str((parent / "sub").resolve(strict=False)),
            display_path="sub",
            depth=1,
            parent_repo_path=str(parent.resolve(strict=False)),
            parent_gitlink_path="sub",
            was_initialized=False,
            phase=SessionPhase.MERGE_PENDING,
        )
        service._restore_frame(frame)
        self.assertFalse((parent / "sub" / ".git").exists())

    def test_abort_restores_parent_before_deinit(self) -> None:
        sub_src = init_repo(self.base / "sub_src")
        (sub_src / "file.txt").write_text("sub\n", encoding="utf-8")
        git(sub_src, "add", "-A")
        git(sub_src, "commit", "-q", "-m", "sub base")

        parent = init_repo(self.base / "parent")
        git(parent, "config", "protocol.file.allow", "always")
        git(
            parent,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(sub_src).replace("\\", "/"),
            "sub",
        )
        git(parent, "commit", "-q", "-m", "add submodule")
        original_head = git(parent, "rev-parse", "HEAD").stdout.strip()
        (parent / ".gitmodules").write_text(
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n",
            encoding="utf-8",
        )
        git(parent, "add", ".gitmodules")

        class OrderOps(ManualRepositoryOps):
            def __init__(self) -> None:
                super().__init__()
                self.order: list[str] = []

            def reset_hard(self, repo_path: Path, sha: str) -> None:
                self.order.append("reset")
                super().reset_hard(repo_path, sha)

            def run_git(self, repo_path: Path, args: list[str], **kwargs: object) -> object:
                if list(args[:3]) == ["submodule", "deinit", "-f"]:
                    self.order.append("deinit")
                return super().run_git(repo_path, args, **kwargs)

        ops = OrderOps()
        service = ManualMergeSessionService(
            ops=ops,
            preflight_service=ManualPreflightService(),
            file_conflicts_service=ManualFileConflictService(),
        )
        root_frame = RepositoryFrame(
            repo_path=str(parent.resolve(strict=False)),
            display_path=".",
            depth=0,
            original_head=original_head,
            original_branch="main",
            phase=SessionPhase.MERGE_PENDING,
        )
        sub_frame = RepositoryFrame(
            repo_path=str((parent / "sub").resolve(strict=False)),
            display_path="sub",
            depth=1,
            parent_repo_path=str(parent.resolve(strict=False)),
            parent_gitlink_path="sub",
            was_initialized=False,
            phase=SessionPhase.MERGE_PENDING,
        )
        session = MergeSession.new(
            root_repo_path=str(parent.resolve(strict=False)),
            work_branch="main",
            target_ref="feature",
            repo_stack=[root_frame],
            touched_frames=[sub_frame],
        )
        service.store.save(parent, session)

        code = service.abort(argparse.Namespace(repo=str(parent), yes=True))

        self.assertEqual(code, 0)
        self.assertEqual(ops.order, ["reset", "deinit"])
        self.assertIsNone(service.store.load(parent))
        self.assertFalse((parent / "sub" / ".git").exists())
        self.assertNotIn("<<<<<<<", (parent / ".gitmodules").read_text(encoding="utf-8"))

    def test_failed_deinit_returns_failure_and_preserves_session(self) -> None:
        sub_src = init_repo(self.base / "sub_src")
        (sub_src / "file.txt").write_text("sub\n", encoding="utf-8")
        git(sub_src, "add", "-A")
        git(sub_src, "commit", "-q", "-m", "sub base")

        parent = init_repo(self.base / "parent")
        git(parent, "config", "protocol.file.allow", "always")
        git(
            parent,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(sub_src).replace("\\", "/"),
            "sub",
        )
        git(parent, "commit", "-q", "-m", "add submodule")

        class FailingDeinitOps(ManualRepositoryOps):
            def run_git(self, repo_path: Path, args: list[str], **kwargs: object) -> object:
                if args[:3] == ["submodule", "deinit", "-f"]:
                    return GitResult(
                        args=list(args),
                        cwd=repo_path,
                        returncode=128,
                        stdout="",
                        stderr="fatal: simulated deinit failure",
                    )
                return super().run_git(repo_path, args, **kwargs)

        ops = FailingDeinitOps()
        service = ManualMergeSessionService(
            ops=ops,
            preflight_service=ManualPreflightService(),
            file_conflicts_service=ManualFileConflictService(),
        )
        root_frame = RepositoryFrame(
            repo_path=str(parent.resolve(strict=False)),
            display_path=".",
            depth=0,
            original_head=git(parent, "rev-parse", "HEAD").stdout.strip(),
            original_branch="main",
            phase=SessionPhase.MERGE_PENDING,
        )
        sub_frame = RepositoryFrame(
            repo_path=str((parent / "sub").resolve(strict=False)),
            display_path="sub",
            depth=1,
            parent_repo_path=str(parent.resolve(strict=False)),
            parent_gitlink_path="sub",
            was_initialized=False,
            phase=SessionPhase.MERGE_PENDING,
        )
        session = MergeSession.new(
            root_repo_path=str(parent.resolve(strict=False)),
            work_branch="main",
            target_ref="feature",
            repo_stack=[root_frame],
            touched_frames=[sub_frame],
        )
        service.store.save(parent, session)

        code = service.abort(argparse.Namespace(repo=str(parent), yes=True))

        self.assertEqual(code, 1)
        preserved = service.store.load(parent)
        self.assertIsNotNone(preserved)
        self.assertEqual(preserved.phase, SessionPhase.ABORTING)
        self.assertTrue((parent / "sub" / ".git").exists())


class PublishCheckedPushLocaleTests(unittest.TestCase):
    def test_checked_push_pins_c_locale(self) -> None:
        from manual_merge_split.publish import PublishPlanItem, _run_checked_push

        recorded: dict[str, object] = {}

        class RecordingOps:
            def _verbose(self) -> bool:
                return False

            def run_git(self, repo_path: Path, args: list[str], **kwargs: object) -> argparse.Namespace:
                recorded["kwargs"] = kwargs
                recorded["args"] = list(args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")

        item = PublishPlanItem(
            repo_path=Path("/repo"),
            display_path=".",
            desired_sha="a" * 40,
            depth=0,
        )
        _run_checked_push(item, "origin", "publish", RecordingOps())  # type: ignore[arg-type]
        self.assertEqual(
            recorded["kwargs"],
            {
                "check": False,
                "stream_output": False,
                "env_overrides": {"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"},
            },
        )


if __name__ == "__main__":
    unittest.main()
