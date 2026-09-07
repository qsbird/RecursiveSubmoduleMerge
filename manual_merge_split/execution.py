"""Local Git execution helpers for the split manual workflow."""

from __future__ import annotations

import codecs
import locale
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Mapping

from subprocess_window import hidden_subprocess_kwargs

from .models import GitBinaryResult, GitResult, MergeAutomationError


VERBOSE = False

#: Config injected into every ``git`` invocation of the manual workflow.
#:
#: ``core.quotePath=false`` keeps non-ASCII paths readable: without it git emits
#: octal-escaped, double-quoted paths (``"\344\270\255..."``) from ``status`` and
#: ``ls-files``, which then flow into ``git add`` / external conflict tools as a
#: path that does not exist on disk.
GIT_BASE_CONFIG: tuple[str, ...] = (
    "-c",
    "safe.directory=*",
    "-c",
    "core.longpaths=true",
    "-c",
    "core.quotePath=false",
)

#: Git subcommands that may talk to a remote and therefore block indefinitely.
NETWORK_SUBCOMMANDS: frozenset[str] = frozenset(
    {"clone", "fetch", "ls-remote", "pull", "push"}
)

#: Backstop wall-clock limit for remote operations, overridable through
#: ``MERGE_TOOL_GIT_NETWORK_TIMEOUT_SECONDS`` (``0`` disables it).
DEFAULT_NETWORK_TIMEOUT_SECONDS = 900
NETWORK_TIMEOUT_ENV = "MERGE_TOOL_GIT_NETWORK_TIMEOUT_SECONDS"


def git_env(
    base_env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment used for manual-workflow git invocations.

    ``GIT_TERMINAL_PROMPT`` is only defaulted, never forced: the GUI launches git
    with a hidden console, so an interactive credential prompt would hang with no
    visible question, but a caller that knows it has a usable terminal can still
    opt back in.
    """
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env["GIT_MERGE_AUTOEDIT"] = "no"
    if overrides:
        env.update(overrides)
    return env


def _network_timeout_seconds() -> float | None:
    raw = os.environ.get(NETWORK_TIMEOUT_ENV, "").strip()
    if not raw:
        return float(DEFAULT_NETWORK_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_NETWORK_TIMEOUT_SECONDS)
    return None if value <= 0 else value


def _first_subcommand(args: Iterable[str]) -> str:
    """Return the first non-option token, skipping leading ``-c key=value`` pairs."""
    tokens = list(args)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-c":
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return ""


def resolve_git_timeout(args: Iterable[str], timeout: float | None) -> float | None:
    if timeout is not None:
        return None if timeout <= 0 else timeout
    if _first_subcommand(args) in NETWORK_SUBCOMMANDS:
        return _network_timeout_seconds()
    return None


def set_verbose(enabled: bool) -> None:
    global VERBOSE
    VERBOSE = enabled


def verbose_print(message: str) -> None:
    if VERBOSE:
        _safe_write(sys.stdout, message)


def decode_stream(payload: bytes | None) -> str:
    if not payload:
        return ""
    candidates = [
        "utf-8-sig",
        "utf-8",
        locale.getpreferredencoding(False),
        "gb18030",
        "gbk",
    ]
    tried: set[str] = set()
    for encoding in candidates:
        normalized = (encoding or "").strip()
        if not normalized or normalized in tried:
            continue
        tried.add(normalized)
        try:
            return payload.decode(normalized)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _safe_write(stream: object, text: str, *, append_newline: bool = True) -> None:
    """Write *text* to *stream* (sys.stdout / sys.stderr), silently ignoring
    characters that cannot be encoded by the stream's current codec (e.g.
    Unicode replacement characters on a gbk terminal).

    ``append_newline=False`` is used for incremental process-output chunks so
    read boundaries do not alter the rendered output.
    """
    rendered = text + "\n" if append_newline and not text.endswith("\n") else text
    try:
        if hasattr(stream, "buffer"):
            stream.buffer.write(rendered.encode(stream.encoding or "utf-8", errors="replace"))
            stream.flush()
        else:
            stream.write(rendered)
            stream.flush()
    except (UnicodeEncodeError, UnicodeDecodeError, OSError):
        pass


def format_git_failure(result: GitResult) -> str:
    rendered = " ".join(shlex.quote(part) for part in result.args)
    details = [f"Command failed in {result.cwd}: {rendered}"]
    if result.stdout:
        details.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        details.append(f"stderr:\n{result.stderr}")
    return "\n".join(details)


def _terminate_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _timeout_error(repo_path: Path, cmd: list[str], limit: float) -> MergeAutomationError:
    rendered = " ".join(shlex.quote(part) for part in cmd)
    return MergeAutomationError(
        f"Command timed out after {limit:g}s in {repo_path}: {rendered}\n"
        f"Set {NETWORK_TIMEOUT_ENV} to raise or disable (0) the limit."
    )


def run_git(
    repo_path: Path,
    args: Iterable[str],
    *,
    check: bool = True,
    stream_output: bool = False,
    timeout: float | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> GitResult:
    args = list(args)
    cmd = ["git", *GIT_BASE_CONFIG, *args]
    limit = resolve_git_timeout(args, timeout)
    env = git_env(overrides=env_overrides)

    if VERBOSE:
        _safe_write(sys.stdout, f"[RUN] {repo_path}")
        _safe_write(sys.stdout, " ".join(shlex.quote(part) for part in cmd))

    if not stream_output:
        try:
            completed = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=False,
                check=False,
                env=env,
                timeout=limit,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise _timeout_error(repo_path, cmd, float(exc.timeout or 0)) from exc
        result = GitResult(
            args=cmd,
            cwd=repo_path,
            returncode=completed.returncode,
            stdout=decode_stream(completed.stdout).strip(),
            stderr=decode_stream(completed.stderr).strip(),
        )
        if VERBOSE:
            if result.stdout:
                _safe_write(sys.stdout, f"[STDOUT]\n{result.stdout}")
            if result.stderr:
                _safe_write(sys.stderr, f"[STDERR]\n{result.stderr}")
            _safe_write(sys.stdout, f"[EXIT] {result.returncode}\n")
        if check and result.returncode != 0:
            raise MergeAutomationError(format_git_failure(result))
        return result

    process = subprocess.Popen(
        cmd,
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        env=env,
        **hidden_subprocess_kwargs(),
    )
    encoding = (locale.getpreferredencoding(False) or "utf-8").strip() or "utf-8"
    events: queue.SimpleQueue[tuple[str, str | None]] = queue.SimpleQueue()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def pump_stream(stream_name: str, pipe: object) -> None:
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        try:
            while True:
                chunk = pipe.read(1024)
                if not chunk:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        events.put((stream_name, tail))
                    break
                text = decoder.decode(chunk)
                if text:
                    events.put((stream_name, text))
        finally:
            pipe.close()
            events.put((stream_name, None))

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        threading.Thread(target=pump_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=pump_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = None if limit is None else time.monotonic() + limit
    closed_streams = 0
    while closed_streams < len(readers):
        if deadline is None:
            stream_name, payload = events.get()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                raise _timeout_error(repo_path, cmd, limit or 0)
            try:
                stream_name, payload = events.get(timeout=remaining)
            except queue.Empty:
                _terminate_process(process)
                raise _timeout_error(repo_path, cmd, limit or 0) from None
        if payload is None:
            closed_streams += 1
            continue
        if stream_name == "stdout":
            stdout_chunks.append(payload)
            _safe_write(sys.stdout, payload, append_newline=False)
        else:
            stderr_chunks.append(payload)
            _safe_write(sys.stderr, payload, append_newline=False)

    result = GitResult(
        args=cmd,
        cwd=repo_path,
        returncode=process.wait(),
        stdout="".join(stdout_chunks).strip(),
        stderr="".join(stderr_chunks).strip(),
    )
    for reader in readers:
        reader.join()
    if VERBOSE:
        print(f"[EXIT] {result.returncode}\n", flush=True)
    if check and result.returncode != 0:
        raise MergeAutomationError(format_git_failure(result))
    return result


def run_git_binary(
    repo_path: Path,
    args: Iterable[str],
    *,
    check: bool = True,
    timeout: float | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> GitBinaryResult:
    args = list(args)
    cmd = ["git", *GIT_BASE_CONFIG, *args]
    limit = resolve_git_timeout(args, timeout)
    env = git_env(overrides=env_overrides)
    if VERBOSE:
        _safe_write(sys.stdout, f"[RUN] {repo_path}")
        _safe_write(sys.stdout, " ".join(shlex.quote(part) for part in cmd))

    try:
        completed = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=False,
            check=False,
            env=env,
            timeout=limit,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise _timeout_error(repo_path, cmd, float(exc.timeout or 0)) from exc
    result = GitBinaryResult(
        args=cmd,
        cwd=repo_path,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if VERBOSE:
        stdout_text = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr_text = completed.stderr.decode("utf-8", errors="replace").strip()
        if stdout_text:
            _safe_write(sys.stdout, f"[STDOUT]\n{stdout_text}")
        if stderr_text:
            _safe_write(sys.stderr, f"[STDERR]\n{stderr_text}")
        _safe_write(sys.stdout, f"[EXIT] {result.returncode}\n")
    if check and result.returncode != 0:
        raise MergeAutomationError(
            format_git_failure(
                GitResult(
                    args=cmd,
                    cwd=repo_path,
                    returncode=result.returncode,
                    stdout=result.stdout.decode("utf-8", errors="replace").strip(),
                    stderr=result.stderr.decode("utf-8", errors="replace").strip(),
                )
            )
        )
    return result
