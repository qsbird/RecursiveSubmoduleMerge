#!/usr/bin/env python3
"""Reusable core for recursive Git submodule gitlink merges.

This module intentionally contains only the smallest workflow that both CI and
manual merge tools need:

* read unmerged index stages;
* classify submodule gitlink conflicts;
* pick a fast-forwardable gitlink result when possible;
* otherwise enter the child repository and recursively merge the two gitlink
  commits;
* stage the resulting child commit back in the parent repository.

Everything policy-shaped is kept behind hooks: cleanup, reporting, publishing,
file-conflict tools, and final root commits belong to callers.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from subprocess_window import hidden_subprocess_kwargs


class GitlinkMergeError(RuntimeError):
    """Base error for the reusable gitlink merge core."""


class NonGitlinkConflictError(GitlinkMergeError):
    """Raised when the default policy sees conflicts outside gitlinks."""

    def __init__(self, repo_path: Path, conflicts: list["Conflict"]) -> None:
        self.repo_path = repo_path
        self.conflicts = conflicts
        paths = ", ".join(conflict.path for conflict in conflicts)
        super().__init__(f"non-gitlink conflicts remain in {repo_path}: {paths}")


@dataclass(frozen=True)
class GitCommandResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


class GitBackend(Protocol):
    def run(
        self,
        repo_path: Path,
        args: list[str],
        *,
        check: bool = True,
    ) -> GitCommandResult:
        """Run git args in repo_path."""


class SubprocessGit:
    """Small default GitBackend for standalone tests and simple callers."""

    def __init__(
        self,
        git: str = "git",
        *,
        timeout_seconds: int | None = 300,
        auto_safe_directory: bool = False,
        verbose: bool = False,
    ) -> None:
        self.git = git
        self.timeout_seconds = timeout_seconds
        self.auto_safe_directory = auto_safe_directory
        self.verbose = verbose

    def run(
        self,
        repo_path: Path,
        args: list[str],
        *,
        check: bool = True,
    ) -> GitCommandResult:
        cwd = repo_path.resolve(strict=False)
        cmd = [self.git, *args]
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_MERGE_AUTOEDIT"] = "no"
        augment_git_for_windows_path(env, self.git)
        if self.auto_safe_directory:
            add_git_config(env, "safe.directory", "*")

        if self.verbose:
            print(f"+ ({cwd}) {' '.join(cmd)}", flush=True)

        started = time.perf_counter()
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(cwd),
                env=env,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.timeout_seconds,
                **hidden_subprocess_kwargs(),
            )
            result = GitCommandResult(
                args=cmd,
                cwd=cwd,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_ms=elapsed_ms(started),
            )
        except subprocess.TimeoutExpired as exc:
            result = GitCommandResult(
                args=cmd,
                cwd=cwd,
                returncode=124,
                stdout=stringify(exc.stdout),
                stderr=stringify(exc.stderr) + "\ncommand timed out",
                duration_ms=elapsed_ms(started),
            )
        except OSError as exc:
            result = GitCommandResult(
                args=cmd,
                cwd=cwd,
                returncode=127 if isinstance(exc, FileNotFoundError) else 126,
                stderr=str(exc),
                duration_ms=elapsed_ms(started),
            )

        if check and result.returncode != 0:
            raise GitlinkMergeError(format_git_failure(result))
        return result


@dataclass(frozen=True)
class ConflictStage:
    mode: str
    sha: str
    stage: str
    path: str
    raw: str


@dataclass(frozen=True)
class Conflict:
    repo_path: Path
    path: str
    stages: tuple[ConflictStage, ...]
    kind: str

    @property
    def modes(self) -> set[str]:
        return {stage.mode for stage in self.stages}

    @property
    def stage_numbers(self) -> set[str]:
        return {stage.stage for stage in self.stages}

    def stage_sha(self, stage_number: str) -> str | None:
        matches = sorted({stage.sha for stage in self.stages if stage.stage == stage_number and stage.sha})
        if len(matches) != 1:
            return None
        return matches[0]


@dataclass(frozen=True)
class GitlinkMergeContext:
    parent_repo: Path
    submodule_path: str
    submodule_repo: Path
    base_sha: str | None
    ours_sha: str
    theirs_sha: str
    depth: int


@dataclass(frozen=True)
class MergeCommitMessage:
    title: str
    body: str = ""

    def git_args(self) -> list[str]:
        args = ["-m", self.title]
        if self.body:
            args.extend(["-m", self.body])
        return args


@dataclass
class GitlinkResolution:
    path: str
    repo_path: Path
    base_sha: str | None
    ours_sha: str
    theirs_sha: str
    result_sha: str
    strategy: str
    recursive_merge: bool


@dataclass
class RepositoryResolution:
    repo_path: Path
    depth: int
    gitlinks: list[GitlinkResolution] = field(default_factory=list)
    file_conflicts: list[Conflict] = field(default_factory=list)

    @property
    def resolved_gitlinks(self) -> int:
        return len(self.gitlinks)


@dataclass(frozen=True)
class MergeEvent:
    name: str
    repo_path: Path
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class MergePolicy:
    """Hooks used by RecursiveGitlinkMergeEngine.

    The default policy is CI-friendly and strict: it refuses non-gitlink
    conflicts and only fetches missing commits when configured to do so.
    """

    def __init__(
        self,
        *,
        fetch_missing_commits: bool = False,
        init_missing_submodules: bool = True,
        update_submodules_before_recursive_merge: bool = True,
    ) -> None:
        self.fetch_missing_commits = fetch_missing_commits
        self.init_missing_submodules = init_missing_submodules
        self.update_submodules_before_recursive_merge = update_submodules_before_recursive_merge

    def on_event(self, event: MergeEvent) -> None:
        return None

    def prepare_recursive_merge(
        self,
        engine: "RecursiveGitlinkMergeEngine",
        context: GitlinkMergeContext,
    ) -> None:
        engine.git.run(context.submodule_repo, ["checkout", "--detach", context.ours_sha])
        if self.update_submodules_before_recursive_merge:
            engine.git.run(
                context.submodule_repo,
                ["submodule", "update", "--init", "--recursive"],
                check=False,
            )

    def handle_file_conflicts(
        self,
        engine: "RecursiveGitlinkMergeEngine",
        repo_path: Path,
        conflicts: list[Conflict],
        depth: int,
    ) -> None:
        raise NonGitlinkConflictError(repo_path, conflicts)

    def validate_after_conflicts(
        self,
        engine: "RecursiveGitlinkMergeEngine",
        repo_path: Path,
        depth: int,
    ) -> None:
        return None

    def build_submodule_commit_message(
        self,
        context: GitlinkMergeContext,
    ) -> MergeCommitMessage:
        body_lines = [
            f"Parent repository: {context.parent_repo}",
            f"Submodule path: {context.submodule_path}",
            f"Base: {context.base_sha or 'n/a'}",
            f"Ours: {context.ours_sha}",
            f"Theirs: {context.theirs_sha}",
        ]
        return MergeCommitMessage(
            title=f"Recursive submodule merge: {context.submodule_path}",
            body="\n".join(body_lines),
        )


class RecursiveGitlinkMergeEngine:
    def __init__(
        self,
        git: GitBackend,
        *,
        policy: MergePolicy | None = None,
        max_depth: int = 16,
    ) -> None:
        self.git = git
        self.policy = policy or MergePolicy()
        self.max_depth = max_depth

    def resolve_active_conflicts(self, repo_path: Path, *, depth: int = 0) -> RepositoryResolution:
        repo = repo_path.resolve(strict=False)
        self._ensure_depth(depth, repo)
        self._ensure_git_worktree(repo)
        result = RepositoryResolution(repo_path=repo, depth=depth)
        self._event("repo.check", repo, "checking unresolved paths", {"depth": depth})

        while True:
            conflicts = self.analyze_conflicts(repo)
            if not conflicts:
                self._event("repo.clean", repo, "no unresolved paths remain", {"depth": depth})
                self.policy.validate_after_conflicts(self, repo, depth)
                return result

            gitlink_conflicts = [conflict for conflict in conflicts if conflict.kind == "gitlink"]
            non_gitlink_conflicts = [conflict for conflict in conflicts if conflict.kind != "gitlink"]

            if gitlink_conflicts:
                for conflict in gitlink_conflicts:
                    result.gitlinks.append(self.resolve_gitlink_conflict(repo, conflict, depth))
                continue

            if non_gitlink_conflicts:
                result.file_conflicts.extend(non_gitlink_conflicts)
                self.policy.handle_file_conflicts(self, repo, non_gitlink_conflicts, depth)
                remaining = [conflict for conflict in self.analyze_conflicts(repo) if conflict.kind != "gitlink"]
                if remaining:
                    raise NonGitlinkConflictError(repo, remaining)

    def merge_commits(
        self,
        repo_path: Path,
        *,
        ours_sha: str,
        theirs_sha: str,
        base_sha: str | None = None,
        depth: int = 0,
        message: MergeCommitMessage | None = None,
    ) -> str:
        repo = repo_path.resolve(strict=False)
        self._ensure_depth(depth, repo)
        self._ensure_git_worktree(repo)
        self.git.run(repo, ["checkout", "--detach", ours_sha])
        merge = self.git.run(repo, ["merge", "--no-ff", "--no-commit", theirs_sha], check=False)
        if merge.returncode == 0:
            self._event("repo.merge_clean", repo, "merge applied cleanly", {"depth": depth})
        elif merge.returncode == 1:
            self._event("repo.merge_conflicts", repo, "merge produced conflicts", {"depth": depth})
            self.resolve_active_conflicts(repo, depth=depth)
            remaining = self.analyze_conflicts(repo)
            if remaining:
                paths = ", ".join(conflict.path for conflict in remaining)
                raise GitlinkMergeError(f"unresolved conflicts remain in {repo}: {paths}")
        else:
            raise GitlinkMergeError(format_git_failure(merge))

        self.policy.validate_after_conflicts(self, repo, depth)
        commit_message = message or MergeCommitMessage(
            title="Recursive gitlink merge",
            body="\n".join(
                [
                    f"Base: {base_sha or 'n/a'}",
                    f"Ours: {ours_sha}",
                    f"Theirs: {theirs_sha}",
                ]
            ),
        )
        return self.commit_pending_merge(repo, commit_message)

    def resolve_gitlink_conflict(
        self,
        parent_repo: Path,
        conflict: Conflict,
        depth: int,
    ) -> GitlinkResolution:
        parent = parent_repo.resolve(strict=False)
        path = conflict.path
        base = conflict.stage_sha("1")
        ours = conflict.stage_sha("2")
        theirs = conflict.stage_sha("3")
        if not ours or not theirs:
            raise GitlinkMergeError(f"cannot resolve ours/theirs gitlink SHAs for {parent}:{path}")

        sub_repo = (parent / path).resolve(strict=False)
        if not self._is_git_worktree(sub_repo):
            if self.policy.init_missing_submodules:
                self.git.run(parent, ["submodule", "update", "--init", "--recursive", "--", path], check=False)
            if not self._is_git_worktree(sub_repo):
                raise GitlinkMergeError(f"submodule is not initialized: {sub_repo}")

        for sha in [value for value in (base, ours, theirs) if value]:
            self.ensure_commit_available(sub_repo, sha)

        strategy = "use_ours"
        result_sha = ours
        recursive_merge = False
        if ours == theirs:
            strategy = "same_sha"
        elif base and ours == base:
            strategy = "use_theirs"
            result_sha = theirs
        elif base and theirs == base:
            strategy = "use_ours"
        elif self.is_ancestor(sub_repo, ours, theirs):
            strategy = "fast_forward_to_theirs"
            result_sha = theirs
        elif self.is_ancestor(sub_repo, theirs, ours):
            strategy = "already_contains_theirs"
        else:
            recursive_merge = True
            strategy = "recursive_merge"
            context = GitlinkMergeContext(
                parent_repo=parent,
                submodule_path=path,
                submodule_repo=sub_repo,
                base_sha=base,
                ours_sha=ours,
                theirs_sha=theirs,
                depth=depth + 1,
            )
            self._ensure_depth(context.depth, sub_repo)
            self.policy.prepare_recursive_merge(self, context)
            result_sha = self.merge_commits(
                sub_repo,
                ours_sha=ours,
                theirs_sha=theirs,
                base_sha=base,
                depth=context.depth,
                message=self.policy.build_submodule_commit_message(context),
            )

        self.git.run(sub_repo, ["checkout", "--detach", result_sha])
        self.git.run(parent, ["add", "--", path])
        self._event(
            "gitlink.resolved",
            parent,
            f"resolved gitlink {path} via {strategy}",
            {
                "path": path,
                "base": base,
                "ours": ours,
                "theirs": theirs,
                "result": result_sha,
                "strategy": strategy,
                "recursive_merge": recursive_merge,
            },
        )
        return GitlinkResolution(
            path=path,
            repo_path=sub_repo,
            base_sha=base,
            ours_sha=ours,
            theirs_sha=theirs,
            result_sha=result_sha,
            strategy=strategy,
            recursive_merge=recursive_merge,
        )

    def analyze_conflicts(self, repo_path: Path) -> list[Conflict]:
        repo = repo_path.resolve(strict=False)
        names = self.git.run(repo, ["diff", "--name-only", "--diff-filter=U"], check=False)
        unmerged = self.git.run(repo, ["ls-files", "-u"], check=False)
        if unmerged.returncode != 0:
            raise GitlinkMergeError(format_git_failure(unmerged))

        grouped: dict[str, list[ConflictStage]] = {}
        for line in split_nonempty_lines(unmerged.stdout):
            parts = line.split(None, 3)
            if len(parts) != 4:
                grouped.setdefault("<unparseable>", []).append(
                    ConflictStage(mode="", sha="", stage="", path="<unparseable>", raw=line)
                )
                continue
            mode, sha, stage, path = parts
            grouped.setdefault(path, []).append(ConflictStage(mode=mode, sha=sha, stage=stage, path=path, raw=line))

        paths = sorted(set(split_nonempty_lines(names.stdout)) | set(grouped))
        conflicts: list[Conflict] = []
        for path in paths:
            stages = tuple(grouped.get(path, []))
            stage_numbers = {stage.stage for stage in stages}
            modes = {stage.mode for stage in stages}
            kind = "file"
            if path == ".gitmodules":
                kind = "gitmodules"
            elif path == "<unparseable>" or not stages:
                kind = "unparseable"
            elif modes == {"160000"} and {"2", "3"}.issubset(stage_numbers):
                kind = "gitlink"
            elif not {"2", "3"}.issubset(stage_numbers):
                kind = "unsupported_stage"
            conflicts.append(Conflict(repo_path=repo, path=path, stages=stages, kind=kind))
        return conflicts

    def ensure_commit_available(self, repo_path: Path, sha: str) -> None:
        repo = repo_path.resolve(strict=False)
        check = self.git.run(repo, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False)
        if check.returncode == 0:
            return
        if self.policy.fetch_missing_commits:
            self.git.run(repo, ["fetch", "--all", "--prune"], check=False)
            retry = self.git.run(repo, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False)
            if retry.returncode == 0:
                return
        raise GitlinkMergeError(f"required commit is unavailable in {repo}: {sha}")

    def is_ancestor(self, repo_path: Path, ancestor: str, descendant: str) -> bool:
        result = self.git.run(
            repo_path.resolve(strict=False),
            ["merge-base", "--is-ancestor", ancestor, descendant],
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitlinkMergeError(format_git_failure(result))

    def commit_pending_merge(self, repo_path: Path, message: MergeCommitMessage) -> str:
        repo = repo_path.resolve(strict=False)
        merge_head = self.git.run(repo, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False)
        staged = self.git.run(repo, ["diff", "--cached", "--quiet", "--ignore-submodules=none"], check=False)
        if staged.returncode not in (0, 1):
            raise GitlinkMergeError(format_git_failure(staged))

        if merge_head.returncode == 0 or staged.returncode == 1:
            self.git.run(repo, ["commit", *message.git_args()])
        return self.current_head(repo)

    def current_head(self, repo_path: Path) -> str:
        return self.git.run(repo_path.resolve(strict=False), ["rev-parse", "HEAD"]).stdout.strip()

    def has_unmerged_paths(self, repo_path: Path) -> bool:
        result = self.git.run(repo_path.resolve(strict=False), ["ls-files", "-u"], check=False)
        if result.returncode != 0:
            raise GitlinkMergeError(format_git_failure(result))
        return bool(result.stdout.strip())

    def _is_git_worktree(self, repo_path: Path) -> bool:
        result = self.git.run(repo_path.resolve(strict=False), ["rev-parse", "--is-inside-work-tree"], check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _ensure_git_worktree(self, repo_path: Path) -> None:
        if not self._is_git_worktree(repo_path):
            raise GitlinkMergeError(f"not a Git worktree: {repo_path}")

    def _ensure_depth(self, depth: int, repo_path: Path) -> None:
        if depth > self.max_depth:
            raise GitlinkMergeError(f"recursive submodule depth exceeded at {repo_path}: {depth} > {self.max_depth}")

    def _event(self, name: str, repo_path: Path, message: str, data: dict[str, Any] | None = None) -> None:
        self.policy.on_event(MergeEvent(name=name, repo_path=repo_path, message=message, data=data or {}))


def split_nonempty_lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def format_git_failure(result: GitCommandResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "no output"
    return (
        f"git command failed with exit code {result.returncode}: {' '.join(result.args)}\n"
        f"cwd: {result.cwd}\n"
        f"{detail}"
    )


def add_git_config(env: dict[str, str], key: str, value: str) -> None:
    try:
        index = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        index = 0
    env[f"GIT_CONFIG_KEY_{index}"] = key
    env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(index + 1)


def augment_git_for_windows_path(env: dict[str, str], git_executable: str) -> None:
    if platform.system().lower() != "windows":
        return

    existing_path = env.get("PATH", "")
    existing_parts = [part for part in existing_path.split(os.pathsep) if part]
    existing_normalized = {os.path.normcase(os.path.abspath(part)) for part in existing_parts}

    candidates: list[Path] = []
    found_git = shutil.which(git_executable, path=existing_path)
    if found_git:
        git_path = Path(found_git).resolve(strict=False)
        for root in possible_git_for_windows_roots(git_path):
            candidates.extend([root / "usr" / "bin", root / "mingw64" / "libexec" / "git-core"])

    for root in (Path(r"C:\Program Files\Git"), Path(r"C:\Program Files (x86)\Git")):
        candidates.extend([root / "usr" / "bin", root / "mingw64" / "libexec" / "git-core"])

    to_prepend: list[str] = []
    for candidate in candidates:
        candidate_str = str(candidate)
        normalized = os.path.normcase(os.path.abspath(candidate_str))
        if candidate.exists() and normalized not in existing_normalized:
            to_prepend.append(candidate_str)
            existing_normalized.add(normalized)

    if to_prepend:
        env["PATH"] = os.pathsep.join([*to_prepend, existing_path])


def possible_git_for_windows_roots(git_path: Path) -> list[Path]:
    roots: list[Path] = []
    for parent in git_path.parents:
        if parent.name.lower() in {"cmd", "bin"} and parent.parent.name.lower() == "git":
            roots.append(parent.parent)
        if parent.name.lower() == "git":
            roots.append(parent)
    return roots
