from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci_merge_split.review import export_review_record
from ci_merge_split.review_html import generate_html_file, parse_patch_rows


class CiReviewHtmlTests(unittest.TestCase):
    def _write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _sample_report(self, root: Path) -> Path:
        report_path = root / "merge-report.json"
        repository_patch = self._write(
            root / "patches" / "root" / "repository.patch",
            "diff --git a/demo.txt b/demo.txt\n--- a/demo.txt\n+++ b/demo.txt\n@@ -1 +1 @@\n-old line\n+new line\n",
        )
        file_patch = self._write(
            root / "patches" / "root" / "files" / "0001-demo.txt.patch",
            "diff --git a/demo.txt b/demo.txt\n@@ -1 +1 @@\n-old line\n+new line\n",
        )
        ours_file = self._write(root / "files" / "root" / "0001-demo.txt" / "ours", "old line\n")
        result_file = self._write(root / "files" / "root" / "0001-demo.txt" / "result", "new line\n")
        base_conflict = self._write(root / "conflicts" / "root" / "conflict" / "base", "base conflict\n")
        ours_conflict = self._write(root / "conflicts" / "root" / "conflict" / "ours", "ours conflict\n")
        theirs_conflict = self._write(root / "conflicts" / "root" / "conflict" / "theirs", "theirs conflict\n")
        worktree_conflict = self._write(root / "conflicts" / "root" / "conflict" / "worktree", "worktree conflict\n")

        report = {
            "schema_version": "1.0",
            "status": "success",
            "exit_code": 0,
            "failure_reason": None,
            "output": {
                "output_dir": str(root),
                "report_path": str(report_path),
                "patch_dir": str(root / "patches"),
                "conflict_dir": str(root / "conflicts"),
                "file_dir": str(root / "files"),
                "generated_file_inside_repo": False,
            },
            "resolved": {
                "source_sha": "a" * 40,
                "target_sha": "b" * 40,
                "result_sha": "c" * 40,
            },
            "summary": {
                "merged_repositories": 1,
                "submodules_processed": 0,
                "submodule_recursive_merges": 0,
                "conflicts": 1,
            },
            "repositories": [
                {
                    "path": "root",
                    "repo_path": str(root / "missing-repo"),
                    "base": "base",
                    "ours": "ours",
                    "theirs": "theirs",
                    "result": "result",
                    "strategy": "merge",
                    "diff_stat": " demo.txt | 2 +-\n",
                    "patch_path": str(repository_patch),
                    "patch_relative_path": "patches/root/repository.patch",
                    "changed_files": [
                        {
                            "status": "M",
                            "path": "demo.txt",
                            "patch_path": str(file_patch),
                            "patch_relative_path": "patches/root/files/0001-demo.txt.patch",
                            "content_artifacts": {
                                "ours": {"path": str(ours_file), "relative_path": "files/root/0001-demo.txt/ours"},
                                "result": {"path": str(result_file), "relative_path": "files/root/0001-demo.txt/result"},
                            },
                        }
                    ],
                }
            ],
            "conflicts": [
                {
                    "repo_path": str(root / "missing-repo"),
                    "path": "conflict.txt",
                    "type": "file_conflict",
                    "stages": [
                        {"stage": "1", "sha": "1" * 40, "mode": "100644"},
                        {"stage": "2", "sha": "2" * 40, "mode": "100644"},
                        {"stage": "3", "sha": "3" * 40, "mode": "100644"},
                    ],
                    "artifacts": {
                        "stages": {
                            "base": {"path": str(base_conflict), "relative_path": "conflicts/root/conflict/base"},
                            "ours": {"path": str(ours_conflict), "relative_path": "conflicts/root/conflict/ours"},
                            "theirs": {"path": str(theirs_conflict), "relative_path": "conflicts/root/conflict/theirs"},
                        },
                        "worktree": {
                            "path": str(worktree_conflict),
                            "relative_path": "conflicts/root/conflict/worktree",
                        },
                    },
                }
            ],
            "commands": [
                {
                    "cmd": ["git", "diff"],
                    "cwd": str(root),
                    "exit_code": 0,
                    "duration_ms": 10,
                    "allow_failure": False,
                    "timed_out": False,
                    "stdout": "command output",
                    "stderr": "",
                }
            ],
            "errors": [],
            "restore": {"attempted": False, "success": None},
            "publish": {"enabled": False, "success": None},
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report_path

    def test_offline_html_embeds_report_artifacts_without_cdn_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = self._sample_report(Path(temp_dir))
            output_path = generate_html_file(report_path)
            html_text = output_path.read_text(encoding="utf-8")

        self.assertIn("Self-contained offline HTML", html_text)
        self.assertIn("CodeMirror Review Workbench", html_text)
        self.assertIn('class="diff-workbench codemirror-review"', html_text)
        self.assertIn('data-view-mode="unified"', html_text)
        self.assertIn('data-view-mode="split"', html_text)
        self.assertIn('class="nav-link nav-file"', html_text)
        self.assertIn("old line", html_text)
        self.assertIn("new line", html_text)
        self.assertIn("base conflict", html_text)
        self.assertIn("ours conflict", html_text)
        self.assertIn("theirs conflict", html_text)
        self.assertIn("worktree conflict", html_text)
        self.assertNotIn("cdn.jsdelivr", html_text)
        self.assertNotIn("esm.sh", html_text)
        self.assertNotIn("diff2html", html_text.lower())
        self.assertNotIn("import(", html_text)
        self.assertNotIn('<pre class="patch"', html_text)

    def test_patch_rows_track_unified_diff_line_numbers(self) -> None:
        rows = parse_patch_rows(
            "diff --git a/demo.txt b/demo.txt\n"
            "@@ -10,2 +10,2 @@\n"
            " unchanged\n"
            "-old line\n"
            "+new line\n"
        )

        self.assertEqual(rows[2]["kind"], "ctx")
        self.assertEqual(rows[2]["old_number"], 10)
        self.assertEqual(rows[2]["new_number"], 10)
        self.assertEqual(rows[3]["kind"], "del")
        self.assertEqual(rows[3]["old_number"], 11)
        self.assertEqual(rows[3]["new_number"], "")
        self.assertEqual(rows[4]["kind"], "add")
        self.assertEqual(rows[4]["old_number"], "")
        self.assertEqual(rows[4]["new_number"], 11)

    def test_review_record_exports_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = self._sample_report(Path(temp_dir))
            json_path, markdown_path = export_review_record(
                report_path,
                decision="APPROVED",
                reviewer="tester",
                notes="looks good",
                checklist={"Changed files reviewed": True},
            )
            review = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(review["decision"], "APPROVED")
        self.assertEqual(review["reviewer"], "tester")
        self.assertIn("looks good", markdown)
        self.assertIn("Changed files reviewed", markdown)


if __name__ == "__main__":
    unittest.main()
