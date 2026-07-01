# merge-report.json 字段说明

本文档说明 CI 自动合并生成的 `merge-report.json` 主要字段。它面向 CI 消费者、排障人员和 review HTML/JSON 的下游工具。

## 1. 总体约定

- `schema_version`：报告结构版本，目前为 `1.0`。
- 时间字段使用 ISO 8601 字符串，通常带本地时区，例如 `2026-06-04T14:57:33+08:00`。
- 路径字段一般是绝对路径；artifact 同时会提供相对 `output_dir` 的 `relative_path`。
- `null` 通常表示该阶段未执行、尚未解析出结果，或当前场景不适用。
- 空数组表示该类记录已初始化，但当前没有条目。
- 下游系统应优先读取 JSON 字段，不建议解析 console log 或 `merge.log`。

## 2. 顶层状态字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | string | 报告结构版本。 |
| `status` | string | 总体结果：`running`、`success`、`failed`、`partial_success`。 |
| `state` | string | 当前或最终流程阶段，例如 `INIT`、`PREFLIGHT`、`MERGING_ROOT`、`SUCCEEDED`、`FAILED`。 |
| `exit_code` | number/null | 进程退出码。成功为 `0`；`partial_success` 为 `10`；失败按错误类型映射。 |
| `started_at` | string | report 创建时间。 |
| `ended_at` | string/null | report finalize 时间。 |
| `duration_ms` | number/null | 从创建 report 到 finalize 的耗时。 |
| `merge_attempted` | boolean | 是否实际进入 merge 尝试。早期失败时可能为 `false`。 |
| `publish_attempted` | boolean | 是否实际尝试 publish。 |
| `failure_phase` | string | 失败阶段，仅失败时出现。 |
| `failure_reason` | string | 失败原因，仅失败时出现。 |
| `failure_message` | string | 失败说明，仅失败时出现。 |

## 3. `input`

记录本次运行的归一化输入参数快照。

| 字段 | 含义 |
|---|---|
| `source` | 单分支模式的来源 ref；batch 模式下通常是第一个来源。 |
| `sources` | 所有来源 ref。长度大于 1 时启用 batch。 |
| `target` | 合并目标 ref。 |
| `repo_path` | 根仓库路径。 |
| `remote` | 远端名，默认 `origin`。 |
| `publish` | 本次是否要求合并后发布。 |
| `publish_only` | 是否只从已有 report 执行发布。 |
| `allow_partial_publish` | publish-only 是否允许发布 `partial_success` report。 |
| `from_report` | publish-only 使用的源 report 路径。 |
| `work_branch` | CI 本地工作分支名。 |
| `timeout` | 单条 Git 命令超时时间，单位秒。 |
| `max_depth` | 递归子模块最大深度。 |
| `submodule_fetch_scope` | 子模块 fetch 范围：`needed` 或 `all`。 |
| `git_network_retries` | 网络相关 Git 命令最大重试次数。 |
| `git_network_retry_delay` | 网络重试间隔，单位秒。 |
| `diff_context_lines` | 导出 patch 时的上下文行数。 |
| `allow_clean_x` | cleanup 是否允许 `git clean -fdx`。 |
| `console_log` | 是否输出更详细的 console 进度日志。 |
| `allow_create_publish_target` | publish 时是否允许创建目标分支。 |

## 4. `output`

记录 report 和 artifact 的输出位置。

| 字段 | 含义 |
|---|---|
| `output_dir` | 所有输出材料的根目录。 |
| `report_path` | `merge-report.json` 路径。 |
| `log_path` | 命令文本日志路径。 |
| `patch_dir` | repository/file patch 输出目录。 |
| `conflict_dir` | 冲突 stage/worktree artifact 输出目录。 |
| `file_dir` | 变更文件内容快照输出目录。 |
| `generated_file_inside_repo` | 输出路径是否位于目标仓库内部。为 `true` 时会附加 warning。 |

## 5. `resolved`

记录被解析后的 ref 和 SHA。

| 字段 | 含义 |
|---|---|
| `source_ref` | 实际解析到的来源 ref。 |
| `source_sha` | 来源 commit SHA。 |
| `target_ref` | 实际解析到的目标 ref。 |
| `target_sha` | 目标 commit SHA。 |
| `merge_base_sha` | source 与 target 的 merge-base SHA。 |
| `result_sha` | 最终合并结果 SHA。失败或未提交时为 `null`。 |

batch 场景中，`resolved` 最终指向整体 batch 的最后成功结果；每个 step 的解析信息在 `batch.steps[]` 内。

## 6. `batch`

batch 模式用于多个 `sources` 顺序合并。

| 字段 | 含义 |
|---|---|
| `enabled` | 是否启用 batch。 |
| `source_count` | 来源分支数量。 |
| `initial_target` | batch 开始时的目标 ref。 |
| `original_target_ref` / `original_target_sha` | 第一步解析到的原始目标。 |
| `final_result_sha` | 最后一个成功 step 的结果 SHA。 |
| `failure_step` | fail-fast 或无法继续时的失败 step 序号。 |
| `steps` | 每个来源分支的执行记录。 |
| `manual_publish` | batch 成功或部分成功后建议的手动发布信息。 |
| `failure_mode` | 失败策略：`continue-success` 或 `fail-fast`。 |
| `status` | batch 状态：`running`、`success`、`failed`、`partial_success`。 |
| `successful_steps` | 成功 step 序号列表。 |
| `failed_steps` | 失败 step 序号列表。 |
| `errors` | batch step 失败摘要。 |

### 6.1 `batch.steps[]`

| 字段 | 含义 |
|---|---|
| `index` / `total` | 当前 step 序号和总数。 |
| `source` / `target` | 当前 step 使用的 source 和 target。 |
| `status` | `running`、`success`、`failed` 等。 |
| `started_at` / `ended_at` | step 起止时间。 |
| `source_ref` / `source_sha` | 当前 source 解析结果。 |
| `target_ref` / `target_sha` | 当前 target 解析结果。 |
| `merge_base_sha` | 当前 step 的 merge-base。 |
| `result_sha` | 当前 step 成功后的结果 SHA。 |
| `strategy` | 当前 step 的合并策略，例如 `clean_merge`、`recursive_gitlink_merge`。 |
| `pre_step_cleanup` | 当前 step 进入 preflight 前执行的工作区清理摘要。 |
| `preflight` | 当前 step 的 preflight 摘要。 |
| `failure` | 当前 step 失败详情，结构同 `errors[]`。 |
| `command_range` | 当前 step 覆盖的 `commands[]` 范围。 |
| `artifact_scope` | 当前 step artifact 输出目录。 |
| `continued_after_failure` | `continue-success` 下失败后是否恢复并继续后续 step。 |
| `restore_after_failure` | 失败后恢复到上一成功 SHA 的结果。 |

`pre_step_cleanup` 常见字段：

| 字段 | 含义 |
|---|---|
| `attempted` | 是否尝试 step 前清理。 |
| `success` | step 前清理是否成功。 |
| `started_at` / `ended_at` | step 前清理起止时间。 |
| `fallback_range` | 本 step 前清理写入 `fallbacks[]` 的范围。 |
| `error` | 清理失败时的结构化错误摘要。 |

## 7. `remote_sync`

记录 merge 前远端同步和子模块 fetch 情况。

| 字段 | 含义 |
|---|---|
| `attempted` | 是否执行 remote sync。 |
| `success` | sync 是否成功；未执行时为 `null`。 |
| `remote` | 远端名。 |
| `recursive_submodules` | 是否按递归子模块处理。 |
| `submodule_fetch_scope` | 用户请求的 fetch 范围。 |
| `submodule_fetch_scope_effective` | 实际执行的 fetch 范围，可能因 fallback 从 `needed` 变为 `all`。 |
| `submodule_fetches` | 每个子模块 fetch 的记录。 |
| `submodule_updates` | 为初始化必要子模块执行的 update 记录。 |
| `submodule_fetch_fallback_reasons` | 从 `needed` fallback 到 `all` 的原因列表。 |
| `started_at` / `ended_at` | sync 起止时间。 |

## 8. `summary`

用于 CI dashboard 或快速判断的计数摘要。

| 字段 | 含义 |
|---|---|
| `merged_repositories` | 写入 `repositories[]` 的仓库数量。 |
| `submodules_processed` | 处理过的 gitlink 子模块数量。 |
| `submodule_recursive_merges` | 发生递归合并的子模块数量。 |
| `fast_forward_submodules` | 可直接快进或使用 theirs 的子模块数量。 |
| `conflicts` | `conflicts[]` 当前条目数量。 |
| `fallbacks` | `fallbacks[]` 当前条目数量。 |

## 9. `preflight` 与 `preflight_runs`

`preflight` 是顶层快捷视图，通常同步 `label="initial"` 的检查结果。`preflight_runs[]` 保留每次 preflight 的完整记录，例如 initial、batch step、final。

| 字段 | 含义 |
|---|---|
| `passed` | 检查是否通过。 |
| `checks` | 所有检查项。 |
| `failed_checks` | 未通过的检查项。 |
| `label` | `preflight_runs[]` 中的运行标签。 |
| `timestamp` | `preflight_runs[]` 中该次检查记录时间。 |

常见 `checks[].name` 包括：`repo_path_exists`、`repo_path_is_directory`、`is_git_repository`、`resolve_toplevel`、`no_operation_in_progress`、`working_tree_clean`、`gitmodules_parseable`、`submodules_initialized_and_at_head`、`resolve_source`、`resolve_target`、`source_target_have_merge_base`。

## 10. `repositories[]`

每个参与合并并成功收集详情的仓库一条记录。根仓库 `path` 通常是 `.`；子模块会使用相对显示路径。

| 字段 | 含义 |
|---|---|
| `path` | 仓库显示路径。 |
| `repo_path` | 仓库绝对路径。 |
| `base` | merge-base SHA。 |
| `ours` | 合并前目标侧 SHA。 |
| `theirs` | 来源侧 SHA。 |
| `result` | 合并结果 SHA。 |
| `strategy` | 合并策略，例如 `clean_merge`、`recursive_gitlink_merge`、`gitlink_update`。 |
| `commits` | `base..result` 的提交摘要。 |
| `changed_files` | `ours..result` 的变更文件列表。 |
| `diff_stat` | `git diff --stat` 文本。 |
| `patch_path` / `patch_relative_path` | 仓库级 patch artifact。 |
| `patches` | 仓库级和文件级 patch artifact 详情。 |
| `content_artifacts` | 变更文件的 ours/result 内容快照。 |
| `warnings` | 收集该仓库 artifact 时产生的 warning。 |

当某个仓库的 `ours..result` 中包含 mode 仍为 `160000` 的 submodule SHA 更新时，CI 会 best-effort 继续为该子模块写入一条 `repositories[]` 记录。此类记录通常使用：

- `base = old submodule SHA`
- `ours = old submodule SHA`
- `theirs = new submodule SHA`
- `result = new submodule SHA`
- `strategy = gitlink_update`

如果该子模块 diff 已经由递归 gitlink 冲突解决路径导出，CI 会按 artifact prefix、repo path、ours SHA、result SHA 去重，避免重复记录。

### 10.1 `changed_files[]`

| 字段 | 含义 |
|---|---|
| `status` | Git name-status 状态，例如 `A`、`M`、`D`、`R100`。 |
| `path` | 新路径或当前路径。 |
| `old_path` | rename/delete 等场景下的旧路径。 |
| `kind` | 可选分类。子模块指针更新会标记为 `gitlink`。 |
| `old_sha` / `new_sha` | `kind=gitlink` 时的旧/新子模块 commit SHA。 |
| `old_mode` / `new_mode` | `kind=gitlink` 时通常均为 `160000`。 |
| `patch_path` / `patch_relative_path` | 文件级 patch artifact。 |
| `content_artifacts` | 对应文件内容快照 artifact。 |

Code Review HTML 会把 `kind=gitlink` 的条目作为子模块指针变更单独折叠展示；读取旧 report 时也会 best-effort 根据文件级 patch 中的 `Subproject commit`/`160000` 识别。

## 11. `conflicts[]`

冲突列表是 review HTML 和外部排障工具展示冲突文件的主要来源。

| 字段 | 含义 |
|---|---|
| `repo_path` | 发生冲突的仓库路径。 |
| `path` | 冲突文件或 gitlink 路径。 |
| `type` | 冲突类型。 |
| `kind` | core 层分类，可选字段；例如 `file`、`gitmodules`、`unsupported_stage`、`gitlink`。 |
| `stages` | unmerged index stage 信息。 |
| `action` | 建议动作，例如 `manual_required` 或 `recursive_merge`。 |
| `artifacts` | 冲突 stage、worktree、metadata artifact。 |

常见 `type`：

| type | 含义 |
|---|---|
| `file_conflict` | 普通文件冲突。 |
| `gitmodules_conflict` | `.gitmodules` 冲突。 |
| `unparseable_conflict` | 无法解析的冲突状态。 |
| `unsupported_conflict_stage` | stage 组合不受自动处理支持。 |
| `gitlink_conflict` | 子模块 gitlink 冲突，通常可递归处理。 |
| `missing_submodule_path` | gitlink 指向的子模块路径缺失。 |
| `submodule_not_initialized` | 子模块未初始化或不是可用 worktree。 |

`NON_GITLINK_CONFLICT` 失败时，`errors[].details.conflicts` 和顶层 `conflicts[]` 会包含完整的非 gitlink 冲突列表；`errors[].details.conflict_paths` 是对应路径列表，`conflict_count` 是数量。混合冲突中，已经可递归处理的 gitlink 路径不会被误写成需要人工处理的非 gitlink 冲突。

### 11.1 `conflicts[].stages[]`

| 字段 | 含义 |
|---|---|
| `stage` | Git index stage，`1` 为 base，`2` 为 ours，`3` 为 theirs。 |
| `mode` | Git 文件模式，例如普通文件 `100644`、gitlink `160000`。 |
| `sha` | stage 对应对象 SHA。 |
| `path` | stage 对应路径。 |

### 11.2 `conflicts[].artifacts`

| 字段 | 含义 |
|---|---|
| `artifact_dir` / `artifact_relative_dir` | 该冲突 artifact 目录。 |
| `stages` | base/ours/theirs 对应内容 artifact。 |
| `worktree` | 工作区冲突文件 artifact。 |
| `metadata` | 冲突 metadata JSON artifact。 |
| `warnings` | 导出 artifact 时产生的 warning。 |

注意：如果冲突来自异常转换路径，可能只记录结构化冲突字段，不一定有 `artifacts`。

## 12. `fallbacks[]` 与 `snapshots[]`

| 字段 | 含义 |
|---|---|
| `fallbacks[]` | 自动流程中触发降级路径的记录，例如 fetch scope fallback、保守处理路径等。 |
| `snapshots[]` | 为失败恢复创建的快照记录。 |

具体条目结构按触发点不同而变化，下游应按 `reason` 或显式字段做容错读取。

CI cleanup fallback 常见字段：

| 字段 | 含义 |
|---|---|
| `level` | 清理层级，例如 `level_1_basic_cleanup`、`level_4_deep_submodule_cleanup`、`level_5_fresh_clone_workspace`。 |
| `repo_path` | 被清理或替换的仓库路径。 |
| `success` | 本层 fallback 是否成功。 |
| `steps` | 本层执行的 Git 或文件系统步骤摘要。 |
| `verify_errors` | fallback 执行后仓库仍不满足 ready 条件的原因。 |
| `scope` | 触发清理的上下文；batch step 前清理会包含 `type=batch_step_start`、`step_index`、`source`、`target`。 |
| `failure_reason` / `failure_detail` | 本层失败原因和细节。 |
| `backup_path` / `staging_path` | fresh clone fallback 使用的旧工作区备份路径和临时 clone 路径。 |
| `preserved_refs` | fresh clone fallback 成功从旧工作区保留的中间提交或 ref。 |

## 13. `publish`

记录合并结果发布状态。

| 字段 | 含义 |
|---|---|
| `enabled` | 是否启用 publish。 |
| `attempted` | 是否实际尝试 publish。 |
| `success` | publish 是否成功。 |
| `remote` | 发布远端。 |
| `target` | 发布目标 ref。 |
| `mode` | 发布模式，例如 `force-with-lease`。 |
| `candidate_ref` | 可选候选 ref。 |
| `allow_create_target` | 是否允许创建目标。 |
| `target_created` | 是否实际创建目标。 |
| `target_publish_mode` | 实际目标发布方式。 |
| `root` | 根仓库发布结果。 |
| `submodules` | 子模块发布结果集合。 |

## 14. `publish_only`

记录 `--publish_only true` 从已有 report 发布时的上下文。

| 字段 | 含义 |
|---|---|
| `enabled` | 是否启用 publish-only。 |
| `from_report` | 用户传入的源 report 路径。 |
| `allow_partial_publish` | 是否允许发布 `partial_success` report。 |
| `partial_publish` | 本次是否按 partial publish 执行。 |
| `source_report_status` | 源 report 的状态。 |
| `source_report_path` | 解析后的源 report 路径。 |

## 15. `commands[]`

所有 Git 命令的结构化审计记录。

| 字段 | 含义 |
|---|---|
| `cmd` | 命令数组。 |
| `cwd` | 命令工作目录。 |
| `exit_code` | 命令退出码。 |
| `stdout` / `stderr` | 命令输出。 |
| `started_at` / `ended_at` | 命令起止时间。 |
| `duration_ms` | 命令耗时。 |
| `allow_failure` | 该命令失败是否允许由上层逻辑继续处理。 |
| `timed_out` | 是否超时。 |
| `attempt` / `max_attempts` | 当前尝试次数和最大尝试次数。 |
| `network_failure_reason` | 网络失败分类；非网络失败为 `null`。 |

batch step 可以通过 `batch.steps[].command_range` 定位到对应的命令区间。

## 16. `errors[]`、`warnings[]` 与 `restore`

### 16.1 `errors[]`

| 字段 | 含义 |
|---|---|
| `exit_code` | 对应错误退出码。 |
| `phase` | 错误发生阶段。 |
| `reason` | 稳定的机器可读失败原因。 |
| `message` | 人类可读错误说明。 |
| `details` | 结构化上下文，随错误类型变化。 |
| `timestamp` | 错误记录时间。 |

下游自动判断失败类型时，优先使用 `failure_reason` 或 `errors[].reason`。

### 16.2 `warnings[]`

warning 不一定导致失败。每条通常包含：

| 字段 | 含义 |
|---|---|
| `reason` | warning 类型。 |
| `message` | 说明。 |
| `details` | 可选结构化上下文。 |
| `timestamp` | 记录时间。 |

### 16.3 `restore`

| 字段 | 含义 |
|---|---|
| `attempted` | 失败后是否尝试恢复工作区。 |
| `success` | 恢复是否成功。 |
| `errors` | 恢复过程错误。 |
| `results` | 每个快照或恢复步骤的结果。 |

## 17. 推荐消费方式

- 判断流水线结果：读取 `status`、`exit_code`、`failure_reason`。
- 判断 batch 可发布结果：读取 `status == "partial_success"`、`batch.final_result_sha`、`batch.successful_steps`、`batch.failed_steps` 和 `batch.manual_publish`。
- 展示完整冲突文件：读取顶层 `conflicts[]`；如果是 `NON_GITLINK_CONFLICT`，也可读取 `errors[].details.conflict_paths`。
- 定位某个 batch step 的命令：读取 `batch.steps[].command_range` 后切片 `commands[]`。
- 展示 review 材料：读取 `repositories[]`、`changed_files[]`、`patches`、`content_artifacts` 和 `conflicts[]`。
- 判断是否需要人工恢复：读取 `restore.success`、`restore.errors` 和失败错误的 `details`。
