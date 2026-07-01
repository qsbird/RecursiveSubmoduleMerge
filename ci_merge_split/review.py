"""Review command helpers for CI merge reports."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from .review_common import (
    REVIEW_DECISIONS,
    build_review_data,
    read_json,
    render_markdown_review,
    write_json,
)
from .review_common import write_text
from .review_html import DEFAULT_DIFF_CONTEXT_LINES, DEFAULT_EXPANDED_DIFF_CONTEXT_LINES, generate_html_file


def default_reviewer() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def parse_checklist(items: list[str] | None) -> dict[str, bool]:
    checklist: dict[str, bool] = {}
    for item in items or []:
        if "=" not in item:
            checklist[item.strip()] = True
            continue
        key, raw_value = item.split("=", 1)
        normalized = raw_value.strip().lower()
        checklist[key.strip()] = normalized in {"1", "true", "yes", "y", "on", "checked"}
    return checklist


def default_review_output_path(report_path: Path) -> Path:
    return report_path.with_name("merge-review.json")


def export_review_record(
    report_path: Path,
    output_path: Path | None = None,
    *,
    decision: str = "INFO_ONLY",
    reviewer: str = "",
    notes: str = "",
    checklist: dict[str, bool] | None = None,
    file_notes: list[dict[str, str]] | None = None,
) -> tuple[Path, Path]:
    report_path = report_path.expanduser().resolve(strict=False)
    report = read_json(report_path)
    review = build_review_data(
        report,
        report_path,
        decision=decision,
        reviewer=reviewer,
        notes=notes,
        checklist=checklist,
        file_notes=file_notes,
    )
    json_path = output_path.expanduser().resolve(strict=False) if output_path else default_review_output_path(report_path)
    markdown_path = json_path.with_suffix(".md")
    write_json(json_path, review)
    write_text(markdown_path, render_markdown_review(review))
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review ci_merge_split JSON reports.")
    subparsers = parser.add_subparsers(dest="command")

    html_parser = subparsers.add_parser("html", help="Generate a self-contained offline HTML review page.")
    html_parser.add_argument("--report", required=True, help="Path to merge-report.json")
    html_parser.add_argument("--output", help="Output HTML path")
    html_parser.add_argument("--title", default="CI Merge Code Review", help="HTML page title")
    html_parser.add_argument(
        "--codemirror-only",
        action="store_true",
        help="Accepted for compatibility. The offline renderer always uses built-in views.",
    )
    html_parser.add_argument(
        "--diff-context-lines",
        "--diff_context_lines",
        dest="diff_context_lines",
        type=int,
        default=DEFAULT_DIFF_CONTEXT_LINES,
        help="Fallback git diff context lines when patch artifacts are missing.",
    )
    html_parser.add_argument(
        "--expanded-diff-context-lines",
        "--expanded_diff_context_lines",
        dest="expanded_diff_context_lines",
        type=int,
        default=DEFAULT_EXPANDED_DIFF_CONTEXT_LINES,
        help="Expanded fallback git diff context lines when expanded patch artifacts are missing.",
    )

    record_parser = subparsers.add_parser("record", help="Export a review decision JSON and Markdown summary.")
    record_parser.add_argument("--report", required=True, help="Path to merge-report.json")
    record_parser.add_argument("--output", help="Output review JSON path")
    record_parser.add_argument("--decision", choices=REVIEW_DECISIONS, default="INFO_ONLY")
    record_parser.add_argument("--reviewer", default=default_reviewer())
    record_parser.add_argument("--notes", default="")
    record_parser.add_argument(
        "--check",
        action="append",
        help="Checklist item. Use 'Label=true' or just 'Label' to mark a checked item.",
    )
    return parser


def normalize_legacy_args(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    if argv[0] in {"html", "record", "-h", "--help"}:
        return argv
    return ["html", *argv]


def run_html(args: argparse.Namespace) -> int:
    if args.diff_context_lines < 0:
        print("--diff-context-lines must be >= 0", file=sys.stderr)
        return 2
    if args.expanded_diff_context_lines < args.diff_context_lines:
        print("--expanded-diff-context-lines must be >= --diff-context-lines", file=sys.stderr)
        return 2
    report_path = Path(args.report).expanduser().resolve(strict=False)
    if not report_path.exists():
        print(f"report not found: {report_path}", file=sys.stderr)
        return 2
    output_path = Path(args.output).expanduser().resolve(strict=False) if args.output else None
    generated = generate_html_file(
        report_path,
        output_path,
        title=args.title,
        diff_context_lines=args.diff_context_lines,
        expanded_diff_context_lines=args.expanded_diff_context_lines,
        codemirror_only=args.codemirror_only,
    )
    print(generated)
    return 0


def run_record(args: argparse.Namespace) -> int:
    report_path = Path(args.report).expanduser().resolve(strict=False)
    if not report_path.exists():
        print(f"report not found: {report_path}", file=sys.stderr)
        return 2
    output_path = Path(args.output).expanduser().resolve(strict=False) if args.output else None
    json_path, markdown_path = export_review_record(
        report_path,
        output_path,
        decision=args.decision,
        reviewer=args.reviewer,
        notes=args.notes,
        checklist=parse_checklist(args.check),
    )
    print(json_path)
    print(markdown_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    normalized_argv = normalize_legacy_args(argv if argv is not None else sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(normalized_argv)
    if args.command == "html":
        return run_html(args)
    if args.command == "record":
        return run_record(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
