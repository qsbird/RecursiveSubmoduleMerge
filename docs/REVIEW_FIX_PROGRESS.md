# 手动合并工具 Review 修复进度

本文档跟踪 2026-09-07 代码审查所发现问题的修复进度，按 **损害 × 成本** 排序。

- 损害：数据完整性 > 不可恢复的破坏性行为 > 流程走不通 > 可恢复的错误 > 卫生问题
- 成本：改动行数与回归风险

状态图例：`[ ]` 未开始 · `[~]` 进行中 · `[x]` 已完成 · `[-]` 已评估后不修（附理由）

---

## 优先级总表

| # | 优先级 | 问题 | 损害 | 成本 | 状态 |
|---|---|---|---|---|---|
| 1 | P0 | `resume` 提交前无冲突标记校验，会静默提交带 `<<<<<<<` 的 merge | 数据完整性 | 低 | `[x]` |
| 2 | P0 | `run_cleanup_step` 丢弃 `step.cwd`，level 4 深度清理打在父仓库 | 破坏性行为错位 | 极低 | `[x]` |
| 3 | P0 | `execution.run_git` 缺 `GIT_TERMINAL_PROMPT` / 超时 / `core.quotePath=false` | 挂死 + 非 ASCII 路径流程不通 | 极低 | `[x]` |
| 4 | P1 | `resume` 中途失败会擦掉 `paused_conflicts` 诊断 | 可恢复性 | 低 | `[x]` |
| 5 | P1 | 冲突标记检测依赖 git 英文文案 `leftover conflict marker` | 数据完整性（脆弱） | 低 | `[x]` |
| 6 | P1 | `commit_if_needed` 有 unmerged 时静默返回，start 路径缺防御 | 静默成功 | 极低 | `[x]` |
| 7 | P1 | `update_submodules(check=False)` 静默吞掉失败 | 状态不一致 | 低 | `[x]` |
| 8 | P1 | session 锁无失效恢复，进程被杀后永久阻塞所有命令 | 可用性 | 低 | `[x]` |
| 9 | P2 | 清理级别自动升级到 `clean -ffd`，用户只确认一次 | 不可恢复的删除 | 中 | `[x]` |
| 10 | P2 | `cleanup` 不失效 session，留下陈旧会话 | 状态不一致 | 低 | `[x]` |
| 11 | P2 | `cleanup_repository` 的 `ignored_root_untracked_paths` 参数失效 | 行为与声明不符 | 极低 | `[x]` |
| 12 | P2 | `abort` 不恢复被非递归策略解决的子模块 | 恢复不完整 | 中 | `[x]` |
| 13 | P3 | `merge_cleanup_core.manual_prepare_cleanup_levels` 死代码 | 分歧风险 | 极低 | `[x]` |
| 14 | P3 | `process_repository` / `ManualMergePolicy` / `ManualSemiAutoMergeService` 死代码，且占用真实 git 测试 | 假覆盖 | 中 | `[-]` |
| 15 | P3 | engine `resolve_active_conflicts` 死循环面 + 重复调用 handler | 挂死（需坏 policy） | 低 | `[x]` |
| 16 | P3 | `_classify_one` 与 `_reason_for` 的 mode 集合不一致，导致空 reason | 报告可读性 | 极低 | `[x]` |
| 17 | P3 | 2 个测试在 Windows 上失败，套件常红 | 掩盖回归 | 极低 | `[x]` |
| 18 | P3 | `test_remaining_split_facades` 66/77 测试默认静默 skip | 假覆盖 | 低 | `[-]` |
| 19 | P3 | publish 的"仅检查"会执行 `submodule update` 改动工作树 | 意外副作用 | 低 | `[x]` |
| 20 | P3 | `parse_missing_submodule_paths` 匹配 git 英文文案 | 恢复逻辑失效（fail-safe） | 低 | `[x]` |
| 21 | HIGH | `staged_changed_paths` 用 `ACMR` 漏掉 type change（`T`） | 数据完整性 | 极低 | `[x]` |
| 22 | HIGH | `run_cleanup` 不持 `command_lock`，session 失效失败仍继续 | 并发破坏 | 低 | `[x]` |
| 23 | MEDIUM | abort 不记录/不还原最初未初始化的子模块 | 恢复不完整 | 中 | `[x]` |
| 24 | MEDIUM | Windows `OpenProcess` 失败一律当死亡，可能误删活跃锁 | 并发破坏 | 极低 | `[x]` |
| 25 | LOW | 打包路径检查在 `git ls-files` 失败时 SkipTest | 假覆盖 | 极低 | `[x]` |
| 26 | MEDIUM | abort 的 deinit 忽略返回码，失败仍清 session 报成功 | 恢复不完整 | 低 | `[x]` |

## 已驳回的候选问题

| 候选 | 判定 | 依据 |
|---|---|---|
| 二进制冲突被误判为 `SUPPORTED_FILE` | **不成立** | 实测 `disposition=MANUAL_TAKEOVER`、`is_binary=True`。`run_git` 用 `text=False`，`decode_stream` 的 gbk 兜底保留 NUL 字节 |
| 非 ASCII 路径被误判为 `SUPPORTED_FILE` 导致数据丢失 | **后果判断错误** | 分类正确（引号转义在 `status` 与 `ls-files` 间一致）。真实后果是工具拿到转义路径、`git add` rc=128、必然 fail-safe 落到人工接管 → 归入问题 3 |
| publish 缺 `--force-with-lease` | **不成立** | 手动侧刻意不 force，裸 push 由 git 拒绝任何非快进，比 lease 更严格 |
| 手动适配器缺 CI 的 fetch / post-merge guards / 混合冲突门禁 | **降级** | 仅存在于死代码 `ManualMergePolicy`，生产路径走 `ManualWorkflowGitlinkPolicy` → 归入问题 14 |

---

## 进度日志

### 2026-09-07

- 建立本文档，确定优先级排序。
- 影响分析：`impact("run_cleanup_step", upstream)` 报 **CRITICAL**（11 个受影响符号 / 6 条执行流 / 直接调用者 3 个：
  `_try_cleanup_level`、`restore_snapshot`、`update_submodules`）。逐一核对后确认：除 level 4 深度清理步骤外，
  所有调用点构造的 `CleanupStep` 都是 `cwd=None`，因此"尊重 `cwd`"的行为变化面只有 level 4 一处，风险可控。
  `impact("run_git", upstream)` 因同名符号 37 个而 ambiguous，手动核对了 `manual_merge_split/execution.py` 版本。

#### 已完成 #3 — `execution.run_git` 加固

- `manual_merge_split/execution.py`
  - 新增 `GIT_BASE_CONFIG`，把 `-c core.quotePath=false` 注入每次 git 调用。这是本轮最关键的一处：
    此前 `git status` / `ls-files` 对非 ASCII 路径返回八进制转义的带引号形式
    （`"\344\270\255\346\226\207/..."`），该字符串一路流到 `git add` 和外部冲突工具，
    导致 `rc=128 pathspec did not match any files`，所有含中文路径的冲突只能落到人工接管。
  - 新增 `git_env()`：`GIT_MERGE_AUTOEDIT=no` 强制设置；`GIT_TERMINAL_PROMPT=0` 用 `setdefault`
    （GUI 用隐藏控制台启动 git，交互式凭据提示会变成"无提示的永久挂起"；但保留调用方显式覆盖的能力）。
  - `run_git` / `run_git_binary` 新增 `timeout` 参数；未显式指定时，对
    `clone/fetch/ls-remote/pull/push` 自动套用 900s 兜底上限，可用
    `MERGE_TOOL_GIT_NETWORK_TIMEOUT_SECONDS` 调整（`0` 关闭）。流式路径通过事件队列 deadline
    实现同一语义，超时后 `terminate` → `kill`。
  - `resolve_git_timeout` 会跳过前导 `-c key=value`，避免 `git -c protocol.file.allow=always clone`
    被误判为本地命令。
- `manual_merge_split/common.py`
  - 新增 `unquote_git_path()`，解码 git 的 C 风格引号路径（八进制 + `\n\t\\\"` 等）。
    `core.quotePath=false` 之后 git 仍会对含引号、反斜杠、控制字符的路径加引号，所以这层防御仍必要。
  - `normalize_status_relative_path()` 先解引号再做分隔符归一化——顺序很重要，
    否则 `replace("\\", "/")` 会先把 C 转义打碎。
- `manual_merge_split/repository_ops.py` / `file_conflicts.py`
  - `get_unmerged_paths`、`get_unmerged_status_codes`、`collect_unmerged_conflicts`、
    `collect_conflict_records` 全部对 git 返回的路径调用 `unquote_git_path`。

#### 已完成 #2 — 清理步骤的工作目录

- `manual_merge_split/repository_ops.py::run_cleanup_step` 改为通过 `cleanup_steps.step_cwd()` /
  `step_args()` 解析步骤，与 CI 侧 `ci_merge_split/recovery.py::run_cleanup_steps` 的行为对齐。
  此前 level 4 的 `fetch --all --prune` / `reset --hard` / `clean -ffd` / `submodule update --force`
  本该在每个子模块里执行，实际全部落在父仓库上。
- `merge_cleanup_core/steps.py::step_cwd` 现在会把相对 `cwd` 解析到 `default_repo_path` 之下
  （此前该函数是死代码，本次修复把它接入了生产路径）。
- `_build_cleanup_levels` 只为**已初始化**的子模块（存在 `.git`）生成 level 4 深度步骤。
  空的 gitlink 目录会让 git 向上找到父仓库，等于把同一个 bug 换了个形式。

#### 已完成 #1 + #4 — resume 的提交门禁与诊断保留

- `manual_merge_split/repository_ops.py`
  - 新增 `staged_changed_paths()`、`_grep_indexed_paths()`、`find_staged_conflict_marker_paths()`。
    检测方式是 `git grep --cached -I -l -E`，**不依赖 git 的英文提示文案**（原先
    `file_conflicts.py` 依赖 `"leftover conflict marker" in output`，在中文 git 上直接失效）。
  - 判定要求同一文件同时含 `^<<<<<<< ` 与 `^>>>>>>> `，并与本次 merge 实际 stage 的路径取交集，
    因此仓库里合法提及冲突标记的文档/测试夹具不会误报（已有回归测试覆盖）。
- `manual_merge_split/session_workflow.py`
  - 新增 `_guard_staged_conflict_markers()`，在 `_start_merge` 与 `_resume` 的两个提交点之前调用：
    发现标记就把会话重新置为 `PAUSED_MANUAL`、写入 `PausedConflictRecord`、刷新报告文件、返回 2。
    此前 `resume` 会直接把带标记的内容提交掉并打印"resume completed successfully"。
  - `_resume` 不再在入口清空 `paused_conflicts` / `paused_reason`；改为在成功结束（会话删除）
    或重新暂停（写入新记录）时自然更新，中途失败不再留下空诊断。
  - 新增 `_mark_resume_blocked()`：resume 因非致命错误失败时把会话状态回写为 `PAUSED_MANUAL`
    并记录 `paused_reason` / `last_error`，避免 `status` 显示 `RUNNING`。
  - 暂停原因现在会打到 stderr，用户能直接看到是哪个文件挡住了提交。

**回归测试** `tests_split_gitlink_merge/test_manual_review_fixes.py`（26 项，全部使用真实 git 仓库）：

| 覆盖点 | 测试类 |
|---|---|
| C 风格引号路径解码 | `UnquoteGitPathTests` |
| 超时/环境变量派生规则 | `GitTimeoutResolutionTests` |
| 步骤 cwd 解析与实际执行目录 | `CleanupStepCwdTests` |
| 暂存冲突标记检测（含误报防护） | `StagedConflictMarkerDetectionTests` |
| resume 拒绝提交 / 正常提交 / 诊断保留 | `ResumeConflictMarkerGuardTests` |
| 中文冲突路径端到端 | `NonAsciiConflictPathTests` |
| level 4 指向子模块 / 跳过未初始化 / 清理脏子模块 | `DeepSubmoduleCleanupTests` |

```
Ran 26 tests in 24.016s
OK
```

#### 已完成 #5 — 冲突标记检测不再依赖 git 语言

- `manual_merge_split/common.py::indexed_paths_with_conflict_markers()` 成为唯一检测入口，
  `repository_ops.find_staged_conflict_marker_paths` 与 `file_conflicts.resolve_single_file_conflict`
  都改为调用它。原先 `file_conflicts.py` 判定 `"leftover conflict marker" in output.lower()`——
  中文 git 输出「残留的冲突标记」，该判定恒为 False，外部工具关闭后的守卫完全失效。
- 新逻辑用 `git grep --cached -I -l -E` 直接检查索引内容，与 git 的诊断文案无关，
  并且检查的是"将被提交的内容"而非工作区。

#### 已完成 #6 — `commit_if_needed` 不再静默放行

- `manual_merge_split/repository_ops.py::commit_if_needed` 在仍有 unmerged 路径时抛出并列出路径，
  而不是静默 `return`。原先 `_start_merge` 在 `_resolve_active` 之后没有 unmerged 复查
  （`_resume` 有），静默返回会让流程一路走到 "Recursive merge completed successfully"。

#### 已完成 #7 — 递归前 submodule update 失败可追溯

- `merge_tool_adapters/manual_semiauto.py::ManualWorkflowGitlinkPolicy.prepare_recursive_merge`
  仍然用 `check=False`（引擎会在下降前复查初始化状态并可补 fetch，强制失败会破坏这条恢复路径），
  但失败现在会写入 `policy.events`，让后续的 "submodule is not initialized" 有上下文可查。

#### 已完成 #8 — session 陈旧锁恢复

- `manual_merge_split/session_store.py`
  - 新增 `process_is_alive()`：Windows 走 `OpenProcess` + `GetExitCodeProcess`。
    **不能用 `os.kill(pid, 0)`** —— CPython 在 Windows 上把非 CTRL_* 信号映射为
    `TerminateProcess`，探测会直接杀掉持锁进程。
  - 新增 `read_lock_owner_pid()` / `_break_stale_lock()`：锁文件里的 pid 已死则自动释放并在 stderr 提示。
    pid 无法解析的锁**不会**被破坏（不是本代码路径写的，破坏它属于猜测），错误信息改为告知用户可手动删除。
  - 锁文件写入后加 `os.fsync`。

**新增回归测试**（`test_manual_review_fixes.py`，累计 34 项）：

| 覆盖点 | 测试类 |
|---|---|
| unmerged 时拒绝提交、非英文 locale 下标记检测仍有效 | `CommitGuardTests` |
| 死进程锁自动释放 / 活进程锁保留 / 无法解析的锁保留 / 存活探测 | `StaleSessionLockTests` |

```
Ran 49 tests in 25.999s   (test_manual_review_fixes + test_manual_cleanup_fallback + test_manual_session_store)
OK
```

**过程中发现并修正的设计问题**：最初把"只对已初始化子模块生成 level 4"写进了
`_build_cleanup_levels`，导致这个纯函数依赖文件系统，并让既有单元测试
`test_cleanup_builds_four_ordered_levels_for_submodules` 变红。改为在
`cleanup_repository` 调用点用新方法 `initialized_submodule_paths()` 过滤，
`_build_cleanup_levels` 保持纯粹。

#### 已完成 #9 — 清理升级可见化

- 影响分析：`impact("resolve_active_conflicts", upstream)` = **HIGH**（14 个受影响符号、5 个直接调用者、
  4 条执行流）。改动只在方法体内、不动签名，故继续。
- `manual_merge_split/repository_ops.py::_announce_cleanup_escalation()`：升级到 level 2/3/4 之前
  向 stderr 打印原因和该级别将执行的完整命令列表。此前升级只在 `--verbose` 下可见，
  而 level 2 起就带 `git clean -ffd`（会删除嵌套 git 仓库）——用户只对"清理"确认了一次，
  却可能在没有任何提示的情况下丢掉一个嵌套仓库。
- `manual_merge_split/models.py::CleanupResult.escalated_levels` 记录每次升级，
  `run_cleanup` 在结尾汇总打印。

#### 已完成 #10 — cleanup 失效 session

- `manual_merge_split/commands.py::_discard_session_for_cleanup()` 在清理开始前删除已保存的会话。
  清理会把每个仓库 reset 到 HEAD，会话里的 frame（`original_head` / `expected_merge_head`）随之失效；
  留着它意味着之后的 `resume` 会拿旧 frame 去对一个已经不匹配的仓库。删除动作会在结果里明确告知用户。

#### 已完成 #11 — `ignored_root_untracked_paths` 真正生效

- `cleanup_repository` 现在把该参数传给两处 `working_tree_has_changes`（"是否脏"判定和每级清理后的复查）。
  此前参数被接收但完全没用到。
- 可观测差异：只有一个未跟踪报告文件时，`result.hard_resets` 不再误报该仓库被硬重置。
  注意 `git clean -fd` 本来就会删掉该文件，所以这个修复的收益是**报告准确性**而非行为改变——
  回归测试因此断言 `hard_resets` 而不是"文件存活"。

#### 已完成 #12 — abort 的恢复范围

- `merge_tool_adapters/manual_semiauto.py`：新增 `on_gitlink_conflict` 回调，从 `on_event` 的
  `gitlink.conflict` 事件分发。选这个事件是因为它在引擎动子模块**之前**触发，
  是唯一还能记录子模块原始状态的时机，而且**每个** gitlink 都会经过它，不只是需要递归合并的那些。
- `manual_merge_split/session_workflow.py`：注册该回调，把子模块的 `original_head` / `original_branch`
  写入 `touched_frames`。此前只有 `on_prepare_recursive_merge` 会写，
  所以被快进/取 theirs 解决的子模块（同样被 detach 到新 commit 并移动了工作分支）abort 时不会被恢复。
- 新增 `_frames_to_restore()`：合并 `repo_stack` 与 `touched_frames`、按路径深度倒序返回。
  原来的 `touched_frames or repo_stack` 是二选一——一旦有 touched_frames，
  根仓库（只存在于 repo_stack）就不会被恢复。
- 残留（未修）：abort 不会删除在子模块里新建的工作分支。该分支可能在合并前就存在，
  删除它需要额外判断，超出本轮范围。

#### 已完成 #13 — 删除并行的清理阶梯

- 删除 `merge_cleanup_core/steps.py` 里的 `manual_prepare_cleanup_levels()` 与
  `manual_merge_todo_file_step()`。两者零 Python 引用、且未从 `merge_cleanup_core/__init__.py` 导出，
  唯一作用是与真正在跑的 `_build_cleanup_levels` 慢慢分歧。原位留了注释说明真实入口在哪。
  （`implementation_plan.md` 里"保留供向后兼容"的说明因此过期。）

#### 已完成 #15 — 引擎文件冲突循环

- `split_gitlink_merge/engine.py::resolve_active_conflicts`：删掉同一轮里对
  `handle_file_conflicts` 的第二次重复调用，改为要求每轮必须有进展——
  若剩余冲突路径集合与本轮处理的完全相同，抛 `GitlinkMergeError`。
  原逻辑下一个既不解决也不抛错的 handler 会让 `while True` 永久空转。

#### 已完成 #16 — 分类与理由合并为单一判定

- `manual_merge_split/conflict_classifier.py`：`_classify_one` + `_reason_for` 两条并行阶梯
  合并为 `evaluate_conflict()`，同时返回 disposition 和 reason。
  两条阶梯的 mode 集合本来不一致（`{100644,100755}` vs `{100644,100755,160000}`），
  文件与 gitlink 混合的冲突会得到"MANUAL_TAKEOVER + 空 reason"，暂停报告里只有路径没有原因。
  同时补上了"stage 越界"这一 FATAL 分支缺失的理由文案。

#### 已完成 #17 — 修复两个常红测试（均为测试自身问题，非产品缺陷）

- `test_packaging_manifest.py`：`test_non_test_python_files_do_not_embed_developer_machine_paths`
  用 `rglob("*.py")` 扫描，把被 `.gitignore` 忽略的本地复现目录
  （`.gitignore:7` 的 `manual_*_run*/`，其中 `run_automated_verification.py` 未被 git 跟踪）
  也算成了违规。改为只扫描 `git ls-files` 列出的文件——测试的本意就是"随包发布的源码"。
- `test_ci_command_reporting.py`：`test_serialization_and_manual_publish_helpers` 把
  `Path("/tmp/repo")` 的序列化结果硬编码为 `/tmp/repo`，Windows 上是 `\tmp\repo`。
  改为用 `str(Path(...))` 构造期望值。

---

## 不修的项及理由

### #14 `process_repository` / `ManualMergePolicy` / `ManualSemiAutoMergeService`

复核后发现这些**不是死代码**：

- `process_repository` 被 `ManualCommandService.process_repository` 门面调用，
  并被 3 个测试模块（`test_manual_core_integration`、`test_remaining_split_facades`、自身）使用。
- `ManualSemiAutoMergeService` / `ManualMergePolicy` 从 `merge_tool_adapters/__init__.py` 导出，
  属于该包的公开 API，并有 `test_adapters.py` 覆盖。

原始发现里有价值的部分是**分歧风险**：`ManualMergePolicy` 缺少
`ManualWorkflowGitlinkPolicy` 具备的 fetch / post-merge guard / 混合冲突门禁，
任何改用前者的调用方会静默拿到更弱的安全保证。删除公开 API 不是合适的应对；
正确的处理是在这里记录该差异。生产 CLI 路径走的是 `ManualWorkflowGitlinkPolicy`。

### #18 `test_remaining_split_facades` 的默认 skip

`ExtractedImplementationParityTests` 通过 `setUpClass` 中的 `unittest.SkipTest` 跳过，
条件是外部 legacy 工程（`CI_LEGACY_PROJECT_ENVVAR` / `MANUAL_LEGACY_PROJECT_ENVVAR`）不可用。
这些是与已迁移的旧工程做**逐函数一致性对比**的可选测试，本仓库里不存在对应工程，
默认跳过是正确设计，且 unittest 已把它报为 skip（每个类聚合为 1 条）。
唯一需要记住的是：套件标题里的用例总数**包含**这些不会在本仓库跑的对比测试，
不要把它当作本仓库的覆盖度。

### 已完成 #19 — publish 检查阶段不再静默改工作树

原先暂缓是因为 `publish.py` 有大量未提交改动。那些改动已提交（`publish.py` 工作树干净），本项落地。

- 影响分析：`impact("run_publish_check", upstream)` = **LOW**。
- 行为不变：检查阶段仍必须 `submodule update --init --recursive` 才能看到嵌套 gitlink。
- 改为在执行前向 stderr 说明这一点，`--check-only` 帮助文本、GUI「仅检查」选项和用户文档同步写明：仅检查仍会初始化子模块并可能移动其工作区。

---

## 交叉 Review 跟进（2026-09-07 下午）

外部交叉 review 指出 6 项，源码对照 + 实证后全部成立。其中 5 项是上一轮修复的边界遗漏，
#20 是原先暂缓项、本轮一并落地。

### 已完成 #21 — `ACMRT` 覆盖 type change

- 影响分析：`impact("staged_changed_paths", upstream)` = **LOW**（唯一调用方
  `find_staged_conflict_marker_paths` → `_guard_staged_conflict_markers`）。
- `staged_changed_paths` 的 `--diff-filter` 从 `ACMR` 改为 `ACMRT`。
  实证：HEAD 为 symlink（`120000`）、索引换成带 `<<<<<<<` 的普通文件时，
  git 报 `T`，`ACMR` 候选集为空，守卫认为安全并提交。
- 回归测试用 `update-index --cacheinfo` 强制构造 `120000 → 100644`，
  不依赖 Windows 是否启用真实 symlink。

### 已完成 #22 — cleanup 持命令锁，session 失效失败则停止

- `run_cleanup` 图上 0 调用边（UNKNOWN）；文本搜索确认调用方为
  `ManualCommandService.run_cleanup`、`runtime.main`、`guided_commands`。
- 确认提示之后，用同一个 `MergeSessionStore.command_lock()` 包住
  「清除 session + 整个 cleanup」。另一个 merge/resume/abort 正在跑时会拒绝清理，
  不再并发 `reset --hard`。
- `_discard_session_for_cleanup` 在 `load`/`clear` 失败时抛错并中止清理，
  不再打印后继续。`--force` 只用于删掉无法读取的 session 文件后继续；
  **不会**绕过仍被活进程持有的 command lock。

### 已完成 #23 — abort 还原合并中才初始化的子模块

- `on_gitlink_conflict` 不再在缺少 `.git` 时直接返回；改为写入
  `RepositoryFrame.was_initialized=False`（旧 session 缺该字段时默认为 `True`）。
- `_frames_to_restore` 让 `touched_frames`（冲突当时的快照）优先于
  `repo_stack`（初始化之后的 head），避免递归合并把"未初始化"覆盖成"已初始化"。
- abort 对 `was_initialized=False` 的 frame 执行 `git submodule deinit -f`。
  abort 本来就需要 `--yes`；stderr 会说明这是在还原本次合并初始化的子模块。

### 已完成 #24 — Windows 按 `GetLastError` 区分死亡与拒绝访问

- `OpenProcess` 失败时：`ERROR_INVALID_PARAMETER` (87) 才视为进程不存在；
  `ERROR_ACCESS_DENIED` (5) 及其它错误视为仍存活，与 POSIX 分支的
  `PermissionError → True` 对齐。
- 实证：PID 4（System）原先被误判死亡，现在返回存活。回归测试在 Windows 上
  用 PID 4 锁文件确认不会被 `_break_stale_lock` 拆掉。

### 已完成 #20 — publish 补推解析固定 C locale

- `execution.run_git` / `run_git_binary` 增加可选 `env_overrides`（默认 `None`，不改现有调用）。
- `_run_checked_push` 注入 `LC_ALL=C`、`LANG=C`、`LANGUAGE=C`，
  使 `parse_missing_submodule_paths` 的英文正则与 git 输出一致。

### 已完成 #25 — 打包路径检查失败即失败

- `git ls-files` 加上 `-c safe.directory=*`。
- git 可执行文件不存在 → `SkipTest`；git 在场但命令失败 → `AssertionError`。

### 已完成 #26 — abort deinit 失败不再假装成功

- `_deinit_merge_initialized_submodule` 检查 `deinit` 返回码；非 0 或缺少 parent gitlink
  元数据时抛错。`_abort` 在 `store.clear` 之前抛出，session 留在 `ABORTING` 供重试。
- `_abort` 改为两遍恢复：先最深优先还原 `was_initialized=True` 的仓库（父仓库
  `reset --hard` 会把冲突的 `.gitmodules` 还原），再 deinit 未初始化子模块。
  影响分析：`impact("_abort", upstream)` = **LOW**。

---

## 最终验证

```
python -m unittest discover -s tests_split_gitlink_merge -t .
Ran 212 tests in 168.781s
OK
```

`detect_changes --scope all`：`changed_count=171`、`affected_count=63`、`changed_files=24`、
`risk_level=critical`，无 `partial` / `truncated`。该 critical 评级是整个改动集的聚合结果，
且包含本轮未触碰的 publish 进行中工作。本轮逐符号影响：
`staged_changed_paths`=LOW、`process_is_alive`=LOW、`_abort`=LOW、`RepositoryFrame`=LOW；
`run_cleanup` / `execution.run_git` 图上 UNKNOWN，已用文本搜索确认调用方，
改动为持锁包裹和可选关键字参数，未改变既有位置参数签名。

> 备注：工作区在本次修复开始前已有未提交的 publish 相关改动
> （`publish.py`、`publish_dialog.py`、`models.py`、`recursive_submodule_merge_gui.py`
> 及对应测试）。这些不属于本轮修复；本轮只在 `publish.py` 的 `_run_checked_push`
> 增加了 locale 注入。
