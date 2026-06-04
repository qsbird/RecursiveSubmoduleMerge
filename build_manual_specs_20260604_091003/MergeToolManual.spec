# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['..\\recursive_submodule_merge_gui.py'],
    pathex=['C:\\Users\\Qs\\Documents\\MergeTool_for_Manual'],
    binaries=[('C:\\Users\\Qs\\AppData\\Local\\Python\\pythoncore-3.14-64\\DLLs\\_tkinter.pyd', '.'), ('C:\\Users\\Qs\\AppData\\Local\\Python\\pythoncore-3.14-64\\DLLs\\tcl86t.dll', '.'), ('C:\\Users\\Qs\\AppData\\Local\\Python\\pythoncore-3.14-64\\DLLs\\tk86t.dll', '.')],
    datas=[('C:\\Users\\Qs\\AppData\\Local\\Python\\pythoncore-3.14-64\\tcl\\tcl8.6', '_tcl_data'), ('C:\\Users\\Qs\\AppData\\Local\\Python\\pythoncore-3.14-64\\tcl\\tk8.6', '_tk_data'), ('C:\\Users\\Qs\\AppData\\Local\\Python\\pythoncore-3.14-64\\tcl\\tcl8', 'tcl8'), ('C:\\Users\\Qs\\AppData\\Local\\Python\\pythoncore-3.14-64\\tcl\\dde1.4', 'dde1.4'), ('C:\\Users\\Qs\\AppData\\Local\\Python\\pythoncore-3.14-64\\tcl\\reg1.3', 'reg1.3')],
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox', '_tkinter'],
    hookspath=['C:\\Users\\Qs\\Documents\\MergeTool_for_Manual\\packaging_hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MergeToolManual',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\Qs\\Documents\\MergeTool_for_Manual\\assets\\manual_exe_icon.ico'],
)
