# Implementation Plan — 实施状态总结

> **状态：已全部实现并通过测试（25/25 tests passing）**

[Overview]
针对手动合并工具的三项功能优化：code 命令预检查、清理功能通用 fallback 逐级恢复、以及 publish 交互式子模块选择推送。

本次修改涉及手动合并工作流的三项核心改进。Feature 1 在合并启动前增加 `code` 命令可用性检查，避免用户在冲突解决阶段才发现工具不可用。Feature 2 复用 CI 的 `ci_prepare_cleanup_levels` 逐级清理逻辑，实现通用清理——从任何仓库状态（merge in progress、rebase、cherry-pick、脏工作树等）恢复到可合并状态，不再依赖 `manual_merge_todo.txt` 文件检测。Feature 3 重构 publish 为"先检查、后确认、再执行"的三阶段流程，从根目录递归运行 `git push --recurse-submodules=check`，在 GUI 中提供可视化子模块勾选界面，允许用户增删待推送子模块后再执行推送。

[Types]

本次修改新增/扩展的类型定义：

### Feature 1: ToolAvailabilityResult

```python
# manual_merge_split/preflight.py
@dataclass(frozen=True)
class ToolAvailabilityResult:
    """External tool availability check result."""
    tool_name: str              # e.g. "code"
    available: bool             # shutil.which 是否找到
    resolved_path: str | None   # shutil.which 返回的路径
    error_message: str | None   # 不可用时的提示信息
```

### Feature 2: 直接复用 CI CleanupLevel

```python
# merge_cleanup_core/steps.py (已有，无需新增类型)
@dataclass(frozen=True)
class CleanupLevel:
    name: str
    steps: tuple[CleanupStep, ...]

# 直接复用已有函数（不再使用 manual_prepare_cleanup_levels）
def ci_prepare_cleanup_levels(allow_clean_x: bool) -> tuple[CleanupLevel, ...]
    # 返回三个级别:
    # level_1_basic_cleanup: reset --hard + clean -fd + submodule update
    # level_2_abort_and_recursive_cleanup: abort + reset + clean + foreach
    # level_3_sync_and_force_update: sync + fetch + force update
```

**设计决策：** 不再使用 `manual_prepare_cleanup_levels` 和 `manual_merge_todo_file_step`。手动清理直接复用 CI 的 `ci_prepare_cleanup_levels`，因为：
- `reset --hard HEAD` 会恢复所有已跟踪文件到 HEAD 状态，处理已跟踪的 `merge_manual_todo.txt`
- `clean -fd` 会删除所有未跟踪文件，处理未跟踪的 `merge_manual_todo.txt`
- 插入 `git clean -f -- merge_manual_todo.txt` 只能删未跟踪文件，无法处理已跟踪的情况
- 在级别循环内 `report_path.unlink()` 会导致仓库出现 unstaged deletion，使 `working_tree_has_changes` 误报

### Feature 3: PublishPlanItem + PublishResult (已有类型扩展)

```python
# manual_merge_split/publish.py (已扩展)
@dataclass
class PublishPlanItem:
    repo_path: Path
    display_path: str
    desired_sha: str
    depth: int
    local_branch: str | None = None
    local_branch_action: str = "not_requested"
    remote_before_sha: str | None = None
    remote_state: str = "unknown"
    dry_run_ok: bool = False
    pushed: bool = False
    skipped: bool = False
    messages: list[str] = field(default_factory=list)

@dataclass
class PublishResult:
    check_only: bool
    items: list[PublishPlanItem] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
```

[Files]

### 新建文件：

- `manual_merge_split/publish_dialog.py` — ✅ 已创建。Publish 子模块选择对话框，包含 Tkinter Toplevel 窗口、Checkbutton 列表、全选/反选按钮、确认/取消按钮。被 `recursive_submodule_merge_gui.py` 调用。

- `tests_split_gitlink_merge/test_tool_availability.py` — ✅ 已创建。测试 `check_tool_availability()` 和 `ToolAvailabilityResult`。

- `tests_split_gitlink_merge/test_manual_cleanup_fallback.py` — ✅ 已创建。测试通用清理检测、CI 级别复用、`_is_path_tracked` 方法等。

- `tests_split_gitlink_merge/test_publish_check_execute.py` — ✅ 已创建。测试 `run_publish_check()` 和 `run_publish_execute()` 的分阶段逻辑。

### 修改文件：

- `manual_merge_split/preflight.py` — ✅ 已修改。新增 `check_tool_availability()` 函数和 `ToolAvailabilityResult` 数据类。

- `manual_merge_split/commands.py` — ✅ 已修改。`run_merge()` 在合并启动前调用工具检查；`run_cleanup()` 使用 `has_any_in_progress_state()` 通用检测替代 `manual_merge_todo_exists()` 文件特定检测。

- `manual_merge_split/publish.py` — ✅ 已有。原有实现已包含 `run_publish_check()` 和 `run_publish_execute()` 分阶段函数，支持 `selected_display_paths` 参数过滤推送项。

- `manual_merge_split/repository_ops.py` — ✅ 已修改。`cleanup_repository_fallback()` 直接复用 `ci_prepare_cleanup_levels`，新增 `_is_path_tracked()` 方法，移除级别循环内的 `report_path.unlink()`，改为在所有级别执行完毕后仅删除未跟踪的报告文件。

- `merge_cleanup_core/steps.py` — ✅ 已扩展。新增 `manual_merge_todo_file_step()` 和 `manual_prepare_cleanup_levels()` 函数（保留供向后兼容，但手动清理流程不再使用）。

- `recursive_submodule_merge_gui.py` — ✅ 已修改。`_start()` 方法增加 `code` 命令检查弹窗；publish 流程重构为三阶段（check → dialog → execute）。

- `manual_merge_split/__init__.py` — ✅ 已更新。导出新增的公共 API。

[Functions]

### Feature 1: code 命令预检查

**新增函数：**

- `check_tool_availability(tool_name: str) -> ToolAvailabilityResult` — `manual_merge_split/preflight.py` — 使用 `shutil.which` 检查外部工具可用性，返回检查结果

**修改函数：**

- `run_merge()` — `manual_merge_split/commands.py` — 在执行合并前增加 conflict tool 可用性检查，不可用时返回错误码 1
- `MergeGuiApp._start()` — `recursive_submodule_merge_gui.py` — merge 分支增加工具可用性检查弹窗

### Feature 2: 通用清理 fallback 逐级清理

**新增方法：**

- `ManualRepositoryOps.manual_merge_todo_exists(repo_path, report_file_name) -> bool` — 检测 todo 文件是否存在（保留供报告生成等场景使用）
- `ManualRepositoryOps.has_any_in_progress_state(repo_path) -> bool` — 通用检测仓库是否处于中断状态（merge in progress / unmerged paths / dirty working tree）
- `ManualRepositoryOps._is_path_tracked(repo_path, relative_path) -> bool` — 检测路径是否被 git 跟踪
- `ManualRepositoryOps.cleanup_repository_fallback(repo_path, result, ...) -> None` — 通用逐级 fallback 清理，直接复用 `ci_prepare_cleanup_levels`

**关键设计变更：**

- `cleanup_repository_fallback()` 改为直接复用 CI 的 `ci_prepare_cleanup_levels()` 而非 `manual_prepare_cleanup_levels()`
- 移除级别循环内的 `report_path.unlink()`（会创建 unstaged deletion 导致仓库"不干净"）
- 在所有清理级别执行完毕后，仅对**未跟踪**的报告文件执行 `unlink()`，已跟踪的由 `reset --hard` 处理
- `run_cleanup()` 触发条件从 `manual_merge_todo_exists()` 改为 `has_any_in_progress_state()`（通用检测）

**保留函数（向后兼容）：**

- `manual_merge_todo_file_step()` — `merge_cleanup_core/steps.py` — 保留但手动清理流程不再使用
- `manual_prepare_cleanup_levels()` — `merge_cleanup_core/steps.py` — 保留但手动清理流程不再使用

### Feature 3: Publish 交互式子模块选择

**已有函数（无需新增，直接复用）：**

- `run_publish_check(repo_path, *, remote, push_target, sync_local_branches, allow_suffix_branch, ops) -> PublishResult` — `manual_merge_split/publish.py`
- `run_publish_execute(check_result, *, remote, push_target, sync_local_branches, allow_suffix_branch, selected_display_paths, ops) -> PublishResult` — `manual_merge_split/publish.py`

**新增 GUI 函数：**

- `PublishSubmoduleDialog.__init__(parent, check_result)` — `manual_merge_split/publish_dialog.py` — 对话框初始化
- `PublishSubmoduleDialog.get_selected_display_paths() -> set[str]` — `manual_merge_split/publish_dialog.py` — 获取用户选中的 display_path 集合
- `MergeGuiApp._check_publish() -> None` — `recursive_submodule_merge_gui.py` — 运行 publish check 并弹出对话框
- `MergeGuiApp._execute_publish(selected_paths) -> None` — `recursive_submodule_merge_gui.py` — 根据用户选择执行推送

[Classes]

### 新增类：

- `ToolAvailabilityResult` — `manual_merge_split/preflight.py` — frozen dataclass，字段：tool_name, available, resolved_path, error_message
- `PublishSubmoduleDialog(tk.Toplevel)` — `manual_merge_split/publish_dialog.py` — 子模块选择对话框，包含 Checkbutton 列表、全选/反选、确认/取消

### 修改类：

- `ManualRepositoryOps` — `manual_merge_split/repository_ops.py` — 新增 `manual_merge_todo_exists()`, `has_any_in_progress_state()`, `_is_path_tracked()`, `cleanup_repository_fallback()` 方法；`cleanup_repository_fallback()` 改为通用检测 + 复用 CI 级别
- `ManualCommandService` — `manual_merge_split/commands.py` — 修改 `run_merge()` 和 `run_cleanup()` 增加新检查
- `MergeGuiApp` — `recursive_submodule_merge_gui.py` — 新增 `_check_publish()` 和 `_execute_publish()` 方法

[Dependencies]

本次修改不引入新的外部依赖。所有实现基于标准库（`shutil.which`, `tkinter`）和项目内部模块。

[Testing]

### 测试结果：25/25 通过 ✅

- `test_tool_availability.py` — 5 tests ✅
  - test_available_tool_returns_available
  - test_unavailable_tool_returns_not_available
  - test_which_returns_none_means_not_available
  - test_which_returns_path_means_available
  - test_result_is_frozen_dataclass

- `test_manual_cleanup_fallback.py` — 13 tests ✅
  - test_manual_merge_todo_exists_true
  - test_manual_merge_todo_exists_false
  - test_manual_merge_todo_custom_filename
  - test_ci_prepare_cleanup_levels_returns_three_levels
  - test_ci_level_names_are_ordered
  - test_ci_levels_include_reset_hard
  - test_ci_levels_include_clean_step
  - test_cleanup_repository_fallback_exists
  - test_has_any_in_progress_state_exists
  - test_is_path_tracked_exists
  - test_fallback_uses_ci_levels_not_manual_levels
  - test_fallback_removes_untracked_report_after_levels
  - test_fallback_trigger_is_generic_not_file_specific

- `test_publish_check_execute.py` — 7 tests ✅
  - test_run_publish_check_returns_plan
  - test_run_publish_check_includes_display_path
  - test_run_publish_execute_with_selected_paths
  - test_run_publish_execute_with_empty_selection_returns_success
  - test_plan_item_fields
  - test_plan_item_skipped
  - test_plan_item_default_fields

### 全量测试：79 tests, 75 pass, 2 FAIL (pre-existing), 2 ERROR (pre-existing)

已有的 FAIL/ERROR 与本次修改无关：
- `test_ci_submodule_fetch_scope.py` — 2 FAIL（环境差异导致）
- `test_ci_cleanup_batch.py` / `test_remaining_split_facades.py` — 2 ERROR（导入缺失符号）

[Implementation Order]

1. ✅ **Feature 1: code 命令预检查** — `preflight.py` 新增类型和检查函数，`commands.py` 集成检查，`recursive_submodule_merge_gui.py` 增加弹窗，测试通过
2. ✅ **Feature 2: 通用清理 fallback 逐级清理** — `repository_ops.py` 新增通用检测和 fallback 方法，直接复用 `ci_prepare_cleanup_levels`，移除 `manual_merge_todo` 特定逻辑，`commands.py` 改用通用触发条件，测试通过
3. ✅ **Feature 3: Publish 交互式子模块选择** — 复用已有 `publish.py` 的 check/execute 函数，新建 `publish_dialog.py` 对话框，`recursive_submodule_merge_gui.py` 重构为三阶段流程，测试通过