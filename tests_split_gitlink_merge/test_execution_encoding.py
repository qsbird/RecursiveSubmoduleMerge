from __future__ import annotations

import io
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from manual_merge_split import execution


class ExecutionEncodingTests(unittest.TestCase):
    def tearDown(self) -> None:
        execution.set_verbose(False)

    @staticmethod
    def _gbk_console() -> tuple[io.BytesIO, io.TextIOWrapper]:
        raw = io.BytesIO()
        return raw, io.TextIOWrapper(raw, encoding="gbk", errors="strict")

    def test_decode_stream_strips_utf8_bom(self) -> None:
        self.assertEqual(execution.decode_stream(b"\xef\xbb\xbfcontent"), "content")

    def test_verbose_run_git_handles_text_outside_gbk(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "show"],
            returncode=0,
            stdout="\ufeffcontent \U0001f600".encode("utf-8"),
            stderr=b"",
        )
        raw, console = self._gbk_console()

        execution.set_verbose(True)
        with mock.patch.object(execution.subprocess, "run", return_value=completed):
            with mock.patch.object(execution.sys, "stdout", console):
                result = execution.run_git(Path("."), ["show", ":2:file.h"], check=False)
                console.flush()

        self.assertEqual(result.stdout, "content \U0001f600")
        self.assertIn(b"[STDOUT]", raw.getvalue())

    def test_verbose_run_git_binary_handles_text_outside_gbk(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "show"],
            returncode=0,
            stdout="content \U0001f600".encode("utf-8"),
            stderr=b"",
        )
        raw, console = self._gbk_console()

        execution.set_verbose(True)
        with mock.patch.object(execution.subprocess, "run", return_value=completed):
            with mock.patch.object(execution.sys, "stdout", console):
                result = execution.run_git_binary(Path("."), ["show", ":2:file.h"], check=False)
                console.flush()

        self.assertEqual(result.stdout, completed.stdout)
        self.assertIn(b"[STDOUT]", raw.getvalue())

    def test_stream_output_preserves_chunk_boundaries(self) -> None:
        class ChunkedPipe:
            def __init__(self, chunks: list[bytes]) -> None:
                self.chunks = iter(chunks)

            def read(self, _size: int) -> bytes:
                return next(self.chunks, b"")

            def close(self) -> None:
                return None

        process = mock.Mock()
        process.stdout = ChunkedPipe([b"Hel", b"lo"])
        process.stderr = ChunkedPipe([])
        process.wait.return_value = 0
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="utf-8")

        with mock.patch.object(execution.subprocess, "Popen", return_value=process):
            with mock.patch.object(execution.sys, "stdout", console):
                result = execution.run_git(Path("."), ["status"], check=False, stream_output=True)
                console.flush()

        self.assertEqual(result.stdout, "Hello")
        self.assertEqual(raw.getvalue(), b"Hello")


if __name__ == "__main__":
    unittest.main()
