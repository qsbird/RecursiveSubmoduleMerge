#!/usr/bin/env python3
"""Deploy a real-Git lab covering manual-merge scenarios and probe the current CLI.

The lab builds isolated repositories for:
- conflict shapes encoded in ``conflict_classifier.py``
- session/resume/abort paths in ``session_workflow.py``
- common real-world Git conflicts (rename, typechange, nested gitlink, unrelated histories)

Usage:
    python3 setup_merge_scenario_lab.py
    python3 setup_merge_scenario_lab.py --workspace /tmp/mergetool_merge_scenario_lab
    python3 setup_merge_scenario_lab.py --skip-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ENV_VAR = "MERGETOOL_MERGE_SCENARIO_LAB"
DEFAULT_WORKSPACE = Path(tempfile.gettempdir()) / "mergetool_merge_scenario_lab"


@dataclass
class Step:
    action: str
    expect_exit: int | None = None
    expect_text: str = ""
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    sid: str
    title: str
    source: str
    build: Callable[[Path], Path]
    steps: list[Step]
    notes: str = ""


@dataclass
class StepResult:
    action: str
    expect_exit: int | None
    actual_exit: int
    passed: bool
    stdout: str
    stderr: str
    detail: str = ""


@dataclass
class ScenarioResult:
    sid: str
    title: str
    source: str
    repo: str
    passed: bool
    steps: list[StepResult]
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and probe the manual-merge scenario lab.")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help=f"Lab root. Default: ${WORKSPACE_ENV_VAR} or {DEFAULT_WORKSPACE}",
    )
    parser.add_argument("--skip-run", action="store_true", help="Only build fixtures, do not invoke the CLI.")
    parser.add_argument("--only", action="append", default=[], help="Run only these scenario ids. Repeatable.")
    parser.add_argument("--keep", action="store_true", help="Do not wipe an existing workspace first.")
    return parser.parse_args()


def run_git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=str(repo),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def configure(repo: Path) -> None:
    run_git(repo, ["config", "user.name", "Lab User"])
    run_git(repo, ["config", "user.email", "lab@example.com"])
    run_git(repo, ["config", "protocol.file.allow", "always"])
    run_git(repo, ["config", "core.autocrlf", "false"])
    run_git(repo, ["config", "advice.detachedHead", "false"])


def init_repo(path: Path, *, branch: str = "main") -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    run_git(path.parent, ["init", "-b", branch, str(path)])
    configure(path)
    return path


def write(repo: Path, relative: str, content: str | bytes, *, executable: bool = False) -> Path:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    if executable:
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def commit_all(repo: Path, message: str, paths: list[str] | None = None) -> str:
    if paths:
        run_git(repo, ["add", "--", *paths])
    else:
        run_git(repo, ["add", "-A"])
    run_git(repo, ["commit", "-m", message])
    return run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()


def checkout_new(repo: Path, name: str, start: str = "main") -> None:
    run_git(repo, ["checkout", "-b", name, start])


def head_sha(repo: Path) -> str:
    return run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()


def diverge_ours_theirs(
    repo: Path,
    *,
    base_files: dict[str, str | bytes] | None = None,
    ours_files: dict[str, str | bytes] | None = None,
    theirs_files: dict[str, str | bytes] | None = None,
    ours_deletes: list[str] | None = None,
    theirs_deletes: list[str] | None = None,
    ours_exec: set[str] | None = None,
    theirs_exec: set[str] | None = None,
) -> Path:
    for relative, content in (base_files or {"base.txt": "base\n"}).items():
        write(repo, relative, content)
    commit_all(repo, "base")
    checkout_new(repo, "ours")
    for relative in ours_deletes or []:
        run_git(repo, ["rm", "-rf", "--", relative], check=False)
    for relative, content in (ours_files or {}).items():
        write(repo, relative, content, executable=relative in (ours_exec or set()))
    if not ours_files and not ours_deletes:
        write(repo, "ours-only.txt", "ours only\n")
    commit_all(repo, "ours")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    for relative in theirs_deletes or []:
        run_git(repo, ["rm", "-rf", "--", relative], check=False)
    for relative, content in (theirs_files or {}).items():
        write(repo, relative, content, executable=relative in (theirs_exec or set()))
    if theirs_files or theirs_deletes:
        commit_all(repo, "theirs")
    else:
        write(repo, "theirs-only.txt", "theirs only\n")
        commit_all(repo, "theirs")
    run_git(repo, ["checkout", "ours"])
    return repo


def add_gitlink(parent: Path, child: Path, name: str, sha: str) -> None:
    target = parent / name
    if target.exists():
        shutil.rmtree(target)
    run_git(parent.parent, ["clone", str(child), str(target)])
    configure(target)
    run_git(target, ["checkout", sha])
    gitmodules = parent / ".gitmodules"
    existing = gitmodules.read_text(encoding="utf-8") if gitmodules.exists() else ""
    block = f'[submodule "{name}"]\n\tpath = {name}\n\turl = {child.as_posix()}\n'
    if f'path = {name}' not in existing:
        gitmodules.write_text(existing + block, encoding="utf-8")
    run_git(parent, ["add", ".gitmodules", name])


def build_gitlink_parent(
    root: Path,
    *,
    child_builder: Callable[[Path], tuple[Path, str, str, str]],
    name: str = "sub",
) -> Path:
    child, base_sha, ours_sha, theirs_sha = child_builder(root / "child_src")
    parent = init_repo(root / "parent")
    add_gitlink(parent, child, name, base_sha)
    commit_all(parent, "add submodule at base")
    checkout_new(parent, "ours")
    run_git(parent / name, ["checkout", ours_sha])
    run_git(parent, ["add", name])
    write(parent, "ours-parent.txt", "ours parent\n")
    commit_all(parent, "ours gitlink")
    run_git(parent, ["checkout", "main"])
    checkout_new(parent, "theirs")
    run_git(parent / name, ["checkout", theirs_sha])
    run_git(parent, ["add", name])
    write(parent, "theirs-parent.txt", "theirs parent\n")
    commit_all(parent, "theirs gitlink")
    run_git(parent, ["checkout", "ours"])
    run_git(parent / name, ["checkout", ours_sha])
    run_git(parent, ["submodule", "update", "--init", "--recursive"], check=False)
    nested = parent / name
    if nested.exists():
        configure(nested)
        if (nested / ".gitmodules").exists():
            run_git(nested, ["submodule", "update", "--init", "--recursive"], check=False)
            for child in nested.iterdir():
                if child.is_dir() and ((child / ".git").exists() or (child / "HEAD").exists()):
                    configure(child)
    return parent


def write_resolve_tool(workspace: Path) -> Path:
    tool = workspace / "tools" / "resolve_conflict.py"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "path = Path(sys.argv[1])\n"
        "data = path.read_bytes() if path.exists() else b''\n"
        "try:\n"
        "    text = data.decode('utf-8')\n"
        "except UnicodeDecodeError:\n"
        "    path.write_bytes(b'resolved-binary\\n')\n"
        "    raise SystemExit(0)\n"
        "if '<<<<<<<' in text:\n"
        "    path.write_text('resolved by lab mergetool\\n', encoding='utf-8')\n"
        "elif path.is_file():\n"
        "    path.write_text('resolved by lab mergetool\\n', encoding='utf-8')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def build_clean(root: Path) -> Path:
    repo = init_repo(root / "repo")
    return diverge_ours_theirs(
        repo,
        base_files={"base.txt": "base\n"},
        ours_files={"ours.txt": "ours\n"},
        theirs_files={"theirs.txt": "theirs\n"},
    )


def build_uu_text(root: Path) -> Path:
    repo = init_repo(root / "repo")
    return diverge_ours_theirs(
        repo,
        base_files={"conflict.txt": "base\n"},
        ours_files={"conflict.txt": "ours\n"},
        theirs_files={"conflict.txt": "theirs\n"},
    )


def build_aa_text(root: Path) -> Path:
    repo = init_repo(root / "repo")
    write(repo, "base.txt", "base\n")
    commit_all(repo, "base")
    checkout_new(repo, "ours")
    write(repo, "added.txt", "ours added\n")
    commit_all(repo, "ours add")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    write(repo, "added.txt", "theirs added\n")
    commit_all(repo, "theirs add")
    run_git(repo, ["checkout", "ours"])
    return repo


def build_uu_exec(root: Path) -> Path:
    repo = init_repo(root / "repo")
    return diverge_ours_theirs(
        repo,
        base_files={"tool.sh": "#!/bin/sh\necho base\n"},
        ours_files={"tool.sh": "#!/bin/sh\necho ours\n"},
        theirs_files={"tool.sh": "#!/bin/sh\necho theirs\n"},
        ours_exec={"tool.sh"},
        theirs_exec={"tool.sh"},
    )


def build_mixed_mode(root: Path) -> Path:
    repo = init_repo(root / "repo")
    write(repo, "mixed.sh", "base\n")
    commit_all(repo, "base")
    checkout_new(repo, "ours")
    write(repo, "mixed.sh", "ours\n")
    commit_all(repo, "ours text mode")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    write(repo, "mixed.sh", "theirs\n", executable=True)
    commit_all(repo, "theirs executable")
    run_git(repo, ["checkout", "ours"])
    return repo


def build_multi_uu(root: Path) -> Path:
    repo = init_repo(root / "repo")
    return diverge_ours_theirs(
        repo,
        base_files={"a.txt": "a-base\n", "b.txt": "b-base\n"},
        ours_files={"a.txt": "a-ours\n", "b.txt": "b-ours\n"},
        theirs_files={"a.txt": "a-theirs\n", "b.txt": "b-theirs\n"},
    )


def build_modify_delete(root: Path) -> Path:
    repo = init_repo(root / "repo")
    return diverge_ours_theirs(
        repo,
        base_files={"conflict.txt": "base\n"},
        ours_files={"conflict.txt": "ours\n"},
        theirs_deletes=["conflict.txt"],
    )


def build_delete_modify(root: Path) -> Path:
    repo = init_repo(root / "repo")
    return diverge_ours_theirs(
        repo,
        base_files={"conflict.txt": "base\n"},
        ours_deletes=["conflict.txt"],
        theirs_files={"conflict.txt": "theirs\n"},
    )


def build_gitmodules(root: Path) -> Path:
    child = init_repo(root / "child")
    write(child, "x.txt", "x\n")
    sha = commit_all(child, "child")
    repo = init_repo(root / "repo")
    add_gitlink(repo, child, "sub", sha)
    commit_all(repo, "add submodule")
    checkout_new(repo, "ours")
    text = (repo / ".gitmodules").read_text(encoding="utf-8")
    write(repo, ".gitmodules", text + "\t# ours marker\n")
    commit_all(repo, "ours gitmodules")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    text = (repo / ".gitmodules").read_text(encoding="utf-8")
    write(repo, ".gitmodules", text.replace("sub", "sub") + "\t# theirs marker\n")
    commit_all(repo, "theirs gitmodules")
    run_git(repo, ["checkout", "ours"])
    return repo


def build_symlink(root: Path) -> Path:
    repo = init_repo(root / "repo")
    write(repo, "target-a.txt", "a\n")
    write(repo, "target-b.txt", "b\n")
    write(repo, "target-base.txt", "base-target\n")
    (repo / "link").symlink_to("target-base.txt")
    commit_all(repo, "base symlink")
    checkout_new(repo, "ours")
    (repo / "link").unlink()
    (repo / "link").symlink_to("target-a.txt")
    commit_all(repo, "ours retarget symlink")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    (repo / "link").unlink()
    (repo / "link").symlink_to("target-b.txt")
    commit_all(repo, "theirs retarget symlink")
    run_git(repo, ["checkout", "ours"])
    return repo


def build_binary(root: Path) -> Path:
    repo = init_repo(root / "repo")
    write(repo, ".gitattributes", "*.bin binary\n")
    write(repo, "data.bin", b"base\x00one")
    commit_all(repo, "base binary")
    checkout_new(repo, "ours")
    write(repo, "data.bin", b"ours\x00two")
    commit_all(repo, "ours binary")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    write(repo, "data.bin", b"theirs\x00three")
    commit_all(repo, "theirs binary")
    run_git(repo, ["checkout", "ours"])
    return repo


def build_rename_delete(root: Path) -> Path:
    repo = init_repo(root / "repo")
    write(repo, "file1.txt", "same\n")
    commit_all(repo, "base")
    checkout_new(repo, "ours")
    run_git(repo, ["mv", "file1.txt", "file2.txt"])
    commit_all(repo, "ours rename")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    run_git(repo, ["rm", "file1.txt"])
    commit_all(repo, "theirs delete")
    run_git(repo, ["checkout", "ours"])
    return repo


def build_rename_rename(root: Path) -> Path:
    repo = init_repo(root / "repo")
    write(repo, "file1.txt", "same\n")
    commit_all(repo, "base")
    checkout_new(repo, "ours")
    run_git(repo, ["mv", "file1.txt", "ours.txt"])
    commit_all(repo, "ours rename")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    run_git(repo, ["mv", "file1.txt", "theirs.txt"])
    commit_all(repo, "theirs rename")
    run_git(repo, ["checkout", "ours"])
    return repo


def build_directory_file(root: Path) -> Path:
    repo = init_repo(root / "repo")
    write(repo, "foo", "file\n")
    commit_all(repo, "base file")
    checkout_new(repo, "ours")
    write(repo, "foo", "ours file\n")
    commit_all(repo, "ours file")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    run_git(repo, ["rm", "-f", "foo"])
    write(repo, "foo/bar.txt", "theirs dir\n")
    commit_all(repo, "theirs directory")
    run_git(repo, ["checkout", "ours"])
    return repo


def build_file_to_gitlink(root: Path) -> Path:
    child = init_repo(root / "child")
    write(child, "x.txt", "x\n")
    sha = commit_all(child, "child")
    repo = init_repo(root / "repo")
    write(repo, "sub", "plain file\n")
    commit_all(repo, "base file named sub")
    checkout_new(repo, "ours")
    write(repo, "sub", "ours file\n")
    commit_all(repo, "ours still a file")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    run_git(repo, ["rm", "-f", "sub"])
    add_gitlink(repo, child, "sub", sha)
    commit_all(repo, "theirs replaces file with gitlink")
    run_git(repo, ["checkout", "ours"])
    return repo


def build_gitlink_to_file(root: Path) -> Path:
    child = init_repo(root / "child")
    write(child, "x.txt", "x\n")
    sha = commit_all(child, "child")
    repo = init_repo(root / "repo")
    add_gitlink(repo, child, "sub", sha)
    commit_all(repo, "base gitlink")
    checkout_new(repo, "ours")
    write(child, "ours-child.txt", "ours child\n")
    ours_child = commit_all(child, "ours child commit")
    run_git(repo / "sub", ["fetch", str(child), "+refs/heads/*:refs/remotes/origin/*"], check=False)
    run_git(repo / "sub", ["fetch", str(child), ours_child], check=False)
    run_git(repo / "sub", ["checkout", ours_child])
    run_git(repo, ["add", "sub"])
    commit_all(repo, "ours advances gitlink")
    run_git(repo, ["checkout", "main"])
    checkout_new(repo, "theirs")
    run_git(repo, ["rm", "-rf", "sub", ".gitmodules"])
    write(repo, "sub", "now a file\n")
    commit_all(repo, "theirs gitlink to file")
    run_git(repo, ["checkout", "ours"])
    return repo


def _child_divergent(src: Path) -> tuple[Path, str, str, str]:
    child = init_repo(src)
    write(child, "base.txt", "base\n")
    base_sha = commit_all(child, "base")
    checkout_new(child, "ours")
    write(child, "ours.txt", "ours\n")
    ours_sha = commit_all(child, "ours")
    run_git(child, ["checkout", "main"])
    checkout_new(child, "theirs")
    write(child, "theirs.txt", "theirs\n")
    theirs_sha = commit_all(child, "theirs")
    run_git(child, ["checkout", "main"])
    return child, base_sha, ours_sha, theirs_sha


def _child_ff(src: Path) -> tuple[Path, str, str, str]:
    child = init_repo(src)
    write(child, "base.txt", "base\n")
    base_sha = commit_all(child, "base")
    checkout_new(child, "theirs")
    write(child, "theirs.txt", "theirs\n")
    theirs_sha = commit_all(child, "theirs")
    run_git(child, ["checkout", "main"])
    return child, base_sha, base_sha, theirs_sha


def _child_contained(src: Path) -> tuple[Path, str, str, str]:
    child = init_repo(src)
    write(child, "base.txt", "base\n")
    base_sha = commit_all(child, "base")
    checkout_new(child, "ours")
    write(child, "ours.txt", "ours\n")
    ours_sha = commit_all(child, "ours")
    run_git(child, ["checkout", "main"])
    return child, base_sha, ours_sha, base_sha


def _child_file_conflict(src: Path) -> tuple[Path, str, str, str]:
    child = init_repo(src)
    write(child, "conflict.txt", "base\n")
    base_sha = commit_all(child, "base")
    checkout_new(child, "ours")
    write(child, "conflict.txt", "ours\n")
    ours_sha = commit_all(child, "ours")
    run_git(child, ["checkout", "main"])
    checkout_new(child, "theirs")
    write(child, "conflict.txt", "theirs\n")
    theirs_sha = commit_all(child, "theirs")
    run_git(child, ["checkout", "main"])
    return child, base_sha, ours_sha, theirs_sha


def _child_modify_delete(src: Path) -> tuple[Path, str, str, str]:
    child = init_repo(src)
    write(child, "conflict.txt", "base\n")
    base_sha = commit_all(child, "base")
    checkout_new(child, "ours")
    write(child, "conflict.txt", "ours\n")
    ours_sha = commit_all(child, "ours")
    run_git(child, ["checkout", "main"])
    checkout_new(child, "theirs")
    run_git(child, ["rm", "conflict.txt"])
    theirs_sha = commit_all(child, "theirs delete")
    run_git(child, ["checkout", "main"])
    return child, base_sha, ours_sha, theirs_sha


def build_gitlink_recursive_clean(root: Path) -> Path:
    return build_gitlink_parent(root, child_builder=_child_divergent)


def build_gitlink_ff(root: Path) -> Path:
    return build_gitlink_parent(root, child_builder=_child_ff)


def build_gitlink_contained(root: Path) -> Path:
    return build_gitlink_parent(root, child_builder=_child_contained)


def build_nested_file_conflict(root: Path) -> Path:
    return build_gitlink_parent(root, child_builder=_child_file_conflict)


def build_nested_manual_takeover(root: Path) -> Path:
    return build_gitlink_parent(root, child_builder=_child_modify_delete)


def build_two_level_nested(root: Path) -> Path:
    leaf, leaf_base, leaf_ours, leaf_theirs = _child_modify_delete(root / "leaf_src")
    mid = init_repo(root / "mid_src")
    add_gitlink(mid, leaf, "leaf", leaf_base)
    commit_all(mid, "mid base")
    mid_base = head_sha(mid)
    checkout_new(mid, "ours")
    run_git(mid / "leaf", ["checkout", leaf_ours])
    run_git(mid, ["add", "leaf"])
    mid_ours = commit_all(mid, "mid ours")
    run_git(mid, ["checkout", "main"])
    checkout_new(mid, "theirs")
    run_git(mid / "leaf", ["checkout", leaf_theirs])
    run_git(mid, ["add", "leaf"])
    mid_theirs = commit_all(mid, "mid theirs")
    run_git(mid, ["checkout", "main"])

    def mid_builder(src: Path) -> tuple[Path, str, str, str]:
        del src
        return mid, mid_base, mid_ours, mid_theirs

    return build_gitlink_parent(root, child_builder=mid_builder, name="mid")


def build_dirty_worktree(root: Path) -> Path:
    repo = build_clean(root)
    write(repo, "dirty.txt", "uncommitted\n")
    return repo


def build_dirty_submodule(root: Path) -> Path:
    parent = build_gitlink_recursive_clean(root)
    write(parent / "sub", "dirty-in-sub.txt", "dirty\n")
    return parent


def build_unrelated(root: Path) -> Path:
    repo = init_repo(root / "repo")
    write(repo, "a.txt", "a\n")
    commit_all(repo, "ours history")
    run_git(repo, ["checkout", "--orphan", "theirs"])
    run_git(repo, ["rm", "-rf", "."], check=False)
    write(repo, "b.txt", "b\n")
    commit_all(repo, "unrelated theirs")
    run_git(repo, ["checkout", "-b", "ours", "main"])
    return repo


def build_already_merged(root: Path) -> Path:
    repo = build_clean(root)
    run_git(repo, ["checkout", "-b", "theirs-copy", "theirs"])
    run_git(repo, ["checkout", "ours"])
    run_git(repo, ["merge", "--no-ff", "-m", "already merged", "theirs"])
    return repo


# ---------------------------------------------------------------------------
# Scenario catalog
# ---------------------------------------------------------------------------

def merge_step(expect: int, **kwargs: Any) -> Step:
    return Step("merge", expect_exit=expect, kwargs=kwargs)


def scenarios() -> list[Scenario]:
    pause_resume = [
        merge_step(2),
        Step("status", 0, expect_text="paused_manual"),
        Step("stage_keep", kwargs={"prefer": "ours"}),
        Step("resume", 0),
    ]
    return [
        Scenario("clean-divergent", "Clean merge of divergent files", "code", build_clean, [merge_step(0)]),
        Scenario("uu-text", "Regular UU text conflict via mergetool", "code", build_uu_text, [merge_step(0)]),
        Scenario("aa-text", "Regular AA add/add text conflict", "code", build_aa_text, [merge_step(0)]),
        Scenario("uu-exec", "UU executable 100755", "code", build_uu_exec, [merge_step(0)]),
        Scenario("mixed-mode", "Mixed 100644/100755 same path", "code", build_mixed_mode, [merge_step(0)]),
        Scenario("multi-uu", "Multiple UU files in one merge", "real-world", build_multi_uu, [merge_step(0)]),
        Scenario(
            "modify-delete",
            "UD modify/delete → pause then resume",
            "code",
            build_modify_delete,
            pause_resume,
        ),
        Scenario(
            "delete-modify",
            "DU delete/modify → pause then resume",
            "code",
            build_delete_modify,
            pause_resume,
        ),
        Scenario("gitmodules", ".gitmodules conflict → MANUAL_TAKEOVER", "code", build_gitmodules, [merge_step(2)]),
        Scenario("symlink", "Symlink retarget conflict", "code/web", build_symlink, [merge_step(2)]),
        Scenario("binary", "Binary NUL conflict", "code/web", build_binary, [merge_step(2)]),
        Scenario("rename-delete", "Rename/delete (AU/UA family)", "web", build_rename_delete, [merge_step(2)]),
        Scenario("rename-rename", "Rename/rename both sides", "web", build_rename_rename, [merge_step(2)]),
        Scenario("directory-file", "Directory vs file", "web", build_directory_file, [merge_step(2)]),
        Scenario("file-to-gitlink", "File replaced by gitlink", "code/web", build_file_to_gitlink, [merge_step(2)]),
        Scenario("gitlink-to-file", "Gitlink replaced by file", "code/web", build_gitlink_to_file, [merge_step(2)]),
        Scenario("gitlink-recursive-clean", "Pure gitlink recursive clean merge", "code", build_gitlink_recursive_clean, [merge_step(0)]),
        Scenario("gitlink-fast-forward", "Gitlink fast-forward", "code", build_gitlink_ff, [merge_step(0)]),
        Scenario("gitlink-already-contained", "Gitlink already contained", "code", build_gitlink_contained, [merge_step(0)]),
        Scenario("nested-uu", "Submodule UU resolved by mergetool then parent commit", "code", build_nested_file_conflict, [merge_step(0)]),
        Scenario(
            "nested-takeover",
            "Submodule modify/delete pause → stage → resume to root",
            "code",
            build_nested_manual_takeover,
            [
                merge_step(2),
                Step("stage_nested", kwargs={"relative": "conflict.txt", "content": "resolved nested\n"}),
                Step("resume", 0),
            ],
        ),
        Scenario(
            "two-level-nested",
            "Two-level submodule pause/resume",
            "code",
            build_two_level_nested,
            [
                merge_step(2),
                Step("stage_nested", kwargs={"relative": "conflict.txt", "content": "resolved leaf\n"}),
                Step("resume", 0),
            ],
        ),
        Scenario(
            "resume-unmerged",
            "Resume while still unmerged stays paused",
            "code",
            build_modify_delete,
            [merge_step(2), Step("resume", 2)],
        ),
        Scenario(
            "resume-wrong-branch",
            "Resume after root branch rewrite is refused",
            "code",
            build_modify_delete,
            [merge_step(2), Step("rewrite_head_ref"), Step("resume", 1)],
        ),
        Scenario(
            "resume-manual-commit",
            "User commits merge; resume accepts ancestry",
            "code",
            build_modify_delete,
            [
                merge_step(2),
                Step("stage_keep", kwargs={"prefer": "ours"}),
                Step("commit_merge"),
                Step("resume", 0),
            ],
        ),
        Scenario(
            "abort-keeps-untracked",
            "Abort restores HEAD and keeps untracked files",
            "code",
            build_modify_delete,
            [
                merge_step(2),
                Step("add_untracked", kwargs={"relative": "keep-me.txt", "content": "keep\n"}),
                Step("abort", 0),
                Step("assert_untracked", kwargs={"relative": "keep-me.txt"}),
            ],
        ),
        Scenario(
            "abort-without-yes",
            "Abort without --yes is refused",
            "code",
            build_modify_delete,
            [merge_step(2), Step("abort_no_yes", 1)],
        ),
        Scenario(
            "session-blocks-second-merge",
            "Active session rejects a second merge",
            "code",
            build_modify_delete,
            [merge_step(2), merge_step(1)],
        ),
        Scenario("dirty-worktree", "Dirty root worktree fails preflight", "code", build_dirty_worktree, [merge_step(1)]),
        Scenario("dirty-submodule", "Dirty submodule fails preflight", "code", build_dirty_submodule, [merge_step(1)]),
        Scenario("unrelated-histories", "Unrelated histories cannot merge", "real-world", build_unrelated, [merge_step(1)]),
        Scenario("already-merged", "Merging an already-merged ref", "real-world", build_already_merged, [merge_step(0)]),
        Scenario("use-current-branch", "Merge on current branch", "code", build_clean, [Step("merge_current", 0)]),
        Scenario("two-refs-rejected", "CLI rejects a second merge ref", "code", build_clean, [Step("two_refs", 2)]),
        Scenario("status-no-session", "Status with no session", "code", build_clean, [Step("status", 0, expect_text="No active")]),
        Scenario("resume-no-session", "Resume with no session", "code", build_clean, [Step("resume", 1)]),
        Scenario("abort-no-session", "Abort with no session", "code", build_clean, [Step("abort", 1)]),
        Scenario(
            "use-current-detached",
            "use-current-branch on detached HEAD is refused",
            "code",
            build_clean,
            [Step("detach_head"), Step("merge_current", 1)],
        ),
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class LabRunner:
    def __init__(self, workspace: Path, tool: Path) -> None:
        self.workspace = workspace
        self.tool = tool
        self.python = sys.executable

    def merge_argv(self, repo: Path, *, use_current: bool = False) -> list[str]:
        argv = [
            "merge",
            "theirs",
            "--repo",
            str(repo),
            "--no-confirm-after-conflict-tool",
            "--conflict-tool-command",
            f'{self.python} "{self.tool}" "{{file}}"',
            "--report-file",
            "merge_manual_todo.txt",
        ]
        if use_current:
            argv[4:4] = ["--use-current-branch"]
        else:
            argv[4:4] = ["--base-remote-branch", "ours", "--work-branch", "lab-work"]
        return argv

    def cli(self, argv: list[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        del timeout
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        from manual_merge_split.commands import ManualCommandService
        from manual_merge_split.runtime import parse_args

        stdout = StringIO()
        stderr = StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                args = parse_args(argv)
                commands = ManualCommandService()
                if args.command == "merge":
                    code = commands.run_merge(args)
                elif args.command == "resume":
                    code = commands.run_resume(args)
                elif args.command == "status":
                    code = commands.run_status(args)
                elif args.command == "abort":
                    code = commands.run_abort(args)
                else:
                    code = 1
        except SystemExit as exc:
            code = int(exc.code or 0)
        except Exception as exc:  # noqa: BLE001 - lab captures unexpected CLI crashes
            stderr.write(str(exc))
            code = 1
        return subprocess.CompletedProcess(
            args=[self.python, "-m", "manual_merge_split", *argv],
            returncode=code,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )

    def deepest_repo(self, repo: Path) -> Path:
        current = repo
        for name in ("mid", "sub", "leaf"):
            candidate = current / name
            if (candidate / ".git").exists() or candidate.is_dir() and (candidate / "HEAD").exists():
                current = candidate
            elif candidate.exists() and (candidate / "conflict.txt").exists():
                current = candidate
        # Prefer the deepest path that still has MERGE_HEAD or unmerged paths.
        stack = [repo]
        deepest = repo
        while stack:
            node = stack.pop()
            if (node / ".git").exists() or (node / "HEAD").exists():
                deepest = node
            for child in node.iterdir() if node.is_dir() else []:
                if child.name in {".git", ".omx"}:
                    continue
                if child.is_dir():
                    stack.append(child)
        # Walk known submodule names from root.
        cursor = repo
        for name in ("mid", "sub", "leaf"):
            nxt = cursor / name
            if nxt.exists() and (nxt / ".git").exists():
                cursor = nxt
        return cursor

    def run_step(self, repo: Path, step: Step) -> StepResult:
        action = step.action
        completed: subprocess.CompletedProcess[str] | None = None
        detail = ""
        actual_exit = 0
        stdout = ""
        stderr = ""

        if action == "merge":
            completed = self.cli(self.merge_argv(repo, use_current=bool(step.kwargs.get("use_current"))))
        elif action == "merge_current":
            completed = self.cli(self.merge_argv(repo, use_current=True))
        elif action == "resume":
            completed = self.cli(["resume", "--repo", str(repo)])
        elif action == "status":
            completed = self.cli(["status", "--repo", str(repo)])
        elif action == "abort":
            completed = self.cli(["abort", "--repo", str(repo), "--yes"])
        elif action == "abort_no_yes":
            completed = self.cli(["abort", "--repo", str(repo)])
        elif action == "two_refs":
            completed = self.cli(["merge", "theirs", "extra", "--repo", str(repo)], timeout=20)
        elif action == "stage_keep":
            self._stage_unmerged(repo, prefer=str(step.kwargs.get("prefer", "ours")))
        elif action == "stage_nested":
            target = self.deepest_repo(repo)
            relative = str(step.kwargs.get("relative", "conflict.txt"))
            content = str(step.kwargs.get("content", "resolved\n"))
            path = target / relative
            if path.exists() or True:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            run_git(target, ["add", "--", relative], check=False)
            run_git(target, ["add", "-u", "--", relative], check=False)
            detail = f"staged {target}:{relative}"
        elif action == "commit_merge":
            run_git(repo, ["commit", "--no-edit"])
            detail = "created user merge commit"
        elif action == "add_untracked":
            relative = str(step.kwargs["relative"])
            write(repo, relative, str(step.kwargs.get("content", "x\n")))
            detail = relative
        elif action == "assert_untracked":
            relative = str(step.kwargs["relative"])
            exists = (repo / relative).exists()
            actual_exit = 0 if exists else 1
            detail = f"exists={exists}"
        elif action == "rewrite_head_ref":
            run_git(repo, ["branch", "wrong-lab-branch"])
            run_git(repo, ["symbolic-ref", "HEAD", "refs/heads/wrong-lab-branch"])
            detail = "HEAD now wrong-lab-branch"
        elif action == "detach_head":
            run_git(repo, ["checkout", "--detach", "HEAD"])
        else:
            raise RuntimeError(f"unknown step action: {action}")

        if completed is not None:
            actual_exit = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        expect = step.expect_exit
        passed = True if expect is None else actual_exit == expect
        if step.expect_text:
            blob = stdout + stderr
            if step.expect_text not in blob:
                passed = False
                detail = (detail + " " if detail else "") + f"missing {step.expect_text!r}"
        return StepResult(
            action=action,
            expect_exit=expect,
            actual_exit=actual_exit,
            passed=passed,
            stdout=stdout,
            stderr=stderr,
            detail=detail,
        )

    def _stage_unmerged(self, repo: Path, *, prefer: str) -> None:
        unmerged = [
            line.strip()
            for line in run_git(repo, ["diff", "--name-only", "--diff-filter=U"]).stdout.splitlines()
            if line.strip()
        ]
        status = run_git(repo, ["status", "--porcelain"]).stdout
        for path in unmerged:
            target = repo / path
            if prefer == "ours":
                run_git(repo, ["checkout", "--ours", "--", path], check=False)
            else:
                run_git(repo, ["checkout", "--theirs", "--", path], check=False)
            if target.exists():
                run_git(repo, ["add", "--", path], check=False)
            else:
                run_git(repo, ["rm", "-f", "--", path], check=False)
        if not unmerged and "D " in status:
            run_git(repo, ["add", "-u"], check=False)


def write_index(workspace: Path, catalog: list[Scenario], results: list[ScenarioResult] | None) -> None:
    lines = [
        "Manual merge scenario lab",
        "=========================",
        "",
        f"Workspace: {workspace}",
        f"Project:   {PROJECT_ROOT}",
        "",
        "Each scenario is an isolated Git repository checked out on branch `ours`,",
        "with a merge target named `theirs`. Probe with:",
        "",
        f"  python3 -m manual_merge_split merge theirs --repo <repo> --base-remote-branch ours --work-branch lab-work --no-confirm-after-conflict-tool --conflict-tool-command '{sys.executable} \"{workspace / 'tools' / 'resolve_conflict.py'}\" \"{{file}}\"'",
        "",
        "Scenarios",
        "---------",
    ]
    for item in catalog:
        lines.append(f"- {item.sid}: {item.title}  [{item.source}]")
        if item.notes:
            lines.append(f"    {item.notes}")
    if results is not None:
        lines.extend(["", "Results", "-------"])
        for result in results:
            mark = "PASS" if result.passed else "FAIL"
            lines.append(f"- {mark}  {result.sid}  ({result.source})  {result.repo}")
            if result.error:
                lines.append(f"    error: {result.error}")
            for step in result.steps:
                step_mark = "ok" if step.passed else "BAD"
                extra = f"  {step.detail}" if step.detail else ""
                lines.append(
                    f"    [{step_mark}] {step.action}: exit {step.actual_exit}"
                    f" (expect {step.expect_exit}){extra}"
                )
                if not step.passed:
                    snippet = (step.stderr or step.stdout).strip().splitlines()
                    for line in snippet[:8]:
                        lines.append(f"      {line}")
    (workspace / "INDEX.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    workspace = (args.workspace or Path(os.environ.get(WORKSPACE_ENV_VAR, str(DEFAULT_WORKSPACE)))).expanduser().resolve()
    if workspace.exists() and not args.keep:
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    tool = write_resolve_tool(workspace)
    catalog = scenarios()
    if args.only:
        wanted = set(args.only)
        catalog = [item for item in catalog if item.sid in wanted]
        missing = wanted - {item.sid for item in catalog}
        if missing:
            print(f"Unknown scenario ids: {sorted(missing)}", file=sys.stderr)
            return 2

    print(f"Lab workspace: {workspace}")
    print(f"Scenarios: {len(catalog)}")
    os.environ.setdefault("GIT_CONFIG_COUNT", "1")
    os.environ.setdefault("GIT_CONFIG_KEY_0", "protocol.file.allow")
    os.environ.setdefault("GIT_CONFIG_VALUE_0", "always")
    runner = LabRunner(workspace, tool)
    results: list[ScenarioResult] = []

    for item in catalog:
        slot = workspace / "scenarios" / item.sid
        slot.mkdir(parents=True, exist_ok=True)
        print(f"\n== {item.sid}: {item.title}")
        try:
            repo = item.build(slot)
        except Exception as exc:  # noqa: BLE001 - lab must continue after a bad fixture
            print(f"  BUILD FAIL: {exc}")
            results.append(
                ScenarioResult(item.sid, item.title, item.source, str(slot), False, [], error=str(exc))
            )
            continue
        print(f"  repo: {repo}")
        if args.skip_run:
            results.append(ScenarioResult(item.sid, item.title, item.source, str(repo), True, []))
            continue
        step_results: list[StepResult] = []
        failed = False
        try:
            for step in item.steps:
                outcome = runner.run_step(repo, step)
                step_results.append(outcome)
                mark = "ok" if outcome.passed else "FAIL"
                print(
                    f"  [{mark}] {step.action} exit={outcome.actual_exit} expect={step.expect_exit}"
                    + (f" {outcome.detail}" if outcome.detail else "")
                )
                if not outcome.passed:
                    failed = True
                    snippet = (outcome.stderr or outcome.stdout).strip()
                    if snippet:
                        print("    " + snippet.splitlines()[0][:240])
                    break
        except subprocess.TimeoutExpired as exc:
            failed = True
            step_results.append(
                StepResult("timeout", 0, 124, False, "", str(exc), "CLI timed out")
            )
            print("  FAIL timeout")
        except Exception as exc:  # noqa: BLE001
            failed = True
            step_results.append(StepResult("error", 0, 1, False, "", str(exc), str(exc)))
            print(f"  FAIL {exc}")
        results.append(
            ScenarioResult(item.sid, item.title, item.source, str(repo), not failed, step_results)
        )

    write_index(workspace, scenarios() if not args.only else catalog, results)
    payload = [
        {
            "id": result.sid,
            "passed": result.passed,
            "source": result.source,
            "repo": result.repo,
            "error": result.error,
            "steps": [
                {
                    "action": step.action,
                    "passed": step.passed,
                    "expect": step.expect_exit,
                    "actual": step.actual_exit,
                    "detail": step.detail,
                    "stderr": step.stderr[-800:],
                    "stdout": step.stdout[-800:],
                }
                for step in result.steps
            ],
        }
        for result in results
    ]
    (workspace / "RESULTS.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    passed = sum(1 for result in results if result.passed)
    failed = len(results) - passed
    print("\n" + "=" * 60)
    print(f"Lab complete: {passed} passed, {failed} failed, {len(results)} total")
    print(f"Index:   {workspace / 'INDEX.txt'}")
    print(f"Results: {workspace / 'RESULTS.json'}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
