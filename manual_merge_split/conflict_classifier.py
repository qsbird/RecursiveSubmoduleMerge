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
        classified[path] = ClassifiedManualConflict(
            repo_path=repo_path.resolve(strict=False),
            path=path,
            disposition=_classify_one(
                repo_path,
                path,
                path_records,
                status_code=status_code,
                is_binary_path=is_binary_path,
            ),
            records=path_records,
            status_code=status_code,
            reason=_reason_for(path, path_records, status_code=status_code, is_binary_path=is_binary_path, repo_path=repo_path),
        )
    return classified


def _classify_one(
    repo_path: Path,
    path: str,
    records: tuple[ManualConflictRecord, ...],
    *,
    status_code: str,
    is_binary_path: Callable[[Path, str, tuple[ManualConflictRecord, ...]], bool] | None,
) -> ManualConflictDisposition:
    if path == "<unparseable>" or not records:
        return ManualConflictDisposition.FATAL

    stages = [record.stage for record in records]
    if any(stage not in {1, 2, 3} for stage in stages):
        return ManualConflictDisposition.FATAL
    if len(set(stages)) != len(stages):
        return ManualConflictDisposition.FATAL

    modes = {record.mode for record in records}
    stage_set = set(stages)
    if modes == {"160000"} and {2, 3}.issubset(stage_set):
        return ManualConflictDisposition.SUPPORTED_GITLINK

    if path == ".gitmodules":
        return ManualConflictDisposition.MANUAL_TAKEOVER
    if status_code and status_code not in {"UU", "AA"}:
        return ManualConflictDisposition.MANUAL_TAKEOVER
    if not {2, 3}.issubset(stage_set):
        return ManualConflictDisposition.MANUAL_TAKEOVER
    if not modes.issubset({"100644", "100755"}):
        return ManualConflictDisposition.MANUAL_TAKEOVER
    if is_binary_path is not None and is_binary_path(repo_path, path, records):
        return ManualConflictDisposition.MANUAL_TAKEOVER
    return ManualConflictDisposition.SUPPORTED_FILE


def _reason_for(
    path: str,
    records: tuple[ManualConflictRecord, ...],
    *,
    status_code: str,
    is_binary_path: Callable[[Path, str, tuple[ManualConflictRecord, ...]], bool] | None,
    repo_path: Path,
) -> str:
    stages = ",".join(str(record.stage) for record in records) or "none"
    modes = ",".join(sorted({record.mode for record in records})) or "unknown"
    if path == "<unparseable>" or not records:
        return "unparseable unmerged index entry"
    if len({record.stage for record in records}) != len(records):
        return f"duplicate index stage detected (stages={stages}, modes={modes})"
    if path == ".gitmodules":
        return ".gitmodules conflicts must be resolved manually"
    if status_code and status_code not in {"UU", "AA"}:
        return f"unsupported conflict status {status_code}"
    if not {2, 3}.issubset({record.stage for record in records}):
        return f"unsupported stage combination (stages={stages}, modes={modes})"
    if not {record.mode for record in records}.issubset({"100644", "100755", "160000"}):
        return f"unsupported mode combination (stages={stages}, modes={modes})"
    if is_binary_path is not None and is_binary_path(repo_path, path, records):
        return "binary conflict must be resolved manually"
    return ""
