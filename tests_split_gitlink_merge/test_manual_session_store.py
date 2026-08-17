from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from manual_merge_split.session_models import (
    MergeSession,
    RepositoryFrame,
    SessionOutcome,
    SessionPhase,
    SessionStatus,
)
from manual_merge_split.session_store import (
    MergeSessionStore,
    SessionLockError,
    SessionRevisionError,
    SessionSchemaError,
)


class FakeGitResult:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class ManualSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="manual-session-store-")
        self.base = Path(self.temp_dir.name)
        self.repo = self.base / "repo"
        self.git_dir = self.repo / ".git"
        self.git_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _resolver(self, repo_path: Path, flag: str) -> str:
        self.assertEqual(repo_path, self.repo.resolve(strict=False))
        self.assertEqual(flag, "--git-dir")
        return ".git"

    def _new_session(self) -> MergeSession:
        return MergeSession.new(
            root_repo_path=str(self.repo),
            work_branch="work",
            target_ref="origin/feature",
            repo_stack=[
                RepositoryFrame(
                    repo_path=str(self.repo),
                    display_path=".",
                    depth=0,
                    original_head="head0",
                    original_branch="work",
                    theirs_sha="feature-sha",
                )
            ],
        )

    def test_save_load_update_and_clear_roundtrip(self) -> None:
        store = MergeSessionStore(git_path_resolver=self._resolver)
        session = self._new_session()

        saved = store.save(self.repo, session)
        self.assertEqual(saved.revision, 1)
        self.assertEqual(saved.status, SessionStatus.RUNNING)

        loaded = store.load(self.repo)
        self.assertEqual(loaded, saved)

        updated = store.update(
            self.repo,
            lambda current: replace(
                current,
                status=SessionStatus.PAUSED_MANUAL,
                phase=SessionPhase.MANUAL_RESOLUTION,
                outcome=SessionOutcome.PAUSED_MANUAL,
                paused_reason="needs manual conflict resolution",
            ),
        )
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.status, SessionStatus.PAUSED_MANUAL)
        self.assertEqual(updated.phase, SessionPhase.MANUAL_RESOLUTION)

        status = store.status(self.repo)
        self.assertTrue(status.exists)
        self.assertFalse(status.locked)
        self.assertEqual(status.session, updated)

        cleared = store.clear(self.repo, expected_revision=2)
        self.assertTrue(cleared)
        self.assertIsNone(store.load(self.repo))
        self.assertFalse(store.status(self.repo).exists)

    def test_save_rejects_stale_revision(self) -> None:
        store = MergeSessionStore(git_path_resolver=self._resolver)
        saved = store.save(self.repo, self._new_session())
        fresher = store.update(self.repo, lambda current: replace(current, last_error="newer error"))
        self.assertIsNotNone(fresher)

        with self.assertRaises(SessionRevisionError):
            store.save(self.repo, saved)

    def test_load_rejects_unknown_schema(self) -> None:
        store = MergeSessionStore(git_path_resolver=self._resolver)
        session_path = store.session_path(self.repo)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

        with self.assertRaises(SessionSchemaError):
            store.load(self.repo)

    def test_save_uses_atomic_replace(self) -> None:
        store = MergeSessionStore(git_path_resolver=self._resolver)
        with mock.patch("manual_merge_split.session_store.os.replace") as replace_mock:
            store.save(self.repo, self._new_session())
        replace_mock.assert_called_once()

    def test_save_rejects_when_locked(self) -> None:
        store = MergeSessionStore(git_path_resolver=self._resolver)
        lock_path = store.lock_path(self.repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("busy\n", encoding="utf-8")

        with self.assertRaises(SessionLockError):
            store.save(self.repo, self._new_session())

    def test_command_lock_rejects_a_concurrent_command(self) -> None:
        store = MergeSessionStore(git_path_resolver=self._resolver)
        with store.command_lock(self.repo):
            with self.assertRaises(SessionLockError):
                with store.command_lock(self.repo):
                    self.fail("nested command lock must not be acquired")

    def test_runner_fallback_resolves_relative_git_dir(self) -> None:
        calls: list[tuple[Path, tuple[str, ...]]] = []

        def runner(repo_path: Path, args: list[str]) -> FakeGitResult:
            calls.append((repo_path, tuple(args)))
            return FakeGitResult(stdout=".git\n")

        store = MergeSessionStore(runner=runner)
        self.assertEqual(
            store.session_path(self.repo),
            (self.git_dir / "recursive-submodule-merge" / "session.json").resolve(strict=False),
        )
        self.assertEqual(calls, [(self.repo.resolve(strict=False), ("rev-parse", "--git-dir"))])

    def test_update_can_create_new_session(self) -> None:
        store = MergeSessionStore(git_path_resolver=self._resolver)

        created = store.update(self.repo, lambda current: self._new_session() if current is None else current)
        self.assertIsNotNone(created)
        assert created is not None
        self.assertEqual(created.revision, 1)


if __name__ == "__main__":
    unittest.main()
