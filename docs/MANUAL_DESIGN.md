# 手动/GUI 合并详细设计

## 1. 设计边界

手动模式一次只合并一个目标 ref。CI 的多分支 batch 行为不属于本模块，保持在 `ci_merge_split`。

内置解决器只处理：

- 普通 regular file 冲突：mode 为 `100644/100755`，stage 2、3 存在，状态为 `UU/AA`。
- 纯 gitlink 冲突：所有 stage mode 都是 `160000`，stage 2、3 存在。

`.gitmodules`、modify/delete、rename、directory/file、symlink、binary、mixed mode、gitlink 删除等合法 Git 冲突进入人工接管。解析失败、重复/非法 stage、index 与 unmerged names 不一致属于 fatal。

## 2. 命令

```powershell
python -m manual_merge_split merge <merge-ref> --repo <root> --base-remote-branch origin/main
python -m manual_merge_split status --repo <root>
python -m manual_merge_split resume --repo <root>
python -m manual_merge_split abort --repo <root> --yes
```

- `merge` 只接受一个位置参数；第二个 ref 在任何 Git 写操作前被 argparse 拒绝。
- `--use-current-branch` 表示在当前根分支发起新合并。
- `--continue-current-branch` 仅作为隐藏兼容别名，不是 resume。
- `status` 只读展示 session 与真实 Git 状态。
- `resume` 验证人工解决结果并继续提交、向父仓库收敛。
- `abort --yes` 只恢复 session 涉及的仓库，不运行 `git clean`。
- `cleanup` 仍是范围更大的显式强清理命令，不参与 resume。

退出码：

| 代码 | 含义 |
|---:|---|
| 0 | 成功 |
| 1 | 参数、session、index 或 Git 状态不可信 |
| 2 | 已保留冲突现场，等待人工处理 |

## 3. 模块职责

| 模块 | 职责 |
|---|---|
| `runtime.py` | 单分支 CLI 与 `status/resume/abort` dispatch。 |
| `session_workflow.py` | start、pause、resume、status、abort 编排。 |
| `session_models.py` | 版本化 session、repository frame 和 phase。 |
| `session_store.py` | Git 内部目录中的原子 JSON、锁和 revision 校验。 |
| `conflict_classifier.py` | 严格冲突 disposition。 |
| `repository_ops.py` | Git 状态、提交、stage、abort/reset 等操作。 |
| `file_conflicts.py` | 仅处理分类为 supported file 的外部工具流程。 |
| `merge_tool_adapters/manual_semiauto.py` | common gitlink core 的手动策略与递归事件回调。 |
| `recursive_submodule_merge_gui.py` | 单分支输入和 session 操作入口。 |

## 4. 开始合并

流程：

1. 检查不存在活动 session。
2. 严格 preflight：根仓库和递归子模块必须无进行中的 merge、无未提交改动并配置 Git identity。
3. 解析/刷新 base 与唯一 merge ref。
4. 创建或切换工作分支。
5. 在执行 merge 前创建 root `RepositoryFrame` 并原子保存 session。
6. 执行 `git merge --no-ff --no-commit`。
7. common core 优先处理纯 gitlink；manual policy 对 non-gitlink 再做严格分类。
8. 全部解决后由程序提交根仓库并清理活动 session。

session 位于当前 worktree Git 目录：

```text
<git-dir>/recursive-submodule-merge/session.json
```

写入使用临时文件、`fsync`、`os.replace`；锁文件防止 GUI/CLI 并发写入；每次保存递增 revision，陈旧写入被拒绝。

## 5. 暂停与人工接管

遇到 manual takeover 或文件工具未解决时：

1. 保留 `MERGE_HEAD`、index 和 worktree。
2. 将最深仓库 frame 标为 `manual_resolution`。
3. 保存冲突路径、disposition、原因和 root→当前仓库栈。
4. 生成 `merge_manual_todo.txt` 人类说明。
5. 返回退出码 2。

用户应解决所有 unmerged path，执行 `git add`/`git rm`，然后运行 `resume`。推荐不要手动提交；若已经提交，resume 只有在 ours、theirs 都是当前 HEAD 祖先且工作树干净时才接受。

## 6. 递归子模块与恢复

进入递归 gitlink merge 前先 push 子模块 frame，记录：

- repo/parent repo/gitlink path/depth
- original HEAD/branch
- ours/theirs/base/expected MERGE_HEAD
- commit title/body
- 当前 phase

resume 从最深 frame 开始：

1. 有 unmerged path 时重新运行严格 resolver；仍需人工处理则再次暂停。
2. 有合法 `MERGE_HEAD` 时由程序创建 merge commit。
3. 无 `MERGE_HEAD` 时只接受 ancestry 可证明的人工提交。
4. 子模块完成后先持久化 `child_committed_pending_stage`。
5. 在父仓库 stage gitlink，再 pop 子模块 frame。
6. 重复到根仓库提交完成并删除 session。

这个 phase 顺序保证进程在“子模块已提交、父 gitlink 尚未 stage”之间退出时，下次只补 stage，不重复提交。

## 7. Abort 与 Cleanup

`abort --yes` 从最深 frame 向 root 逆序恢复：

- 有活动 merge 时先 `git merge --abort`。
- 恢复 session 记录的 branch/HEAD。
- 对 involved repo 执行精确 `reset --hard <original-head>`。
- 不运行 `git clean`，不删除未跟踪文件，不扫描未涉及仓库。

`cleanup` 的范围更大，可递归 reset/clean。它保留为用户明确选择的灾难恢复入口，不能替代 session abort。

## 8. GUI

GUI 的 Merge 页只提供一个 merge ref 输入框，并提供：

- Session status
- Resume merge
- Abort merge
- Start merge

Abort 按钮先显示破坏性确认，再向 CLI 传 `--yes`。GUI 仅构造 CLI argv 和展示输出，不复制业务状态机。

## 9. Publish

`publish` 与本次单分支/session 重构解耦，仍按从深到浅顺序发布子模块和根提交，不 force push，并要求远端缺失、已最新或可 fast-forward。

## 10. 测试重点

- 第二个 merge ref 被拒绝。
- regular `UU/AA`、pure gitlink `{1,2,3}/{2,3}` 分类正确。
- `.gitmodules`、modify/delete、symlink、binary、mixed mode 进入人工接管。
- root pause/resume 和用户手动提交校验。
- 子模块 pause 后跨进程 resume，完成 child commit、parent gitlink stage、root commit。
- root/嵌套 abort 恢复原 HEAD 且保留未跟踪文件。
- session schema、锁、revision、atomic replace。
- GUI 单分支 argv 与 status/resume/abort。
- CI 现有 batch 测试保持不变。
