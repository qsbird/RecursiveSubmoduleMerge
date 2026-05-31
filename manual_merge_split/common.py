"""Low-coupling helpers split out of the manual recursive merge script."""

from __future__ import annotations

import codecs
import datetime as dt
import difflib
from pathlib import Path, PurePosixPath
from typing import Iterable

from .models import (
    CROSS_SUBMODULE_MOVE_CONTENT_SAMPLE_BYTES,
    WORK_BRANCH_TEMPLATE,
    ConflictEntry,
    ManualSplitError,
    MergeContext,
    TextMergeNormalization,
)


def infer_clone_directory_name(clone_url: str) -> str:
    normalized = clone_url.strip().rstrip("/\\").replace("\\", "/")
    candidate = normalized.rsplit("/", maxsplit=1)[-1]
    if ":" in candidate:
        candidate = candidate.rsplit(":", maxsplit=1)[-1]
    if candidate.lower().endswith(".git"):
        candidate = candidate[:-4]
    candidate = candidate.strip()
    if not candidate or candidate in {".", ".."}:
        raise ManualSplitError(f"Could not infer a repository directory name from clone URL: {clone_url}")
    return candidate


def path_contains_git_repo(path: Path) -> bool:
    return (
        (path / ".git").exists()
        or (
            (path / "HEAD").is_file()
            and (path / "objects").is_dir()
            and (path / "refs").is_dir()
        )
    )


def resolve_clone_target_path(
    clone_url: str,
    clone_path: Path,
    *,
    create_parent: bool = True,
) -> Path:
    selected_path = clone_path.resolve()
    if selected_path.exists() and not selected_path.is_dir():
        raise ManualSplitError(f"Clone path is not a directory: {selected_path}")

    if selected_path.exists():
        if path_contains_git_repo(selected_path):
            raise ManualSplitError(
                "Clone path already points at an existing repository. "
                f"Choose its parent directory or use --repo directly: {selected_path}"
            )
        target = selected_path / infer_clone_directory_name(clone_url)
    else:
        target = selected_path

    if target.exists():
        if path_contains_git_repo(target):
            raise ManualSplitError(f"Clone target repository already exists: {target}")
        if any(target.iterdir()):
            raise ManualSplitError(f"Clone target already exists and is not empty: {target}")
    elif create_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def normalize_status_relative_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    if " -> " in normalized:
        normalized = normalized.split(" -> ", maxsplit=1)[-1].strip()
    return normalized.rstrip("/")


def looks_like_commit_id(value: str) -> bool:
    if len(value) < 7 or len(value) > 40:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value)


def normalize_user_merge_ref(ref: str) -> str:
    normalized = ref.strip()
    if not normalized:
        return normalized
    if normalized.startswith("origin/"):
        return normalized
    if normalized.startswith("refs/") or looks_like_commit_id(normalized):
        return normalized
    return f"origin/{normalized}"


def remote_tracking_ref(ref: str) -> str:
    remote, _, remote_branch = ref.partition("/")
    if not remote_branch:
        return ref
    return f"refs/remotes/{remote}/{remote_branch}"


def parse_branch_tokens(raw_value: str) -> list[str]:
    normalized = raw_value.replace("\r", "\n").replace(",", "\n")
    return [part.strip() for part in normalized.splitlines() if part.strip()]


def normalize_argv(argv: list[str]) -> list[str]:
    if argv and argv[0] in {"merge", "cleanup", "guided", "-h", "--help"}:
        return argv
    return ["merge", *argv]


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def summarize_conflict_entries(entries: Iterable[ConflictEntry]) -> str:
    reasons = [entry.reason.strip() for entry in entries if entry.reason.strip()]
    if not reasons:
        return "no additional reason recorded"
    return "; ".join(dict.fromkeys(reasons))


def can_skip_failed_branch(context: MergeContext, completed: bool) -> bool:
    if completed or context.rollback_requested or not context.report_entries:
        return False
    return all(entry.category == "missing_submodule_commit" for entry in context.report_entries)


def compute_work_branch(work_branch: str | None) -> str:
    return work_branch or WORK_BRANCH_TEMPLATE.format(date=dt.date.today().strftime("%Y%m%d"))


def file_content_similarity(left: bytes, right: bytes) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    if b"\0" in left[:4096] or b"\0" in right[:4096]:
        return 0.0

    left_text = left[:CROSS_SUBMODULE_MOVE_CONTENT_SAMPLE_BYTES].decode(
        "utf-8",
        errors="ignore",
    )
    right_text = right[:CROSS_SUBMODULE_MOVE_CONTENT_SAMPLE_BYTES].decode(
        "utf-8",
        errors="ignore",
    )
    if not left_text or not right_text:
        return 0.0
    return difflib.SequenceMatcher(None, left_text, right_text, autojunk=False).ratio()


def path_name_similarity(left_path: str, right_path: str) -> float:
    left = PurePosixPath(left_path.lower().replace("\\", "/"))
    right = PurePosixPath(right_path.lower().replace("\\", "/"))
    if left.as_posix() == right.as_posix():
        return 1.0
    if left.name == right.name:
        return 0.95
    if left.stem and left.stem == right.stem:
        return 0.80
    if left.suffix and left.suffix == right.suffix:
        return 0.25
    return 0.0


def score_cross_submodule_move_candidate(
    old_file_path: str,
    new_file_path: str,
    old_base_content: bytes,
    new_file_content: bytes,
) -> float:
    content_score = file_content_similarity(old_base_content, new_file_content)
    name_score = path_name_similarity(old_file_path, new_file_path)
    return max(content_score, (content_score * 0.80) + (name_score * 0.20))


def detect_text_newline(content: bytes) -> bytes:
    if b"\r\n" in content:
        return b"\r\n"
    if b"\r" in content:
        return b"\r"
    return b"\n"


def normalize_utf8_text_for_merge(
    content: bytes,
) -> tuple[bytes, TextMergeNormalization] | None:
    if b"\0" in content[:4096]:
        return None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    return (
        normalized_text.encode("utf-8"),
        TextMergeNormalization(
            had_utf8_bom=content.startswith(codecs.BOM_UTF8),
            newline=detect_text_newline(content),
        ),
    )


def prepare_cross_submodule_merge_inputs(
    current_content: bytes,
    base_content: bytes,
    other_content: bytes,
) -> tuple[bytes, bytes, bytes, TextMergeNormalization | None]:
    normalized_values = [
        normalize_utf8_text_for_merge(content)
        for content in (current_content, base_content, other_content)
    ]
    if any(value is None for value in normalized_values):
        return current_content, base_content, other_content, None

    current_normalized, current_metadata = normalized_values[0]
    base_normalized, _base_metadata = normalized_values[1]
    other_normalized, _other_metadata = normalized_values[2]
    return current_normalized, base_normalized, other_normalized, current_metadata


def restore_cross_submodule_merge_output(
    merged_content: bytes,
    normalization: TextMergeNormalization | None,
) -> bytes:
    if normalization is None:
        return merged_content
    restored = merged_content
    if normalization.newline != b"\n":
        restored = restored.replace(b"\n", normalization.newline)
    if normalization.had_utf8_bom and not restored.startswith(codecs.BOM_UTF8):
        restored = codecs.BOM_UTF8 + restored
    return restored
