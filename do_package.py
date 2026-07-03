import os, shutil
from pathlib import Path
from datetime import datetime

base = Path(r"C:\Users\Qs\Documents\MergeTool_for_Manual")
ts = "20260701_174842"
now = datetime.now().strftime("%Y%m%d_%H%M%S")

print(f"=== PACKAGING (ts={ts}, now={now}) ===")

# Step 5: Create staging
staging = base / f"_package_stage_release_bundle_{now}"
staging.mkdir(parents=True, exist_ok=True)
print(f"STAGING: {staging}")

# Copy packages
packages = ["manual_merge_split","merge_cleanup_core","merge_tool_adapters",
            "split_gitlink_merge","ci_merge_split","packaging_hooks","assets"]
for pkg in packages:
    src = base / pkg
    dst = staging / pkg
    if src.is_dir():
        shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
        print(f"  Copied package: {pkg}")
    else:
        print(f"  SKIP: {pkg} (not found)")

# Copy root scripts
scripts = ["recursive_submodule_merge_gui.py","recursive_gitlink_merge_core.py",
           "subprocess_window.py","ci_merge_diff2html.py","ci_merge_mergecore_bridge.py",
           "ci_merge_review.py","ci_merge_sequence_mergecore_bridge.py"]
for s in scripts:
    src = base / s
    if src.exists():
        shutil.copy2(str(src), str(staging / s))
        print(f"  Copied script: {s}")

# Copy exe dir
exe_src = base / f"dist_manual_exe_{ts}"
exe_dst = staging / f"dist_manual_exe_{ts}"
if exe_src.is_dir():
    shutil.copytree(str(exe_src), str(exe_dst), dirs_exist_ok=True)
    print(f"  Copied exe dir: dist_manual_exe_{ts}")
    # Verify exes inside staging
    for exe_name in ["MergeToolManual.exe", "MergeToolManualCli.exe"]:
        exe_path = exe_dst / exe_name
        if exe_path.exists():
            sz = os.path.getsize(str(exe_path))
            print(f"    VERIFIED: {exe_name} ({sz} bytes)")
        else:
            print(f"    MISSING: {exe_name}!!!")
else:
    print(f"  ERROR: exe dir not found: {exe_src}")

# Copy README
readme = base / "README.md"
if readme.exists():
    shutil.copy2(str(readme), str(staging / "README.md"))
    print(f"  Copied: README.md")

# Copy docs
docs = base / "docs"
if docs.is_dir():
    shutil.copytree(str(docs), str(staging / "docs"), dirs_exist_ok=True)
    print(f"  Copied: docs/")

# Step 6: PACKAGING_INFO.txt
info = f"""PACKAGING INFO
================
Timestamp: {now}
Source: C:/Users/Qs/Documents/MergeTool_for_Manual
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

# Step 7: ZIP
zip_name = f"MergeTool_manual_CI_bundle_{now}"
zip_path = base / zip_name
print(f"\n=== Creating ZIP: {zip_name}.zip ===")
shutil.make_archive(str(zip_path), "zip", str(staging))
zip_file = base / f"{zip_name}.zip"
if zip_file.exists():
    size_mb = os.path.getsize(str(zip_file)) / (1024*1024)
    print(f"ZIP CREATED: {zip_file}")
    print(f"Size: {size_mb:.1f} MB")
else:
    print("ERROR: ZIP not created!")

print("\n=== CLEANUP: moving old artifacts to backup ===")
bk = Path(r"C:\Users\Qs\Documents\MergeTool_for_Manual_bk")
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
    except Exception as e:
        print(f"  SKIP: {path.name} ({e})")

print(f"Cleaned up: {moved} items")
print("\n=== DONE ===")
print(f"ZIP: C:\\Users\\Qs\\Documents\\MergeTool_for_Manual\\{zip_name}.zip")