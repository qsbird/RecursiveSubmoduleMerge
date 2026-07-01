"""Manual merge branch naming and commit-message helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ManualCommitMessage:
    title: str
    body: str = ""

    def git_args(self) -> list[str]:
        args = ["-m", self.title]
        if self.body.strip():
            args.extend(["-m", self.body.strip()])
        return args


def short_sha(value: str | None, length: int = 12) -> str:
    text = str(value or "").strip()
    return text[:length] if text else "unknown"


def value_or_na(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "N/A"


def compact_label(value: str | None, *, max_chars: int = 48, fallback: str = "unknown") -> str:
    text = str(value or "").strip() or fallback
    text = text.replace("refs/remotes/", "").replace("refs/heads/", "")
    if len(text) <= max_chars:
        return text
    keep = max_chars - 3
    left = max(1, keep // 2)
    right = keep - left
    return f"{text[:left]}...{text[-right:]}"


def commit_body(fields: Iterable[tuple[str, Any]]) -> str:
    return "\n".join(f"{name}: {value_or_na(value)}" for name, value in fields)


def join_display_path(parent: str, child: str) -> str:
    normalized_child = child.replace("\\", "/").strip("/")
    if parent in {"", "."}:
        return normalized_child or "."
    return f"{parent.rstrip('/')}/{normalized_child}"


def manual_merge_title(
    source_ref: str | None,
    target_ref: str | None,
    source_sha: str | None,
    target_sha: str | None,
) -> str:
    source_label = compact_label(source_ref, fallback=short_sha(source_sha))
    target_label = compact_label(target_ref, fallback=short_sha(target_sha))
    title = f"Manual merge: {source_label} into {target_label}"
    if len(title) > 100:
        title = f"Manual merge: {short_sha(source_sha)} into {short_sha(target_sha)}"
    return title


def submodule_merge_title(display_path: str, source_sha: str | None, target_sha: str | None) -> str:
    repo_label = compact_label(display_path, max_chars=68, fallback=short_sha(target_sha))
    title = f"Manual submodule merge: {repo_label}"
    if len(title) > 100:
        title = f"Manual submodule merge: {short_sha(source_sha)} into {short_sha(target_sha)}"
    return title


def conflict_path_lines(paths: Iterable[str]) -> list[str]:
    unique_paths: list[str] = []
    for path in paths:
        normalized = str(path or "").strip()
        if normalized and normalized not in unique_paths:
            unique_paths.append(normalized)
    return [f"    {path}" for path in unique_paths]


def build_root_commit_message(
    *,
    repo_path: Path,
    source_ref: str,
    target_ref: str,
    source_sha: str | None,
    target_sha: str | None,
    work_branch: str,
    resolved_submodules: Iterable[str] = (),
) -> ManualCommitMessage:
    fields: list[tuple[str, Any]] = [
        ("Source", source_ref),
        ("Source SHA", source_sha),
        ("Target", target_ref),
        ("Target SHA", target_sha),
        ("Work Branch", work_branch),
        ("Repository", repo_path),
    ]
    body = commit_body(fields)
    submodule_lines = conflict_path_lines(resolved_submodules)
    if submodule_lines:
        body += "\n\nResolved submodules:\n"
        body += "\n".join(submodule_lines)
    return ManualCommitMessage(
        title=manual_merge_title(source_ref, target_ref, source_sha, target_sha),
        body=body,
    )


def build_submodule_commit_message(
    *,
    repo_path: Path,
    parent_repo: Path | None,
    display_path: str,
    source_ref: str,
    target_ref: str,
    source_sha: str | None,
    target_sha: str | None,
    base_sha: str | None,
    work_branch: str,
    resolved_submodules: Iterable[str] = (),
) -> ManualCommitMessage:
    parent_title = manual_merge_title(source_ref, target_ref, source_sha, target_sha)
    fields: list[tuple[str, Any]] = [
        ("Parent Merge", parent_title),
        ("Parent Repository", parent_repo),
        ("Submodule Path", display_path),
        ("Repository", repo_path),
        ("Base SHA", base_sha),
        ("Ours SHA", target_sha),
        ("Theirs SHA", source_sha),
        ("Work Branch", work_branch),
    ]
    body = commit_body(fields)
    submodule_lines = conflict_path_lines(resolved_submodules)
    if submodule_lines:
        body += "\n\nResolved nested submodules:\n"
        body += "\n".join(submodule_lines)
    return ManualCommitMessage(
        title=submodule_merge_title(display_path, source_sha, target_sha),
        body=body,
    )
