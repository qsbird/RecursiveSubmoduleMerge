"""Manual cross-submodule moved-file detection and content merge boundary."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from . import common
from . import execution
from .file_conflicts import get_unmerged_paths as file_get_unmerged_paths
from .file_conflicts import resolve_single_file_conflict
from .models import (
    CROSS_SUBMODULE_MOVE_SIMILARITY_THRESHOLD,
    CrossSubmoduleAddedFile,
    CrossSubmoduleDeletedFile,
    CrossSubmoduleMove,
    SubmoduleConflictInfo,
)


CROSS_SUBMODULE_MOVE_FUNCTIONS: tuple[str, ...] = ()


def describe_cross_submodule_move(move: CrossSubmoduleMove) -> str:
    return (
        f"{move.old_submodule_path}/{move.old_file_path} -> "
        f"{move.new_submodule_path}/{move.new_file_path}"
    )


def get_unmerged_submodule_stage_commit(
    repo_path: Path,
    relative_path: str,
    stage: int,
    *,
    run_git: Any,
) -> str | None:
    result = run_git(repo_path, ["ls-files", "-u", "--", relative_path], check=False)
    if result.returncode != 0 or not result.stdout:
        return None

    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mode, commit_sha, stage_token = parts[0], parts[1], parts[2]
        if mode == "160000" and stage_token == str(stage):
            return commit_sha
    return None


def get_submodule_commit_at_revision(
    repo_path: Path,
    revision: str,
    relative_path: str,
    *,
    run_git: Any,
) -> str | None:
    result = run_git(repo_path, ["ls-tree", revision, "--", relative_path], check=False)
    if result.returncode != 0 or not result.stdout:
        return None

    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        mode, object_type, commit_sha = parts[0], parts[1], parts[2]
        if mode == "160000" and object_type == "commit":
            return commit_sha
    return None


def collect_submodule_conflict_info(
    repo_path: Path,
    submodule_conflicts: Iterable[str],
    *,
    run_git: Any,
) -> list[SubmoduleConflictInfo]:
    infos: list[SubmoduleConflictInfo] = []
    for submodule_path in submodule_conflicts:
        infos.append(
            SubmoduleConflictInfo(
                path=submodule_path,
                repo_path=repo_path / submodule_path,
                base_commit=get_unmerged_submodule_stage_commit(repo_path, submodule_path, 1, run_git=run_git),
                current_commit=get_unmerged_submodule_stage_commit(repo_path, submodule_path, 2, run_git=run_git),
                merge_commit=get_unmerged_submodule_stage_commit(repo_path, submodule_path, 3, run_git=run_git),
            )
        )
    return infos


def get_merge_head_commit(repo_path: Path, *, run_git: Any) -> str | None:
    result = run_git(repo_path, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], check=False)
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout.splitlines()[0].strip() or None


def get_merge_base_commit(repo_path: Path, merge_head: str, *, run_git: Any) -> str | None:
    result = run_git(repo_path, ["merge-base", "HEAD", merge_head], check=False)
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout.splitlines()[0].strip() or None


def get_submodule_paths(repo_path: Path, *, run_git: Any) -> list[str]:
    gitmodules_path = repo_path / ".gitmodules"
    if not gitmodules_path.exists():
        return []

    result = run_git(
        repo_path,
        ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return []

    paths: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            paths.append(parts[1])
    return paths


def collect_submodule_move_candidate_info(
    repo_path: Path,
    submodule_conflicts: Iterable[str],
    *,
    run_git: Any,
) -> list[SubmoduleConflictInfo]:
    infos_by_path = {
        info.path: info
        for info in collect_submodule_conflict_info(repo_path, submodule_conflicts, run_git=run_git)
    }
    merge_head = get_merge_head_commit(repo_path, run_git=run_git)
    if not merge_head:
        return list(infos_by_path.values())
    merge_base = get_merge_base_commit(repo_path, merge_head, run_git=run_git)
    if not merge_base:
        return list(infos_by_path.values())

    for submodule_path in get_submodule_paths(repo_path, run_git=run_git):
        if submodule_path in infos_by_path:
            continue
        base_commit = get_submodule_commit_at_revision(repo_path, merge_base, submodule_path, run_git=run_git)
        current_commit = get_submodule_commit_at_revision(repo_path, "HEAD", submodule_path, run_git=run_git)
        merge_commit = get_submodule_commit_at_revision(repo_path, merge_head, submodule_path, run_git=run_git)
        commit_variants = {
            commit
            for commit in (base_commit, current_commit, merge_commit)
            if commit
        }
        if len(commit_variants) <= 1:
            continue
        infos_by_path[submodule_path] = SubmoduleConflictInfo(
            path=submodule_path,
            repo_path=repo_path / submodule_path,
            base_commit=base_commit,
            current_commit=current_commit,
            merge_commit=merge_commit,
        )
    return list(infos_by_path.values())


def list_diff_paths(
    repo_path: Path,
    base_commit: str,
    target_commit: str,
    diff_filter: str,
    *,
    run_git: Any,
    verbose_print: Any | None = None,
) -> list[str]:
    if base_commit == target_commit:
        return []
    result = run_git(
        repo_path,
        ["diff", "--name-only", f"--diff-filter={diff_filter}", base_commit, target_commit, "--"],
        check=False,
    )
    if result.returncode != 0:
        if callable(verbose_print):
            verbose_print(
                "Failed to inspect changed paths while detecting cross-submodule moves in "
                f"{repo_path}: {result.stderr or result.stdout}"
            )
        return []
    return [
        common.normalize_status_relative_path(line)
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def collect_deleted_files_for_move_side(
    infos: Iterable[SubmoduleConflictInfo],
    move_side: int,
    *,
    run_git: Any,
    verbose_print: Any | None = None,
) -> list[CrossSubmoduleDeletedFile]:
    deleted: list[CrossSubmoduleDeletedFile] = []
    for info in infos:
        side_commit = info.commit_for_stage(move_side)
        if not info.base_commit or not side_commit:
            continue
        for file_path in list_diff_paths(info.repo_path, info.base_commit, side_commit, "D", run_git=run_git, verbose_print=verbose_print):
            deleted.append(
                CrossSubmoduleDeletedFile(
                    info=info,
                    file_path=file_path,
                    move_side=move_side,
                )
            )
    return deleted


def collect_added_files_for_move_side(
    infos: Iterable[SubmoduleConflictInfo],
    move_side: int,
    *,
    run_git: Any,
    verbose_print: Any | None = None,
) -> list[CrossSubmoduleAddedFile]:
    added: list[CrossSubmoduleAddedFile] = []
    for info in infos:
        side_commit = info.commit_for_stage(move_side)
        if not info.base_commit or not side_commit:
            continue
        for file_path in list_diff_paths(info.repo_path, info.base_commit, side_commit, "A", run_git=run_git, verbose_print=verbose_print):
            added.append(
                CrossSubmoduleAddedFile(
                    info=info,
                    file_path=file_path,
                    move_side=move_side,
                )
            )
    return added


def merge_cross_submodule_file_contents(
    repo_path: Path,
    move: CrossSubmoduleMove,
    current_content: bytes,
    base_content: bytes,
    other_content: bytes,
    *,
    run_git_binary: Any,
) -> tuple[bytes | None, bool, str | None]:
    current_content, base_content, other_content, text_normalization = (
        common.prepare_cross_submodule_merge_inputs(current_content, base_content, other_content)
    )
    with tempfile.TemporaryDirectory(prefix="cross-submodule-move-") as temp_dir:
        temp_path = Path(temp_dir)
        current_file = temp_path / "current"
        base_file = temp_path / "base"
        other_file = temp_path / "other"
        current_file.write_bytes(current_content)
        base_file.write_bytes(base_content)
        other_file.write_bytes(other_content)

        result = run_git_binary(
            repo_path,
            [
                "merge-file",
                "-p",
                "-L",
                f"new location: {move.new_submodule_path}/{move.new_file_path}",
                "-L",
                f"base: {move.old_submodule_path}/{move.old_file_path}",
                "-L",
                f"old location edits: {move.old_submodule_path}/{move.old_file_path}",
                str(current_file),
                str(base_file),
                str(other_file),
            ],
            check=False,
        )
    if result.returncode in (0, 1):
        return (
            common.restore_cross_submodule_merge_output(result.stdout, text_normalization),
            result.returncode == 1,
            None,
        )
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    stdout = result.stdout.decode("utf-8", errors="replace").strip()
    return None, True, stderr or stdout or "git merge-file failed"


def read_file_from_revision(
    repo_path: Path,
    revision: str,
    relative_path: str,
    *,
    run_git_binary: Any,
) -> bytes | None:
    result = run_git_binary(
        repo_path,
        ["show", f"{revision}:{relative_path}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def collect_cross_submodule_moves(
    repo_path: Path,
    submodule_conflicts: Iterable[str],
    *,
    candidate_infos: Iterable[SubmoduleConflictInfo] | None = None,
    run_git: Any,
    run_git_binary: Any,
    verbose_print: Any | None = None,
) -> list[CrossSubmoduleMove]:
    infos = (
        list(candidate_infos)
        if candidate_infos is not None
        else collect_submodule_move_candidate_info(
            repo_path,
            submodule_conflicts,
            run_git=run_git,
        )
    )
    moves: list[CrossSubmoduleMove] = []
    content_cache: dict[tuple[Path, str, str], bytes | None] = {}

    def read_cached(path: Path, commit: str, file_path: str) -> bytes | None:
        key = (path, commit, file_path)
        if key not in content_cache:
            content_cache[key] = read_file_from_revision(path, commit, file_path, run_git_binary=run_git_binary)
        return content_cache[key]

    for move_side in (2, 3):
        other_side = 3 if move_side == 2 else 2
        deleted_files = collect_deleted_files_for_move_side(infos, move_side, run_git=run_git, verbose_print=verbose_print)
        added_files = collect_added_files_for_move_side(infos, move_side, run_git=run_git, verbose_print=verbose_print)
        candidates: list[tuple[float, CrossSubmoduleDeletedFile, CrossSubmoduleAddedFile]] = []

        for deleted_file in deleted_files:
            old_info = deleted_file.info
            if not old_info.base_commit:
                continue
            old_other_commit = old_info.commit_for_stage(other_side)
            if not old_other_commit:
                continue
            old_base_content = read_cached(
                old_info.repo_path,
                old_info.base_commit,
                deleted_file.file_path,
            )
            old_other_content = read_cached(
                old_info.repo_path,
                old_other_commit,
                deleted_file.file_path,
            )
            if old_base_content is None or old_other_content is None:
                continue

            for added_file in added_files:
                new_info = added_file.info
                if new_info.path == old_info.path:
                    continue
                new_added_commit = new_info.commit_for_stage(move_side)
                if not new_added_commit:
                    continue
                new_file_content = read_cached(
                    new_info.repo_path,
                    new_added_commit,
                    added_file.file_path,
                )
                if new_file_content is None:
                    continue
                score = common.score_cross_submodule_move_candidate(
                    deleted_file.file_path,
                    added_file.file_path,
                    old_base_content,
                    new_file_content,
                )
                if score >= CROSS_SUBMODULE_MOVE_SIMILARITY_THRESHOLD:
                    candidates.append((score, deleted_file, added_file))

        candidates.sort(
            key=lambda candidate: (
                candidate[0],
                common.path_name_similarity(candidate[1].file_path, candidate[2].file_path),
            ),
            reverse=True,
        )
        used_deleted: set[tuple[str, str]] = set()
        used_added: set[tuple[str, str]] = set()
        for score, deleted_file, added_file in candidates:
            deleted_key = (deleted_file.info.path, deleted_file.file_path)
            added_key = (added_file.info.path, added_file.file_path)
            if deleted_key in used_deleted or added_key in used_added:
                continue
            old_base_commit = deleted_file.info.base_commit
            old_other_commit = deleted_file.info.commit_for_stage(other_side)
            new_added_commit = added_file.info.commit_for_stage(move_side)
            if not old_base_commit or not old_other_commit or not new_added_commit:
                continue
            moves.append(
                CrossSubmoduleMove(
                    old_submodule_path=deleted_file.info.path,
                    old_repo_path=deleted_file.info.repo_path,
                    old_file_path=deleted_file.file_path,
                    old_base_commit=old_base_commit,
                    old_other_commit=old_other_commit,
                    new_submodule_path=added_file.info.path,
                    new_repo_path=added_file.info.repo_path,
                    new_file_path=added_file.file_path,
                    new_added_commit=new_added_commit,
                    move_side=move_side,
                    score=score,
                )
            )
            used_deleted.add(deleted_key)
            used_added.add(added_key)

    if callable(verbose_print):
        for move in moves:
            verbose_print(
                "Detected cross-submodule move: "
                f"{move.old_submodule_path}/{move.old_file_path} -> "
                f"{move.new_submodule_path}/{move.new_file_path} "
                f"(similarity {move.score:.2f})"
            )
    return moves


def apply_cross_submodule_moved_files(
    repo_path: Path,
    moves: Iterable[CrossSubmoduleMove],
    context: Any,
    *,
    run_git: Any,
    run_git_binary: Any,
    get_unmerged_paths: Any,
    resolve_single_file_conflict: Any,
    verbose_print: Any | None = None,
) -> tuple[list[str], str, bool]:
    unresolved: list[str] = []
    reasons: list[str] = []
    rollback_requested = False
    unmerged_paths = set(get_unmerged_paths(repo_path))
    for move in moves:
        target_path = move.new_file_path
        target_file = repo_path / target_path
        if target_path in unmerged_paths:
            unresolved.append(target_path)
            reasons.append(f"{target_path}: target path is already an unresolved file conflict")
            continue

        base_content = read_file_from_revision(
            move.old_repo_path,
            move.old_base_commit,
            move.old_file_path,
            run_git_binary=run_git_binary,
        )
        other_content = read_file_from_revision(
            move.old_repo_path,
            move.old_other_commit,
            move.old_file_path,
            run_git_binary=run_git_binary,
        )
        if target_file.exists():
            current_content = target_file.read_bytes()
        else:
            current_content = read_file_from_revision(
                move.new_repo_path,
                move.new_added_commit,
                move.new_file_path,
                run_git_binary=run_git_binary,
            )
        if base_content is None or other_content is None or current_content is None:
            unresolved.append(target_path)
            reasons.append(f"{target_path}: could not read all revisions needed for the move merge")
            continue

        merged_content, had_conflicts, failure_reason = merge_cross_submodule_file_contents(
            repo_path,
            move,
            current_content,
            base_content,
            other_content,
            run_git_binary=run_git_binary,
        )
        if merged_content is None:
            if callable(verbose_print):
                verbose_print(
                    "Failed to merge cross-submodule moved file "
                    f"{describe_cross_submodule_move(move)}: {failure_reason}"
                )
            unresolved.append(target_path)
            reasons.append(f"{target_path}: {failure_reason or 'git merge-file failed'}")
            continue

        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(merged_content)
        if had_conflicts:
            resolution = resolve_single_file_conflict(repo_path, target_path, context)
            rollback_requested = rollback_requested or resolution.rollback_requested
            if resolution.reason is not None:
                unresolved.append(target_path)
                reasons.append(f"{target_path}: {resolution.reason}")
                continue

        run_git(repo_path, ["add", "--", target_path])
        if callable(verbose_print):
            verbose_print(
                "Merged cross-submodule moved file at new location: "
                f"{repo_path / target_path}"
            )
    return unresolved, "; ".join(reasons), rollback_requested


@dataclass(frozen=True)
class CrossSubmoduleMoveService:
    """Facade for moved-file handling across submodule boundaries."""

    manual_module: ModuleType | None = None

    def __post_init__(self) -> None:
        return None

    def _run_git(self, repo_path: Path, args: list[str], **kwargs: Any) -> Any:
        runner = getattr(self.manual_module, "run_git", execution.run_git)
        return runner(repo_path, args, **kwargs)

    def _run_git_binary(self, repo_path: Path, args: list[str], **kwargs: Any) -> Any:
        runner = getattr(self.manual_module, "run_git_binary", execution.run_git_binary)
        return runner(repo_path, args, **kwargs)

    def _verbose_print(self, message: str) -> None:
        printer = getattr(self.manual_module, "verbose_print", None)
        if callable(printer):
            printer(message)
            return
        execution.verbose_print(message)

    def collect_submodule_move_candidate_info(self, repo_path: Path, submodule_conflicts: Iterable[str]) -> list[Any]:
        return collect_submodule_move_candidate_info(
            repo_path,
            submodule_conflicts,
            run_git=self._run_git,
        )

    def collect_cross_submodule_moves(
        self,
        repo_path: Path,
        submodule_conflicts: Iterable[str],
        *,
        candidate_infos: list[Any] | None = None,
    ) -> list[Any]:
        return collect_cross_submodule_moves(
            repo_path,
            submodule_conflicts,
            candidate_infos=candidate_infos,
            run_git=self._run_git,
            run_git_binary=self._run_git_binary,
            verbose_print=self._verbose_print,
        )

    def file_content_similarity(self, left: bytes, right: bytes) -> float:
        return common.file_content_similarity(left, right)

    def path_name_similarity(self, left_path: str, right_path: str) -> float:
        return common.path_name_similarity(left_path, right_path)

    def score_cross_submodule_move_candidate(
        self,
        old_file_path: str,
        new_file_path: str,
        old_base_content: bytes,
        new_file_content: bytes,
    ) -> float:
        return common.score_cross_submodule_move_candidate(
            old_file_path,
            new_file_path,
            old_base_content,
            new_file_content,
        )

    def prepare_cross_submodule_merge_inputs(
        self,
        current_content: bytes,
        base_content: bytes,
        other_content: bytes,
    ) -> tuple[bytes, bytes, bytes, common.TextMergeNormalization | None]:
        return common.prepare_cross_submodule_merge_inputs(current_content, base_content, other_content)

    def restore_cross_submodule_merge_output(
        self,
        merged_content: bytes,
        normalization: common.TextMergeNormalization | None,
    ) -> bytes:
        return common.restore_cross_submodule_merge_output(merged_content, normalization)

    def merge_cross_submodule_file_contents(
        self,
        repo_path: Path,
        move: Any,
        current_content: bytes,
        base_content: bytes,
        other_content: bytes,
    ) -> tuple[bytes | None, bool, str | None]:
        return merge_cross_submodule_file_contents(
            repo_path,
            move,
            current_content,
            base_content,
            other_content,
            run_git_binary=self._run_git_binary,
        )

    def apply_cross_submodule_moved_files(self, repo_path: Path, moves: list[Any], context: Any) -> tuple[list[str], str, bool]:
        return apply_cross_submodule_moved_files(
            repo_path,
            moves,
            context,
            run_git=self._run_git,
            run_git_binary=self._run_git_binary,
            get_unmerged_paths=lambda path: file_get_unmerged_paths(path, run_git=self._run_git),
            resolve_single_file_conflict=lambda path, relative, merge_context: resolve_single_file_conflict(
                path,
                relative,
                merge_context,
                run_git=self._run_git,
            ),
            verbose_print=self._verbose_print,
        )

    def describe_cross_submodule_move(self, move: Any) -> str:
        return describe_cross_submodule_move(move)
