"""CI remote-sync and preflight implementations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import common
from .models import CiMergeError, ExitCode, Phase, PreflightResult, now_iso
from . import repository_ops


SUBMODULE_FETCH_SCOPE_ALL = "all"
SUBMODULE_FETCH_SCOPE_NEEDED = "needed"
SUBMODULE_FETCH_SCOPE_SKIPPED = "skipped"


@dataclass(frozen=True)
class MergeRefTriple:
    source: str
    source_sha: str
    target_sha: str
    merge_base_sha: str


@dataclass(frozen=True)
class GitlinkChange:
    path: str
    old_sha: str
    new_sha: str


def _ci_error_class(ci_module: ModuleType | None) -> type[Exception]:
    return getattr(ci_module, "CiMergeError", CiMergeError) if ci_module is not None else CiMergeError


def _exit_code(ci_module: ModuleType | None, name: str) -> int:
    exit_codes = getattr(ci_module, "ExitCode", ExitCode) if ci_module is not None else ExitCode
    return getattr(exit_codes, name)


def _phase(ci_module: ModuleType | None, name: str) -> str:
    phases = getattr(ci_module, "Phase", Phase) if ci_module is not None else Phase
    return getattr(phases, name)


def _raise_ci_error(
    ci_module: ModuleType | None,
    exit_code_name: str,
    phase_name: str,
    reason: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    restore_needed: bool = False,
) -> None:
    error_cls = _ci_error_class(ci_module)
    raise error_cls(
        _exit_code(ci_module, exit_code_name),
        _phase(ci_module, phase_name),
        reason,
        message,
        details=details,
        restore_needed=restore_needed,
    )


def _preflight_result_class(ci_module: ModuleType | None) -> type[Any]:
    return getattr(ci_module, "PreflightResult", PreflightResult) if ci_module is not None else PreflightResult


def add_check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    reason: str | None = None,
    **details: Any,
) -> None:
    entry: dict[str, Any] = {"name": name, "passed": passed}
    if reason:
        entry["reason"] = reason
    entry.update({key: value for key, value in details.items() if value is not None})
    checks.append(entry)


def run_sync_command(
    ctx: Any,
    repo_path: Path,
    args: list[str],
    reason: str,
    message: str,
    ci_module: ModuleType | None = None,
    *,
    details: dict[str, Any] | None = None,
) -> Any:
    result = ctx.runner.run(repo_path, args, allow_failure=True)
    if result.exit_code != 0:
        error_details = {"repo_path": str(repo_path), "command": result.to_dict()}
        if details:
            error_details.update(details)
        _raise_ci_error(
            ci_module,
            "GIT_COMMAND_FAILED",
            "SYNCING_REFS",
            reason,
            message,
            details=error_details,
            restore_needed=False,
        )
    return result


def parse_submodule_status_paths(stdout: str) -> list[str]:
    paths: list[str] = []
    for line in common.split_lines(stdout):
        body = line[1:].strip() if line[:1] in {" ", "-", "+", "U"} else line.strip()
        parts = body.split(None, 1)
        if len(parts) >= 2:
            path = parts[1].split(" (", 1)[0].strip()
            if path:
                paths.append(path)
    return list(dict.fromkeys(paths))


def parse_raw_gitlink_changes(stdout: str) -> tuple[list[GitlinkChange], list[str]]:
    changes: list[GitlinkChange] = []
    fallback_reasons: list[str] = []
    seen: set[str] = set()
    for line in common.split_lines(stdout):
        if not line.startswith(":") or "\t" not in line:
            fallback_reasons.append("RAW_DIFF_UNPARSEABLE")
            continue
        meta, path_text = line[1:].split("\t", 1)
        fields = meta.split()
        if len(fields) < 5:
            fallback_reasons.append("RAW_DIFF_UNPARSEABLE")
            continue
        old_mode, new_mode, old_sha, new_sha, status = fields[:5]
        paths = [part.strip() for part in path_text.split("\t") if part.strip()]
        status_letter = status[:1]
        if ".gitmodules" in paths:
            fallback_reasons.append("GITMODULES_CHANGED")
            continue
        if "160000" not in {old_mode, new_mode}:
            continue
        if old_mode != "160000" or new_mode != "160000" or status_letter in {"A", "D", "R", "C", "T"}:
            fallback_reasons.append("SUBMODULE_TOPOLOGY_CHANGED")
            continue
        path = paths[-1] if paths else ""
        if not path:
            fallback_reasons.append("RAW_DIFF_UNPARSEABLE")
            continue
        if path in seen:
            continue
        seen.add(path)
        changes.append(GitlinkChange(path=path, old_sha=old_sha, new_sha=new_sha))
    return changes, list(dict.fromkeys(fallback_reasons))


def gitlink_sha_at_commit(ctx: Any, repo_path: Path, commitish: str, path: str) -> str | None:
    result = ctx.runner.run(repo_path, ["ls-tree", commitish, "--", path], allow_failure=True)
    if result.exit_code != 0 or not result.stdout.strip():
        return None
    line = result.stdout.splitlines()[0]
    meta, _separator, _path = line.partition("\t")
    parts = meta.split()
    if len(parts) >= 3 and parts[0] == "160000" and parts[1] == "commit":
        return parts[2]
    return None


def is_git_worktree(ctx: Any, repo_path: Path) -> bool:
    result = ctx.runner.run(repo_path, ["rev-parse", "--is-inside-work-tree"], allow_failure=True)
    return result.exit_code == 0 and result.stdout.strip() == "true"


def append_unique(values: list[str], *items: str) -> None:
    seen = set(values)
    for item in items:
        if item and item not in seen:
            values.append(item)
            seen.add(item)


def display_submodule_path(parent_display: str, path: str) -> str:
    normalized_parent = parent_display.strip().replace("\\", "/")
    normalized_path = path.strip().replace("\\", "/")
    if not normalized_parent or normalized_parent == ".":
        return normalized_path
    return f"{normalized_parent.rstrip('/')}/{normalized_path}"


def resolve_merge_ref_triples(ctx: Any, repo_path: Path, ci_module: ModuleType | None = None) -> tuple[list[MergeRefTriple], list[dict[str, Any]]]:
    skipped: list[dict[str, Any]] = []
    target = getattr(ctx.args, "target", None)
    sources = list(getattr(ctx.args, "sources", []) or ([getattr(ctx.args, "source", None)] if getattr(ctx.args, "source", None) else []))
    if not target or not sources:
        skipped.append({"reason": "SOURCE_OR_TARGET_MISSING", "target": target, "sources": sources})
        return [], skipped

    target_resolution = repository_ops.resolve_commit_ref(ctx, repo_path, target, ci_module)
    if target_resolution.command.exit_code != 0 or not target_resolution.sha:
        skipped.append(
            {
                "reason": "TARGET_REF_UNRESOLVED",
                "target": target,
                "command": target_resolution.command.to_dict(),
            }
        )
        return [], skipped

    triples: list[MergeRefTriple] = []
    for source in sources:
        source_resolution = repository_ops.resolve_commit_ref(ctx, repo_path, source, ci_module)
        if source_resolution.command.exit_code != 0 or not source_resolution.sha:
            skipped.append(
                {
                    "reason": "SOURCE_REF_UNRESOLVED",
                    "source": source,
                    "command": source_resolution.command.to_dict(),
                }
            )
            continue
        merge_base = ctx.runner.run(repo_path, ["merge-base", source_resolution.sha, target_resolution.sha], allow_failure=True)
        if merge_base.exit_code != 0 or not merge_base.stdout.strip():
            skipped.append(
                {
                    "reason": "MERGE_BASE_UNRESOLVED",
                    "source": source,
                    "target": target,
                    "command": merge_base.to_dict(),
                }
            )
            continue
        triples.append(
            MergeRefTriple(
                source=str(source),
                source_sha=str(source_resolution.sha),
                target_sha=str(target_resolution.sha),
                merge_base_sha=merge_base.stdout.strip(),
            )
        )
    return triples, skipped


def collect_changed_gitlink_paths(
    ctx: Any,
    repo_path: Path,
    triple: MergeRefTriple,
) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    fallback_reasons: list[str] = []
    for left, right in (
        (triple.merge_base_sha, triple.source_sha),
        (triple.merge_base_sha, triple.target_sha),
    ):
        diff = ctx.runner.run(repo_path, ["diff", "--raw", "--no-abbrev", left, right, "--"], allow_failure=True)
        if diff.exit_code != 0:
            fallback_reasons.append("RAW_DIFF_FAILED")
            continue
        changes, reasons = parse_raw_gitlink_changes(diff.stdout)
        append_unique(paths, *(change.path for change in changes))
        append_unique(fallback_reasons, *reasons)
    return paths, fallback_reasons


def fetch_submodule_repo(
    ctx: Any,
    repo_path: Path,
    display_path: str,
    depth: int,
    sync_report: dict[str, Any],
    ci_module: ModuleType | None = None,
) -> Any:
    item = {
        "path": display_path,
        "repo_path": str(repo_path),
        "depth": depth,
        "success": False,
        "command": None,
    }
    sync_report.setdefault("submodule_fetches", []).append(item)
    remote = str(getattr(ctx.args, "remote", "origin") or "origin")
    fetch_cmd = ["fetch", "--no-recurse-submodules", remote, "--prune"]
    result = ctx.runner.run(repo_path, fetch_cmd, allow_failure=True)
    item["command"] = result.to_dict()

    if result.exit_code != 0 and _stderr_contains_ref_lock_error(result.stderr):
        _fix_packed_refs_and_retry(ctx, repo_path, sync_report)
        result = ctx.runner.run(repo_path, fetch_cmd, allow_failure=True)
        item["command"] = result.to_dict()
        item["ref_lock_recovery_attempted"] = True

    if result.exit_code != 0:
        item["failure_reason"] = "SUBMODULE_FETCH_FAILED"
        _raise_ci_error(
            ci_module,
            "GIT_COMMAND_FAILED",
            "SYNCING_REFS",
            "SUBMODULE_FETCH_FAILED",
            "failed to fetch submodule refs before merge",
            details={"repo_path": str(repo_path), "path": display_path, "command": result.to_dict()},
            restore_needed=False,
        )
    item["success"] = True
    return result


def _stderr_contains_ref_lock_error(stderr: str) -> bool:
    return "cannot lock ref" in (stderr or "")


def _fix_packed_refs_and_retry(ctx: Any, repo_path: Path, sync_report: dict[str, Any]) -> None:
    pack_refs_result = ctx.runner.run(
        repo_path,
        ["pack-refs", "--all", "--prune"],
        allow_failure=True,
    )
    sync_report.setdefault("submodule_pack_refs_recovery", []).append(
        {
            "repo_path": str(repo_path),
            "success": pack_refs_result.exit_code == 0,
            "command": pack_refs_result.to_dict(),
        }
    )


def ensure_needed_submodule_worktree(
    ctx: Any,
    parent_repo: Path,
    submodule_path: str,
    display_path: str,
    sync_report: dict[str, Any],
    ci_module: ModuleType | None = None,
) -> bool:
    sub_repo = parent_repo / submodule_path
    if is_git_worktree(ctx, sub_repo):
        return True
    update = ctx.runner.run(
        parent_repo,
        ["submodule", "update", "--init", "--recursive", "--", submodule_path],
        allow_failure=True,
    )
    sync_report.setdefault("submodule_updates", []).append(
        {
            "path": display_path,
            "repo_path": str(sub_repo),
            "success": update.exit_code == 0,
            "command": update.to_dict(),
        }
    )
    if update.exit_code != 0:
        _raise_ci_error(
            ci_module,
            "GIT_COMMAND_FAILED",
            "SYNCING_REFS",
            "SUBMODULE_UPDATE_FAILED",
            "failed to initialize required submodule before merge",
            details={"repo_path": str(parent_repo), "path": display_path, "command": update.to_dict()},
            restore_needed=False,
        )
    return is_git_worktree(ctx, sub_repo)


def fetch_all_submodule_refs(ctx: Any, repo_path: Path, sync_report: dict[str, Any], ci_module: ModuleType | None = None) -> None:
    status = ctx.runner.run(repo_path, ["submodule", "status", "--recursive"], allow_failure=True)
    sync_report["submodule_status_command"] = status.to_dict()
    if status.exit_code != 0:
        _raise_ci_error(
            ci_module,
            "GIT_COMMAND_FAILED",
            "SYNCING_REFS",
            "SUBMODULE_STATUS_FAILED",
            "failed to list submodules before recursive fetch",
            details={"repo_path": str(repo_path), "command": status.to_dict()},
            restore_needed=False,
        )
    for path in parse_submodule_status_paths(status.stdout):
        child_repo = repo_path / path
        if not is_git_worktree(ctx, child_repo):
            sync_report.setdefault("submodule_fetches", []).append(
                {
                    "path": path,
                    "repo_path": str(child_repo),
                    "success": None,
                    "skipped": True,
                    "skip_reason": "SUBMODULE_NOT_INITIALIZED",
                }
            )
            continue
        fetch_submodule_repo(ctx, child_repo, path, len([part for part in path.replace("\\", "/").split("/") if part]), sync_report, ci_module)


def fetch_needed_submodule_refs(ctx: Any, repo_path: Path, sync_report: dict[str, Any], ci_module: ModuleType | None = None) -> None:
    triples, skipped = resolve_merge_ref_triples(ctx, repo_path, ci_module)
    sync_report["submodule_fetch_analysis"] = {"skipped": skipped, "triples": len(triples)}
    if not triples:
        sync_report["submodule_fetch_scope_effective"] = SUBMODULE_FETCH_SCOPE_SKIPPED
        sync_report["submodule_fetch_skipped_reason"] = "NO_RESOLVED_MERGE_REFS"
        return

    fallback_reasons: list[str] = []
    stack: list[tuple[Path, str, MergeRefTriple, int]] = [(repo_path, ".", triple, 0) for triple in triples]
    fetched_repos: set[Path] = set()
    visited_triples: set[tuple[str, str, str, str]] = set()
    needed_paths: list[str] = []
    max_depth = int(getattr(ctx, "max_depth", getattr(ctx.args, "max_depth", 16)))

    while stack:
        parent_repo, parent_display, triple, depth = stack.pop()
        visit_key = (str(parent_repo.resolve(strict=False)), triple.merge_base_sha, triple.target_sha, triple.source_sha)
        if visit_key in visited_triples:
            continue
        visited_triples.add(visit_key)
        if depth > max_depth:
            append_unique(fallback_reasons, "SUBMODULE_FETCH_DEPTH_EXCEEDED")
            break

        changed_paths, reasons = collect_changed_gitlink_paths(ctx, parent_repo, triple)
        append_unique(fallback_reasons, *reasons)
        if fallback_reasons:
            break

        for path in changed_paths:
            display_path = display_submodule_path(parent_display, path)
            append_unique(needed_paths, display_path)
            child_base = gitlink_sha_at_commit(ctx, parent_repo, triple.merge_base_sha, path)
            child_target = gitlink_sha_at_commit(ctx, parent_repo, triple.target_sha, path)
            child_source = gitlink_sha_at_commit(ctx, parent_repo, triple.source_sha, path)
            if not child_base or not child_target or not child_source:
                append_unique(fallback_reasons, "SUBMODULE_TOPOLOGY_CHANGED")
                break

            if not ensure_needed_submodule_worktree(ctx, parent_repo, path, display_path, sync_report, ci_module):
                append_unique(fallback_reasons, "SUBMODULE_NOT_INITIALIZED")
                break

            child_repo = parent_repo / path
            resolved_child_repo = child_repo.resolve(strict=False)
            if resolved_child_repo not in fetched_repos:
                fetch_submodule_repo(ctx, child_repo, display_path, depth + 1, sync_report, ci_module)
                fetched_repos.add(resolved_child_repo)

            stack.append(
                (
                    child_repo,
                    display_path,
                    MergeRefTriple(
                        source=triple.source,
                        source_sha=child_source,
                        target_sha=child_target,
                        merge_base_sha=child_base,
                    ),
                    depth + 1,
                )
            )
        if fallback_reasons:
            break

    sync_report["submodule_fetch_needed_paths"] = needed_paths
    if fallback_reasons:
        sync_report["submodule_fetch_scope_effective"] = SUBMODULE_FETCH_SCOPE_ALL
        sync_report["submodule_fetch_fallback_reasons"] = fallback_reasons
        fetch_all_submodule_refs(ctx, repo_path, sync_report, ci_module)
        return
    sync_report["submodule_fetch_scope_effective"] = SUBMODULE_FETCH_SCOPE_NEEDED


def sync_remote_refs(ctx: Any, repo_path: Path, ci_module: ModuleType | None = None) -> None:
    ctx.set_state(_phase(ci_module, "SYNCING_REFS"))
    sync_report = ctx.report.data["remote_sync"]
    sync_report["attempted"] = True
    sync_report.setdefault("submodule_fetches", [])
    sync_report.setdefault("submodule_updates", [])
    sync_report.setdefault("submodule_fetch_fallback_reasons", [])
    sync_report["started_at"] = now_iso()
    scope = str(getattr(ctx.args, "submodule_fetch_scope", SUBMODULE_FETCH_SCOPE_NEEDED) or SUBMODULE_FETCH_SCOPE_NEEDED).strip().lower()
    if scope not in {SUBMODULE_FETCH_SCOPE_ALL, SUBMODULE_FETCH_SCOPE_NEEDED}:
        scope = SUBMODULE_FETCH_SCOPE_NEEDED
    sync_report["submodule_fetch_scope"] = scope
    sync_report["submodule_fetch_scope_effective"] = None
    try:
        run_sync_command(
            ctx,
            repo_path,
            ["fetch", "--no-recurse-submodules", ctx.args.remote, "--prune"],
            "REMOTE_FETCH_FAILED",
            "failed to fetch remote refs before resolving source and target",
            ci_module,
        )
        run_sync_command(
            ctx,
            repo_path,
            ["submodule", "sync", "--recursive"],
            "SUBMODULE_SYNC_FAILED",
            "failed to sync submodule URLs before recursive fetch",
            ci_module,
        )
        if scope == SUBMODULE_FETCH_SCOPE_ALL:
            sync_report["submodule_fetch_scope_effective"] = SUBMODULE_FETCH_SCOPE_ALL
            fetch_all_submodule_refs(ctx, repo_path, sync_report, ci_module)
        else:
            fetch_needed_submodule_refs(ctx, repo_path, sync_report, ci_module)
        sync_report["success"] = True
    except Exception:
        sync_report["success"] = False
        raise
    finally:
        sync_report["ended_at"] = now_iso()


def preflight_check(
    ctx: Any,
    repo_path: Path,
    source: str | None,
    target: str | None,
    *,
    label: str,
    resolve_refs: bool,
    ci_module: ModuleType | None = None,
) -> Any:
    ctx.set_state(_phase(ci_module, "PREFLIGHT"))
    checks: list[dict[str, Any]] = []
    source_ref: str | None = None
    source_sha: str | None = None
    target_ref: str | None = None
    target_sha: str | None = None
    merge_base_sha: str | None = None
    result_cls = _preflight_result_class(ci_module)

    exists = repo_path.exists()
    add_check(checks, "repo_path_exists", exists, None if exists else "repo_path does not exist", path=str(repo_path))
    if not exists:
        result = result_cls(False, checks, [item for item in checks if not item["passed"]])
        record_preflight(ctx, label, result)
        return result

    is_directory = repo_path.is_dir()
    add_check(
        checks,
        "repo_path_is_directory",
        is_directory,
        None if is_directory else "repo_path is not a directory",
        path=str(repo_path),
    )
    if not is_directory:
        result = result_cls(False, checks, [item for item in checks if not item["passed"]])
        record_preflight(ctx, label, result)
        return result

    inside = ctx.runner.run(repo_path, ["rev-parse", "--is-inside-work-tree"], allow_failure=True)
    is_git_repo = inside.exit_code == 0 and inside.stdout.strip() == "true"
    add_check(
        checks,
        "is_git_repository",
        is_git_repo,
        None if is_git_repo else "path is not a git work tree",
    )
    if not is_git_repo:
        result = result_cls(False, checks, [item for item in checks if not item["passed"]])
        record_preflight(ctx, label, result)
        return result

    top = ctx.runner.run(repo_path, ["rev-parse", "--show-toplevel"], allow_failure=True)
    add_check(
        checks,
        "resolve_toplevel",
        top.exit_code == 0 and bool(top.stdout.strip()),
        None if top.exit_code == 0 else "cannot resolve repository top-level path",
        toplevel=top.stdout.strip() or None,
    )

    markers = repository_ops.operation_markers(ctx, repo_path)
    add_check(
        checks,
        "no_operation_in_progress",
        not markers,
        None if not markers else "merge/rebase/cherry-pick/revert operation is in progress",
        markers=markers or None,
    )

    is_clean, status = repository_ops.repository_is_clean(ctx, repo_path)
    add_check(
        checks,
        "working_tree_clean",
        is_clean,
        None if is_clean else "working tree is not clean",
        status=status or None,
    )

    gitmodules = repo_path / ".gitmodules"
    if gitmodules.exists():
        parsed = ctx.runner.run(repo_path, ["config", "-f", ".gitmodules", "--list"], allow_failure=True)
        add_check(
            checks,
            "gitmodules_parseable",
            parsed.exit_code == 0,
            None if parsed.exit_code == 0 else ".gitmodules cannot be parsed",
        )
    else:
        add_check(checks, "gitmodules_parseable", True, gitmodules="absent")

    submodules = ctx.runner.run(repo_path, ["submodule", "status", "--recursive"], allow_failure=True)
    submodule_ok = submodules.exit_code == 0
    bad_submodules: list[str] = []
    if submodule_ok:
        for line in common.split_lines(submodules.stdout):
            if line[0] in {"-", "+", "U"}:
                bad_submodules.append(line)
    add_check(
        checks,
        "submodules_initialized_and_at_head",
        submodule_ok and not bad_submodules,
        None if submodule_ok and not bad_submodules else "submodules are not initialized or not at expected HEAD",
        bad_submodules=bad_submodules or None,
    )

    if resolve_refs and source and target:
        source_resolution = repository_ops.resolve_commit_ref(ctx, repo_path, source, ci_module)
        source_ref = source_resolution.resolved_ref
        source_sha = source_resolution.sha
        add_check(
            checks,
            "resolve_source",
            source_resolution.command.exit_code == 0 and bool(source_sha),
            None if source_resolution.command.exit_code == 0 else "source ref cannot be resolved to commit",
            ref=source,
            resolved_ref=source_ref,
            resolution_strategy=source_resolution.strategy,
            sha=source_sha,
        )

        target_resolution = repository_ops.resolve_commit_ref(ctx, repo_path, target, ci_module)
        target_ref = target_resolution.resolved_ref
        target_sha = target_resolution.sha
        add_check(
            checks,
            "resolve_target",
            target_resolution.command.exit_code == 0 and bool(target_sha),
            None if target_resolution.command.exit_code == 0 else "target ref cannot be resolved to commit",
            ref=target,
            resolved_ref=target_ref,
            resolution_strategy=target_resolution.strategy,
            sha=target_sha,
        )

        if source_sha and target_sha:
            merge_base = ctx.runner.run(repo_path, ["merge-base", source_sha, target_sha], allow_failure=True)
            if merge_base.exit_code == 0:
                merge_base_sha = merge_base.stdout.strip()
            add_check(
                checks,
                "source_target_have_merge_base",
                merge_base.exit_code == 0 and bool(merge_base_sha),
                None if merge_base.exit_code == 0 else "source and target have no merge-base",
                sha=merge_base_sha,
            )

    result = result_cls(
        passed=all(item["passed"] for item in checks),
        checks=checks,
        failed_checks=[item for item in checks if not item["passed"]],
        source_ref=source_ref,
        source_sha=source_sha,
        target_ref=target_ref,
        target_sha=target_sha,
        merge_base_sha=merge_base_sha,
    )
    record_preflight(ctx, label, result)
    return result


def record_preflight(ctx: Any, label: str, result: Any) -> None:
    entry = {
        "label": label,
        "passed": result.passed,
        "checks": result.checks,
        "failed_checks": result.failed_checks,
        "timestamp": now_iso(),
    }
    ctx.report.data["preflight_runs"].append(entry)
    if label == "initial":
        ctx.report.data["preflight"] = {
            "passed": result.passed,
            "checks": result.checks,
            "failed_checks": result.failed_checks,
        }


def preflight_failure_reason(result: Any) -> str:
    if not result.failed_checks:
        return "PREFLIGHT_FAILED"
    first = result.failed_checks[0]["name"]
    mapping = {
        "resolve_source": "SOURCE_REF_NOT_FOUND",
        "resolve_target": "TARGET_REF_NOT_FOUND",
        "source_target_have_merge_base": "NO_MERGE_BASE",
        "working_tree_clean": "WORKTREE_NOT_CLEAN",
        "no_operation_in_progress": "OPERATION_IN_PROGRESS",
        "submodules_initialized_and_at_head": "SUBMODULE_NOT_READY",
        "gitmodules_parseable": "GITMODULES_INVALID",
        "is_git_repository": "NOT_A_GIT_REPOSITORY",
        "repo_path_exists": "REPO_PATH_NOT_FOUND",
    }
    return mapping.get(first, "PREFLIGHT_FAILED")
