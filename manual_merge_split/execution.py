"""Local Git execution helpers for the split manual workflow."""

from __future__ import annotations

import codecs
import locale
import queue
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterable

from subprocess_window import hidden_subprocess_kwargs

from .models import GitBinaryResult, GitResult, MergeAutomationError


VERBOSE = False


def set_verbose(enabled: bool) -> None:
    global VERBOSE
    VERBOSE = enabled


def verbose_print(message: str) -> None:
    if VERBOSE:
        print(message)


def decode_stream(payload: bytes | None) -> str:
    if not payload:
        return ""
    candidates = [
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


def format_git_failure(result: GitResult) -> str:
    rendered = " ".join(shlex.quote(part) for part in result.args)
    details = [f"Command failed in {result.cwd}: {rendered}"]
    if result.stdout:
        details.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        details.append(f"stderr:\n{result.stderr}")
    return "\n".join(details)


def run_git(
    repo_path: Path,
    args: Iterable[str],
    *,
    check: bool = True,
    stream_output: bool = False,
) -> GitResult:
    cmd = ["git", *args]

    if VERBOSE:
        print(f"[RUN] {repo_path}", flush=True)
        print(" ".join(shlex.quote(part) for part in cmd), flush=True)

    if not stream_output:
        completed = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=False,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        result = GitResult(
            args=cmd,
            cwd=repo_path,
            returncode=completed.returncode,
            stdout=decode_stream(completed.stdout).strip(),
            stderr=decode_stream(completed.stderr).strip(),
        )
        if VERBOSE:
            if result.stdout:
                print(f"[STDOUT]\n{result.stdout}", flush=True)
            if result.stderr:
                print(f"[STDERR]\n{result.stderr}", flush=True)
            print(f"[EXIT] {result.returncode}\n", flush=True)
        if check and result.returncode != 0:
            raise MergeAutomationError(format_git_failure(result))
        return result

    process = subprocess.Popen(
        cmd,
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
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

    closed_streams = 0
    while closed_streams < len(readers):
        stream_name, payload = events.get()
        if payload is None:
            closed_streams += 1
            continue
        if stream_name == "stdout":
            stdout_chunks.append(payload)
            sys.stdout.write(payload)
            sys.stdout.flush()
        else:
            stderr_chunks.append(payload)
            sys.stderr.write(payload)
            sys.stderr.flush()

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
) -> GitBinaryResult:
    cmd = ["git", *args]
    if VERBOSE:
        print(f"[RUN] {repo_path}", flush=True)
        print(" ".join(shlex.quote(part) for part in cmd), flush=True)

    completed = subprocess.run(
        cmd,
        cwd=repo_path,
        capture_output=True,
        text=False,
        check=False,
        **hidden_subprocess_kwargs(),
    )
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
            print(f"[STDOUT]\n{stdout_text}", flush=True)
        if stderr_text:
            print(f"[STDERR]\n{stderr_text}", flush=True)
        print(f"[EXIT] {result.returncode}\n", flush=True)
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
