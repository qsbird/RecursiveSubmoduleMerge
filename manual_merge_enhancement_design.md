# 手动合并功能优化设计方案

> 基于 `MergeTool_for_Manual` 当前代码架构，针对手动合并（manual merge）的四个功能优化点进行可行性分析和详细设计。

---

## 目录

1. [架构概述](#1-架构概述)
2. [优化一：code 命令启动时预检查（已修订）](#2-优化一code-命令启动时预检查已修订)
3. [优化二：清理功能多级 fallback 逐级清理](#3-优化二清理功能多级-fallback-逐级清理)
4. [优化三：Publish 递归子模块推送 + 交互确认](#4-优化三publish-递归子模块推送--交互确认)
5. [优化四：启动时配置文件自动存取](#5-优化四启动时配置文件自动存取)
6. [总体影响范围汇总](#6-总体影响范围汇总)
7. [实施建议与风险](#7-实施建议与风险)

---

## 1. 架构概述

### 1.1 当前模块关系

```
manual_merge_split/          # 手动合并工作流
├── runtime.py               # CLI 入口、argparse、main()
├── commands.py              # 命令编排（merge/cleanup/publish）
├── preflight.py             # 合并前预检查
├── publish.py               # 递归发布推送
├── repository_ops.py        # Git 操作封装（Facade 模式）
├── file_conflicts.py        # 文件冲突解决（调用 code）
├── guided_commands.py       # 交互式命令行向导
├── workflow.py              # 工作流服务
├── models.py                # 数据模型
└── ...

ci_merge_split/              # CI 自动化合并
├── recovery.py              # 多级清理 fallback + 快照恢复
├── commands.py              # CI 命令编排
├── publish.py               # CI 发布
└── ...

merge_cleanup_core/          # 共享清理步骤（CI 和 manual 共用）
├── __init__.py
└── steps.py                 # CleanupStep / CleanupLevel / ci_prepare_cleanup_levels 等
```

### 1.2 关键设计模式

- **Facade 模式**：`ManualRepositoryOps` 封装所有 Git 操作，通过 `manual_module` 参数支持动态注入
- **共享核心**：`merge_cleanup_core/steps.py` 已设计为 CI 和 manual 共用，无需修改
- **Service 层**：`ManualCommandService` 聚合 `ManualPreflightService`、`ManualPublishService`、`ManualFileConflictService` 等

---

## 2. 优化一：code 命令启动时预检查（已修订）

### 2.1 需求描述

手动合并使用 `code --wait` 作为默认冲突解决工具（`DEFAULT_CONFLICT_TOOL_COMMAND`）。当前检查逻辑发生在冲突解决阶段（`file_conflicts.py:prepare_conflict_tool_command()`），此时合并已经进行到一半，如果 `code` 命令不可用，用户只能在冲突发生时才知道，体验很差。

**修订**：检查时机从 preflight 阶段提前到**程序启动时**（`main()` 入口处，在任何子命令分发之前），确保用户打开工具界面就能立即看到明确提示，不必等到开始合并才报错。

### 2.2 现状分析

**现有检查逻辑（`manual_merge_split/file_conflicts.py`）**：

```python
# 第25行：已知的 VS Code 命令名
VSCODE_COMMAND_NAMES = {"code", "code.cmd", "code.exe"}

# 第61-82行：Windows 下 VS Code 安装路径探测
def vscode_command_candidates(env=None) -> list[Path]:
    # 检查 LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd
    # 检查 ProgramFiles\Microsoft VS Code\Code.exe
    # 兜底 D:/vscode/bin/code.cmd

# 第85-117行：准备冲突工具命令
def prepare_conflict_tool_command(command: str) -> ConflictToolCommand:
    # 1. 解析命令，提取可执行文件名
    # 2. 判断是否为 VS Code 命令
    # 3. 通过 shutil.which() 检查 PATH
    # 4. 若找不到，尝试从已知安装路径探测
    # 5. 返回结果含 error 字段
```

**调用时机**（`file_conflicts.py:resolve_single_file_conflict()` 第217-229行）：
```python
prepared_command = prepare_conflict_tool_command(command)
if prepared_command.error is not None:
    emit_line(output_func, prepared_command.error)
    return FileConflictResolution(reason=prepared_command.error)
```

**当前默认命令**（`models.py`）：
```python
DEFAULT_CONFLICT_TOOL_COMMAND = 'code --wait "{file}"'
```

### 2.3 设计方案

#### 2.3.1 核心思路（修订）

**原方案**：在 `run_merge()` 入口、preflight 之前检查。
**修订为**：将检查提前到 `main()` 入口处——在 `parse_args()` 之后、`if args.command == "merge":` 之前立即执行。如果 `code` 不可用，在程序启动阶段就输出错误并退出，不给用户进入子命令流程的机会。

```python
# runtime.py:main() 修订后
def main(argv=None, *, command_service=None, guided_service=None):
    args = parse_args(argv)
    commands = command_service or ManualCommandService()
    
    # ==== 新增强：启动时 code 命令可用性检查 ====
    _check_code_at_startup(args)
    # ==== 继续原有逻辑 ====
    if args.command == "cleanup":
        return commands.run_cleanup(args)
    ...
```

#### 2.3.2 新增函数

**文件：`manual_merge_split/runtime.py`**（追加）

```python
def _check_code_at_startup(args: argparse.Namespace) -> None:
    """
    在 main() 入口处检查 code 命令可用性。
    
    仅当子命令为 merge、file-conflict-mode 为 tool、
    且 conflict-tool-command 依赖 VS Code 时才检查。
    如果检查失败，直接输出错误并调用 sys.exit(1)。
    """
    if args.command != "merge":
        return
    # 默认 mode 是 "tool"，所以 merge 命令默认会触发检查
    from .file_conflicts import prepare_conflict_tool_command
    from .models import DEFAULT_CONFLICT_TOOL_COMMAND
    
    command_template = getattr(args, "conflict_tool_command", None) or DEFAULT_CONFLICT_TOOL_COMMAND
    result = prepare_conflict_tool_command(command_template)
    if result.error:
        print(result.error, file=sys.stderr)
        sys.exit(1)
    if result.message:
        print(result.message)
```

#### 2.3.3 逻辑说明

1. `main()` 解析完参数后立即调用 `_check_code_at_startup(args)`
2. 只有 `merge` 子命令需要检查（cleanup/publish/guided 不涉及 code）
3. 直接复用 `file_conflicts.py:prepare_conflict_tool_command()`，不重复实现探测逻辑
4. 非 VS Code 命令 → `result.error` 为 None，检查通过
5. VS Code 命令找不到 → `result.error` 有值，打印并 `sys.exit(1)`
6. VS Code 命令通过候选路径找到 → `result.message` 有提示，打印后继续

#### 2.3.4 数据模型

**文件：`manual_merge_split/models.py`**（追加）

```python
# 注意：此数据类为 file_conflicts.py 中 ConflictToolCommand 的镜像，
# 如果 file_conflicts 已有则不需要新增。此处仅用于 preflight 场景的返回类型。
# 启动时检查直接使用 prepare_conflict_tool_command() 的返回值，
# 无需此数据类。
```

> 启动时方案的优势：直接复用 `prepare_conflict_tool_command()` 的返回类型 `ConflictToolCommand`（`file_conflicts.py` 中已有），无需新增数据模型。

#### 2.3.5 为什么选择启动时而非 preflight 时

| 时机 | 用户体验 | 代码耦合 | 结论 |
|------|---------|---------|------|
| **启动时（修订方案）** | 打开工具界面就能看到，立即感知 | 在 `main()` 中，与 merge/cleanup/publish 解耦 | ✅ 推荐 |
| preflight 时（原方案） | 进入 merge 命令后才看到 | 依赖 `preflight_service`、`make_merge_context` 等 | 过晚 |

### 2.4 影响范围（修订后）

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `manual_merge_split/runtime.py` | 修改 | `main()` 入口新增 `_check_code_at_startup()` 调用 |
| `manual_merge_split/file_conflicts.py` | **无修改** | 直接复用 `prepare_conflict_tool_command()` |
| `manual_merge_split/models.py` | **无需修改** | 现有 `ConflictToolCommand` 足够 |
| `manual_merge_split/preflight.py` | **无修改** | 不再需要 `ensure_code_command_available()` |
| `manual_merge_split/commands.py` | **无修改** | 不再需要在 `run_merge()` 中调用 |
| CI 路径 | 无影响 | `ci_merge_split/` 不涉及 `code` 命令 |

### 2.5 兼容性

- **向后兼容**：用户自定义 `--conflict-tool-command` 为非 VS Code 命令时直接跳过检查
- **非交互场景**：检查失败调用 `sys.exit(1)`，与现有一致
- **GUI 场景**：错误信息通过 stderr 输出，GUI 可在启动时捕获显示
- **仅 merge 子命令**：cleanup/publish/guided 不受影响

---

## 3. 优化二：清理功能多级 fallback 逐级清理

### 3.1 需求描述

手动合并过程中可能产生 `manual_merge_todo` 等文件导致中断状态。当前的 `cleanup_repository()` 只做单层清理（abort → reset），无法处理复杂的中断场景（如子模块内部残留的 merge 状态、嵌套的 `.gitignore` 污染等）。

需要复用 CI 的 multi-level fallback 逐级清理逻辑，从 Level 1（温和清理）逐步升级到 Level 4（深度子模块清理），最后给出明确的用户提示。

### 3.2 现状分析

**当前 manual cleanup 流程**（`manual_merge_split/repository_ops.py:645-740`）：

```python
def cleanup_repository(self, repo_path, result, *, skip_submodule_update, ...):
    """单层清理：
    1. 递归进入子模块
    2. abort merge / reset merge
    3. git reset --hard HEAD
    4. git submodule update --init --recursive（仅在 root 执行）
    """
```

**CI 的 multi-level fallback**（`ci_merge_split/recovery.py:333-370`）：

```python
def clean_and_prepare_repo(ctx, repo_path, ...):
    # Level 1: basic cleanup（reset hard + clean + submodule update）
    # Level 2: abort + recursive cleanup
    # Level 3: sync + force update
    # Level 4: deep_submodule_cleanup（逐个子模块深度清理）
    # Level 5: fresh_clone_fallback（全新 clone 替换工作区）
```

**共享清理步骤**（`merge_cleanup_core/steps.py`）：

```python
# ci_prepare_cleanup_levels() → (Level1, Level2, Level3)
# ci_deep_submodule_cleanup_steps() → Level 4
```

**关键优势**：`merge_cleanup_core` 已经是 CI 和 manual 共用的模块，`manual_merge_split/repository_ops.py` 已经在引用它（第13行 `from merge_cleanup_core import steps as cleanup_steps`）。可以直接复用。

### 3.3 设计方案

#### 3.3.1 核心思路

将 `ManualRepositoryOps.cleanup_repository()` 从单层递归清理重构为多级 fallback 模式，对**每个仓库**尝试逐级升级的清理策略。

#### 3.3.2 清理级别设计

借鉴 CI 的 5 级设计，适配手动场景：

| 级别 | 名称 | 步骤（复用 `merge_cleanup_core/steps.py`） | 适用场景 |
|------|------|-------------------------------------------|---------|
| **Level 1** | 基础清理 | `reset_hard_step` → `clean_step` → `submodule_update_step` | 正常完成后的清理 |
| **Level 2** | 中断恢复 | `abort_operation_step("merge")` / `("rebase")` / `("cherry-pick")` → `reset_merge_step` → `reset_hard_step` → `clean_step(remove_nested_git_repos=True)` | 存在 in-progress 操作 |
| **Level 3** | 递归深度中断恢复 | Level 2 基础上 + `foreach_abort_operation_step` + `foreach_reset_hard_step` + `foreach_clean_step` | 子模块内部也有 in-progress |
| **Level 4** | 深度子模块清理 | `ci_deep_submodule_cleanup_steps()` —— 逐个子模块：fetch → reset hard → clean → submodule update --force | 子模块 worktree 状态异常 |
| **Fallback** | 提示用户手动干预 | 输出具体失败仓库路径和建议命令 | 所有自动化清理均失败 |

> 注：**不引入 CI 的 Level 5（fresh clone workspace）**。因为手动工作流中用户的本地改动必须保留，全新 clone 会丢失数据。改为输出明确的用户指引。

#### 3.3.3 新增函数

**文件：`manual_merge_split/repository_ops.py`**（重构 + 追加）

```python
def cleanup_repository_with_fallback(
    self,
    repo_path: Path,
    result: CleanupResult,
    *,
    skip_submodule_update: bool,
    allow_clean_x: bool = False,
    ignored_root_untracked_paths: Iterable[str] = (),
    is_root: bool = True,
    preserved_dirty_paths_by_repo: dict[Path, set[str]] | None = None,
    preserved_dirty_repos: set[Path] | None = None,
) -> None:
    """
    多级 fallback 清理单个仓库。

    对每个仓库按 Level 1 → Level 2 → Level 3 → Level 4 顺序尝试，
    任一级别成功后即停止。所有级别均失败则抛出异常引导用户手动处理。
    """
```

```python
def _try_cleanup_level(
    self,
    repo_path: Path,
    level_name: str,
    steps: list[CleanupStep],
    result: CleanupResult,
    *,
    check_clean: bool = True,
) -> bool:
    """
    尝试执行一组清理步骤，返回是否成功。
    成功标准：所有步骤执行成功 + (可选) 仓库变为 clean 状态。
    """
```

#### 3.3.4 清理级别实现

```python
def _build_cleanup_levels(self, allow_clean_x: bool) -> list[tuple[str, list[CleanupStep]]]:
    """构建手动工作流的清理级别列表"""
    return [
        ("level_1_basic", [
            cleanup_steps.reset_hard_step("HEAD"),
            cleanup_steps.clean_step(allow_clean_x),
            cleanup_steps.submodule_update_step(),
        ]),
        ("level_2_abort_and_recursive", [
            cleanup_steps.abort_operation_step("merge"),
            cleanup_steps.abort_operation_step("rebase"),
            cleanup_steps.abort_operation_step("cherry-pick"),
            cleanup_steps.reset_merge_step(),
            cleanup_steps.reset_hard_step("HEAD"),
            cleanup_steps.clean_step(allow_clean_x, remove_nested_git_repos=True),
        ]),
        ("level_3_foreach_abort_and_clean", [
            cleanup_steps.abort_operation_step("merge"),
            cleanup_steps.abort_operation_step("rebase"),
            cleanup_steps.abort_operation_step("cherry-pick"),
            cleanup_steps.foreach_abort_operation_step("merge"),
            cleanup_steps.foreach_abort_operation_step("rebase"),
            cleanup_steps.foreach_abort_operation_step("cherry-pick"),
            cleanup_steps.reset_hard_step("HEAD"),
            cleanup_steps.clean_step(allow_clean_x, remove_nested_git_repos=True),
            cleanup_steps.foreach_reset_hard_step(),
            cleanup_steps.foreach_clean_step(allow_clean_x, remove_nested_git_repos=True),
        ]),
        ("level_4_deep_submodule", cleanup_steps.ci_deep_submodule_cleanup_steps(
            repo_path=...,  # 动态填充
            submodule_paths=...,
            allow_clean_x=allow_clean_x,
        )),
    ]
```

#### 3.3.5 CLI 参数扩展

**文件：`manual_merge_split/runtime.py`** — `add_cleanup_arguments()`

```python
parser.add_argument(
    "--allow-clean-x",
    action="store_true",
    help="Allow git clean -x (remove ignored files) during cleanup for stubborn states.",
)
```

不再需要用户手动传入；默认 `False`（安全），Level 3-4 如果失败会提示用户使用 `--allow-clean-x` 重试。

#### 3.3.6 失败提示

当所有级别失败时，输出类似以下信息：

```
Cleanup failed for repository: D:/project/submod/nested
All cleanup levels exhausted:
  - Level 1 (basic cleanup): still dirty
  - Level 2 (abort + recursive): git reset --merge failed
  - Level 3 (foreach abort + clean): submodule foreach returned error
  - Level 4 (deep submodule cleanup): clean step refused to remove nested .git

Manual recovery options:
  1. cd D:/project/submod/nested
  2. git status  # inspect the remaining untracked/ignored files
  3. git clean -ffdx  # caution: removes all untracked files including ignored
  4. Re-run: merge-tool cleanup --repo D:/project --allow-clean-x
  5. If still failing, manually delete and re-clone this repository
```

### 3.4 影响范围

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `manual_merge_split/repository_ops.py` | **重构** | `cleanup_repository()` 从单层改为多级 fallback；新增 `_try_cleanup_level()`、`_build_cleanup_levels()` |
| `manual_merge_split/commands.py` | 修改 | `run_cleanup()` 传递 `allow_clean_x` 参数 |
| `manual_merge_split/runtime.py` | 修改 | 新增 `--allow-clean-x` CLI 参数 |
| `manual_merge_split/guided_commands.py` | 修改 | 交互向导增加 `allow_clean_x` 提示 |
| `merge_cleanup_core/steps.py` | **无修改** | 直接复用现有 `ci_prepare_cleanup_levels` 和 `ci_deep_submodule_cleanup_steps` |
| `ci_merge_split/recovery.py` | **无影响** | CI 路径不变 |

### 3.5 兼容性

- **向后兼容**：现有一级清理逻辑被保留为 Level 1，原有行为不变
- **渐进增强**：只有 Level 1 失败时才尝试更高级别
- **保持 preserved_dirty 逻辑**：dirty submodule 保留逻辑不变
- **幂等性**：每级清理都可以安全重试

---

## 4. 优化三：Publish 递归子模块推送 + 交互确认

### 4.1 需求描述

当前 publish 功能体验不佳：

1. **无预览**：直接从 deepest 子模块推送到 root，用户不知道有哪些仓库会被推送
2. **无确认**：即使有 `--check-only`，也只是 dry-run push，用户体验割裂
3. **无选择性**：无法增删待推送的子模块列表

需求改进为：
1. 从最外层根目录开始，递归执行 `git push --recurse-submodules=check`（dry-run）
2. 收集所有需要推送的子模块
3. 弹窗展示待推送列表，提供增加/删除子模块的交互界面
4. 用户确认后按 deepest-first 顺序执行推送

### 4.2 现状分析

**当前 publish 流程**（`manual_merge_split/publish.py`）：

```
run_publish_workflow()
├── ensure_valid_push_target()        # 验证推送目标
├── ensure_no_in_progress_operations() # 检查 in-progress
├── submodule update --init --recursive
├── collect_publish_plan()            # 递归收集需要推送的仓库（基于 gitlink diff）
│   └── 对比 desired_sha vs baseline_sha（parent commits）
├── classify_remote_state()           # 检查 remote 状态（up_to_date/fast_forward/non_ff）
├── sync_local_branch_ref()           # 可选：创建本地分支
├── run_push_check()                  # --dry-run push
└── push_item()                       # 实际 push
```

**关键函数**：
- `collect_publish_plan()`：基于 root HEAD 的 gitlink 递归收集；如果 child gitlink 未变则跳过
- `run_push_check()`：单个仓库执行 `git push --dry-run --recurse-submodules=check`
- `push_item()`：单个仓库执行 `git push --recurse-submodules=check`

**当前问题**：
- `collect_publish_plan()` 只考虑了 gitlink 有变化的子模块，而非从 `--recurse-submodules=check` 的视角
- 没有交互确认环节
- dry-run 和实际执行是同一函数的不同参数，用户缺少中间决策点

### 4.3 设计方案

#### 4.3.1 核心思路

重构 publish 工作流为**三阶段**：

```
阶段 1：发现（Discovery）
  从根目录执行 git push --recurse-submodules=check --dry-run <remote> HEAD:<target>
  → Git 自动递归检查所有子模块
  → 解析输出，收集所有需要推送的仓库列表

阶段 2：确认（Confirmation）
  → 展示待推送仓库列表（路径、提交 SHA、remote 状态）
  → 提供交互式增删界面：
    - 输入数字选择要移除的仓库
    - 输入路径添加额外仓库
    - 确认执行 / 取消

阶段 3：执行（Execution）
  → 按 deepest-first 顺序执行 git push --recurse-submodules=check
  → 实时输出进度
  → 汇总结果
```

#### 4.3.2 新增函数

**文件：`manual_merge_split/publish.py`**（追加）

```python
@dataclass
class PublishCandidate:
    """推送给选项"""
    repo_path: Path
    display_path: str
    commit_sha: str          # 要推送的 commit
    remote_before_sha: str | None  # 远端当前 SHA
    remote_state: str        # "missing" / "up_to_date" / "needs_push"
    depth: int
    selected: bool = True    # 用户是否选中

def discover_publish_candidates(
    repo_path: Path,
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
) -> list[PublishCandidate]:
    """
    阶段 1：通过 dry-run push 发现所有需要推送的仓库。
    
    使用 git push --recurse-submodules=check --dry-run 收集完整列表。
    """
```

```python
def confirm_publish_plan_interactive(
    candidates: list[PublishCandidate],
    *,
    input_func=input,
    output_func=print,
) -> list[PublishCandidate] | None:
    """
    阶段 2：交互式确认推送计划。

    展示待推送列表，提供操作菜单：
      [数字] 切换选中/取消选中某个仓库
      [+]   添加额外仓库路径
      [d]   显示详情
      [a]   全选
      [n]   取消全选
      [c]   确认执行
      [q]   取消退出

    返回用户确认后的候选列表（仅 selected=True 的项），取消则返回 None。
    """
```

```python
def execute_publish_plan(
    candidates: list[PublishCandidate],
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
    check_only: bool = False,
) -> PublishResult:
    """
    阶段 3：按 deepest-first 顺序执行推送。
    复用现有的 push_item() 函数。
    """
```

#### 4.3.3 交互界面设计

```
============================================================
 Publish Plan Confirmation
============================================================
 Remote: origin
 Target branch: main

 Repositories to push (13 total, deepest-first order):
 ============================================================
  [✓] 1. src/shared/sub3                     (depth=3)
        Commit: a1b2c3d4e5f6 → main
        Remote: up-to-date (already at a1b2c3d)

  [✓] 2. src/shared/sub2                      (depth=2)
        Commit: b2c3d4e5f6a1 → main
        Remote: needs push (currently at xxxxxxxxxx)

  [✓] 3. src/lib/sub1                         (depth=2)
        Commit: c3d4e5f6a1b2 → main
        Remote: not found (will create)

  [ ] 4. src/tools/plugin/src/external/vendor (depth=5)
        Commit: d4e5f6a1b2c3 → main
        Remote: needs push (currently at yyyyyyyyyy)

  [+] 5. . (root)                              (depth=0)
        Commit: e5f6a1b2c3d4 → main
        Remote: needs push (currently at zzzzzzzzzz)
 ============================================================

 Commands:
  1-5   Toggle selection for that repository
  a     Select all
  n     Deselect all
  d     Show detailed info for a repository
  c     Confirm and execute push
  q     Cancel and exit

 Your choice:
```

#### 4.3.4 发现阶段的实现策略

`discover_publish_candidates()` 有两种实现方案：

**方案 A（推荐）：解析 dry-run 输出**

```python
def discover_publish_candidates(repo_path, remote, push_target, ops):
    result = ops.run_git(
        repo_path,
        [
            "push", "--recurse-submodules=check", "--dry-run",
            "--porcelain",  # 如果支持
            remote,
            f"HEAD:refs/heads/{push_target}",
        ],
        check=False,
    )
    # 解析 stderr 中的 submodule 信息行
    # Git 输出类似：
    # To <remote>
    #   <old_sha>..<new_sha>  HEAD -> <target>
    # Submodule 'path/to/sub' pushed to 'remote'
    #   <old_sha>..<new_sha>  <sha> -> refs/heads/<target>
```

**方案 B（fallback）：复现有 collect_publish_plan() + 补充检查**

```python
def discover_publish_candidates(repo_path, remote, push_target, ops):
    # 1. 先用现有逻辑收集 gitlink 有变化的子模块
    root_sha = ops.resolve_commit(repo_path, "HEAD")
    plan = collect_publish_plan(repo_path, root_sha, ops)
    
    # 2. 再递归检查所有子模块的 remote 状态
    # 把 collect_existing_repositories() 跑一遍，确保不遗漏
    all_repos = collect_existing_repositories(repo_path, ops)
    
    # 3. 合并为完整的 PublishCandidate 列表
```

**推荐方案 A + B 组合**：先尝试 A（dry-run 解析），如果 Git 版本不支持解析或失败，fallback 到 B（使用现有 `collect_publish_plan()` + `collect_existing_repositories()`）。

#### 4.3.5 CLI 参数扩展

**文件：`manual_merge_split/runtime.py`** — `add_publish_arguments()`

```python
parser.add_argument(
    "--no-interactive",
    action="store_true",
    help="Skip the interactive confirmation and push immediately (current behavior).",
)
parser.add_argument(
    "--check-only",
    "--check_only",
    dest="check_only",
    action="store_true",
    help="Run discovery and checks without pushing.",
)
# 保留现有参数：--remote, --push-target, --no-sync-local-branches, --no-branch-suffix
```

#### 4.3.6 工作流变更

**`run_publish_workflow()` 重构**：

```python
def run_publish_workflow(
    repo_path: Path,
    *,
    remote: str,
    push_target: str,
    check_only: bool,
    interactive: bool = True,          # 新增
    sync_local_branches: bool,
    allow_suffix_branch: bool,
    ops: ManualRepositoryOps,
) -> PublishResult:
    ops.ensure_git_repo(repo_path)
    ensure_valid_push_target(repo_path, push_target, ops)
    if not ops.remote_exists(repo_path, remote):
        raise ops._automation_error(...)

    # 阶段 1：发现
    candidates = discover_publish_candidates(repo_path, remote, push_target, ops)
    if not candidates:
        print("No repositories need pushing.")
        return PublishResult(check_only=check_only, items=[])

    # 阶段 2：交互确认（可跳过）
    if interactive and not check_only and sys.stdin.isatty():
        confirmed = confirm_publish_plan_interactive(candidates)
        if confirmed is None:
            print("Publish cancelled by user.")
            return PublishResult(check_only=check_only, items=[])
        candidates = [c for c in confirmed if c.selected]
    elif interactive and check_only:
        display_publish_plan(candidates)  # 只展示，不交互

    # 阶段 3：执行
    # 按 deepest-first 排序
    candidates.sort(key=lambda c: c.depth, reverse=True)

    result = PublishResult(check_only=check_only)
    for candidate in candidates:
        item = PublishPlanItem(
            repo_path=candidate.repo_path,
            display_path=candidate.display_path,
            desired_sha=candidate.commit_sha,
            depth=candidate.depth,
        )
        # 复用现有的 classify_remote_state + run_push_check + push_item
        classify_remote_state(item, remote, push_target, ops)
        run_push_check(item, remote, push_target, ops)
        if not check_only and not item.skipped:
            push_item(item, remote, push_target, ops)
        result.items.append(item)

    return result
```

### 4.4 Push 策略细节

**deeepest-first 顺序的重要性**：

`--recurse-submodules=check` 在每次 push 时会验证子模块的 commit 是否已存在于远端。如果父仓库先于子模块推送，可能导致验证失败。因此必须 deepest-first：

```
Level 3 (deepest) → Level 2 → Level 1 → Level 0 (root)
```

这与现有 `sorted(plan, key=lambda item: item.depth, reverse=True)` 逻辑一致。

### 4.5 影响范围

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `manual_merge_split/publish.py` | **重构** | 新增 `PublishCandidate`、`discover_publish_candidates()`、`confirm_publish_plan_interactive()`、`execute_publish_plan()`；重构 `run_publish_workflow()` |
| `manual_merge_split/commands.py` | 修改 | `ManualCommandService.run_publish()` 传递 `interactive` 参数 |
| `manual_merge_split/runtime.py` | 修改 | 新增 `--no-interactive` 参数 |
| `manual_merge_split/models.py` | 新增 | `PublishCandidate` 数据类 |
| CI 路径 | **无影响** | `ci_merge_split/publish.py` 不变，CI publish 是完全独立的实现 |

### 4.6 兼容性

- **`--no-interactive`**：默认识别 TTY，非 TTY 环境自动跳过交互（与 `--no-prompt-dirty-submodules` 保持一致风格）
- **`--check-only`**：行为不变，只展示计划不推送
- **向后兼容**：不传 `--no-interactive` 时行为升级为交互模式；传 `--no-interactive` 回退到当前行为

---

## 5. 优化四：启动时配置文件自动存取

### 5.1 需求描述

用户每次运行手动合并工具都需要手动输入 repo 路径、merge 分支、base branch、work branch 等参数。需要新增一个能力：

1. **自动保存**：每次成功执行后，将本次运行的参数保存到与工具同目录的配置文件中
2. **自动读取**：下次启动时自动加载配置文件，填充为默认参数值
3. **自动填充**：在交互式向导（`guided` 命令）和命令行参数中均能体现

配置文件应当是 JSON 格式，位于工具可执行文件同级目录下，方便用户手动编辑或删除。

### 5.2 现状分析

**当前参数来源**（`manual_merge_split/runtime.py`）：

```python
# merge 参数默认值
--repo              → "."
--base-remote-branch → ROOT_BASE_REMOTE_BRANCH  # "origin/main"
--work-branch       → None（动态生成）
--report-file       → REPORT_FILE_NAME           # "manual_merge_report.txt"
--conflict-tool-command → DEFAULT_CONFLICT_TOOL_COMMAND  # 'code --wait "{file}"'

# publish 参数默认值
--remote            → "origin"
--push-target       → required（无默认值）

# guided 向导以 prompt_with_default() 交互收集，参数为独立构建的 argparse.Namespace
```

**现有数据持久化**：项目中已有 `REPORT_FILE_NAME` 和日志写入机制，但无"运行时参数记忆"功能。

### 5.3 设计方案

#### 5.3.1 核心思路

新增一个轻量级配置管理模块，负责配置文件的读、写、合并。遵循"文件优先、命令行覆盖、向导最后"的优先级链。

```
优先级（高→低）：
  1. 命令行参数（--repo=...）     → 最高优先级，直接使用
  2. 配置文件（last_run.json）    → 只有命令行未指定时才生效
  3. 代码默认值（"."、"origin/main"） → 兜底
```

#### 5.3.2 配置文件格式

**文件位置**：`<工具所在目录>/merge_tool_last_run.json`

```json
{
  "version": 1,
  "last_command": "merge",
  "last_run_at": "2026-06-30T16:30:00+08:00",
  "merge": {
    "repo": "D:/project/my_repo",
    "base_remote_branch": "origin/develop",
    "work_branch": null,
    "merge_branches": ["origin/feature/foo", "origin/feature/bar"],
    "continue_current_branch": false,
    "allow_dirty_submodules": true,
    "conflict_tool_command": "code --wait \"{file}\"",
    "conflict_tool_timeout_seconds": 600,
    "enable_cross_submodule_moves": false,
    "verbose": true
  },
  "publish": {
    "remote": "origin",
    "push_target": "main",
    "no_sync_local_branches": false,
    "no_branch_suffix": false
  },
  "cleanup": {
    "repo": "D:/project/my_repo",
    "work_branch": null,
    "rebuild_work_branch": true,
    "skip_submodule_update": false,
    "allow_clean_x": false
  }
}
```

**设计原则**：
- 按子命令分组（`merge` / `publish` / `cleanup`），可以独立存取
- `last_command` 记录上一次执行的子命令，启动时可据此设置默认命令
- 使用语义化的参数名（与 argparse `dest` 一致），方便直接映射
- 敏感信息不写（无 token、无密码）

#### 5.3.3 新增模块

**文件：`manual_merge_split/config_persistence.py`**（新建）

```python
"""Runtime configuration persistence — auto-save and auto-load last-used parameters.

File location: <sys.executable dir>/merge_tool_last_run.json
Fallback: CWD/merge_tool_last_run.json（当 exe 路径不可写时）
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIG_FILE_NAME = "merge_tool_last_run.json"


@dataclass
class MergeConfig:
    repo: str = "."
    base_remote_branch: str = "origin/main"
    work_branch: str | None = None
    merge_branches: list[str] = field(default_factory=list)
    continue_current_branch: bool = False
    allow_dirty_submodules: bool = False
    conflict_tool_command: str = 'code --wait "{file}"'
    conflict_tool_timeout_seconds: int = 600
    enable_cross_submodule_moves: bool = False
    verbose: bool = False


@dataclass
class PublishConfig:
    remote: str = "origin"
    push_target: str = ""
    no_sync_local_branches: bool = False
    no_branch_suffix: bool = False


@dataclass
class CleanupConfig:
    repo: str = "."
    work_branch: str | None = None
    rebuild_work_branch: bool = True
    skip_submodule_update: bool = False
    allow_clean_x: bool = False


@dataclass
class LastRunConfig:
    version: int = 1
    last_command: str = ""
    last_run_at: str = ""
    merge: MergeConfig = field(default_factory=MergeConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)


def config_file_path() -> Path:
    """Determine the config file location."""
    # 优先使用可执行文件所在目录
    try:
        exe_dir = Path(sys.executable).parent
    except Exception:
        exe_dir = Path.cwd()
    candidate = exe_dir / CONFIG_FILE_NAME
    # 如果 exe 目录不可写（如打包路径），回退到 CWD
    if candidate.exists():
        return candidate
    try:
        candidate.touch()
        candidate.unlink()
        return candidate
    except (OSError, PermissionError):
        return Path.cwd() / CONFIG_FILE_NAME


def load_config() -> LastRunConfig:
    """Load config from file, returning defaults if not found or corrupt."""
    path = config_file_path()
    try:
        if not path.exists():
            return LastRunConfig()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _deserialize(raw)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return LastRunConfig()


def save_config(config: LastRunConfig) -> None:
    """Save config to file, creating parent dirs if needed."""
    path = config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config.last_run_at = datetime.now(timezone.utc).isoformat()
    payload = _serialize(config)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def save_config_from_args(args: Any, command: str) -> None:
    """Convenience: extract relevant fields from argparse Namespace and save."""
    config = load_config()
    config.last_command = command
    if command == "merge":
        _fill_merge_config(config.merge, args)
    elif command == "publish":
        _fill_publish_config(config.publish, args)
    elif command == "cleanup":
        _fill_cleanup_config(config.cleanup, args)
    save_config(config)


def apply_config_to_args(args: Any) -> Any:
    """
    Apply saved defaults to an argparse Namespace for fields the user didn't explicitly set.
    
    'Explicitly set' is determined by comparing against argparse defaults.
    This function should be called after parse_args() but before any command execution.
    """
    config = load_config()
    # 只在用户没有通过命令行显式指定时填充默认值
    # 具体实现通过 argparse 的 default 值对比来判断
    return _apply_defaults(args, config)


# ...helper functions: _serialize, _deserialize, _fill_*, _apply_defaults...
```

#### 5.3.4 调用点集成

**1. 启动时加载——`main()` 入口**

**文件：`manual_merge_split/runtime.py`**

```python
def main(argv=None, *, command_service=None, guided_service=None):
    from .config_persistence import load_config, apply_config_to_args, save_config_from_args
    
    args = parse_args(argv)
    
    # 启动时 code 检查
    _check_code_at_startup(args)
    
    # 应用已保存的配置默认值
    args = apply_config_to_args(args)
    
    commands = command_service or ManualCommandService()
    exit_code = 0
    
    try:
        if args.command == "cleanup":
            exit_code = commands.run_cleanup(args)
        elif args.command == "publish":
            exit_code = commands.run_publish(args)
        elif args.command == "guided":
            ...
        else:
            exit_code = commands.run_merge(args)
    finally:
        # 无论成功失败，保存本次参数（失败时保存有助于复现问题）
        if exit_code == 0:  # 仅成功时保存，避免污染配置
            save_config_from_args(args, args.command)
    
    return exit_code
```

**2. 向导模式下填充默认值——`run_guided()`**

**文件：`manual_merge_split/guided_commands.py`**

```python
def run_guided(args, *, run_merge, run_cleanup, set_verbose, input_func=input):
    from .config_persistence import load_config
    
    config = load_config()
    default_repo = config.merge.repo if config.last_command == "merge" else str(Path(args.repo).resolve())
    
    repo = Path(prompt_with_default("Repository path", default_repo, input_func=input_func)).resolve()
    action = prompt_with_default("Choose action: merge or cleanup", 
                                  config.last_command or "merge", 
                                  input_func=input_func).lower()
    
    if action == "merge":
        mc = config.merge
        default_branches = ", ".join(mc.merge_branches) if mc.merge_branches else ""
        ...
        merge_args = argparse.Namespace(
            ...
            base_remote_branch=prompt_with_default("Root base remote branch", mc.base_remote_branch, ...),
            conflict_tool_command=prompt_with_default("Conflict tool command", mc.conflict_tool_command, ...),
            allow_dirty_submodules=prompt_yes_no("Allow unaffected dirty submodules...", mc.allow_dirty_submodules, ...),
            verbose=prompt_yes_no("Enable verbose command output", mc.verbose, ...),
        )
    ...
```

**3. 自动保存——成功执行后**

每次 `run_merge()`、`run_cleanup()`、`run_publish()` 返回 `exit_code == 0` 时，在 `main()` 的 `finally` 块中调用 `save_config_from_args()`。

#### 5.3.5 "参数是否被用户显式指定"的判断逻辑

```python
def _apply_defaults(args: argparse.Namespace, config: LastRunConfig) -> argparse.Namespace:
    """
    核心逻辑：
    1. 获取每个子命令的 parser 定义的 default 值（add_argument 中的 default=）
    2. 对比 args 当前值是否等于 parser default
    3. 如果相等 → 用户未指定 → 从 config 填充
    4. 如果不等 → 用户显式指定 → 保持 args 值
    """
    from .runtime import build_parser
    parser = build_parser()
    # 获取子命令对应的 parser defaults
    subparsers = next(a for a in parser._actions if a.dest == "command")
    subparser = subparsers.choices.get(args.command)
    if subparser is None:
        return args
    
    parser_defaults = subparser.get_default(args.command)
    
    config_section = getattr(config, args.command, None)
    if config_section is None:
        return args
    
    for field_name, config_value in asdict(config_section).items():
        if not hasattr(args, field_name):
            continue
        current_value = getattr(args, field_name)
        parser_default = parser_defaults.get(field_name)
        
        # 如果当前值等于 parser 默认值（用户未指定），用 config 覆盖
        if current_value == parser_default:
            setattr(args, field_name, config_value)
    
    return args
```

> 注：对于 `merge_branches` 这种 `nargs="+"` 位置参数，其 parser default 为 `None`，且用户必然提供（因为是 required）。因此不会从配置文件覆盖。处理方式：如果配置中有值且命令行没有位置参数传入（实际上 merge_branches nargs="+" 要求必须有），则使用配置值。需要在 `parse_args` 前后做特殊处理。

#### 5.3.6 配置文件的安全性

- **不存储敏感信息**：只存公开的 Git 分支名、路径、命令模板
- **不自动共享**：文件在工具目录下，随工具分发，不跨机器同步
- **手动可清理**：删除 `merge_tool_last_run.json` 即可恢复默认
- **JSON 格式**：用户可手动编辑，错误格式时静默降级为默认值
- **仅在成功时保存**：`exit_code == 0` 时才写入，失败运行不污染配置

### 5.4 影响范围

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `manual_merge_split/config_persistence.py` | **新建** | 配置文件管理模块（约 200 行） |
| `manual_merge_split/runtime.py` | 修改 | `main()` 入口新增加载/保存调用；`build_parser()` 可能需要导出 subparser 映射 |
| `manual_merge_split/guided_commands.py` | 修改 | `run_guided()` 从配置文件读取默认值填充 prompt |
| `manual_merge_split/models.py` | **无需修改** | 配置数据类在 config_persistence.py 中定义，与 models.py 无耦合 |
| CI 路径 | **无影响** | CI 不使用手动工具 |

### 5.5 兼容性

- **无配置文件时**：等同于当前默认行为，零影响
- **配置文件版本升级**：`version` 字段预留，未来可做迁移
- **字段增加**：旧配置文件缺少新字段 → deserialize 时使用 dataclass default
- **仅成功时写入**：不会因为反复失败而覆盖掉之前有效的配置

---

## 6. 总体影响范围汇总

### 6.1 需要修改的文件

| 文件 | 优化一 | 优化二 | 优化三 | 优化四 | 变更程度 |
|------|:---:|:---:|:---:|:---:|------|
| `manual_merge_split/runtime.py` | ✅ 修改 | ✅ 新参数 | ✅ 新参数 | ✅ 修改 | **中等** |
| `manual_merge_split/models.py` | — | — | ✅ 新增 | — | **轻微** |
| `manual_merge_split/repository_ops.py` | — | ✅ **重构** | — | — | **重大** |
| `manual_merge_split/publish.py` | — | — | ✅ **重构** | — | **重大** |
| `manual_merge_split/commands.py` | — | ✅ 小改 | ✅ 小改 | — | **轻微** |
| `manual_merge_split/guided_commands.py` | — | ✅ 小改 | — | ✅ 修改 | **轻微** |
| `manual_merge_split/config_persistence.py` | — | — | — | ✅ **新建** | **新增** |

### 6.2 不需要修改的文件

| 文件 | 原因 |
|------|------|
| `merge_cleanup_core/steps.py` | 已是共享模块，直接复用 |
| `merge_cleanup_core/__init__.py` | 接口不变 |
| `ci_merge_split/`（全部） | CI 路径完全独立，不受影响 |
| `manual_merge_split/file_conflicts.py` | 保留原有检查为二次防卫；优化一改为直接复用 |
| `manual_merge_split/preflight.py` | 优化一改为启动时检查，preflight 不受影响 |
| `manual_merge_split/workflow.py` | Facade 层自动通过 Service 继承变更 |
| `manual_merge_split/execution.py` | Git 执行层不变 |
| `manual_merge_split/cross_submodule_moves.py` | 跨子模块移动逻辑不变 |
| `tests_split_gitlink_merge/` | 需后续更新测试（非本方案范围） |

### 6.3 风险矩阵

| 风险 | 优化 | 等级 | 缓解措施 |
|------|:---:|:---:|------|
| `code` 检查误判导致程序拒绝启动 | 一 | 低 | 非 VS Code 命令直接跳过；探测失败才阻止 |
| 多级清理误删用户改动 | 二 | 中 | 保持 `preserved_dirty` 逻辑；`git clean -x` 需显式参数 |
| Publish 交互在非 TTY 环境卡住 | 三 | 低 | 自动检测 TTY，非 TTY 降级为非交互模式 |
| dry-run 解析依赖 Git 版本差异 | 三 | 中 | 采用方案 A + B 组合策略，解析失败自动 fallback |
| 配置文件损坏导致崩溃 | 四 | 低 | 损坏时静默降级为默认值；JSON parse 失败返回 defaults |
| 配置文件在 exe 目录不可写 | 四 | 低 | fallback 到 CWD；写入失败静默忽略，不影响核心功能 |

---

## 7. 实施建议与风险

### 7.1 推荐实施顺序

```
Phase 1：优化一（code 检查）—— 风险最低，最先实施
  → 只增加一个 preflight 检查，不影响现有流程
  → 测试简单：模拟 code 不可用的环境

Phase 2：优化二（清理 fallback）—— 中等风险
  → 核心改动在 repository_ops.py 的 cleanup_repository()
  → 需要在各种脏状态仓库上测试
  → 依赖 merge_cleanup_core 无需改动（已就绪）

Phase 3：优化三（publish 交互）—— 最高复杂度
  → publish.py 重构工作量最大
  → 交互界面需要多次迭代调整
  → 可以与 Phase 1、2 并行开发（互不依赖）
```

### 7.2 测试策略

| 优化 | 关键测试场景 |
|------|------------|
| 一 | `code` 在 PATH 中 / 不在 PATH 中 / 自定义 VS Code 路径 / 非 code 工具 / 命令行参数中有空格和引号 |
| 二 | 正常清理 / merge 中断 / rebase 中断 / 子模块 merge 中断 / 嵌套子模块脏状态 / `manual_merge_todo` 残留 / preserved dirty 仓库 |
| 三 | 无子模块 / 单层子模块 / 深度嵌套 / 部分子模块 up-to-date / 远端不存在 / 非 TTY 回退 / 用户取消 / 用户增删候选 |
| 四 | 首次运行（无配置文件）/ 二次运行（自动填充）/ 配置文件损坏 / exe 目录不可写 / 手动编辑 JSON 后 / 仅成功时保存不污染 |

### 7.3 注意事项

1. **merge_cleanup_core 不可修改**：这是 CI 和 manual 的唯一共享核心，CI 的稳定运行依赖它。所有 manual 特有的行为封装在 `repository_ops.py` 中。
2. **facade 模式保持一致**：`ManualRepositoryOps`、`ManualPreflightService` 等 facade 类需要同步更新方法签名。
3. **保持向后兼容**：所有新增参数都是 optional，默认值保持现有行为。
4. **打包需重新编译**：修改后需要重新运行 PyInstaller 打包生成 `MergeToolManual.exe` 和 `MergeToolManualCli.exe`。

---

## 附录

### A. 关键文件路径

```
c:\Users\Qs\Documents\MergeTool_for_Manual\
├── manual_merge_split/
│   ├── models.py              # 数据模型（新增 ConflictToolAvailability, PublishCandidate）
│   ├── preflight.py           # 预检查（新增 ensure_code_command_available）
│   ├── repository_ops.py      # Git 操作（重构 cleanup_repository）
│   ├── publish.py             # 发布推送（重构 run_publish_workflow）
│   ├── commands.py            # 命令编排（更新三个入口）
│   ├── runtime.py             # CLI 解析（新增参数）
│   └── guided_commands.py     # 交互向导（更新 cleanup 提示）
├── merge_cleanup_core/
│   └── steps.py               # 共享清理步骤（不修改，只复用）
└── ci_merge_split/            # CI 路径（不修改）
    ├── recovery.py
    └── publish.py
```

### B. 相关常量

```python
# models.py 中需新增
DEFAULT_CONFLICT_TOOL_COMMAND = 'code --wait "{file}"'  # 已存在

# file_conflicts.py 中已有，需在 preflight.py 中引用
VSCODE_COMMAND_NAMES = {"code", "code.cmd", "code.exe"}

# steps.py 中已有，可直接复用
ci_prepare_cleanup_levels()   # → (Level1, Level2, Level3)
ci_deep_submodule_cleanup_steps()  # → Level 4