# Merge Core Adaptation Plan

This plan is intentionally preserved as a new file. Existing source files in
`D:\Git\MergeTool_for_CICD` and `D:\Git\PythonTool` are not modified by this
adaptation pass.

## Goal

Use one split minimal merge core for two workflows:

- CI automatic merge behavior, modeled after `D:\Git\MergeTool_for_CICD\ci_merge.py`.
- Manual/semi-automatic merge behavior, modeled after
  `D:\Git\PythonTool\recursive_submodule_merge.py` and the local manual script.

## New Structure

- `split_gitlink_merge/`
  - `model.py`: shared data contracts.
  - `planner.py`: pure unmerged-index parsing and gitlink decision logic.
  - `logic.py`: pure domain logic for conflict grouping, gitlink stage
    extraction, ancestry lookup gating, and recursive-merge context building.
  - `ports.py`: query/mutation execution protocols used by engine and policy.
  - `executor.py`: concrete Git command execution layer implementing the ports.
  - `policy.py`: behavior hooks for CI/manual differences over execution ports.
  - `engine.py`: orchestration over planner, logic, execution ports, and policy.
- `merge_tool_adapters/`
  - `ci_auto.py`: CI-oriented adapter and policy.
  - `manual_semiauto.py`: manual/semi-automatic adapter and policy.
- `ci_merge_split/`
  - `models.py`: CI exit codes, phases, structured errors, command result,
    preflight/conflict/merge result data contracts.
  - `common.py`: CI low-coupling helpers for refs, paths, process output,
    JSON loading, unmerged-index parsing, commit logs, name-status output, and
    patch names.
  - `artifacts.py`: CI conflict artifacts, patch exports, changed-file content
    artifacts, conflict analysis, and repository detail collection.
  - `report_artifacts.py`: CI JSON report, conflict artifact, patch artifact,
    and repository detail boundary.
  - `repository_ops.py`: CI repository query and guard helpers that execute
    through `ctx.runner`.
  - `recovery.py`: CI cleanup fallback levels, snapshots, and restore logic.
  - `preflight.py`: CI remote sync, preflight checks, and preflight report
    recording.
  - `repository_guards.py`: CI preflight, cleanup, and post-merge guard
    boundary.
  - `snapshot_restore.py`: CI snapshot and restore boundary.
  - `publish.py`: CI publish and publish-only boundary.
  - `messages.py`: CI branch naming and commit-message boundary.
  - `workflow.py`: composition root for the split CI services.
- `manual_merge_split/`
  - `models.py`: manual workflow constants, errors, result structures,
    conflict/report entries, snapshots, and cross-submodule move records.
  - `common.py`: manual low-coupling helpers for clone paths, refs, branch
    tokens, conflict summaries, text normalization, and moved-file scoring.
  - `preflight.py`: manual dirty-submodule and branch/ref preflight boundary.
  - `file_conflicts.py`: manual file conflict and conflict-tool boundary.
  - `cross_submodule_moves.py`: cross-submodule moved-file detection and
    content-merge boundary.
  - `repository_ops.py`: manual Git/repository operation boundary.
  - `guided_commands.py`: guided CLI prompt/command boundary.
  - `workflow.py`: composition root for the split manual services.
- `ci_merge_mergecore_bridge.py`: validation-only bridge that runs the original
  `ci_merge.py` workflow with `merge_repo` routed through the mergecore CI
  adapter.
- `ci_merge_sequence_mergecore_bridge.py`: validation-only sequence bridge that
  routes each `ci_merge_sequence.py` step through the mergecore CI bridge.
- `tests_split_gitlink_merge/`
  - Tests for the split core and adapters.

## CI Adapter Plan

`CiAutoGitlinkMergeService` keeps CI workflow responsibilities outside the core:

1. Checkout a CI work branch from target SHA.
2. Update submodules with `--force`.
3. Run `git merge --no-ff --no-commit <source_sha>`.
4. If the merge is clean, validate and commit.
5. If conflicts are pure gitlinks, call `RecursiveGitlinkMergeEngine`.
6. If conflicts are mixed or file-only, fail through CI policy.
7. Reuse CI hooks where available:
   - `record_snapshot`
   - `branch_for_depth`
   - `clean_and_prepare_repo`
   - `build_merge_commit_message`
   - `ensure_no_unmerged_paths`
   - `ensure_gitmodules_not_changed`
   - `ensure_no_submodule_topology_change`
   - `collect_repository_details`

## Manual Adapter Plan

`ManualSemiAutoMergeService` keeps manual workflow responsibilities outside the
core:

1. Optionally run `git merge --no-ff --no-commit <ref>`.
2. Call `RecursiveGitlinkMergeEngine` for recursive gitlinks.
3. Use `git mergetool` for file conflicts through `ManualMergePolicy`.
4. Commit root merge with `git commit --no-edit`.
5. Run final `git submodule update --init --recursive`.

## Further Modularization Notes

Beyond merge core, these original areas are good candidates for later split:

- CI report/artifact writing from `ci_merge.py`.
- CI repository guards: no `.gitmodules` change and no submodule topology change.
- CI snapshot/restore logic.
- CI publish logic.
- Manual dirty-submodule preflight.
- Manual cross-submodule move detection and file-content merge helpers.
- Manual GUI and CLI command construction.

Recommended next module boundaries:

- `ci_report_artifacts`: JSON report updates, conflict stage export, patch export.
- `ci_repository_guards`: unmerged-path, `.gitmodules`, and submodule topology checks.
- `ci_snapshot_restore`: snapshot capture, restore, cleanup fallback.
- `ci_publish`: publish-only and publish-after-merge flows.
- `manual_preflight`: dirty-submodule and branch/ref safety checks.
- `manual_file_conflicts`: mergetool invocation, marker checks, moved-file helpers.
- `manual_cross_submodule_moves`: move candidate scoring and content merge.
- `manual_gui_commands`: command construction shared by GUI/workflow launchers.

First remaining-split pass:

- The module boundaries above now exist as compatibility facades.
- The facades fail fast when the expected original function is missing.
- `merge_tool_adapters.ci_auto` and `ci_merge_mergecore_bridge.py` now use the
  CI service composition root for report/artifacts, guards, snapshots, and
  commit-message hooks instead of reaching directly into unrelated pieces of
  `ci_merge.py`.
- This keeps behavior unchanged while making the next migration step mechanical:
  each facade can absorb the original implementation one responsibility at a
  time, without changing the adapter/bridge call sites again.

Second remaining-split pass:

- CI models and several low-coupling helpers now live in `ci_merge_split`
  instead of requiring the original `ci_merge.py` module:
  - structured error/report model shapes;
  - process/path/ref helpers;
  - unmerged-index parsing and `stage_sha`;
  - branch naming and commit-message construction;
  - publish failure classification;
  - repository readiness and post-merge guard checks.
- Manual models and several low-coupling helpers now live in
  `manual_merge_split` instead of requiring the original
  `recursive_submodule_merge.py` module:
  - workflow dataclasses and constants;
  - clone/ref/branch-token helpers;
  - moved-file scoring and text-merge normalization;
  - conflict-tool command rendering;
  - common Git repository operations through `ManualRepositoryOps`.
- The compatibility facades now require fewer functions from the original
  scripts.  Complex flows with high rollback/recovery surface still delegate to
  the original modules until they can be split and verified independently.

Third remaining-split pass:

- CI report/artifact behavior was moved from facade delegation into
  `ci_merge_split.artifacts`:
  - binary/JSON artifact writing;
  - conflict stage/worktree/metadata artifact export;
  - conflict analysis;
  - repository and per-file patch export;
  - changed-file content export;
  - repository detail collection.
- Manual file/move helpers were moved from facade delegation into split
  modules:
  - file-conflict report entry creation;
  - moved-away file conflict auto-resolution;
  - cross-submodule move description;
  - `git merge-file` content merge for moved files;
  - applying cross-submodule moved-file merges.
- At this point, manual cross-submodule move candidate discovery still
  delegated to the original script because it touched several Git history query
  helpers.  It was moved in the sixth remaining-split pass below.

Fourth remaining-split pass:

- CI recovery behavior was moved from facade delegation into
  `ci_merge_split.recovery`:
  - cleanup fallback levels;
  - snapshot capture;
  - single-repository restore;
  - all-snapshot restore.
- CI publish behavior was moved from facade delegation into
  `ci_merge_split.publish` for the normal publish path:
  - publish log entries;
  - submodule candidate ref calculation;
  - submodule publish candidate discovery and push;
  - publish command wrapper and remote-ref verification;
  - root publish with force-with-lease validation.
- At this point, `run_publish_only_workflow` still delegated to the original
  script because it depended on report reverse-loading, partial batch publish
  metadata, and several orchestration helpers.  It was moved in the sixth
  remaining-split pass below.

Fifth remaining-split pass:

- CI preflight/sync behavior was moved from facade delegation into
  `ci_merge_split.preflight`:
  - remote fetch and recursive submodule sync/fetch;
  - repository existence/worktree/cleanliness checks;
  - operation marker checks;
  - `.gitmodules` and submodule readiness checks;
  - source/target ref resolution and merge-base check;
  - preflight report recording and failure reason mapping.
- Manual preflight low-coupling helpers were moved into
  `manual_merge_split.preflight`:
  - submodule lineage collection;
  - local revision/ref variant resolution;
  - dirty submodule impact assessment;
  - dirty submodule list formatting;
  - preserved dirty path map building.
- At this point, manual `preflight_repository` still delegated because it
  coordinated refresh/fetch and dirty submodule safety across the whole manual
  merge entry flow.  It was moved in the sixth remaining-split pass below.

Sixth remaining-split pass:

- CI publish-only behavior was moved from facade delegation into
  `ci_merge_split.publish`:
  - explicit publish-target validation;
  - nested report value lookup;
  - publish target inference from prior reports;
  - publish-only report loading, including partial-success validation;
  - publish-only workflow execution and success reporting.
- `ci_merge_mergecore_bridge.py` now also routes
  `run_publish_only_workflow` through `CiWorkflowServices.publish`, so the
  original CI full matrix exercises the split publish-only implementation.
- Manual repository recovery behavior was moved into
  `manual_merge_split.repository_ops`:
  - rollback to a captured snapshot;
  - recursive cleanup with merge abort/reset fallback;
  - recursive snapshot restore and root submodule refresh.
- Manual cross-submodule move discovery was moved into
  `manual_merge_split.cross_submodule_moves`:
  - unmerged submodule stage lookup;
  - merge-head/base lookup;
  - candidate submodule lineage expansion;
  - deleted/added file scanning and similarity-based move selection.
- Manual file conflict handling was moved into
  `manual_merge_split.file_conflicts`:
  - single-file mergetool launch;
  - timeout handling;
  - marker check;
  - unresolved-path recheck;
  - batch file-conflict resolution.
- Manual preflight orchestration was moved into `manual_merge_split.preflight`:
  - repository ready checks;
  - recursive submodule sync/init/fetch;
  - root direct-change guard;
  - dirty submodule allow/block classification.
- Manual guided prompt and `run_guided` construction were moved into
  `manual_merge_split.guided_commands`.  The final `run_merge`/`run_cleanup`
  calls remain execution/workflow ports to the existing script for now.
- Remaining original-module dependencies are now explicit execution or
  orchestration ports rather than the logic chunks split in this pass:
  `run_git`, `run_git_binary`, `run_merge`, `run_cleanup`, `set_verbose`, and
  the CI adapter's `display_path_for` compatibility helper.

Seventh remaining-split pass:

- Manual command orchestration was moved into `manual_merge_split.commands`:
  - clone-before-merge command construction;
  - dirty-submodule prompt/warning helpers;
  - conflict report writing;
  - recursive `process_repository`;
  - work-branch rebuild after cleanup;
  - merge context construction;
  - top-level `run_merge`;
  - top-level `run_cleanup`.
- `ManualWorkflowServices` now exposes `ManualCommandService`, and
  `ManualGuidedCommandService` calls local command-service methods instead of
  requiring the original script's `run_merge`, `run_cleanup`, or `set_verbose`.
- `merge_tool_adapters.ci_auto` now uses the local
  `ci_merge_split.repository_ops.display_path_for` helper instead of the
  original CI module's helper.
- Remaining original-module dependencies are execution ports only:
  `run_git` and `run_git_binary`, plus original dataclass/error classes when a
  service is asked to preserve exact original exception/result shapes.

Eighth remaining-split pass:

- Manual Git execution was moved into `manual_merge_split.execution`:
  - text Git command execution;
  - binary Git command execution;
  - locale-aware output decoding;
  - streamed command output;
  - verbose command logging;
  - Git failure formatting.
- Manual services now have local defaults and no longer require the original
  manual script for `run_git` or `run_git_binary`:
  - `ManualRepositoryOps`;
  - `ManualFileConflictService`;
  - `CrossSubmoduleMoveService`;
  - `ManualCommandService`.
- `ManualWorkflowServices.from_local()` was added as a composition root that
  does not load `D:\Git\PythonTool\recursive_submodule_merge.py`.
- The original manual script is now used by this split workspace only for
  compatibility/parity loading through `manual_merge_split.legacy`, not as a
  required runtime provider.

Ninth remaining-split pass:

- CI top-level orchestration was moved into `ci_merge_split.commands`:
  - resolved-field updates;
  - batch artifact scopes and command ranges;
  - batch continue-state restore;
  - manual publish command metadata;
  - preflight failure helpers;
  - single-source workflow;
  - batch workflow;
  - publish-only/cleanup/sync routing;
  - failure handling and `main`.
- `CiCommandService` was added as a composition facade, and the default
  command path uses `CiWorkflowServices.from_local()` plus the mergecore CI
  adapter without requiring the original `ci_merge.py` module.
- The mergecore CI adapter now uses local CI services when it receives a
  runtime context but no original CI module.
- Manual CLI parsing and entry routing were moved into
  `manual_merge_split.runtime`, including parser construction, normalized
  default `merge` command handling, and `main` dispatch to merge/cleanup/guided
  command services.
- Both split packages now have module entry points:
  - `python -m ci_merge_split`
  - `python -m manual_merge_split`

## Logic/Execution Split

Current split after the second pass:

- Logic layer:
  - `model.py`
  - `planner.py`
  - `logic.py`
- Execution layer:
  - `ports.py`
  - `executor.py`
- Orchestration layer:
  - `engine.py`
- Policy/adapter layer:
  - `policy.py`
  - `merge_tool_adapters/ci_auto.py`
  - `merge_tool_adapters/manual_semiauto.py`

The engine now depends on `GitExecutionPort` instead of the concrete
`GitActionExecutor`.  Policies receive the execution port instead of the whole
engine, so CI/manual custom behavior can run commands without depending on
orchestration internals.

## Verification Strategy

1. Run split core unit and integration tests.
2. Run adapter tests with local Git repositories.
3. Best-effort run original CI matrix tests.
4. Best-effort run original PythonTool unittest suite.

Any generated validation artifacts from original test suites should be left in
place unless explicitly requested otherwise.

## Verification Log

Current pass:

- New split core + adapter tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 9 tests ... OK`
- PythonTool original focused merge tests:
  - Command: `python -m unittest tests.test_recursive_submodule_merge -v`
  - Result: `Ran 22 tests ... OK`
- PythonTool original full unittest suite:
  - Command: `python -m unittest discover -s tests -v`
  - Result: `Ran 98 tests`; `89 ok`, `1 skipped`, `8 errors`.
  - Errors are all Tk/Tcl GUI setup failures:
    `TclError: Can't find a usable init.tcl`.
- MergeTool_for_CICD full matrix:
  - Command: `python ci_merge_full_matrix_test.py --run-root C:\Users\Qs\Documents\MergeTool_for_Manual\ci_full_matrix_run`
  - Result: `Passed: 21 / 21`
  - Report: `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_full_matrix_run\TEST_REPORT.md`
- MergeTool_for_CICD sequence stress:
  - Command: imported `ci_merge_sequence_stress_test` and ran its fixture,
    sequence, and validation functions with run root
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_sequence_stress_run`.
  - Result: `passed: true`

Logic/execution split pass:

- Split core + adapter tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 13 tests ... OK`
- Compile check:
  - Command: `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,tests_split_gitlink_merge -Filter *.py | ForEach-Object { python -m py_compile $_.FullName }`
  - Result: `OK`
- Notes:
  - Added pure logic tests for conflict grouping, gitlink stage extraction, and
    ancestry lookup gating.

Remaining-split implementation pass:

- Compile check:
  - Command:
    `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,ci_merge_split,manual_merge_split,tests_split_gitlink_merge -Filter *.py -Recurse | ForEach-Object { python -m py_compile $_.FullName }; python -m py_compile ci_merge_mergecore_bridge.py ci_merge_sequence_mergecore_bridge.py recursive_gitlink_merge_core.py`
  - Result: `OK`
- Split core + adapter + facade parity tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 22 tests ... OK`
- Notes:
  - Facade parity tests now compare extracted CI/manual implementations against
    the original scripts for model shapes, message text, parser behavior,
    branch-token semantics, moved-file scoring, text normalization, conflict
    tool command rendering, and publish failure classification.

Artifact/file-move split pass:

- Compile check:
  - Command:
    `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,ci_merge_split,manual_merge_split,tests_split_gitlink_merge -Filter *.py -Recurse | ForEach-Object { python -m py_compile $_.FullName }; python -m py_compile ci_merge_mergecore_bridge.py ci_merge_sequence_mergecore_bridge.py recursive_gitlink_merge_core.py`
  - Result: `OK`
- Split core + adapter + facade parity tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 26 tests ... OK`
- MergeTool_for_CICD full matrix using mergecore bridge after local artifact
  extraction:
  - Command: imported `ci_merge_full_matrix_test`, set `CI_MERGE` to
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_merge_mergecore_bridge.py`,
    and ran all cases with run root
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_report_artifacts_bridge_full_matrix_run_2`.
  - Result: `Passed: 21 / 21`
  - Report:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_report_artifacts_bridge_full_matrix_run_2\TEST_REPORT.md`

Recovery/publish split pass:

- Compile check:
  - Command:
    `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,ci_merge_split,manual_merge_split,tests_split_gitlink_merge -Filter *.py -Recurse | ForEach-Object { python -m py_compile $_.FullName }; python -m py_compile ci_merge_mergecore_bridge.py ci_merge_sequence_mergecore_bridge.py recursive_gitlink_merge_core.py`
  - Result: `OK`
- Split core + adapter + facade parity tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 28 tests ... OK`
- MergeTool_for_CICD full matrix using mergecore bridge after local recovery
  extraction:
  - Command: imported `ci_merge_full_matrix_test`, set `CI_MERGE` to
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_merge_mergecore_bridge.py`,
    and ran all cases with run root
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_recovery_publish_bridge_full_matrix_run`.
  - Result: `Passed: 21 / 21`
  - Report:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_recovery_publish_bridge_full_matrix_run\TEST_REPORT.md`

Preflight split pass:

- Compile check:
  - Command:
    `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,ci_merge_split,manual_merge_split,tests_split_gitlink_merge -Filter *.py -Recurse | ForEach-Object { python -m py_compile $_.FullName }; python -m py_compile ci_merge_mergecore_bridge.py ci_merge_sequence_mergecore_bridge.py recursive_gitlink_merge_core.py`
  - Result: `OK`
- Split core + adapter + facade parity tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 29 tests ... OK`
- MergeTool_for_CICD full matrix using mergecore bridge after local preflight
  extraction:
  - Command: imported `ci_merge_full_matrix_test`, set `CI_MERGE` to
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_merge_mergecore_bridge.py`,
    and ran all cases with run root
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_preflight_bridge_full_matrix_run`.
  - Result: `Passed: 21 / 21`
  - Report:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_preflight_bridge_full_matrix_run\TEST_REPORT.md`

Sixth remaining-split validation pass:

- Compile check:
  - Command:
    `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,ci_merge_split,manual_merge_split,tests_split_gitlink_merge -Filter *.py -Recurse | ForEach-Object { python -m py_compile $_.FullName }; python -m py_compile ci_merge_mergecore_bridge.py ci_merge_sequence_mergecore_bridge.py recursive_gitlink_merge_core.py`
  - Result: `OK`
- Split core + adapter + facade parity tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 34 tests ... OK`
- MergeTool_for_CICD full matrix using mergecore bridge after publish-only and
  manual remaining-split extraction:
  - Command:
    `python D:\Git\MergeTool_for_CICD\ci_merge_full_matrix_test.py --run-root C:\Users\Qs\Documents\MergeTool_for_Manual\ci_publish_only_bridge_full_matrix_run`
  - Result: `Passed: 21 / 21`
  - Report:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_publish_only_bridge_full_matrix_run\TEST_REPORT.md`

Seventh remaining-split validation pass:

- Compile check:
  - Command:
    `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,ci_merge_split,manual_merge_split,tests_split_gitlink_merge -Filter *.py -Recurse | ForEach-Object { python -m py_compile $_.FullName }; python -m py_compile ci_merge_mergecore_bridge.py ci_merge_sequence_mergecore_bridge.py recursive_gitlink_merge_core.py`
  - Result: `OK`
- Split core + adapter + facade parity tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 36 tests ... OK`
- Manual command-service validation:
  - Command: created a local bare remote and clone, then ran
    `ManualCommandService.run_merge(...)` using the original manual script's Git
    execution port.  Because this sandbox cannot expose Git for Windows
    `usr/bin` tools to subprocess PATH, the validation wrapper treated
    `git submodule update --init --recursive` as a no-op only when `.gitmodules`
    is absent.
  - Result: exit code `0`, clean status, branch `validation-work`,
    `feature.txt` present, no report file.
  - JSON log:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\manual_command_validation_run_20260530_140140\manual_command_validation.json`
- MergeTool_for_CICD full matrix using mergecore bridge after manual command
  split and local CI display-path helper:
  - Command:
    `python D:\Git\MergeTool_for_CICD\ci_merge_full_matrix_test.py --run-root C:\Users\Qs\Documents\MergeTool_for_Manual\ci_command_split_bridge_full_matrix_run`
  - Result: `Passed: 21 / 21`
  - Report:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_command_split_bridge_full_matrix_run\TEST_REPORT.md`

Eighth remaining-split validation pass:

- Compile check:
  - Command:
    `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,ci_merge_split,manual_merge_split,tests_split_gitlink_merge -Filter *.py -Recurse | ForEach-Object { python -m py_compile $_.FullName }; python -m py_compile ci_merge_mergecore_bridge.py ci_merge_sequence_mergecore_bridge.py recursive_gitlink_merge_core.py`
  - Result: `OK`
- Split core + adapter + facade parity tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 37 tests ... OK`
- Manual local execution validation:
  - Command: created a local bare remote and clone, patched only the no-op
    `git submodule update --init --recursive` call for repositories without
    `.gitmodules` because of this sandbox's Git for Windows PATH issue, then
    ran `ManualCommandService().run_merge(...)` without loading the original
    manual module.
  - Result: exit code `0`, clean status, branch `validation-work`,
    `feature.txt` present, no report file, `used_original_manual_module=false`.
  - JSON log:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\manual_local_execution_validation_run_20260530_141247\manual_local_execution_validation.json`

Ninth remaining-split validation pass:

- Compile check:
  - Command:
    `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,ci_merge_split,manual_merge_split,tests_split_gitlink_merge -Filter *.py -Recurse | ForEach-Object { python -m py_compile $_.FullName }; python -m py_compile ci_merge_mergecore_bridge.py ci_merge_sequence_mergecore_bridge.py recursive_gitlink_merge_core.py`
  - Result: `OK`
- Split core + adapter + facade parity tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 43 tests ... OK`
- Split package entry checks:
  - Commands: `python -m ci_merge_split --help`;
    `python -m manual_merge_split --help`
  - Result: both returned exit code `0` and printed usage.
- Dependency scan:
  - Command: searched split modules for direct `ci_module.*` /
    `manual_module.*` calls and original-script loader usage.
  - Result: direct original-module runtime calls were not found in split
    modules; remaining original-script references are limited to legacy loaders,
    parity tests, and plan documentation.

Original-test bridge pass:

- CI full matrix using mergecore bridge:
  - Command: imported `ci_merge_full_matrix_test`, set `CI_MERGE` to
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_merge_mergecore_bridge.py`,
    and ran all cases with run root
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_mergecore_bridge_full_matrix_run`.
  - Result: `Passed: 21 / 21`
  - Report:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_mergecore_bridge_full_matrix_run\TEST_REPORT.md`
- CI sequence stress using mergecore bridge:
  - Command: imported `ci_merge_sequence_stress_test`, set `SEQUENCE_RUNNER` to
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_merge_sequence_mergecore_bridge.py`,
    and ran with run root
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_mergecore_bridge_sequence_run_2`.
  - Result: validation `passed: true`; process return code is `1` as expected
    because the stress sequence intentionally contains failed steps while
    validating continuation behavior.
  - Report:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\ci_mergecore_bridge_sequence_run_2\sequence_output\sequence-report.json`
- Manual/PythonTool original focused tests:
  - Command: `PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_recursive_submodule_merge -v`
    in `D:\Git\PythonTool`.
  - Result: `Ran 22 tests ... OK`.
  - Note: these tests are old-script API unit tests and are not parameterized
    to run against `ManualSemiAutoMergeService` directly.
- Manual mergecore scenario validation:
  - Command: generated a parent/submodule gitlink conflict and ran
    `ManualSemiAutoMergeService` with a recording backend.
  - Result: `resolved_gitlinks=1`, `root_committed=true`,
    `final_submodule_update=true`, no `MERGE_HEAD`, clean status.
  - JSON log:
    `C:\Users\Qs\Documents\MergeTool_for_Manual\manual_mergecore_validation_run_20260529_180819\manual_mergecore_validation.json`

Remaining-split facade pass:

- Split core + adapter + facade tests:
  - Command: `python -m unittest discover -s tests_split_gitlink_merge -v`
  - Result: `Ran 16 tests ... OK`
- Compile check:
  - Command: `Get-ChildItem -Path split_gitlink_merge,merge_tool_adapters,ci_merge_split,manual_merge_split,tests_split_gitlink_merge -Filter *.py | ForEach-Object { python -m py_compile $_.FullName }; python -m py_compile ci_merge_mergecore_bridge.py ci_merge_sequence_mergecore_bridge.py`
  - Result: `OK`
- Real original-module compatibility check:
  - Command: load `D:\Git\MergeTool_for_CICD\ci_merge.py` through
    `CiWorkflowServices.from_module(...)` and load
    `D:\Git\PythonTool\recursive_submodule_merge.py` through
    `ManualWorkflowServices.from_module(...)`.
  - Result: both composition roots instantiated successfully.
