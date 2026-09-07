"""Strict manual-merge conflict classification."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Callable

from .models import (
    ClassifiedManualConflict,
    ManualConflictDisposition,
    ManualConflictRecord,
)


def classify_conflict_records(
    repo_path: Path,
    names: list[str],
    records: list[ManualConflictRecord],
    *,
    status_by_path: dict[str, str] | None = None,
    is_binary_path: Callable[[Path, str, tuple[ManualConflictRecord, ...]], bool] | None = None,
) -> dict[str, ClassifiedManualConflict]:
    grouped: dict[str, list[ManualConflictRecord]] = defaultdict(list)
    for record in records:
        grouped[record.path].append(record)

    paths = sorted(set(names) | set(grouped))
    classified: dict[str, ClassifiedManualConflict] = {}
    for path in paths:
        path_records = tuple(sorted(grouped.get(path, []), key=lambda item: (item.stage, item.mode, item.sha)))
        status_code = (status_by_path or {}).get(path, "")
        disposition, reason = evaluate_conflict(
            repo_path,
            path,
            path_records,
            status_code=status_code,
            is_binary_path=is_binary_path,
        )
        classified[path] = ClassifiedManualConflict(
            repo_path=repo_path.resolve(strict=False),
            path=path,
            disposition=disposition,
            records=path_records,
            status_code=status_code,
            reason=reason,
        )
    return classified


TEXT_FILE_MODES = {"100644", "100755"}
GITLINK_MODE = "160000"


def evaluate_conflict(
    repo_path: Path,
    path: str,
    records: tuple[ManualConflictRecord, ...],
    *,
    status_code: str,
    is_binary_path: Callable[[Path, str, tuple[ManualConflictRecord, ...]], bool] | None,
) -> tuple[ManualConflictDisposition, str]:
    """Classify one unmerged path and explain the verdict in one pass.

    Disposition and reason are decided together on purpose: they used to be two
    parallel ladders with slightly different mode sets, so a mixed
    file/gitlink conflict came out as MANUAL_TAKEOVER with an empty reason and
    the pause report showed a path with no explanation.
    """
    stages = ",".join(str(record.stage) for record in records) or "none"
    modes_text = ",".join(sorted({record.mode for record in records})) or "unknown"
    shape = f"(stages={stages}, modes={modes_text})"

    if path == "<unparseable>" or not records:
        return ManualConflictDisposition.FATAL, "unparseable unmerged index entry"

    stage_list = [record.stage for record in records]
    if any(stage not in {1, 2, 3} for stage in stage_list):
        return ManualConflictDisposition.FATAL, f"unmerged stage outside 1-3 {shape}"
    if len(set(stage_list)) != len(stage_list):
        return ManualConflictDisposition.FATAL, f"duplicate index stage detected {shape}"

    modes = {record.mode for record in records}
    stage_set = set(stage_list)
    if modes == {GITLINK_MODE} and {2, 3}.issubset(stage_set):
        return ManualConflictDisposition.SUPPORTED_GITLINK, ""

    if path == ".gitmodules":
        return (
            ManualConflictDisposition.MANUAL_TAKEOVER,
            ".gitmodules conflicts must be resolved manually",
        )
    if status_code and status_code not in {"UU", "AA"}:
        return (
            ManualConflictDisposition.MANUAL_TAKEOVER,
            f"unsupported conflict status {status_code}",
        )
    if not {2, 3}.issubset(stage_set):
        return (
            ManualConflictDisposition.MANUAL_TAKEOVER,
            f"unsupported stage combination {shape}",
        )
    if not modes.issubset(TEXT_FILE_MODES):
        return (
            ManualConflictDisposition.MANUAL_TAKEOVER,
            f"unsupported mode combination {shape}",
        )
    if is_binary_path is not None and is_binary_path(repo_path, path, records):
        return (
            ManualConflictDisposition.MANUAL_TAKEOVER,
            "binary conflict must be resolved manually",
        )
    return ManualConflictDisposition.SUPPORTED_FILE, ""
