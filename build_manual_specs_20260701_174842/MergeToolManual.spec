# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

_base = Path(r"C:\Users\Qs\Documents\MergeTool_for_Manual")

_packages = ["manual_merge_split", "merge_cleanup_core", "merge_tool_adapters",
             "split_gitlink_merge", "ci_merge_split", "packaging_hooks", "assets"]

datas = []
for pkg in _packages:
    src = _base / pkg
    if src.is_dir():
        datas.append((str(src), pkg))

a = Analysis(
    [str(_base / "recursive_submodule_merge_gui.py")],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
        "manual_merge_split", "manual_merge_split.config_persistence",
        "manual_merge_split.publish_dialog",
        "manual_merge_split.publish", "manual_merge_split.commands",
        "manual_merge_split.preflight", "manual_merge_split.repository_ops",
        "manual_merge_split.file_conflicts", "manual_merge_split.common",
        "manual_merge_split.execution", "manual_merge_split.workflow",
        "manual_merge_split.cross_submodule_moves", "manual_merge_split.legacy",
        "manual_merge_split.runtime", "manual_merge_split.models",
        "manual_merge_split.guided_commands", "manual_merge_split.window_icon",
        "merge_cleanup_core", "merge_cleanup_core.steps",
        "merge_tool_adapters", "merge_tool_adapters.ci_auto", "merge_tool_adapters.manual_semiauto",
        "split_gitlink_merge", "split_gitlink_merge.engine", "split_gitlink_merge.executor",
        "split_gitlink_merge.logic", "split_gitlink_merge.model",
        "split_gitlink_merge.planner", "split_gitlink_merge.policy", "split_gitlink_merge.ports",
        "ci_merge_split", "ci_merge_split.commands", "ci_merge_split.common",
        "ci_merge_split.models", "ci_merge_split.preflight", "ci_merge_split.publish",
        "ci_merge_split.recovery", "ci_merge_split.repository_ops",
        "ci_merge_split.runtime", "ci_merge_split.workflow",
        "subprocess_window",
    ],
    hookspath=[str(_base / "packaging_hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="MergeToolManual",
    debug=False,
    strip=False,
    upx=True,
    console=False,
    icon=[str(_base / "assets" / "manual_exe_icon.ico")],
)