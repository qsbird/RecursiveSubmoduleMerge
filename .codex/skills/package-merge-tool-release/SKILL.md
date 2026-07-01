---
name: package-merge-tool-release
description: 'Package the local MergeTool_for_Manual release only for the repository at C:\Users\Qs\Documents\MergeTool_for_Manual. Use when the user asks to package, build a release zip, rebuild the GUI/CLI exe bundle, create MergeTool_manual_CI_bundle, or says 打包/发布包 for this project. Do not use for any other directory or repository.'
---

# Package Merge Tool Release

## Scope Guard

Use this skill only from `C:\Users\Qs\Documents\MergeTool_for_Manual`.

Before packaging, verify the current directory is this project by checking these files/directories exist:

- `recursive_submodule_merge_gui.py`
- `manual_merge_split/`
- `ci_merge_split/`
- `packaging_hooks/`
- `assets/manual_exe_icon.ico`

If the working directory is different, stop and explain that this skill is intentionally repository-local.

## Workflow

1. Inspect local changes with `git -c safe.directory=C:/Users/Qs/Documents/MergeTool_for_Manual status --short`. Do not revert unrelated user changes.
2. Run focused validation before packaging:
   - `python -m py_compile recursive_submodule_merge_gui.py recursive_gitlink_merge_core.py subprocess_window.py`
   - `python -m unittest discover -s tests_split_gitlink_merge`
3. Build fresh GUI and CLI executables with PyInstaller, using timestamped build/dist/spec directories. Prefer the Python environment that already has PyInstaller and Tkinter available. If a command is blocked by sandboxing or needs the user's Python installation, request escalation instead of silently falling back to stale executables.
4. Create a timestamped staging directory named `_package_stage_release_bundle_<timestamp>` or similar.
5. Copy source modules and runtime assets into the staging directory:
   - root scripts: `recursive_submodule_merge_gui.py`, `recursive_gitlink_merge_core.py`, `subprocess_window.py`, bridge scripts
   - packages: `manual_merge_split/`, `ci_merge_split/`, `split_gitlink_merge/`, `merge_tool_adapters/`, `merge_cleanup_core/`
   - `assets/`, `packaging_hooks/`, `README.md`, `docs/` when present
   - fresh exe directory containing `MergeToolManual.exe` and `MergeToolManualCli.exe`
6. Create `PACKAGING_INFO.txt` in the staging directory with timestamp, source path, validation results, exe paths, and smoke-check results.
7. Zip the staging directory to `MergeTool_manual_CI_bundle_<timestamp>.zip`.
8. After the new bundle is created, run release housekeeping:
   - Use `C:\Users\Qs\Documents\MergeTool_for_Manual_bk` as the backup root.
   - Move older release artifacts into the backup root instead of deleting them. Older artifacts include previous `MergeTool_manual_CI_bundle_*.zip`, `MergeTool_manual_CI_with_exe_structured_*.zip`, `_package_stage*`, `dist_manual_exe*`, `build_manual_exe*`, `build_manual_cli_exe*`, and timestamped `build_manual_specs_*` directories. Do not move the freshly created timestamp's bundle/staging/dist/build/spec directories.
   - Skip any path that contains tracked Git files. Never move `.git`, `.codex`, `.codegraph`, source packages, `README.md`, or `docs/`.
   - Clear packaging caches by moving cache directories into the same backup root rather than permanently deleting them. Cache candidates include `.pytest_cache`, `__pycache__` directories, `.build_deps`, and PyInstaller `build`/`dist` leftovers that are not part of the fresh timestamp.
   - If a file is locked or permission-restricted, move everything else and report the remaining path explicitly.

## Smoke Checks

After building, run these checks when possible:

- `MergeToolManual.exe --smoke-gui`
- `MergeToolManualCli.exe --help`
- `MergeToolManualCli.exe --cli --help`

If smoke checks cannot run in the sandbox, report that clearly and include the exact command that was not run.

## Notes

- For networked dependency installs, obey the project proxy instructions from `AGENTS.md`.
- Do not use an older `dist_manual_exe_*` directory unless the user explicitly asks for a source-only or reuse-existing-exe package.
- If PyInstaller is unavailable, stop with a clear message that packaging cannot produce fresh executables until PyInstaller is available.
