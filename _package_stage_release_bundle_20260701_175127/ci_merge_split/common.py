"""Low-coupling CI helpers split out of the original CI merge script."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import CiMergeError, ExitCode, Phase, now_iso


TRUE_VALUES = {"1", "true", "t", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "f", "no", "n", "off"}
CONFLICT_STAGE_LABELS = {"1": "base", "2": "ours", "3": "theirs"}


def milliseconds(start: float, end: float) -> int:
    return int((end - start) * 1000)


def elapsed_ms(started: float) -> int:
    return milliseconds(started, time.perf_counter())


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value!r}")


def sanitize_ref_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._/-]+", "-", value.strip())
    cleaned = cleaned.strip("/.")
    return cleaned or "job"


def short_ref_token(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def default_job_id() -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{os.getpid()}"


def resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def is_path_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def split_lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]


def stringify_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def bytes_process_output(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", errors="replace")


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "REPORT_READ_FAILED",
            "cannot read JSON report",
            details={"path": str(path), "message": str(exc)},
            restore_needed=False,
        ) from exc
    except json.JSONDecodeError as exc:
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "REPORT_JSON_INVALID",
            "JSON report cannot be parsed",
            details={"path": str(path), "message": str(exc)},
            restore_needed=False,
        ) from exc
    if not isinstance(data, dict):
        raise CiMergeError(
            ExitCode.ARGUMENT_ERROR,
            Phase.INIT,
            "REPORT_JSON_INVALID",
            "JSON report root must be an object",
            details={"path": str(path), "type": type(data).__name__},
            restore_needed=False,
        )
    return data


def parse_unmerged_index(stdout: str) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for line in split_lines(stdout):
        parts = line.split(None, 3)
        if len(parts) != 4:
            grouped.setdefault("<unparseable>", []).append({"raw": line})
            continue
        mode, sha, stage, path = parts
        grouped.setdefault(path, []).append(
            {
                "mode": mode,
                "sha": sha,
                "stage": stage,
                "raw": line,
            }
        )
    return grouped


def stage_sha(conflict: dict[str, Any], stage_number: str) -> str | None:
    matches = [
        stage.get("sha")
        for stage in conflict.get("stages", [])
        if str(stage.get("stage")) == stage_number and stage.get("sha")
    ]
    unique = sorted(set(matches))
    if len(unique) != 1:
        return None
    return unique[0]


def parse_commit_log(stdout: str) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for line in split_lines(stdout):
        parts = line.split("\t", 4)
        if len(parts) != 5:
            commits.append({"raw": line})
            continue
        sha, author, email, date, subject = parts
        commits.append(
            {
                "sha": sha,
                "author": author,
                "email": email,
                "date": date,
                "subject": subject,
            }
        )
    return commits


def parse_name_status(stdout: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for line in split_lines(stdout):
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0]
        if len(parts) == 3:
            files.append({"status": status, "old_path": parts[1], "path": parts[2]})
        elif len(parts) >= 2:
            files.append({"status": status, "path": parts[1]})
        else:
            files.append({"status": status})
    return files


def sanitize_patch_name(value: str, fallback: str) -> str:
    normalized = value.replace("\\", "/").strip("/").strip()
    if normalized in {"", "."}:
        normalized = fallback
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized)
    cleaned = cleaned.strip("._-")
    return (cleaned or fallback)[:120]
