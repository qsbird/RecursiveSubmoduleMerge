"""Manual recursive publish workflow.

The publish source of truth is the root repository HEAD.  Submodule commits are
read from the parent commit's gitlink entries, not from each submodule worktree
HEAD.  The workflow never checks out branches and never force-pushes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from . import execution
from .repository_ops import ManualRepositoryOps


IN_PROGRESS_GIT_PATHS: tuple[tuple[str, str], ...] = (
    ("merge", "MERGE_HEAD"),
    ("rebase", "rebase-merge"),
    ("rebase", "rebase-apply"),
    ("cherry-pick", "CHERRY_PICK_HEAD"),
    ("revert", "REVERT_HEAD"),
    ("sequencer", "sequencer"),
    ("bisect", "BISECT_LOG"),
)


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

    @property
    def success(self) -> bool:
        return not self.failures


def short_sha(value: str) -> str:
    return value[:12]


def target_ref(push_target: str) -> str:
    return f"refs/heads/{push_target}"


def normalize_display_path(parent: str, child: str) -> str:
    normalized_child = child.replace("\\", "/").strip("/")
    if parent in {"", "."}:
        return normalized_child or "."
    return f"{parent.rstrip('/')}/{normalized_child}"


def ensure_valid_push_target(repo_path: Path, push_target: str, ops: ManualRepositoryOps) -> None:
    if not push_target.strip():
        raise ops._automation_error("Publish target branch cannot be empty.")
    if push_target.startswith("-"):
        raise ops._automation_error("Publish target branch cannot start with '-'.")
    if push_target.startswith("refs/"):
        raise ops._automation_error(
            "Publish target branch must be a branch name, not a full ref. "
            f"Use '{push_target.removeprefix('refs/heads/')}' instead of '{push_target}'."
        )
    branch_result = ops.run_git(repo_path, ["check-ref-format", "--branch", push_target], check=False)
    if branch_result.returncode != 0:
        raise ops._automation_error(f"Invalid publish target branch: {push_target}\n{branch_result.stderr}")
    remote_ref = target_ref(push_target)
    remote_ref_result = ops.run_git(repo_path, ["check-ref-format", remote_ref], check=False)
    if remote_ref_result.returncode != 0:
        raise ops._automation_error(
            f"Invalid remote publish target ref: {remote_ref}\n{remote_ref_result.stderr}"
        )


def git_state_path(repo_path: Path, state_name: str, ops: ManualRepositoryOps) -> Path:
    result = ops.run_git(repo_path, ["rev-parse", "--git-path", state_name], check=False)
    raw_path = result.stdout.strip() if result.returncode == 0 else ""
    if not raw_path:
        return repo_path / ".git" / state_name
    path = Path(raw_path)
    return path if path.is_absolute() else repo_path / path


def in_progress_operations(repo_path: Path, ops: ManualRepositoryOps) -> list[str]:
    operations: list[str] = []
    for operation, state_name in IN_PROGRESS_GIT_PATHS:
        if git_state_path(repo_path, state_name, ops).exists():
            operations.append(operation)
    return sorted(set(operations))


def collect_existing_repositories(repo_path: Path, ops: ManualRepositoryOps) -> list[Path]:
    repos = [repo_path]
    for submodule_path in ops.get_submodule_paths(repo_path):
        child_repo = repo_path / submodule_path
        if not child_repo.exists():
            continue
        probe = ops.run_git(child_repo, ["rev-parse", "--show-toplevel"], check=False)
        if probe.returncode != 0:
            continue
        repos.extend(collect_existing_repositories(child_repo, ops))
    return repos


def ensure_no_in_progress_operations(repos: Iterable[Path], ops: ManualRepositoryOps) -> None:
    blocked: list[str] = []
    for repo in repos:
        operations = in_progress_operations(repo, ops)
        if operations:
            blocked.append(f"{repo}: {', '.join(operations)}")
    if blocked:
        raise ops._automation_error(
            "Publish cannot continue while repositories have in-progress Git operations:\n"
            + "\n".join(f"  - {item}" for item in blocked)
        )


def submodule_paths_at_commit(repo_path: Path, commitish: str, ops: ManualRepositoryOps) -> list[str]:
    result = ops.run_git(
        repo_path,
        ["config", "--blob", f"{commitish}:.gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    paths: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            paths.append(parts[1].strip())
    return paths


def gitlink_sha_at_commit(repo_path: Path, commitish: str, submodule_path: str, ops: ManualRepositoryOps) -> str | None:
    result = ops.run_git(repo_path, ["ls-tree", commitish, "--", submodule_path], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    line = result.stdout.splitlines()[0]
    meta, _tab, _path = line.partition("\t")
    parts = meta.split()
    if len(parts) >= 3 and parts[0] == "160000" and parts[1] == "commit":
        return parts[2]
    return None


def collect_publish_plan(
    repo_path: Path,
    desired_sha: str,
    ops: ManualRepositoryOps,
    *,
    display_path: str = ".",
    depth: int = 0,
    seen: set[tuple[Path, str]] | None = None,
) -> list[PublishPlanItem]:
    resolved_repo = repo_path.resolve(strict=False)
    seen_items = seen if seen is not None else set()
    key = (resolved_repo, desired_sha)
    if key in seen_items:
        return []
    seen_items.add(key)

    if not ops.commit_exists(repo_path, desired_sha):
        raise ops._automation_error(f"Desired commit is not available locally in {repo_path}: {desired_sha}")

    items = [
        PublishPlanItem(
            repo_path=resolved_repo,
            display_path=display_path,
            desired_sha=desired_sha,
            depth=depth,
        )
    ]
    for submodule_path in submodule_paths_at_commit(repo_path, desired_sha, ops):
        child_sha = gitlink_sha_at_commit(repo_path, desired_sha, submodule_path, ops)
        if not child_sha:
            raise ops._automation_error(
                f"Could not resolve gitlink commit for submodule {submodule_path} at {repo_path}:{desired_sha}"
            )
        child_repo = repo_path / submodule_path
        if not child_repo.exists():
            raise ops._automation_error(
                f"Submodule worktree is missing after update: {child_repo}. "
                "Run git submodule update --init --recursive and retry."
            )
        ops.ensure_git_repo(child_repo)
        items.extend(
            collect_publish_plan(
                child_repo,
                child_sha,
                ops,
                display_path=normalize_display_path(display_path, submodule_path),
                depth=depth + 1,
                seen=seen_items,
            )
        )
    return items


def branch_commit(repo_path: Path, branch: str, ops: ManualRepositoryOps) -> str | None:
    result = ops.run_git(repo_path, ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def create_branch_ref(repo_path: Path, branch: str, desired_sha: str, ops: ManualRepositoryOps) -> bool:
    result = ops.run_git(repo_path, ["branch", branch, desired_sha], check=False)
    return result.returncode == 0


def unique_suffix_branch(repo_path: Path, push_target: str, desired_sha: str, ops: ManualRepositoryOps) -> str | None:
    base_name = f"{push_target}-publish-{short_sha(desired_sha)}"
    for index in range(0, 100):
        candidate = base_name if index == 0 else f"{base_name}-{index + 1}"
        existing = branch_commit(repo_path, candidate, ops)
        if existing == desired_sha:
            return candidate
        if existing is not None:
            continue
        if create_branch_ref(repo_path, candidate, desired_sha, ops):
            return candidate
    return None


def sync_local_branch_ref(
    item: PublishPlanItem,
    push_target: str,
    ops: ManualRepositoryOps,
    *,
    allow_suffix_branch: bool,
    dry_run: bool,
) -> None:
    existing = branch_commit(item.repo_path, push_target, ops)
    if existing == item.desired_sha:
        item.local_branch = push_target
        item.local_branch_action = "already_aligned"
        return
    if dry_run:
        item.local_branch = push_target if existing is None else None
        item.local_branch_action = "would_create" if existing is None else "would_create_suffix"
        return
    if existing is None:
        if create_branch_ref(item.repo_path, push_target, item.desired_sha, ops):
            item.local_branch = push_target
            item.local_branch_action = "created"
            return
        item.messages.append(f"Could not create local branch {push_target}; trying suffix branch.")
    elif allow_suffix_branch:
        item.messages.append(
            f"Local branch {push_target} points to {short_sha(existing)}, "
            f"not desired {short_sha(item.desired_sha)}; using suffix branch."
        )
    else:
        item.local_branch_action = "name_conflict_left_unchanged"
        item.messages.append(
            f"Local branch {push_target} points to {short_sha(existing)} and was left unchanged."
        )
        return

    if allow_suffix_branch:
        suffix = unique_suffix_branch(item.repo_path, push_target, item.desired_sha, ops)
        if suffix:
            item.local_branch = suffix
            item.local_branch_action = "suffix_created_or_aligned"
            return
    item.local_branch_action = "not_created"
    item.messages.append("Local branch ref was not created; push still uses the desired commit SHA directly.")


def remote_branch_sha(repo_path: Path, remote: str, push_target: str, ops: ManualRepositoryOps) -> str | None:
    ref = target_ref(push_target)
    result = ops.run_git(repo_path, ["ls-remote", remote, ref], check=False)
    if result.returncode != 0:
        raise ops._automation_error(ops._format_git_failure(result))
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == ref:
            return parts[0]
    return None


def fetch_remote_target(repo_path: Path, remote: str, push_target: str, ops: ManualRepositoryOps) -> str | None:
    result = ops.run_git(repo_path, ["fetch", remote, target_ref(push_target)], check=False, stream_output=ops._verbose())
    if result.returncode != 0:
        return None
    parsed = ops.run_git(repo_path, ["rev-parse", "FETCH_HEAD"], check=False)
    return parsed.stdout.strip() if parsed.returncode == 0 else None


def is_ancestor(repo_path: Path, ancestor: str, descendant: str, ops: ManualRepositoryOps) -> bool:
    result = ops.run_git(repo_path, ["merge-base", "--is-ancestor", ancestor, descendant], check=False)
    return result.returncode == 0


def classify_remote_state(
    item: PublishPlanItem,
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
) -> None:
    remote_sha = remote_branch_sha(item.repo_path, remote, push_target, ops)
    item.remote_before_sha = remote_sha
    if remote_sha is None:
        item.remote_state = "missing"
        return
    if remote_sha == item.desired_sha:
        item.remote_state = "up_to_date"
        item.skipped = True
        return
    fetched = fetch_remote_target(item.repo_path, remote, push_target, ops)
    if not fetched:
        raise ops._automation_error(f"Could not fetch remote target {remote}/{push_target} in {item.repo_path}")
    if is_ancestor(item.repo_path, fetched, item.desired_sha, ops):
        item.remote_state = "fast_forward"
        return
    item.remote_state = "non_fast_forward"
    raise ops._automation_error(
        f"Remote branch is not an ancestor of the desired commit; refusing non-fast-forward push.\n"
        f"Repository: {item.repo_path}\n"
        f"Remote: {remote}/{push_target} -> {remote_sha}\n"
        f"Desired: {item.desired_sha}"
    )


def run_push_check(
    item: PublishPlanItem,
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
) -> None:
    result = ops.run_git(
        item.repo_path,
        ["push", "--dry-run", "--recurse-submodules=check", remote, f"{item.desired_sha}:{target_ref(push_target)}"],
        check=False,
        stream_output=ops._verbose(),
    )
    if result.returncode != 0:
        raise ops._automation_error(ops._format_git_failure(result))
    item.dry_run_ok = True


def push_item(
    item: PublishPlanItem,
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
) -> None:
    result = ops.run_git(
        item.repo_path,
        ["push", "--recurse-submodules=check", remote, f"{item.desired_sha}:{target_ref(push_target)}"],
        check=False,
        stream_output=ops._verbose(),
    )
    if result.returncode != 0:
        raise ops._automation_error(ops._format_git_failure(result))
    item.pushed = True


def print_publish_item(item: PublishPlanItem) -> None:
    branch = item.local_branch or "(none)"
    action = item.local_branch_action
    remote_before = short_sha(item.remote_before_sha) if item.remote_before_sha else "(missing)"
    status = "skipped" if item.skipped else "pushed" if item.pushed else "checked"
    print(
        f"{status}: {item.display_path} "
        f"commit={short_sha(item.desired_sha)} "
        f"remote_before={remote_before} "
        f"remote_state={item.remote_state} "
        f"local_branch={branch} ({action})"
    )
    for message in item.messages:
        print(f"  note: {message}")


def run_publish_workflow(
    repo_path: Path,
    *,
    remote: str,
    push_target: str,
    check_only: bool,
    sync_local_branches: bool,
    allow_suffix_branch: bool,
    ops: ManualRepositoryOps,
) -> PublishResult:
    result = PublishResult(check_only=check_only)
    ops.ensure_git_repo(repo_path)
    ensure_valid_push_target(repo_path, push_target, ops)
    if not ops.remote_exists(repo_path, remote):
        raise ops._automation_error(f"Remote does not exist in root repository: {remote}")

    ensure_no_in_progress_operations(collect_existing_repositories(repo_path, ops), ops)
    update_result = ops.run_git(
        repo_path,
        ["submodule", "update", "--init", "--recursive"],
        check=False,
        stream_output=ops._verbose(),
    )
    if update_result.returncode != 0:
        raise ops._automation_error(ops._format_git_failure(update_result))

    root_sha = ops.resolve_commit(repo_path, "HEAD")
    plan = collect_publish_plan(repo_path, root_sha, ops)
    ensure_no_in_progress_operations((item.repo_path for item in plan), ops)
    result.items = sorted(plan, key=lambda item: item.depth, reverse=True)

    for item in result.items:
        if not ops.remote_exists(item.repo_path, remote):
            raise ops._automation_error(f"Remote does not exist in repository {item.repo_path}: {remote}")
        classify_remote_state(item, remote, push_target, ops)

    for item in result.items:
        if sync_local_branches:
            sync_local_branch_ref(
                item,
                push_target,
                ops,
                allow_suffix_branch=allow_suffix_branch,
                dry_run=check_only,
            )

    for item in result.items:
        run_push_check(item, remote, push_target, ops)
        if not check_only and not item.skipped:
            push_item(item, remote, push_target, ops)
        print_publish_item(item)
        result.warnings.extend(item.messages)

    return result


def run_publish(
    args: Any,
    *,
    manual_module: ModuleType | None = None,
    ops: ManualRepositoryOps | None = None,
) -> int:
    execution.set_verbose(bool(getattr(args, "verbose", False)))
    resolved_ops = ops or ManualRepositoryOps(manual_module)
    repo_path = Path(args.repo).resolve()
    try:
        result = run_publish_workflow(
            repo_path,
            remote=str(args.remote or "origin"),
            push_target=str(args.push_target or "").strip(),
            check_only=bool(getattr(args, "check_only", False)),
            sync_local_branches=not bool(getattr(args, "no_sync_local_branches", False)),
            allow_suffix_branch=not bool(getattr(args, "no_branch_suffix", False)),
            ops=resolved_ops,
        )
    except resolved_ops._automation_error_class() as exc:
        print(str(exc))
        return 1
    action = "Publish check completed successfully." if result.check_only else "Publish completed successfully."
    print(action)
    print(f"Repositories processed: {len(result.items)}")
    return 0


@dataclass(frozen=True)
class ManualPublishService:
    manual_module: ModuleType | None = None

    def __post_init__(self) -> None:
        return None

    def run_publish(self, args: Any) -> int:
        return run_publish(args, manual_module=self.manual_module, ops=ManualRepositoryOps(self.manual_module))

    def run_publish_workflow(
        self,
        repo_path: Path,
        *,
        remote: str,
        push_target: str,
        check_only: bool = False,
        sync_local_branches: bool = True,
        allow_suffix_branch: bool = True,
    ) -> PublishResult:
        return run_publish_workflow(
            repo_path,
            remote=remote,
            push_target=push_target,
            check_only=check_only,
            sync_local_branches=sync_local_branches,
            allow_suffix_branch=allow_suffix_branch,
            ops=ManualRepositoryOps(self.manual_module),
        )
