from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


BASE_ENV_VAR = "MERGETOOL_PACKAGE_BASE"
BACKUP_ENV_VAR = "MERGETOOL_PACKAGE_BACKUP"
EXE_TIMESTAMP_ENV_VAR = "MERGETOOL_PACKAGE_EXE_TIMESTAMP"
DEFAULT_BASE = Path(__file__).resolve().parent

packages = [
    "manual_merge_split",
    "merge_cleanup_core",
    "merge_tool_adapters",
    "split_gitlink_merge",
    "ci_merge_split",
    "packaging_hooks",
    "assets",
]

scripts = [
    "recursive_submodule_merge_gui.py",
    "gui_command_builders.py",
    "subprocess_window.py",
    "ci_merge_diff2html.py",
    "ci_merge_review.py",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a release bundle zip from the current workspace.")
    parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help=f"source workspace; defaults to ${BASE_ENV_VAR} or {DEFAULT_BASE}",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help=f"backup workspace for old artifacts; defaults to ${BACKUP_ENV_VAR} or <base>_bk",
    )
    parser.add_argument(
        "--exe-timestamp",
        default=None,
        help=(
            "timestamp suffix for dist_manual_exe_<timestamp>; defaults to "
            f"${EXE_TIMESTAMP_ENV_VAR} or the sole local dist directory"
        ),
    )
    args = parser.parse_args()
    args.base = (args.base or Path(os.environ.get(BASE_ENV_VAR, str(DEFAULT_BASE)))).expanduser().resolve()
    args.backup = (
        args.backup
        or (Path(os.environ[BACKUP_ENV_VAR]) if BACKUP_ENV_VAR in os.environ else args.base.parent / f"{args.base.name}_bk")
    ).expanduser().resolve()
    args.exe_timestamp = args.exe_timestamp or os.environ.get(EXE_TIMESTAMP_ENV_VAR)
    return args


def resolve_unambiguous_exe_timestamp(base: Path) -> str | None:
    timestamps = [
        path.name.removeprefix("dist_manual_exe_")
        for path in base.glob("dist_manual_exe_*")
        if path.is_dir() and path.name != "dist_manual_exe_"
    ]
    return timestamps[0] if len(timestamps) == 1 else None


def main() -> int:
    args = parse_args()
    base = args.base
    bk = args.backup
    ts = args.exe_timestamp or resolve_unambiguous_exe_timestamp(base)
    if not ts:
        print(
            "ERROR: executable timestamp is missing or ambiguous. "
            f"Pass --exe-timestamp or set ${EXE_TIMESTAMP_ENV_VAR}.",
            file=sys.stderr,
        )
        return 1

    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"=== PACKAGING (ts={ts}, now={now}) ===")

    staging = base / f"_package_stage_release_bundle_{now}"
    staging.mkdir(parents=True, exist_ok=True)
    print(f"STAGING: {staging}")

    for pkg in packages:
        src = base / pkg
        dst = staging / pkg
        if src.is_dir():
            shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
            print(f"  Copied package: {pkg}")
        else:
            print(f"  SKIP: {pkg} (not found)")

    for s in scripts:
        src = base / s
        if src.exists():
            shutil.copy2(str(src), str(staging / s))
            print(f"  Copied script: {s}")

    exe_src = base / f"dist_manual_exe_{ts}"
    exe_dst = staging / f"dist_manual_exe_{ts}"
    if exe_src.is_dir():
        shutil.copytree(str(exe_src), str(exe_dst), dirs_exist_ok=True)
        print(f"  Copied exe dir: dist_manual_exe_{ts}")
        for exe_name in ["MergeToolManual.exe", "MergeToolManualCli.exe"]:
            exe_path = exe_dst / exe_name
            if exe_path.exists():
                sz = os.path.getsize(str(exe_path))
                print(f"    VERIFIED: {exe_name} ({sz} bytes)")
            else:
                print(f"    MISSING: {exe_name}!!!")
    else:
        print(f"  ERROR: exe dir not found: {exe_src}")

    readme = base / "README.md"
    if readme.exists():
        shutil.copy2(str(readme), str(staging / "README.md"))
        print("  Copied: README.md")

    docs = base / "docs"
    if docs.is_dir():
        shutil.copytree(str(docs), str(staging / "docs"), dirs_exist_ok=True)
        print("  Copied: docs/")

    info = f"""PACKAGING INFO
================
Timestamp: {now}
Source: {base.as_posix()}
Python: 3.14.0
PyInstaller: 6.18.0

Changes:
- Phase 1: code command startup check
- Phase 2: Multi-level cleanup fallback
- Phase 3: Interactive publish dialog
- Phase 4: Config persistence
- Bugfix: safe.directory=*, core.longpaths=true, refs/heads/*
- Bugfix: _safe_write for gbk terminal Unicode errors
- New: manual_merge_split/publish_dialog.py
- New: manual_merge_split/config_persistence.py

Executables:
- dist_manual_exe_{ts}/MergeToolManual.exe (GUI)
- dist_manual_exe_{ts}/MergeToolManualCli.exe (CLI)
"""
    (staging / "PACKAGING_INFO.txt").write_text(info, encoding="utf-8")
    print("  Created: PACKAGING_INFO.txt")

    zip_name = f"MergeTool_manual_CI_bundle_{now}"
    zip_path = base / zip_name
    print(f"\n=== Creating ZIP: {zip_name}.zip ===")
    shutil.make_archive(str(zip_path), "zip", str(staging))
    zip_file = base / f"{zip_name}.zip"
    if zip_file.exists():
        size_mb = os.path.getsize(str(zip_file)) / (1024 * 1024)
        print(f"ZIP CREATED: {zip_file}")
        print(f"Size: {size_mb:.1f} MB")
    else:
        print("ERROR: ZIP not created!")

    print("\n=== CLEANUP: moving old artifacts to backup ===")
    bk.mkdir(parents=True, exist_ok=True)
    keep = [ts, now]

    candidates = list(base.glob("_package_stage_release_bundle_*"))
    candidates += list(base.glob("dist_manual_exe_*"))
    candidates += list(base.glob("build_manual_exe_*"))
    candidates += list(base.glob("build_manual_cli_exe_*"))
    candidates += list(base.glob("build_manual_specs_*"))
    candidates += list(base.glob("MergeTool_manual_CI_bundle_*.zip"))
    candidates += list(base.glob("__pycache__"))

    moved = 0
    for path in candidates:
        if not path.exists():
            continue
        if any(tag in path.name for tag in keep):
            continue
        try:
            dest = bk / path.relative_to(base)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            print(f"  Moved: {path.name}")
            moved += 1
        except Exception as exc:
            print(f"  SKIP: {path.name} ({exc})")

    print(f"Cleaned up: {moved} items")
    print("\n=== DONE ===")
    print(f"ZIP: {zip_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
