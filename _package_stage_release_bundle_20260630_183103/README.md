# Recursive Submodule Merge Tool

递归子模块合并工具用于处理根仓库和多层 Git submodule 之间的 gitlink 合并问题。项目同时提供 CI 自动合并流程和手动/GUI 辅助合并流程：CI 模式强调无人值守、结构化输出和可恢复性；手动模式强调保护本地现场、辅助人工处理冲突和安全发布。

## 适用场景

- 根仓库引用多个子模块，分支合并时经常出现 `160000` gitlink 冲突。
- 子模块内部也存在嵌套子模块，需要递归合并。
- CI 需要自动生成合并候选提交、报告、补丁、冲突材料，并在安全条件下发布。
- 本地手动合并需要保留未受影响的 dirty submodule，并用外部冲突工具处理普通文件冲突。
- 多分支按顺序合并，部分分支失败时希望继续合并后续分支并产出可审计结果。

## 快速开始

源码方式运行 CI 合并：

```powershell
python -m ci_merge_split --source origin/feature --target origin/main --repo_path C:\path\to\repo --report merge-report.json
```

多分支 CI 合并：

```powershell
python -m ci_merge_split --source origin/feature-a --source origin/feature-b --target origin/main --repo_path C:\path\to\repo --batch_failure_mode continue-success
```

源码方式运行手动合并：

```powershell
python -m manual_merge_split merge origin/feature --repo C:\path\to\repo --base-remote-branch origin/main
```

手动发布当前根仓库 HEAD 以及它引用的子模块提交：

```powershell
python -m manual_merge_split publish --repo C:\path\to\repo --remote origin --push-target main
```

GUI 入口：

```powershell
python recursive_submodule_merge_gui.py
```

生成 CI Code Review HTML：

```powershell
python ci_merge_review.py html --report C:\path\to\output\merge-report.json
```

默认会在 report 同目录生成 `merge-diff-review.html`。也可以用兼容入口：

```powershell
python ci_merge_diff2html.py --report C:\path\to\output\merge-report.json --output C:\path\to\output\merge-diff-review.html
```

记录 review 结论：

```powershell
python ci_merge_review.py record --report C:\path\to\output\merge-report.json --decision APPROVED --reviewer qs --notes "reviewed"
```

默认生成 `merge-review.json` 和同名 Markdown 摘要；`--decision` 支持 `APPROVED`、`NEEDS_CHANGES`、`REJECTED`、`INFO_ONLY`。

## 主要能力

- 自动识别 gitlink 冲突、普通文件冲突、`.gitmodules` 冲突和不完整 index stage。
- 对可自动处理的 gitlink 冲突执行递归子模块合并。
- 对 fast-forward、already-contains、ours/theirs 与真正递归合并场景做不同决策。
- CI 模式输出 JSON 报告、命令日志、patch、冲突 stage 文件和变更文件材料。
- CI review 工具可基于 `merge-report.json` 生成离线 HTML review 页面，并导出结构化 review 结论。
- CI 多分支 batch 默认使用 `continue-success`：失败分支会记录为 failed step，仓库恢复到最近成功提交后继续后续分支。
- 手动模式支持 dirty submodule 影响分析，只允许“本次合并不会触碰 gitlink”的 dirty submodule 被保留。
- 手动模式支持 cleanup、guided CLI、GUI 入口和递归 publish。

## 目录结构

| 路径 | 职责 |
|---|---|
| `split_gitlink_merge/` | 公共递归 gitlink 合并核心，不绑定 CI 或 GUI 策略。 |
| `merge_tool_adapters/` | CI 和手动流程接入公共核心的 adapter。 |
| `ci_merge_split/` | CI 运行时、preflight、报告、发布、恢复和结构化错误处理。 |
| `manual_merge_split/` | 手动 CLI、preflight、冲突处理、cleanup、publish。 |
| `merge_cleanup_core/` | CI 与手动可复用的 cleanup step 定义。 |
| `recursive_submodule_merge_gui.py` | Tkinter 桌面 GUI。 |
| `tests_split_gitlink_merge/` | 核心逻辑、CI、手动、GUI、发布等测试。 |

## 文档索引

- [公共核心详细设计](docs/COMMON_CORE_DESIGN.md)
- [CI 自动合并详细设计](docs/CI_DESIGN.md)
- [手动/GUI 合并详细设计](docs/MANUAL_DESIGN.md)
- [merge-report.json 字段说明](docs/MERGE_REPORT_SCHEMA.md)

现有 [REPORT_MATERIALS.md](REPORT_MATERIALS.md) 偏汇报材料，[MERGECORE_ADAPTATION_PLAN.md](MERGECORE_ADAPTATION_PLAN.md) 偏重构过程记录；维护和二次开发优先阅读 `docs/` 下的设计文档。

## 测试

运行完整测试集：

```powershell
python -m unittest discover -s tests_split_gitlink_merge -v
```

运行重点 CI/手动 facade 测试：

```powershell
python -m unittest tests_split_gitlink_merge.test_remaining_split_facades -v
```

## 打包

项目可打包为包含手动 GUI、手动 CLI 和 CI minimal bundle 的发布 zip。当前打包流程由本地 `package-merge-tool-release` skill 驱动，产物通常包含：

- `full/`：完整手动/GUI 使用包。
- `ci_minimal/`：CI 所需最小运行包。
- `PACKAGING_INFO.txt`：打包环境和校验信息。

打包细节后续可单独整理到 `docs/PACKAGING.md`。
