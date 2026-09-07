"""Persistent storage for resumable manual merge sessions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator

from .session_models import MergeSession, SESSION_SCHEMA_VERSION, SessionStatusSnapshot, utc_now_iso


SESSION_DIRECTORY_NAME = "recursive-submodule-merge"
SESSION_FILE_NAME = "session.json"
LOCK_FILE_NAME = "session.lock"
COMMAND_LOCK_FILE_NAME = "command.lock"


class SessionStoreError(RuntimeError):
    """Raised when session persistence cannot proceed safely."""


class SessionSchemaError(SessionStoreError):
    """Raised when a session file uses an unsupported schema."""


class SessionRevisionError(SessionStoreError):
    """Raised when a caller attempts to write a stale session revision."""


class SessionLockError(SessionStoreError):
    """Raised when a concurrent writer already holds the session lock."""


GitPathResolver = Callable[[Path, str], str | Path]
GitRunner = Callable[[Path, list[str]], Any]
SessionMutator = Callable[[MergeSession | None], MergeSession | None]


def process_is_alive(pid: int) -> bool:
    """Best-effort liveness probe for a lock owner.

    ``os.kill(pid, 0)`` must not be used on Windows: CPython maps every signal
    other than CTRL_* to ``TerminateProcess``, so probing would kill the owner.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # 87 = the PID is not a process (dead / never existed).  Any other
            # OpenProcess failure, including ACCESS_DENIED (5), only means this
            # caller cannot inspect the owner — it does not mean the owner is gone.
            return ctypes.get_last_error() != error_invalid_parameter
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock_owner_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    try:
        return int(raw.splitlines()[0])
    except ValueError:
        return None


def _break_stale_lock(lock_path: Path) -> bool:
    """Remove a lock whose owning process is gone; return True if it was removed.

    Without this a merge command killed mid-run (or a crashed GUI) leaves a lock
    file that blocks every later merge, status, resume and abort until the user
    finds and deletes it by hand.  A lock file without a readable pid is left
    alone: it did not come from this code path, so breaking it would be a guess.
    """
    pid = read_lock_owner_pid(lock_path)
    if pid is None or process_is_alive(pid):
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    print(
        f"Released a stale merge lock left by pid {pid}: {lock_path}",
        file=sys.stderr,
    )
    return True


class MergeSessionStore:
    def __init__(
        self,
        *,
        git_path_resolver: GitPathResolver | None = None,
        runner: GitRunner | None = None,
    ) -> None:
        self._git_path_resolver = git_path_resolver
        self._runner = runner

    def resolve_git_dir(self, repo_path: Path) -> Path:
        repo = Path(repo_path).resolve(strict=False)
        if self._git_path_resolver is not None:
            raw_path = self._git_path_resolver(repo, "--git-dir")
        else:
            raw_path = self._run_git_path(repo, ["rev-parse", "--git-dir"])
        git_dir = Path(str(raw_path))
        if not git_dir.is_absolute():
            git_dir = repo / git_dir
        return git_dir.resolve(strict=False)

    def session_path(self, repo_path: Path) -> Path:
        return self.resolve_git_dir(repo_path) / SESSION_DIRECTORY_NAME / SESSION_FILE_NAME

    def lock_path(self, repo_path: Path) -> Path:
        return self.session_path(repo_path).with_name(LOCK_FILE_NAME)

    def command_lock_path(self, repo_path: Path) -> Path:
        return self.session_path(repo_path).with_name(COMMAND_LOCK_FILE_NAME)

    @contextmanager
    def command_lock(self, repo_path: Path) -> Iterator[None]:
        with self._exclusive_lock(
            self.command_lock_path(repo_path),
            message="Another manual merge command is already running",
        ):
            yield

    def load(self, repo_path: Path) -> MergeSession | None:
        session_path = self.session_path(repo_path)
        if not session_path.exists():
            return None
        payload = self._read_payload(session_path)
        return self._session_from_payload(payload, source=session_path)

    def save(
        self,
        repo_path: Path,
        session: MergeSession,
        *,
        expected_revision: int | None = None,
    ) -> MergeSession:
        session_path = self.session_path(repo_path)
        with self._lock(session_path):
            current = self._load_from_path(session_path)
            current_revision = 0 if current is None else current.revision
            required_revision = session.revision if expected_revision is None else expected_revision
            if current_revision != required_revision:
                raise SessionRevisionError(
                    f"Session revision mismatch for {session_path}: expected {required_revision}, found {current_revision}"
                )
            next_session = replace(
                session,
                schema_version=SESSION_SCHEMA_VERSION,
                revision=current_revision + 1,
                updated_at=utc_now_iso(),
            )
            self._atomic_write(session_path, next_session.to_dict())
            return next_session

    def update(self, repo_path: Path, mutator: SessionMutator) -> MergeSession | None:
        session_path = self.session_path(repo_path)
        with self._lock(session_path):
            current = self._load_from_path(session_path)
            updated = mutator(current)
            if updated is None:
                if session_path.exists():
                    session_path.unlink()
                return None
            current_revision = 0 if current is None else current.revision
            next_session = replace(
                updated,
                schema_version=SESSION_SCHEMA_VERSION,
                revision=current_revision + 1,
                created_at=updated.created_at if current is None else current.created_at,
                updated_at=utc_now_iso(),
            )
            self._atomic_write(session_path, next_session.to_dict())
            return next_session

    def clear(self, repo_path: Path, *, expected_revision: int | None = None) -> bool:
        session_path = self.session_path(repo_path)
        with self._lock(session_path):
            current = self._load_from_path(session_path)
            if current is None:
                return False
            if expected_revision is not None and current.revision != expected_revision:
                raise SessionRevisionError(
                    f"Session revision mismatch for {session_path}: expected {expected_revision}, found {current.revision}"
                )
            session_path.unlink()
            return True

    def status(self, repo_path: Path) -> SessionStatusSnapshot:
        session_path = self.session_path(repo_path)
        return SessionStatusSnapshot(
            session_path=str(session_path),
            exists=session_path.exists(),
            locked=self.lock_path(repo_path).exists(),
            session=self.load(repo_path),
        )

    def _run_git_path(self, repo_path: Path, args: list[str]) -> str:
        if self._runner is not None:
            result = self._runner(repo_path, args)
            return self._read_runner_stdout(result, repo_path, args)
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or f"git {' '.join(args)} failed"
            raise SessionStoreError(stderr)
        return completed.stdout.strip()

    def _read_runner_stdout(self, result: Any, repo_path: Path, args: list[str]) -> str:
        returncode = int(getattr(result, "returncode", 0))
        if returncode != 0:
            stderr = str(getattr(result, "stderr", "")).strip() or f"git {' '.join(args)} failed in {repo_path}"
            raise SessionStoreError(stderr)
        return str(getattr(result, "stdout", "")).strip()

    def _load_from_path(self, session_path: Path) -> MergeSession | None:
        if not session_path.exists():
            return None
        payload = self._read_payload(session_path)
        return self._session_from_payload(payload, source=session_path)

    @staticmethod
    def _read_payload(session_path: Path) -> Any:
        try:
            return json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionSchemaError(f"Could not read session file {session_path}: {exc}") from exc

    def _session_from_payload(self, payload: Any, *, source: Path) -> MergeSession:
        if not isinstance(payload, dict):
            raise SessionSchemaError(f"Session file {source} does not contain a JSON object")
        schema_version = payload.get("schema_version")
        if schema_version != SESSION_SCHEMA_VERSION:
            raise SessionSchemaError(
                f"Unsupported session schema in {source}: expected {SESSION_SCHEMA_VERSION}, found {schema_version!r}"
            )
        try:
            return MergeSession.from_dict(payload)
        except Exception as exc:
            raise SessionSchemaError(f"Invalid session payload in {source}: {exc}") from exc

    @contextmanager
    def _lock(self, session_path: Path) -> Iterator[None]:
        lock_path = session_path.with_name(LOCK_FILE_NAME)
        with self._exclusive_lock(lock_path, message="Session is already locked"):
            yield

    @contextmanager
    def _exclusive_lock(self, lock_path: Path, *, message: str) -> Iterator[None]:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = self._create_lock_file(lock_path)
        except FileExistsError as exc:
            if not _break_stale_lock(lock_path):
                raise SessionLockError(
                    f"{message}: {lock_path}\n"
                    "If no merge command is running, delete that file to release the lock."
                ) from exc
            try:
                fd = self._create_lock_file(lock_path)
            except FileExistsError as retry_exc:
                raise SessionLockError(f"{message}: {lock_path}") from retry_exc
        try:
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            os.fsync(fd)
            yield
        finally:
            os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _create_lock_file(lock_path: Path) -> int:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    def _atomic_write(self, session_path: Path, payload: dict[str, Any]) -> None:
        session_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = session_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, session_path)
