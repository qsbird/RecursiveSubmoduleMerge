"""Manual recursive publish workflow.

The publish source of truth is the root repository HEAD.  Submodule commits are
read from the parent commit's gitlink entries, not from each submodule worktree
HEAD.  The workflow never checks out branches and never force-pushes.

Check dry-runs each repository with ``--recurse-submodules=no``.  Git's
``--recurse-submodules=check`` is an execute-time safety net after descendants
have been pushed; it is not a pre-condition of dry-run, because dry-run never
uploads submodule objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

import sys as _sys

from . import execution
from .models import PublishCandidate
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
    reachable_on_remote: bool = False
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


def first_parent_commit(repo_path: Path, commitish: str, ops: ManualRepositoryOps) -> str | None:
    result = ops.run_git(repo_path, ["rev-parse", "--verify", f"{commitish}^1^{{commit}}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def gitlink_baseline_sha(
    repo_path: Path,
    baseline_sha: str | None,
    submodule_path: str,
    ops: ManualRepositoryOps,
) -> str | None:
    if not baseline_sha:
        return None
    return gitlink_sha_at_commit(repo_path, baseline_sha, submodule_path, ops)


def collect_publish_plan(
    repo_path: Path,
    desired_sha: str,
    ops: ManualRepositoryOps,
    *,
    display_path: str = ".",
    depth: int = 0,
    baseline_sha: str | None = None,
    infer_baseline: bool = True,
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
    parent_baseline_sha = (
        first_parent_commit(repo_path, desired_sha, ops) if infer_baseline and baseline_sha is None else baseline_sha
    )
    for submodule_path in submodule_paths_at_commit(repo_path, desired_sha, ops):
        child_sha = gitlink_sha_at_commit(repo_path, desired_sha, submodule_path, ops)
        if not child_sha:
            raise ops._automation_error(
                f"Could not resolve gitlink commit for submodule {submodule_path} at {repo_path}:{desired_sha}"
            )
        child_baseline_sha = gitlink_baseline_sha(repo_path, parent_baseline_sha, submodule_path, ops)
        child_repo = repo_path / submodule_path
        if not child_repo.exists():
            raise ops._automation_error(
                f"Submodule worktree is missing after update: {child_repo}. "
                "Run git submodule update --init --recursive and retry."
            )
        ops.ensure_git_repo(child_repo)
        child_items = collect_publish_plan(
            child_repo,
            child_sha,
            ops,
            display_path=normalize_display_path(display_path, submodule_path),
            depth=depth + 1,
            baseline_sha=child_baseline_sha,
            infer_baseline=False,
            seen=seen_items,
        )
        if child_baseline_sha == child_sha and gitlink_reachable_on_any_remote(child_repo, child_sha, ops):
            child_items = child_items[1:]
        items.extend(child_items)
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


def gitlink_reachable_on_any_remote(repo_path: Path, sha: str, ops: ManualRepositoryOps) -> bool:
    """Return True when *sha* is already reachable from some remote-tracking ref.

    This matches Git's ``--recurse-submodules=check`` question: whether the
    commit can be found on a remote of this repository, not whether it is
    already on the publish target branch.
    """
    result = ops.run_git(
        repo_path,
        ["rev-list", "--max-count=1", sha, "--not", "--remotes"],
        check=False,
    )
    if result.returncode != 0:
        return False
    return not result.stdout.strip()


def parse_missing_submodule_paths(output: str) -> list[str]:
    """Parse submodule paths from ``git push --recurse-submodules=check`` output.

    The markers are English because ``_run_checked_push`` pins ``LC_ALL=C``.
    Localized git output is not a supported input.
    """
    marker = re.compile(
        r"The following submodule paths contain changes that can\s+not be found on any remote:"
    )
    match = marker.search(output)
    if match is None:
        return []
    rest = output[match.end() :]
    paths: list[str] = []
    for line in rest.splitlines():
        stripped = line.strip().replace("\\", "/")
        if not stripped:
            if paths:
                break
            continue
        lowered = stripped.lower()
        if lowered.startswith(("fatal:", "please try", "or cd ", "to push ", "git push")):
            break
        paths.append(stripped)
    return paths


def is_strict_descendant_display_path(parent: str, child: str) -> bool:
    parent_norm = parent.replace("\\", "/").strip("/") or "."
    child_norm = child.replace("\\", "/").strip("/") or "."
    if child_norm == parent_norm:
        return False
    if parent_norm == ".":
        return child_norm != "."
    return child_norm.startswith(parent_norm + "/")


def required_descendant_display_paths(items: list[PublishPlanItem], parent_path: str) -> list[str]:
    required: list[str] = []
    for item in items:
        if not is_strict_descendant_display_path(parent_path, item.display_path):
            continue
        if item.skipped:
            continue
        if item.reachable_on_remote:
            continue
        required.append(item.display_path)
    return required


def ensure_required_descendants_selected(
    items: list[PublishPlanItem],
    selected_display_paths: set[str] | None,
    ops: ManualRepositoryOps,
) -> None:
    if selected_display_paths is None:
        return
    pushing = {
        item.display_path
        for item in items
        if not item.skipped and item.display_path in selected_display_paths
    }
    blocked: list[str] = []
    for item in items:
        if item.display_path not in pushing:
            continue
        missing = [
            path
            for path in required_descendant_display_paths(items, item.display_path)
            if path not in pushing
        ]
        if missing:
            blocked.append(f"{item.display_path} requires {', '.join(missing)}")
    if blocked:
        raise ops._automation_error(
            "Cannot publish a parent repository without also pushing submodule "
            "commits that are not on any remote:\n"
            + "\n".join(f"  - {line}" for line in blocked)
        )


def run_push_check(
    item: PublishPlanItem,
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
) -> None:
    """Dry-run this repository only.

    ``--recurse-submodules=no`` is required: Git's ``check`` would fail a parent
    whose child SHA is not already on a remote, even when that child is part of
    the same publish plan and will be pushed first.
    """
    result = ops.run_git(
        item.repo_path,
        ["push", "--dry-run", "--recurse-submodules=no", remote, f"{item.desired_sha}:{target_ref(push_target)}"],
        check=False,
        stream_output=ops._verbose(),
    )
    if result.returncode != 0:
        raise ops._automation_error(ops._format_git_failure(result))
    item.dry_run_ok = True


def _push_spec(item: PublishPlanItem, push_target: str) -> str:
    return f"{item.desired_sha}:{target_ref(push_target)}"


def _run_checked_push(
    item: PublishPlanItem,
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
) -> Any:
    return ops.run_git(
        item.repo_path,
        ["push", "--recurse-submodules=check", remote, _push_spec(item, push_target)],
        check=False,
        stream_output=ops._verbose(),
        env_overrides={"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"},
    )


def push_item(
    item: PublishPlanItem,
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
    *,
    plan_items: list[PublishPlanItem] | None = None,
) -> None:
    result = _run_checked_push(item, remote, push_target, ops)
    if result.returncode != 0:
        missing_paths = parse_missing_submodule_paths(f"{result.stderr}\n{result.stdout}")
        if missing_paths:
            pushed_missing = False
            for relative_path in missing_paths:
                child_repo = (item.repo_path / relative_path).resolve(strict=False)
                child = None
                if plan_items is not None:
                    child = next(
                        (
                            candidate
                            for candidate in plan_items
                            if candidate.repo_path.resolve(strict=False) == child_repo
                        ),
                        None,
                    )
                if child is not None:
                    if child.skipped:
                        raise ops._automation_error(
                            f"Cannot publish {item.display_path}: submodule {relative_path} "
                            "is not on any remote and was excluded from this publish."
                        )
                    if not child.pushed:
                        push_item(child, remote, push_target, ops, plan_items=plan_items)
                        pushed_missing = True
                    continue
                raise ops._automation_error(
                    f"Cannot publish {item.display_path}: submodule {relative_path} is not on any remote "
                    "but was not included in the checked publish plan. Refusing an unconfirmed ad-hoc push; "
                    "run the publish check again."
                )
            if pushed_missing:
                result = _run_checked_push(item, remote, push_target, ops)
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


def run_publish_check(
    repo_path: Path,
    *,
    remote: str,
    push_target: str,
    sync_local_branches: bool,
    allow_suffix_branch: bool,
    ops: ManualRepositoryOps,
) -> PublishResult:
    """Run the check phase of publish: collect plan, classify remote state, dry-run push.

    Returns a :class:`PublishResult` with ``check_only=True`` and all plan
    items populated with their remote state.  No actual push is performed.
    """
    result = PublishResult(check_only=True)
    ops.ensure_git_repo(repo_path)
    ensure_valid_push_target(repo_path, push_target, ops)
    if not ops.remote_exists(repo_path, remote):
        raise ops._automation_error(f"Remote does not exist in root repository: {remote}")

    ensure_no_in_progress_operations(collect_existing_repositories(repo_path, ops), ops)
    print(
        "Publish check runs git submodule update --init --recursive before building the plan. "
        "That can initialize missing submodules and move existing submodule worktrees to the "
        "gitlink commits recorded by HEAD; --check-only does not skip this step.",
        file=_sys.stderr,
    )
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
        item.reachable_on_remote = gitlink_reachable_on_any_remote(item.repo_path, item.desired_sha, ops)
        if not item.reachable_on_remote:
            item.messages.append(
                "Commit is not on any remote yet; it will be pushed before parent repositories."
            )
        classify_remote_state(item, remote, push_target, ops)

    for item in result.items:
        if sync_local_branches:
            sync_local_branch_ref(
                item,
                push_target,
                ops,
                allow_suffix_branch=allow_suffix_branch,
                dry_run=True,
            )

    for item in result.items:
        if item.skipped:
            print_publish_item(item)
            result.warnings.extend(item.messages)
            continue
        run_push_check(item, remote, push_target, ops)
        print_publish_item(item)
        result.warnings.extend(item.messages)

    return result


# -------------------------------------------------------------------- #
#  Interactive / discovery helpers for the three-phase publish workflow
# -------------------------------------------------------------------- #

def discover_publish_candidates(
    repo_path: Path,
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
) -> list[PublishCandidate]:
    """Phase 1 – discover every repository that would be pushed.

    1.  Use the existing ``collect_publish_plan`` to find repos whose gitlink
        has changed relative to the first-parent baseline.
    2.  Augment the list with every repository reachable via
        ``collect_existing_repositories`` so the user can add back repos
        that are not detected by gitlink-diff (e.g. unchanged baseline but
        local commits that still need to go out).
    3.  Deduplicate by (resolved repo_path, commit_sha).
    """
    root_sha = ops.resolve_commit(repo_path, "HEAD")
    plan = collect_publish_plan(repo_path, root_sha, ops)

    candidate_map: dict[tuple[str, str], PublishCandidate] = {}
    for item in plan:
        key = (str(item.repo_path.resolve()), item.desired_sha)
        if key in candidate_map:
            continue
        remote_before = None
        remote_state = "unknown"
        try:
            if ops.remote_exists(item.repo_path, remote):
                remote_before = remote_branch_sha(item.repo_path, remote, push_target, ops)
                if remote_before is None:
                    remote_state = "missing"
                elif remote_before == item.desired_sha:
                    remote_state = "up_to_date"
                else:
                    remote_state = "needs_push"
        except Exception:
            pass
        candidate_map[key] = PublishCandidate(
            repo_path=item.repo_path,
            display_path=item.display_path,
            commit_sha=item.desired_sha,
            remote_before_sha=remote_before,
            remote_state=remote_state,
            depth=item.depth,
            reachable_on_remote=item.reachable_on_remote,
        )

    # also include repos that collect_publish_plan skipped so the user
    # can manually add them in the interactive phase
    for existing_path in collect_existing_repositories(repo_path, ops):
        try:
            sha = ops.resolve_commit(existing_path, "HEAD")
        except Exception:
            continue
        key = (str(existing_path.resolve()), sha)
        if key in candidate_map:
            continue
        candidate_map[key] = PublishCandidate(
            repo_path=existing_path,
            display_path=_repo_display_path(repo_path, existing_path),
            commit_sha=sha,
            remote_state="unknown",
            depth=_compute_depth(existing_path),
        )

    candidates = list(candidate_map.values())
    candidates.sort(key=lambda c: c.depth, reverse=True)
    return candidates


def _repo_display_path(root: Path, repo: Path) -> str:
    try:
        rel = repo.resolve().relative_to(root.resolve())
        display = rel.as_posix()
        return display if display != "." else "."
    except ValueError:
        return str(repo)


def _compute_depth(repo: Path) -> int:
    try:
        parts = repo.resolve().as_posix().strip("/").split("/")
    except Exception:
        return 0
    return max(0, len(parts) - 1)


def display_publish_plan(candidates: list[PublishCandidate]) -> None:
    """Print the publish plan without any interaction (used by --check-only + interactive)."""
    print("=" * 60)
    print(" Publish Plan Preview")
    print("=" * 60)
    print(f" Repositories to push: {len(candidates)}")
    print("=" * 60)
    for i, c in enumerate(candidates, 1):
        marker = "[✓]" if c.selected else "[ ]"
        print(f"  {marker} {i:>3}. {c.display_path:<40} (depth={c.depth})")
        print(f"        Commit: {short_sha(c.commit_sha)}")
        print(f"        Remote: {c.remote_state} ", end="")
        if c.remote_before_sha:
            print(f"(currently at {short_sha(c.remote_before_sha)})")
        else:
            print()
    print("=" * 60)


def _candidates_as_plan_items(candidates: list[PublishCandidate]) -> list[PublishPlanItem]:
    return [
        PublishPlanItem(
            repo_path=candidate.repo_path,
            display_path=candidate.display_path,
            desired_sha=candidate.commit_sha,
            depth=candidate.depth,
            reachable_on_remote=candidate.reachable_on_remote,
        )
        for candidate in candidates
    ]


def missing_required_selection_messages(candidates: list[PublishCandidate]) -> list[str]:
    selected = {candidate.display_path for candidate in candidates if candidate.selected}
    items = _candidates_as_plan_items(candidates)
    blocked: list[str] = []
    for candidate in candidates:
        if not candidate.selected:
            continue
        missing = [
            path
            for path in required_descendant_display_paths(items, candidate.display_path)
            if path not in selected
        ]
        if missing:
            blocked.append(f"{candidate.display_path} requires {', '.join(missing)}")
    return blocked


def apply_candidate_toggle(candidates: list[PublishCandidate], index: int) -> None:
    if not 0 <= index < len(candidates):
        return
    candidates[index].selected = not candidates[index].selected
    items = _candidates_as_plan_items(candidates)
    path = candidates[index].display_path
    if candidates[index].selected:
        required = set(required_descendant_display_paths(items, path))
        for candidate in candidates:
            if candidate.display_path in required:
                candidate.selected = True
        return
    for candidate in candidates:
        if path in required_descendant_display_paths(items, candidate.display_path):
            candidate.selected = False


def confirm_publish_plan_interactive(
    candidates: list[PublishCandidate],
    *,
    input_func: Any = input,
    output_func: Any = print,
) -> list[PublishCandidate] | None:
    """Phase 2 – interactive confirmation with toggle/add/remove capabilities."""
    if not _sys.stdin.isatty():
        # non-interactive — just return the full list
        return candidates

    local = list(candidates)  # mutable copy for the session

    while True:
        output_func()
        output_func("=" * 60)
        output_func(" Publish Plan Confirmation")
        output_func("=" * 60)
        output_func(f" Repositories to push: {sum(1 for c in local if c.selected)}"
                     f" of {len(local)}")
        output_func("=" * 60)
        for i, c in enumerate(local, 1):
            marker = "[✓]" if c.selected else "[ ]"
            output_func(f"  {marker} {i:>3}. {c.display_path:<40} (depth={c.depth})")
            output_func(f"        Commit: {short_sha(c.commit_sha)}")
            output_func(f"        Remote: {c.remote_state} ", end="")
            if c.remote_before_sha:
                output_func(f"(currently at {short_sha(c.remote_before_sha)})")
            else:
                output_func("")
        output_func("=" * 60)
        output_func()
        output_func(" Commands:")
        output_func("  1-N    Toggle selection for that repository")
        output_func("  a      Select all")
        output_func("  n      Deselect all")
        output_func("  c      Confirm and execute push")
        output_func("  q      Cancel and exit")
        output_func()

        try:
            choice = input_func("Your choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            output_func("\nPublish cancelled.")
            return None

        if choice in {"q", "quit", "exit"}:
            return None
        if choice == "c":
            blocked = missing_required_selection_messages(local)
            if blocked:
                output_func(
                    "Cannot publish a parent repository without also pushing "
                    "submodule commits that are not on any remote:"
                )
                for line in blocked:
                    output_func(f"  - {line}")
                continue
            return local
        if choice == "a":
            for c in local:
                c.selected = True
            continue
        if choice == "n":
            for c in local:
                c.selected = False
            continue
        try:
            apply_candidate_toggle(local, int(choice) - 1)
        except ValueError:
            pass


# -------------------------------------------------------------------- #
#  run_publish helpers — wrappers that handle PublishCandidate lists
# -------------------------------------------------------------------- #

def _run_publish_workflow_with_candidates(
    candidates: list[PublishCandidate],
    remote: str,
    push_target: str,
    ops: ManualRepositoryOps,
    *,
    check_only: bool,
    sync_local_branches: bool,
    allow_suffix_branch: bool,
) -> PublishResult:
    """Execute the actual push steps (Phase 3) using PublishCandidate list."""
    result = PublishResult(check_only=check_only)
    for candidate in candidates:
        item = PublishPlanItem(
            repo_path=candidate.repo_path,
            display_path=candidate.display_path,
            desired_sha=candidate.commit_sha,
            depth=candidate.depth,
        )
        if not ops.remote_exists(item.repo_path, remote):
            raise ops._automation_error(f"Remote does not exist in repository {item.repo_path}: {remote}")
        classify_remote_state(item, remote, push_target, ops)
        if sync_local_branches:
            sync_local_branch_ref(
                item, push_target, ops,
                allow_suffix_branch=allow_suffix_branch,
                dry_run=check_only,
            )
        run_push_check(item, remote, push_target, ops)
        if not check_only and not item.skipped:
            push_item(item, remote, push_target, ops, plan_items=result.items)
        print_publish_item(item)
        result.items.append(item)
        result.warnings.extend(item.messages)
    return result


def run_publish_execute(
    check_result: PublishResult,
    *,
    remote: str,
    push_target: str,
    sync_local_branches: bool,
    allow_suffix_branch: bool,
    selected_display_paths: set[str] | None = None,
    ops: ManualRepositoryOps,
) -> PublishResult:
    """Execute the actual push for items that passed the check phase.

    If *selected_display_paths* is provided, only items whose
    ``display_path`` is in the set will be pushed; all others are skipped.
    """
    result = PublishResult(check_only=False)
    result.items = list(check_result.items)
    result.warnings = list(check_result.warnings)

    ensure_required_descendants_selected(result.items, selected_display_paths, ops)

    if selected_display_paths is not None:
        for item in result.items:
            if item.display_path not in selected_display_paths:
                item.skipped = True
                item.messages.append("Excluded by user selection.")

    for item in result.items:
        if item.skipped:
            print_publish_item(item)
            continue

        if item.pushed:
            print_publish_item(item)
            continue

        if sync_local_branches:
            sync_local_branch_ref(
                item,
                push_target,
                ops,
                allow_suffix_branch=allow_suffix_branch,
                dry_run=False,
            )

        push_item(item, remote, push_target, ops, plan_items=result.items)
        print_publish_item(item)
        result.warnings.extend(item.messages)

    return result


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
    check_result = run_publish_check(
        repo_path,
        remote=remote,
        push_target=push_target,
        sync_local_branches=sync_local_branches,
        allow_suffix_branch=allow_suffix_branch,
        ops=ops,
    )
    if check_only:
        return check_result

    return run_publish_execute(
        check_result,
        remote=remote,
        push_target=push_target,
        sync_local_branches=sync_local_branches,
        allow_suffix_branch=allow_suffix_branch,
        ops=ops,
    )


def run_publish(
    args: Any,
    *,
    manual_module: ModuleType | None = None,
    ops: ManualRepositoryOps | None = None,
) -> int:
    execution.set_verbose(bool(getattr(args, "verbose", False)))
    resolved_ops = ops or ManualRepositoryOps(manual_module)
    repo_path = Path(args.repo).resolve()
    remote = str(args.remote or "origin")
    push_target = str(args.push_target or "").strip()
    check_only = bool(getattr(args, "check_only", False))
    no_interactive = bool(getattr(args, "no_interactive", False))
    sync_local = not bool(getattr(args, "no_sync_local_branches", False))
    allow_suffix = not bool(getattr(args, "no_branch_suffix", False))

    try:
        # Phase 1: use the same recursive check path as the GUI. This
        # initializes every nested submodule before building the gitlink plan,
        # so deep repositories cannot disappear merely because their worktree
        # was not initialized before publish started.
        check_result = run_publish_check(
            repo_path,
            remote=remote,
            push_target=push_target,
            sync_local_branches=sync_local,
            allow_suffix_branch=allow_suffix,
            ops=resolved_ops,
        )
        if check_only:
            result = check_result
        else:
            selected_display_paths: set[str] | None = None
            pushable_items = [item for item in check_result.items if not item.skipped]

            # Phase 2: optional interactive selection over the checked plan.
            if not no_interactive and _sys.stdin.isatty():
                candidates = [
                    PublishCandidate(
                        repo_path=item.repo_path,
                        display_path=item.display_path,
                        commit_sha=item.desired_sha,
                        remote_before_sha=item.remote_before_sha,
                        remote_state=item.remote_state,
                        depth=item.depth,
                        reachable_on_remote=item.reachable_on_remote,
                    )
                    for item in pushable_items
                ]
                if not candidates:
                    print("No repositories need pushing.")
                    return 0
                confirmed = confirm_publish_plan_interactive(candidates)
                if confirmed is None:
                    print("Publish cancelled by user.")
                    return 0
                selected_display_paths = {
                    candidate.display_path for candidate in confirmed if candidate.selected
                }
                if not selected_display_paths:
                    print("No repositories selected for push.")
                    return 0

            # Phase 3: execute exactly the plan that passed recursive check.
            result = run_publish_execute(
                check_result,
                remote=remote,
                push_target=push_target,
                sync_local_branches=sync_local,
                allow_suffix_branch=allow_suffix,
                selected_display_paths=selected_display_paths,
                ops=resolved_ops,
            )
    except resolved_ops._automation_error_class() as exc:
        print(str(exc))
        return 1
    action = "Publish check completed successfully." if check_only else "Publish completed successfully."
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

    def run_publish_check(
        self,
        repo_path: Path,
        *,
        remote: str,
        push_target: str,
        sync_local_branches: bool = True,
        allow_suffix_branch: bool = True,
    ) -> PublishResult:
        return run_publish_check(
            repo_path,
            remote=remote,
            push_target=push_target,
            sync_local_branches=sync_local_branches,
            allow_suffix_branch=allow_suffix_branch,
            ops=ManualRepositoryOps(self.manual_module),
        )

    def run_publish_execute(
        self,
        check_result: PublishResult,
        *,
        remote: str,
        push_target: str,
        sync_local_branches: bool = True,
        allow_suffix_branch: bool = True,
        selected_display_paths: set[str] | None = None,
    ) -> PublishResult:
        return run_publish_execute(
            check_result,
            remote=remote,
            push_target=push_target,
            sync_local_branches=sync_local_branches,
            allow_suffix_branch=allow_suffix_branch,
            selected_display_paths=selected_display_paths,
            ops=ManualRepositoryOps(self.manual_module),
        )

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
