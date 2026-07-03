"""手动测试环境设置脚本.

为三项功能优化创建测试环境：
1. code 命令预检查
2. 清理功能 fallback 逐级清理
3. Publish 交互式子模块选择推送

使用方法：
    python setup_manual_test_env.py

会在 D:\Git\MergeTool_for_CICD\external_builds 下创建 manual_test_workspace 目录，
包含一个带子模块的测试仓库和远端仓库。

测试完成后可用以下命令清理：
    rmdir /s /q D:\Git\MergeTool_for_CICD\external_builds\manual_test_workspace
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(r"D:\Git\MergeTool_for_CICD\external_builds\manual_test_workspace")
REMOTE_ROOT = WORKSPACE_ROOT / "remotes"
LOCAL_ROOT = WORKSPACE_ROOT / "local"


def run(cmd: list[str], cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command and print it."""
    cwd_str = str(cwd) if cwd else None
    print(f"  $ {' '.join(cmd)}" + (f"  (in {cwd_str})" if cwd_str else ""))
    result = subprocess.run(
        cmd,
        cwd=cwd_str,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            print(f"    {line}")
    if result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            print(f"    [stderr] {line}")
    if check and result.returncode != 0:
        print(f"    ERROR: exit code {result.returncode}")
        sys.exit(1)
    return result


def init_local_repo(name: str) -> Path:
    """Create and initialize a local git repository."""
    repo_path = LOCAL_ROOT / name
    if repo_path.exists():
        print(f"[SKIP] Local {name} already exists: {repo_path}")
        return repo_path

    print(f"\n[SETUP] Creating local repo: {name}")
    repo_path.mkdir(parents=True, exist_ok=True)
    run(["git", "init"], cwd=repo_path)
    run(["git", "config", "user.name", "Test User"], cwd=repo_path)
    run(["git", "config", "user.email", "test@example.com"], cwd=repo_path)

    # Create initial content
    readme_path = repo_path / "README.md"
    readme_path.write_text(f"# {name}\n\nInitial content for {name}.\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=repo_path)
    run(["git", "commit", "-m", f"Initial commit for {name}"], cwd=repo_path)

    # Create bare remote and push
    remote_path = REMOTE_ROOT / f"{name}.git"
    if not remote_path.exists():
        remote_path.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--bare", str(remote_path)])

    run(["git", "remote", "add", "origin", str(remote_path)], cwd=repo_path)
    run(["git", "branch", "-M", "master"], cwd=repo_path)
    run(["git", "push", "-u", "origin", "master"], cwd=repo_path)
    return repo_path


def create_test_branches(repo_path: Path, base_branch: str = "master") -> None:
    """Create test branches with content differences for merge testing."""
    print(f"\n[SETUP] Creating test branches in {repo_path.name}")

    # Branch A: modify README
    run(["git", "checkout", "-b", "feature/test-branch-A", base_branch], cwd=repo_path)
    readme = repo_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\n## Branch A changes\n\nAdded by feature/test-branch-A.\n",
        encoding="utf-8",
    )
    run(["git", "add", "README.md"], cwd=repo_path)
    run(["git", "commit", "-m", "Branch A: modify README"], cwd=repo_path)
    run(["git", "push", "-u", "origin", "feature/test-branch-A"], cwd=repo_path)

    # Branch B: add a new file
    run(["git", "checkout", base_branch], cwd=repo_path)
    run(["git", "checkout", "-b", "feature/test-branch-B", base_branch], cwd=repo_path)
    new_file = repo_path / "new_feature.txt"
    new_file.write_text("New feature from branch B.\n", encoding="utf-8")
    run(["git", "add", "new_feature.txt"], cwd=repo_path)
    run(["git", "commit", "-m", "Branch B: add new_feature.txt"], cwd=repo_path)
    run(["git", "push", "-u", "origin", "feature/test-branch-B"], cwd=repo_path)

    # Go back to master
    run(["git", "checkout", base_branch], cwd=repo_path)


def create_conflict_branches(repo_path: Path, base_branch: str = "master") -> None:
    """Create branches with conflicting changes for conflict resolution testing."""
    print(f"\n[SETUP] Creating conflict branches in {repo_path.name}")

    # Conflict branch: modify same area as branch A
    run(["git", "checkout", "-b", "feature/conflict-branch", base_branch], cwd=repo_path)
    readme = repo_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\n## Conflict branch changes\n\nThis conflicts with branch A.\n",
        encoding="utf-8",
    )
    run(["git", "add", "README.md"], cwd=repo_path)
    run(["git", "commit", "-m", "Conflict branch: modify README differently"], cwd=repo_path)
    run(["git", "push", "-u", "origin", "feature/conflict-branch"], cwd=repo_path)

    run(["git", "checkout", base_branch], cwd=repo_path)


def setup_todo_interrupt_state(repo_path: Path) -> None:
    """Create a manual_merge_todo file to simulate an interrupted merge."""
    print(f"\n[SETUP] Creating interrupted merge state in {repo_path.name}")
    todo_file = repo_path / "merge_manual_todo.txt"
    todo_content = """# Manual Merge Todo
# This file indicates an interrupted manual merge.

## Remaining branches to merge:
- feature/test-branch-B

## Status:
- feature/test-branch-A: completed
- feature/test-branch-B: pending
"""
    todo_file.write_text(todo_content, encoding="utf-8")
    run(["git", "add", "merge_manual_todo.txt"], cwd=repo_path)
    run(["git", "commit", "-m", "Simulate interrupted merge: add manual_merge_todo"], cwd=repo_path)
    print(f"  Created: {todo_file}")


def main() -> int:
    print("=" * 60)
    print("手动合并工具 — 测试环境设置")
    print("=" * 60)

    # Create workspace directories
    REMOTE_ROOT.mkdir(parents=True, exist_ok=True)
    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)

    # ─── Step 1: Create sub-module repo first ───
    print("\n" + "─" * 40)
    print("Step 1: 创建子模块仓库")
    print("─" * 40)

    sub_local = init_local_repo("test-sub")
    create_test_branches(sub_local)

    # ─── Step 2: Create main repo ───
    print("\n" + "─" * 40)
    print("Step 2: 创建主仓库")
    print("─" * 40)

    main_local = init_local_repo("test-main")

    # Add submodule (allow file protocol for local repos)
    print(f"\n[SETUP] Adding submodule to main repo")
    sub_remote = REMOTE_ROOT / "test-sub.git"
    run(["git", "config", "protocol.file.allow", "always"], cwd=main_local)
    run(["git", "-c", "protocol.file.allow=always", "submodule", "add", str(sub_remote), "sub"], cwd=main_local)
    run(["git", "commit", "-m", "Add test-sub as submodule"], cwd=main_local)
    run(["git", "push", "origin", "master"], cwd=main_local)

    # Create test branches in main repo
    create_test_branches(main_local)
    create_conflict_branches(main_local)

    # ─── Step 3: Create interrupted state for cleanup testing ───
    print("\n" + "─" * 40)
    print("Step 3: 创建中断状态（用于清理功能测试）")
    print("─" * 40)

    # Create a work branch with interrupted state
    run(["git", "checkout", "-b", "validation-work", "master"], cwd=main_local)
    run(["git", "merge", "feature/test-branch-A"], cwd=main_local)
    setup_todo_interrupt_state(main_local)
    run(["git", "push", "-u", "origin", "validation-work"], cwd=main_local)

    # Back to master
    run(["git", "checkout", "master"], cwd=main_local)

    # ─── Step 4: Summary ───
    main_remote = REMOTE_ROOT / "test-main.git"
    print("\n" + "=" * 60)
    print("测试环境设置完成！")
    print("=" * 60)
    print(f"""
测试仓库位置：
  主仓库：{main_local}
  子模块仓库：{sub_local}
  远端主仓库：{main_remote}
  远端子模块仓库：{sub_remote}

分支列表：
  master                    — 基线
  feature/test-branch-A     — 修改 README（可合并）
  feature/test-branch-B     — 添加新文件（可合并）
  feature/conflict-branch   — 修改 README（与 A 冲突）
  validation-work           — 包含中断状态（merge_manual_todo.txt）

════════════════════════════════════════════════════════════════
功能测试指南
════════════════════════════════════════════════════════════════

【Feature 1: code 命令预检查】

启动 GUI：
  python recursive_submodule_merge_gui.py

1. 正常测试：选择主仓库路径，点击"开始合并"
   → 如果 code 命令存在，不会弹窗
2. 异常测试：在高级选项中将冲突工具命令改为 nonexistent_tool --wait "{{file}}"
   → 点击"开始合并"时应弹出"冲突工具未找到"对话框

【Feature 2: 清理功能 fallback 逐级清理】

1. 切换到 validation-work 分支：
   cd {main_local}
   git checkout validation-work

2. 启动 GUI 并选择清理标签页：
   python recursive_submodule_merge_gui.py

3. 选择主仓库路径，点击"开始清理"
   → 工具应检测到 merge_manual_todo.txt 文件
   → 执行逐级 fallback 清理（level 1 -> level 2 -> level 3）

4. 也可用 CLI 测试：
   python recursive_submodule_merge_gui.py --cli cleanup --repo {main_local}

【Feature 3: Publish 交互式子模块选择推送】

1. 切回 master 并确保代码已提交：
   cd {main_local}
   git checkout master

2. 启动 GUI：
   python recursive_submodule_merge_gui.py

3. 选择"发布"标签页
4. 填写推送目标分支（例如 test-publish-target）
5. 点击"开始发布"
   → 应先执行 publish check
   → 弹出子模块选择对话框（显示主仓库和子模块的推送信息）
   → 可勾选/取消勾选要推送的子模块
   → 确认后执行推送

6. 也可用 CLI 测试 check-only 模式：
   python recursive_submodule_merge_gui.py --cli publish --repo {main_local} --push-target test-publish-target --check-only

════════════════════════════════════════════════════════════════
清理测试环境：
  rmdir /s /q {WORKSPACE_ROOT}
════════════════════════════════════════════════════════════════
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())