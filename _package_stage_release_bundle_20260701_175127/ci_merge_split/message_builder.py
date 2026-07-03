"""CI branch naming and commit-message implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .common import sanitize_ref_component


@dataclass(frozen=True)
class CommitMessage:
    title: str
    body: str

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


def root_commit_ref_label(input_ref: str | None, resolved_ref: str | None, sha: str) -> str:
    return compact_label(input_ref or resolved_ref, fallback=short_sha(sha))


def root_merge_title(ctx: Any, source_sha: str | None = None, target_sha: str | None = None) -> str:
    step = getattr(ctx, "merge_step", None)
    if step:
        resolved_source_sha = str(source_sha or getattr(step, "source_sha", None) or "")
        resolved_target_sha = str(target_sha or getattr(step, "target_sha", None) or "")
        source_label = root_commit_ref_label(getattr(step, "source", None), getattr(step, "source_ref", None), resolved_source_sha)
        target_label = root_commit_ref_label(getattr(step, "target", None), getattr(step, "target_ref", None), resolved_target_sha)
    else:
        report = getattr(ctx, "report", None)
        resolved = getattr(report, "data", {}).get("resolved") if report is not None else {}
        resolved = resolved or {}
        args = getattr(ctx, "args", None)
        source_ref = resolved.get("source_ref")
        target_ref = resolved.get("target_ref")
        resolved_source_sha = str(resolved.get("source_sha") or source_sha or "")
        resolved_target_sha = str(resolved.get("target_sha") or target_sha or "")
        source_label = root_commit_ref_label(getattr(args, "source", None), source_ref, resolved_source_sha)
        target_label = root_commit_ref_label(getattr(args, "target", None), target_ref, resolved_target_sha)
    title = f"CI merge: {source_label} into {target_label}"
    if len(title) > 100:
        title = f"CI merge: {short_sha(resolved_source_sha)} into {short_sha(resolved_target_sha)}"
    return title


def conflict_path_lines(resolved_gitlinks: list[dict[str, Any]] | None) -> list[str]:
    paths: list[str] = []
    for conflict in resolved_gitlinks or []:
        path = str(conflict.get("path") or "").strip()
        if path and path not in paths:
            paths.append(path)
    return [f"    {path}" for path in paths]


def branch_for_depth(ctx: Any, depth: int) -> str:
    if depth == 0:
        return getattr(getattr(ctx, "args", None), "work_branch")
    job = sanitize_ref_component(getattr(ctx, "job_id", "job"))
    return f"ci-submodule-merge/{job}-d{depth}"


def build_merge_commit_message(
    ctx: Any,
    *,
    repo_path: Path,
    display_path: str,
    source_sha: str,
    target_sha: str,
    base_sha: str | None,
    depth: int,
    strategy: str,
    gitlink_context: Any | None = None,
    resolved_gitlinks: list[dict[str, Any]] | None = None,
) -> CommitMessage:
    del repo_path, base_sha, strategy
    if depth == 0:
        step = getattr(ctx, "merge_step", None)
        args = getattr(ctx, "args", None)
        source_value = getattr(step, "source", None) if step else getattr(args, "source", None)
        target_value = getattr(step, "target", None) if step else getattr(args, "target", None)
        fields: list[tuple[str, Any]] = [
            ("Source", source_value),
            ("Source SHA", source_sha),
            ("Target", target_value),
            ("Target SHA", target_sha),
        ]
        if step and getattr(step, "total", 0) > 1:
            fields.insert(0, ("Batch Step", f"{step.index}/{step.total}"))
        title = root_merge_title(ctx, source_sha, target_sha)
        body = commit_body(fields)
        if resolved_gitlinks:
            body += "\n\nConflicts:\n"
            body += "\n".join(conflict_path_lines(resolved_gitlinks))
        return CommitMessage(title, body)

    repo_label = compact_label(display_path, max_chars=44, fallback=short_sha(target_sha))
    title = f"Parent conflict: {root_merge_title(ctx)}"
    if len(title) > 100:
        title = f"Parent conflict: {compact_label(root_merge_title(ctx), max_chars=83)}"
    body_lines = [
        f"Submodule path: {repo_label}",
    ]
    if gitlink_context:
        body_lines.extend(
            [
                f"Conflict base SHA: {gitlink_context.base_sha}",
                f"Ours SHA: {gitlink_context.ours_sha}",
                f"Theirs SHA: {gitlink_context.theirs_sha}",
            ]
        )
    else:
        body_lines.extend(
            [
                f"Ours SHA: {target_sha}",
                f"Theirs SHA: {source_sha}",
            ]
        )
    conflicts = conflict_path_lines(resolved_gitlinks)
    if conflicts:
        body_lines.append("")
        body_lines.append("Conflicts:")
        body_lines.extend(conflicts)
    return CommitMessage(title, "\n".join(body_lines))

