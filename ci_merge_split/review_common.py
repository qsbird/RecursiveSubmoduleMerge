"""Shared helpers for CI merge review reports and artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


CONFLICT_STAGE_LABELS = {"1": "base", "2": "ours", "3": "theirs"}
REVIEW_DECISIONS = ("APPROVED", "NEEDS_CHANGES", "REJECTED", "INFO_ONLY")


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


def safe_get(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def short_sha(value: Any) -> str:
    text = str(value or "")
    return text[:12] if text else ""


def is_probably_text(value: str) -> bool:
    return "\x00" not in value


def line_count(value: str | None) -> int:
    if not value:
        return 0
    return value.count("\n") + (0 if value.endswith("\n") else 1)


def text_size_bytes(value: str | None) -> int:
    return len((value or "").encode("utf-8", errors="replace"))


def format_size(size_bytes: Any) -> str:
    try:
        size = int(size_bytes or 0)
    except (TypeError, ValueError):
        return "0 B"
    if size >= 1_048_576:
        return f"{size / 1_048_576:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def report_dir_from_report(report: dict[str, Any], fallback_report_path: Path | None = None) -> Path:
    report_path = safe_get(report, "output", "report_path")
    if report_path:
        return Path(report_path).expanduser().resolve(strict=False).parent
    if fallback_report_path is not None:
        return fallback_report_path.expanduser().resolve(strict=False).parent
    return Path.cwd()


def _candidate_dirs(report: dict[str, Any], report_dir: Path) -> list[Path]:
    candidates = [report_dir]
    output_dir = safe_get(report, "output", "output_dir")
    report_path = safe_get(report, "output", "report_path")
    if output_dir:
        candidates.append(Path(output_dir).expanduser().resolve(strict=False))
    if report_path:
        candidates.append(Path(report_path).expanduser().resolve(strict=False).parent)

    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def resolve_artifact_path(
    report: dict[str, Any],
    owner: dict[str, Any] | None,
    report_dir: Path | None = None,
    *,
    path_keys: tuple[str, ...] = ("path",),
    relative_keys: tuple[str, ...] = ("relative_path",),
) -> Path | None:
    """Resolve an artifact reference stored in a report entry."""

    if not owner:
        return None
    resolved_report_dir = report_dir or report_dir_from_report(report)
    for key in path_keys:
        value = owner.get(key)
        if not value:
            continue
        direct = Path(str(value)).expanduser().resolve(strict=False)
        if direct.exists() and direct.is_file():
            return direct

    for key in relative_keys:
        value = owner.get(key)
        if not value:
            continue
        for candidate_dir in _candidate_dirs(report, resolved_report_dir):
            candidate = (candidate_dir / str(value)).resolve(strict=False)
            if candidate.exists() and candidate.is_file():
                return candidate
    return None


def resolve_patch_path(report: dict[str, Any], owner: dict[str, Any] | None, report_dir: Path | None = None) -> Path | None:
    return resolve_artifact_path(
        report,
        owner,
        report_dir,
        path_keys=("patch_path",),
        relative_keys=("patch_relative_path",),
    )


def resolve_content_artifact_path(
    report: dict[str, Any],
    record: dict[str, Any] | None,
    report_dir: Path | None = None,
) -> Path | None:
    return resolve_artifact_path(report, record, report_dir, path_keys=("path",), relative_keys=("relative_path",))


def read_artifact_text(
    report: dict[str, Any],
    record: dict[str, Any] | None,
    report_dir: Path | None = None,
) -> tuple[str | None, str | None]:
    path = resolve_content_artifact_path(report, record, report_dir)
    if not path:
        return None, None
    return str(path), read_text(path)


def safe_git_directory(repo_path: Path) -> str:
    return str(repo_path.expanduser().resolve(strict=False)).replace("\\", "/")


def run_git(repo_path: Path, args: list[str], *, timeout: int = 60) -> tuple[int, str]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={safe_git_directory(repo_path)}", *args],
            cwd=str(repo_path),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except OSError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "") + "\ncommand timed out"
        return 124, output
    output = completed.stdout
    if completed.stderr:
        output += "\n[stderr]\n" + completed.stderr
    return completed.returncode, output


def commit_exists(repo_path: Path, sha: str) -> bool:
    code, _output = run_git(repo_path, ["cat-file", "-e", f"{sha}^{{commit}}"], timeout=20)
    return code == 0


def blob_exists(repo_path: Path, sha: str) -> bool:
    code, _output = run_git(repo_path, ["cat-file", "-e", f"{sha}^{{blob}}"], timeout=20)
    return code == 0


def tree_file_exists(repo_path: Path, ref: str, file_path: str) -> bool:
    code, output = run_git(repo_path, ["cat-file", "-t", f"{ref}:{file_path}"], timeout=20)
    return code == 0 and output.strip().splitlines()[0:1] == ["blob"]


def read_git_blob(repo_path: Path, sha: str) -> tuple[int, str]:
    if not blob_exists(repo_path, sha):
        return 128, f"Blob is not available in original repository: {sha}\n"
    return run_git(repo_path, ["cat-file", "-p", sha], timeout=60)


def read_git_tree_file(repo_path: Path, ref: str, file_path: str) -> tuple[int, str]:
    return run_git(repo_path, ["cat-file", "-p", f"{ref}:{file_path}"], timeout=60)


def render_original_repository_diff(
    repo: dict[str, Any],
    diff_context_lines: int,
    *,
    file_path: str | None = None,
) -> tuple[bool, str]:
    repo_path = Path(repo.get("repo_path") or "")
    old_sha = repo.get("ours")
    new_sha = repo.get("result")
    if not repo_path.exists():
        return False, f"repository path does not exist: {repo_path}"
    if not old_sha or not new_sha:
        return False, "repository entry does not contain both ours and result SHAs"
    missing = [str(sha) for sha in (old_sha, new_sha) if not commit_exists(repo_path, str(sha))]
    if missing:
        return False, f"missing commit(s) in original repository: {', '.join(missing)}"
    args = [
        "diff",
        "--no-ext-diff",
        "--binary",
        "--find-renames",
        f"--unified={diff_context_lines}",
        f"{old_sha}..{new_sha}",
    ]
    if file_path:
        args.extend(["--", file_path])
    exit_code, output = run_git(repo_path, args, timeout=120)
    if exit_code != 0:
        return False, output or f"git diff failed with exit code {exit_code}"
    return True, output


def build_risk_items(report: dict[str, Any]) -> list[dict[str, str]]:
    status = report.get("status")
    exit_code = report.get("exit_code")
    summary = report.get("summary") or {}
    publish = report.get("publish") or {}
    restore = report.get("restore") or {}
    errors = report.get("errors") or []
    risks: list[dict[str, str]] = []

    if status != "success" or exit_code not in {0, None}:
        risks.append(
            {
                "level": "high",
                "title": "Merge did not finish cleanly",
                "detail": f"status={status}, exit_code={exit_code}, reason={report.get('failure_reason')}",
            }
        )
    if report.get("conflicts"):
        risks.append(
            {
                "level": "high",
                "title": "Conflicts are present",
                "detail": f"{len(report.get('conflicts') or [])} conflict entries are available for review.",
            }
        )
    if errors:
        risks.append(
            {
                "level": "high",
                "title": "Errors were recorded",
                "detail": f"{len(errors)} structured error entries are present in the report.",
            }
        )
    if int(summary.get("submodule_recursive_merges") or 0) > 0:
        risks.append(
            {
                "level": "medium",
                "title": "Recursive submodule merges",
                "detail": f"{summary.get('submodule_recursive_merges')} recursive submodule merge(s) need focused review.",
            }
        )
    if publish.get("enabled") and publish.get("success") is not True:
        risks.append(
            {
                "level": "high",
                "title": "Publish was enabled but not confirmed",
                "detail": f"publish_success={publish.get('success')}, target={publish.get('target')}",
            }
        )
    if restore.get("attempted") and restore.get("success") is not True:
        risks.append(
            {
                "level": "high",
                "title": "Restore did not fully succeed",
                "detail": f"restore_success={restore.get('success')}",
            }
        )
    if safe_get(report, "output", "generated_file_inside_repo"):
        risks.append(
            {
                "level": "info",
                "title": "Generated files are inside the repository",
                "detail": "Report/log/output paths were generated under the target repository.",
            }
        )
    if not risks:
        risks.append(
            {
                "level": "info",
                "title": "No automatic risk flags",
                "detail": "The report has no conflicts, errors, restore failures, or publish failures.",
            }
        )
    return risks


def build_review_data(
    report: dict[str, Any],
    report_path: Path,
    *,
    decision: str = "INFO_ONLY",
    reviewer: str = "",
    checklist: dict[str, bool] | None = None,
    notes: str = "",
    file_notes: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"unsupported decision: {decision}")
    return {
        "schema_version": "1.0",
        "reviewed_at": now_text(),
        "reviewer": reviewer,
        "decision": decision,
        "report_path": str(report_path.resolve(strict=False)),
        "report": {
            "status": report.get("status"),
            "exit_code": report.get("exit_code"),
            "failure_reason": report.get("failure_reason"),
            "resolved": report.get("resolved"),
            "summary": report.get("summary"),
            "publish": report.get("publish"),
            "restore": report.get("restore"),
        },
        "risks": build_risk_items(report),
        "checklist": checklist or {},
        "file_notes": file_notes or [],
        "notes": notes,
    }


def render_markdown_review(review: dict[str, Any]) -> str:
    report = review.get("report") or {}
    resolved = report.get("resolved") or {}
    lines = [
        "# CI Merge Review",
        "",
        f"- Reviewed at: {review.get('reviewed_at')}",
        f"- Reviewer: {review.get('reviewer') or 'N/A'}",
        f"- Decision: {review.get('decision')}",
        f"- Report: `{review.get('report_path')}`",
        f"- Result SHA: `{resolved.get('result_sha')}`",
        "",
        "## Checklist",
        "",
    ]
    checklist = review.get("checklist") or {}
    if checklist:
        for key, value in checklist.items():
            lines.append(f"- [{'x' if value else ' '}] {key}")
    else:
        lines.append("- [ ] Changed files reviewed")
        lines.append("- [ ] Submodule results reviewed")
        lines.append("- [ ] Conflicts/errors reviewed")
        lines.append("- [ ] Publish/restore state reviewed")

    lines.extend(["", "## Automatic Risk Flags", ""])
    for risk in review.get("risks") or []:
        lines.append(f"- **{risk.get('level')}** {risk.get('title')}: {risk.get('detail')}")

    if review.get("file_notes"):
        lines.extend(["", "## File Notes", ""])
        for note in review.get("file_notes") or []:
            lines.append(f"- `{note.get('repo_path')}` :: `{note.get('path')}`")
            lines.append(f"  {note.get('note')}")

    lines.extend(["", "## Notes", "", str(review.get("notes") or "N/A"), ""])
    return "\n".join(lines)


def open_path(path: Path) -> None:
    path = path.resolve(strict=False)
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
