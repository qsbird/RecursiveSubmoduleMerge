"""CI publish and publish-only workflow boundary."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from . import common
from .models import CiMergeError, ExitCode, Phase, now_iso


PUBLISH_FUNCTIONS: tuple[str, ...] = ()


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


def publish_log(ctx: Any, message: str, details: dict[str, Any] | None = None) -> None:
    ctx.report.append_log(f"\n[{now_iso()}] PUBLISH {message}\n")
    if details:
        ctx.report.append_log(json.dumps(details, ensure_ascii=False, indent=2) + "\n")
    ctx.report.console(f"PUBLISH {message}")


def target_requires_explicit_publish_target(target: str, remote: str) -> bool:
    normalized = target.strip()
    if normalized in {"HEAD", "FETCH_HEAD", "MERGE_HEAD", "ORIG_HEAD", "@"}:
        return True
    if re.fullmatch(r"[0-9A-Fa-f]{7,40}", normalized):
        return True
    if normalized.startswith(f"{remote}/") or normalized.startswith("refs/remotes/"):
        return True
    if normalized.startswith("refs/tags/"):
        return True
    if normalized.startswith("refs/") and not normalized.startswith("refs/heads/"):
        return True
    return any(token in normalized for token in ("^", "~", ":"))


def report_value(data: dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def submodule_candidate_ref(ctx: Any, display_path: str) -> str:
    job = common.short_ref_token(ctx.job_id)
    path = common.short_ref_token(display_path.replace("\\", "/").strip("/"))
    return f"refs/heads/ci-merge-submodules/{job}/{path}"


def remote_branch_ref(target: str) -> str:
    return target if target.startswith("refs/heads/") else f"refs/heads/{target}"


def submodule_publish_candidates(ctx: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for repository in ctx.report.data.get("repositories") or []:
        display_path = str(repository.get("path") or "").strip()
        repo_path = str(repository.get("repo_path") or "").strip()
        result_sha = str(repository.get("result") or "").strip()
        if not display_path or display_path == "." or not repo_path or not result_sha:
            continue
        key = (str(common.resolve_path(repo_path)), result_sha)
        if key in seen:
            continue
        seen.add(key)
        normalized_path = display_path.replace("\\", "/").strip("/")
        depth = len([part for part in normalized_path.split("/") if part])
        candidates.append(
            {
                "path": display_path,
                "repo_path": repo_path,
                "result_sha": result_sha,
                "candidate_ref": submodule_candidate_ref(ctx, display_path),
                "strategy": repository.get("strategy"),
                "depth": depth,
            }
        )
    return sorted(candidates, key=lambda item: int(item.get("depth") or 0), reverse=True)


def publish_run(
    ctx: Any,
    repo_path: Path,
    args: list[str],
    fallback_reason: str,
    ci_module: ModuleType | None = None,
) -> Any:
    result = ctx.runner.run(repo_path, args, allow_failure=True)
    if result.exit_code != 0:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            classify_publish_command_failure(result) if (result.stderr or getattr(result, "network_failure_reason", None)) else fallback_reason,
            "publishing command failed",
            details={"command": result.to_dict()},
            restore_needed=False,
        )
    return result


def publish_remote_ref_sha(
    ctx: Any,
    repo_path: Path,
    remote: str,
    ref: str,
    fallback_reason: str,
    ci_module: ModuleType | None = None,
) -> str:
    result = publish_run(ctx, repo_path, ["ls-remote", remote, ref], fallback_reason, ci_module)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == ref:
            return parts[0]
    _raise_ci_error(
        ci_module,
        "PUBLISH_FAILED",
        "PUBLISHING",
        "PUBLISH_REMOTE_REF_NOT_FOUND",
        "published remote ref was not found during verification",
        details={"remote": remote, "ref": ref, "command": result.to_dict()},
        restore_needed=False,
    )
    raise AssertionError("unreachable")


def remote_ref_missing(stderr: str) -> bool:
    text = stderr.lower()
    return "couldn't find remote ref" in text or "could not find remote ref" in text


def classify_publish_failure(stderr: str, network_failure_reason: str | None = None) -> str:
    if network_failure_reason:
        return "PUBLISH_NETWORK_FAILED"
    text = stderr.lower()
    if "stale info" in text or "fetch first" in text or "non-fast-forward" in text:
        return "PUBLISH_RACE"
    if "permission" in text or "denied" in text or "forbidden" in text:
        return "PUBLISH_PERMISSION_DENIED"
    if "authentication" in text or "auth" in text:
        return "PUBLISH_AUTH_FAILED"
    if "hook declined" in text or "pre-receive hook declined" in text:
        return "PUBLISH_HOOK_REJECTED"
    if "could not resolve host" in text or "failed to connect" in text or "network" in text:
        return "PUBLISH_NETWORK_FAILED"
    return "PUBLISH_FAILED"


def classify_publish_command_failure(result: Any) -> str:
    return classify_publish_failure(
        getattr(result, "stderr", "") or "",
        getattr(result, "network_failure_reason", None),
    )


def publish_submodule_candidates(ctx: Any, ci_module: ModuleType | None = None) -> list[dict[str, Any]]:
    publish = ctx.report.data["publish"]
    submodules = publish.setdefault(
        "submodules",
        {"attempted": False, "success": None, "count": 0, "items": []},
    )
    candidates = submodule_publish_candidates(ctx)
    submodules["attempted"] = bool(candidates)
    submodules["count"] = len(candidates)
    submodules["items"] = []
    publish_log(ctx, "submodule_publish_start", {"count": len(candidates)})

    for candidate in candidates:
        repo_path = common.resolve_path(candidate["repo_path"])
        result_sha = str(candidate["result_sha"])
        candidate_ref = str(candidate["candidate_ref"])
        item = {
            "path": candidate["path"],
            "repo_path": str(repo_path),
            "remote": ctx.args.remote,
            "candidate_ref": candidate_ref,
            "result_sha": result_sha,
            "strategy": candidate.get("strategy"),
            "success": False,
            "failure_reason": None,
            "remote_ref_after_sha": None,
        }
        submodules["items"].append(item)
        publish_log(
            ctx,
            "submodule_publish_item_start",
            {"path": item["path"], "candidate_ref": candidate_ref, "result_sha": result_sha},
        )

        local = ctx.runner.run(repo_path, ["cat-file", "-e", f"{result_sha}^{{commit}}"], allow_failure=True)
        if local.exit_code != 0:
            item["failure_reason"] = "PUBLISH_SUBMODULE_RESULT_NOT_LOCAL"
            publish_log(ctx, "submodule_publish_item_failed", item)
            _raise_ci_error(
                ci_module,
                "PUBLISH_FAILED",
                "PUBLISHING",
                "PUBLISH_SUBMODULE_RESULT_NOT_LOCAL",
                "submodule result commit is not available locally",
                details={"submodule": item, "command": local.to_dict()},
                restore_needed=False,
            )

        push = ctx.runner.run(
            repo_path,
            [
                "push",
                "--recurse-submodules=check",
                ctx.args.remote,
                f"{result_sha}:{candidate_ref}",
            ],
            allow_failure=True,
        )
        if push.exit_code != 0:
            item["failure_reason"] = classify_publish_command_failure(push)
            publish_log(ctx, "submodule_publish_item_failed", item)
            _raise_ci_error(
                ci_module,
                "PUBLISH_FAILED",
                "PUBLISHING",
                item["failure_reason"],
                "publishing submodule candidate result failed",
                details={"submodule": item, "command": push.to_dict()},
                restore_needed=False,
            )

        remote_after = publish_remote_ref_sha(
            ctx,
            repo_path,
            ctx.args.remote,
            candidate_ref,
            "PUBLISH_SUBMODULE_VERIFY_LS_REMOTE_FAILED",
            ci_module,
        )
        item["remote_ref_after_sha"] = remote_after
        if remote_after != result_sha:
            item["failure_reason"] = "PUBLISH_SUBMODULE_VERIFY_FAILED"
            publish_log(ctx, "submodule_publish_item_failed", item)
            _raise_ci_error(
                ci_module,
                "PUBLISH_FAILED",
                "PUBLISHING",
                "PUBLISH_SUBMODULE_VERIFY_FAILED",
                "submodule candidate ref SHA after publish does not match result",
                details={"submodule": item, "expected_result_sha": result_sha, "remote_ref_after_sha": remote_after},
                restore_needed=False,
            )

        item["success"] = True
        publish_log(ctx, "submodule_publish_item_success", item)

    submodules["success"] = all(item.get("success") for item in submodules["items"])
    publish_log(ctx, "submodule_publish_done", {"success": submodules["success"], "count": len(candidates)})
    return submodules["items"]


def publish_result(ctx: Any, repo_path: Path, target_sha: str, result_sha: str, ci_module: ModuleType | None = None) -> dict[str, Any]:
    ctx.set_state(_phase(ci_module, "PUBLISHING"))
    publish_target = ctx.args.publish_target
    if not publish_target:
        _raise_ci_error(
            ci_module,
            "ARGUMENT_ERROR",
            "INIT",
            "PUBLISH_TARGET_REQUIRED",
            "--publish_target must be set for publishing",
            details={"target": getattr(ctx.args, "target", None), "remote": ctx.args.remote},
            restore_needed=False,
        )
    allow_create_target = bool(getattr(ctx.args, "allow_create_publish_target", False))
    publish = ctx.report.data["publish"]
    publish["enabled"] = True
    publish["attempted"] = True
    publish["success"] = False
    publish["remote"] = ctx.args.remote
    publish["target"] = publish_target
    publish["mode"] = ctx.args.publish_mode
    publish["candidate_ref"] = ctx.args.candidate_ref
    publish["allow_create_target"] = allow_create_target
    publish["target_created"] = False
    publish["target_publish_mode"] = None
    publish["expected_result_sha"] = result_sha
    ctx.report.data["publish_attempted"] = True
    publish_log(ctx, "start", {"target": publish_target, "result_sha": result_sha})

    fetch_target = ctx.runner.run(repo_path, ["fetch", ctx.args.remote, publish_target], allow_failure=True)
    target_exists = fetch_target.exit_code == 0
    remote_before: str | None = None
    if target_exists:
        before = publish_run(ctx, repo_path, ["rev-parse", "FETCH_HEAD"], "PUBLISH_RESOLVE_REMOTE_FAILED", ci_module)
        remote_before = before.stdout.strip()
    elif allow_create_target and remote_ref_missing(fetch_target.stderr):
        publish_log(ctx, "target_missing_create_allowed", {"target": publish_target, "remote": ctx.args.remote})
    elif remote_ref_missing(fetch_target.stderr):
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            "PUBLISH_TARGET_NOT_FOUND",
            "remote publish target does not exist; pass --allow_create_publish_target true to create it",
            details={"target": publish_target, "remote": ctx.args.remote, "command": fetch_target.to_dict()},
            restore_needed=False,
        )
    else:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            classify_publish_command_failure(fetch_target) if (fetch_target.stderr or getattr(fetch_target, "network_failure_reason", None)) else "PUBLISH_FETCH_FAILED",
            "publishing command failed",
            details={"command": fetch_target.to_dict()},
            restore_needed=False,
        )

    publish["remote_target_before_sha"] = remote_before
    publish["target_publish_mode"] = "force-with-lease" if target_exists else "create"
    publish["target_created"] = not target_exists
    if target_exists and remote_before != target_sha:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            "PUBLISH_RACE",
            "remote target branch advanced after merge candidate was created",
            details={"remote_target_before_sha": remote_before, "expected_target_sha": target_sha},
            restore_needed=False,
        )

    local_result = ctx.runner.run(repo_path, ["cat-file", "-e", f"{result_sha}^{{commit}}"], allow_failure=True)
    if local_result.exit_code != 0:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            "PUBLISH_RESULT_NOT_LOCAL",
            "candidate result commit is not available locally",
            details={"result_sha": result_sha, "command": local_result.to_dict()},
            restore_needed=False,
        )

    contains = ctx.runner.run(repo_path, ["merge-base", "--is-ancestor", target_sha, result_sha], allow_failure=True)
    if contains.exit_code != 0:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            "PUBLISH_CANDIDATE_NOT_BASED_ON_TARGET",
            "candidate result does not contain target history",
            details={"target_sha": target_sha, "result_sha": result_sha, "command": contains.to_dict()},
            restore_needed=False,
        )

    publish_submodule_candidates(ctx, ci_module)

    if ctx.args.candidate_ref:
        publish_run(
            ctx,
            repo_path,
            ["push", "--recurse-submodules=check", ctx.args.remote, f"{result_sha}:{ctx.args.candidate_ref}"],
            "PUBLISH_CANDIDATE_REF_FAILED",
            ci_module,
        )
        publish["root"]["candidate_ref_pushed"] = True
        publish_log(ctx, "root_candidate_ref_pushed", {"candidate_ref": ctx.args.candidate_ref, "result_sha": result_sha})

    push_args = ["push", "--recurse-submodules=check"]
    if target_exists:
        push_args.append(f"--force-with-lease={publish_target}:{remote_before}")
    push_target = publish_target if target_exists else remote_branch_ref(publish_target)
    push_args.extend([ctx.args.remote, f"{result_sha}:{push_target}"])
    push = ctx.runner.run(repo_path, push_args, allow_failure=True)
    if push.exit_code != 0:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            classify_publish_command_failure(push),
            "publishing candidate result failed",
            details={"command": push.to_dict(), "remote_target_before_sha": remote_before},
            restore_needed=False,
        )
    publish["root"]["target_pushed"] = True
    publish_log(
        ctx,
        "root_target_created" if not target_exists else "root_target_pushed",
        {"target": publish_target, "expected_before": remote_before, "result_sha": result_sha},
    )

    publish_run(ctx, repo_path, ["fetch", ctx.args.remote, publish_target], "PUBLISH_VERIFY_FETCH_FAILED", ci_module)
    after = publish_run(ctx, repo_path, ["rev-parse", "FETCH_HEAD"], "PUBLISH_VERIFY_RESOLVE_FAILED", ci_module)
    remote_after = after.stdout.strip()
    publish["remote_target_after_sha"] = remote_after
    if remote_after != result_sha:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            "PUBLISH_VERIFY_FAILED",
            "remote target SHA after publish does not match candidate result",
            details={"remote_target_after_sha": remote_after, "expected_result_sha": result_sha},
            restore_needed=False,
        )
    publish["root"]["success"] = True
    publish["success"] = True
    publish_log(ctx, "success", {"target": publish_target, "remote_after": remote_after, "result_sha": result_sha})
    return publish


def infer_publish_target_from_report(
    ctx: Any,
    source_report: dict[str, Any],
    ci_module: ModuleType | None = None,
) -> str:
    if ctx.args.publish_target:
        target = str(ctx.args.publish_target).strip()
        if target and not target_requires_explicit_publish_target(target, ctx.args.remote):
            return target
        _raise_ci_error(
            ci_module,
            "ARGUMENT_ERROR",
            "INIT",
            "PUBLISH_TARGET_REQUIRED",
            "--publish_target must name a remote branch to publish",
            details={"publish_target": ctx.args.publish_target, "remote": ctx.args.remote},
            restore_needed=False,
        )

    _raise_ci_error(
        ci_module,
        "ARGUMENT_ERROR",
        "INIT",
        "PUBLISH_TARGET_REQUIRED",
        "--publish_target is required to publish this report",
        details={
            "source_target": report_value(source_report, "input", "target"),
            "resolved_target_ref": report_value(source_report, "resolved", "target_ref"),
            "remote": ctx.args.remote,
        },
        restore_needed=False,
    )
    raise AssertionError("unreachable")


def load_publish_only_report(
    ctx: Any,
    ci_module: ModuleType | None = None,
) -> tuple[dict[str, Any], str, str]:
    source_report_path = common.resolve_path(ctx.args.from_report)
    source_report = common.load_json_file(source_report_path)
    source_status = str(source_report.get("status") or "")
    allow_partial = bool(ctx.args.allow_partial_publish)
    partial_publish = source_status == "partial_success"
    ctx.report.data.setdefault("publish_only", {}).update(
        {
            "enabled": True,
            "from_report": str(source_report_path),
            "allow_partial_publish": allow_partial,
            "partial_publish": partial_publish,
            "source_report_path": str(source_report_path),
            "source_report_status": source_status,
        }
    )
    if source_status == "partial_success" and not allow_partial:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            "PUBLISH_ONLY_PARTIAL_NOT_ALLOWED",
            "partial_success merge reports require --allow_partial_publish true",
            details={"from_report": str(source_report_path), "source_status": source_status},
            restore_needed=False,
        )
    if source_status not in {"success", "partial_success"}:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            "PUBLISH_ONLY_SOURCE_NOT_SUCCESS",
            "only a successful merge report can be published",
            details={"from_report": str(source_report_path), "source_status": source_status},
            restore_needed=False,
        )
    if partial_publish:
        batch = source_report.get("batch") or {}
        if batch.get("status") != "partial_success" or not batch.get("failed_steps"):
            _raise_ci_error(
                ci_module,
                "PUBLISH_FAILED",
                "PUBLISHING",
                "PUBLISH_ONLY_PARTIAL_REPORT_INVALID",
                "partial publish requires a partial_success batch report with failed step metadata",
                details={"from_report": str(source_report_path), "batch": batch},
                restore_needed=False,
            )

    source_remote = report_value(source_report, "input", "remote") or report_value(source_report, "publish", "remote")
    if source_remote and ctx.args.remote == "origin":
        ctx.args.remote = str(source_remote)

    source_target = report_value(source_report, "input", "target") or report_value(source_report, "resolved", "target_ref")
    if source_target and not ctx.args.target:
        ctx.args.target = str(source_target)

    result_sha = str(
        report_value(source_report, "resolved", "result_sha")
        or report_value(source_report, "batch", "final_result_sha")
        or ""
    ).strip()
    target_sha = str(
        report_value(source_report, "resolved", "target_sha")
        or report_value(source_report, "batch", "original_target_sha")
        or ""
    ).strip()
    if not result_sha or not target_sha:
        _raise_ci_error(
            ci_module,
            "PUBLISH_FAILED",
            "PUBLISHING",
            "PUBLISH_ONLY_REPORT_INCOMPLETE",
            "merge report does not contain target/result SHA required for publishing",
            details={"from_report": str(source_report_path), "target_sha": target_sha, "result_sha": result_sha},
            restore_needed=False,
        )

    ctx.args.publish_target = infer_publish_target_from_report(ctx, source_report, ci_module)
    ctx.report.data.setdefault("input", {})["target"] = ctx.args.target
    ctx.report.data.setdefault("input", {})["remote"] = ctx.args.remote
    ctx.report.data.setdefault("publish", {})["remote"] = ctx.args.remote
    ctx.report.data.setdefault("publish", {})["target"] = ctx.args.publish_target
    ctx.report.data.setdefault("publish", {})["candidate_ref"] = ctx.args.candidate_ref
    ctx.report.data["resolved"] = dict(source_report.get("resolved") or {})
    ctx.report.data["resolved"]["target_sha"] = target_sha
    ctx.report.data["resolved"]["result_sha"] = result_sha
    ctx.report.data["batch"] = dict(source_report.get("batch") or ctx.report.data.get("batch") or {})
    ctx.report.data["repositories"] = list(source_report.get("repositories") or [])
    ctx.report.data["conflicts"] = list(source_report.get("conflicts") or [])
    ctx.report.data["summary"] = dict(source_report.get("summary") or ctx.report.data.get("summary") or {})
    return source_report, target_sha, result_sha


def run_publish_only_workflow(ctx: Any, ci_module: ModuleType | None = None) -> None:
    source_report, target_sha, result_sha = load_publish_only_report(ctx, ci_module)
    publish_log(
        ctx,
        "publish_only_loaded_report",
        {
            "from_report": ctx.report.data["publish_only"]["source_report_path"],
            "source_status": source_report.get("status"),
            "allow_partial_publish": bool(ctx.args.allow_partial_publish),
            "target_sha": target_sha,
            "result_sha": result_sha,
            "publish_target": ctx.args.publish_target,
        },
    )
    publish_result(ctx, ctx.root_repo, target_sha, result_sha, ci_module)
    ctx.report.set_success()


@dataclass(frozen=True)
class CiPublishService:
    """Facade for CI publish behavior."""

    ci_module: ModuleType | None

    def __post_init__(self) -> None:
        return None

    def publish_result(self, ctx: Any, repo_path: Path, target_sha: str, result_sha: str) -> dict[str, Any]:
        return publish_result(ctx, repo_path, target_sha, result_sha, self.ci_module)

    def publish_run(self, ctx: Any, repo_path: Path, args: list[str], fallback_reason: str) -> Any:
        return publish_run(ctx, repo_path, args, fallback_reason, self.ci_module)

    def publish_remote_ref_sha(self, ctx: Any, repo_path: Path, remote: str, ref: str, fallback_reason: str) -> str:
        return publish_remote_ref_sha(ctx, repo_path, remote, ref, fallback_reason, self.ci_module)

    def classify_publish_failure(self, stderr: str) -> str:
        return classify_publish_failure(stderr)

    def submodule_publish_candidates(self, ctx: Any) -> list[dict[str, Any]]:
        return submodule_publish_candidates(ctx)

    def publish_submodule_candidates(self, ctx: Any) -> list[dict[str, Any]]:
        return publish_submodule_candidates(ctx, self.ci_module)

    def run_publish_only_workflow(self, ctx: Any) -> None:
        run_publish_only_workflow(ctx, self.ci_module)
