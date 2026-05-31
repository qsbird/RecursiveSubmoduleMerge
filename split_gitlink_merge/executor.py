"""Git execution layer for the split recursive gitlink merge core."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

from .model import Ancestry, Conflict, GitCommandResult, GitlinkMergeError, MergeCommitMessage
from .planner import GitlinkMergePlanner
from .ports import GitBackend


class SubprocessGitBackend:
    """Default subprocess-based Git backend."""

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


class GitActionExecutor:
    """High-level execution operations used by the engine."""

    def __init__(self, backend: GitBackend, planner: GitlinkMergePlanner | None = None) -> None:
        self.backend = backend
        self.planner = planner or GitlinkMergePlanner()

    def run(self, repo_path: Path, args: list[str], *, check: bool = True) -> GitCommandResult:
        return self.backend.run(repo_path, args, check=check)

    def read_conflicts(self, repo_path: Path) -> list[Conflict]:
        repo = repo_path.resolve(strict=False)
        names = self.run(repo, ["diff", "--name-only", "--diff-filter=U"], check=False)
        unmerged = self.run(repo, ["ls-files", "-u"], check=False)
        if unmerged.returncode != 0:
            raise GitlinkMergeError(format_git_failure(unmerged))
        return self.planner.classify_conflicts(
            repo,
            names_stdout=names.stdout,
            unmerged_stdout=unmerged.stdout,
        )

    def ensure_git_worktree(self, repo_path: Path) -> None:
        if not self.is_git_worktree(repo_path):
            raise GitlinkMergeError(f"not a Git worktree: {repo_path}")

    def is_git_worktree(self, repo_path: Path) -> bool:
        result = self.run(repo_path.resolve(strict=False), ["rev-parse", "--is-inside-work-tree"], check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def ensure_commit_available(self, repo_path: Path, sha: str, *, fetch_missing: bool) -> None:
        repo = repo_path.resolve(strict=False)
        check = self.run(repo, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False)
        if check.returncode == 0:
            return
        if fetch_missing:
            self.run(repo, ["fetch", "--all", "--prune"], check=False)
            retry = self.run(repo, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False)
            if retry.returncode == 0:
                return
        raise GitlinkMergeError(f"required commit is unavailable in {repo}: {sha}")

    def ancestry(self, repo_path: Path, ours_sha: str, theirs_sha: str) -> Ancestry:
        return Ancestry(
            ours_is_ancestor_of_theirs=self.is_ancestor(repo_path, ours_sha, theirs_sha),
            theirs_is_ancestor_of_ours=self.is_ancestor(repo_path, theirs_sha, ours_sha),
        )

    def is_ancestor(self, repo_path: Path, ancestor: str, descendant: str) -> bool:
        result = self.run(
            repo_path.resolve(strict=False),
            ["merge-base", "--is-ancestor", ancestor, descendant],
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitlinkMergeError(format_git_failure(result))

    def checkout_detached(self, repo_path: Path, sha: str) -> None:
        self.run(repo_path.resolve(strict=False), ["checkout", "--detach", sha])

    def update_submodules(self, repo_path: Path, *, path: str | None = None, check: bool = True) -> GitCommandResult:
        args = ["submodule", "update", "--init", "--recursive"]
        if path is not None:
            args.extend(["--", path])
        return self.run(repo_path.resolve(strict=False), args, check=check)

    def merge_no_commit(self, repo_path: Path, sha: str) -> GitCommandResult:
        return self.run(repo_path.resolve(strict=False), ["merge", "--no-ff", "--no-commit", sha], check=False)

    def stage_path(self, repo_path: Path, path: str) -> None:
        self.run(repo_path.resolve(strict=False), ["add", "--", path])

    def commit_pending_merge(self, repo_path: Path, message: MergeCommitMessage) -> str:
        repo = repo_path.resolve(strict=False)
        merge_head = self.run(repo, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False)
        staged = self.run(repo, ["diff", "--cached", "--quiet", "--ignore-submodules=none"], check=False)
        if staged.returncode not in (0, 1):
            raise GitlinkMergeError(format_git_failure(staged))
        if merge_head.returncode == 0 or staged.returncode == 1:
            self.run(repo, ["commit", *message.git_args()])
        return self.current_head(repo)

    def current_head(self, repo_path: Path) -> str:
        return self.run(repo_path.resolve(strict=False), ["rev-parse", "HEAD"]).stdout.strip()


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
