from __future__ import annotations

import ast
import subprocess
import tempfile
import unittest
from pathlib import Path

from do_package import resolve_unambiguous_exe_timestamp


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = REPO_ROOT / "do_package.py"
LEGACY_CORE = "recursive_gitlink_merge_core.py"
REQUIRED_RUNTIME_SCRIPTS = {"gui_command_builders.py"}
VALIDATION_BRIDGES = {
    "ci_merge_mergecore_bridge.py",
    "ci_merge_sequence_mergecore_bridge.py",
}
FORBIDDEN_MACHINE_PATHS = ("D:\\Git", "C:\\Users\\Qs")


def _tracked_python_files() -> list[str]:
    """Python files git tracks, so local lab-run artifacts are not scanned.

    ``rglob`` also walks git-ignored scratch directories (``manual_*_run*/``),
    which are throwaway local reproductions and legitimately full of absolute
    paths; only files that actually ship are interesting here.
    """
    try:
        completed = subprocess.run(
            ["git", "-c", "safe.directory=*", "ls-files", "--", "*.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise unittest.SkipTest("git executable is required to list tracked Python files") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip() or f"exit {completed.returncode}"
        raise AssertionError(f"git ls-files failed: {detail}")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _extract_string_list_assignment(module: ast.Module, variable_name: str) -> list[str]:
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != variable_name or not isinstance(node.value, ast.List):
            continue
        values: list[str] = []
        for element in node.value.elts:
            if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                raise AssertionError(f"{variable_name} must remain a simple string list in do_package.py")
            values.append(element.value)
        return values
    raise AssertionError(f"assignment to {variable_name!r} not found in do_package.py")


class PackagingManifestTests(unittest.TestCase):
    def test_release_bundle_scripts_exclude_deleted_legacy_core(self) -> None:
        module = ast.parse(PACKAGE_SCRIPT.read_text(encoding="utf-8"), filename=str(PACKAGE_SCRIPT))

        scripts = _extract_string_list_assignment(module, "scripts")

        self.assertNotIn(LEGACY_CORE, scripts)
        self.assertTrue(VALIDATION_BRIDGES.isdisjoint(scripts))
        self.assertTrue(REQUIRED_RUNTIME_SCRIPTS.issubset(scripts))

    def test_deleted_legacy_core_file_is_absent(self) -> None:
        self.assertFalse((REPO_ROOT / LEGACY_CORE).exists())

    def test_package_executable_timestamp_requires_an_unambiguous_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertIsNone(resolve_unambiguous_exe_timestamp(root))

            (root / "dist_manual_exe_20260101_000000").mkdir()
            self.assertEqual(resolve_unambiguous_exe_timestamp(root), "20260101_000000")

            (root / "dist_manual_exe_20260202_000000").mkdir()
            self.assertIsNone(resolve_unambiguous_exe_timestamp(root))

    def test_non_test_python_files_do_not_embed_developer_machine_paths(self) -> None:
        offenders: list[str] = []
        for relative_path in _tracked_python_files():
            path = REPO_ROOT / relative_path
            if not path.is_file():
                continue
            if path.is_relative_to(REPO_ROOT / "tests_split_gitlink_merge"):
                continue
            text = path.read_text(encoding="utf-8")
            if any(marker in text for marker in FORBIDDEN_MACHINE_PATHS):
                offenders.append(relative_path)
        self.assertEqual([], offenders)
