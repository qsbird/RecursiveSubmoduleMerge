"""Single-branch manual merge orchestration with resumable sessions."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from merge_tool_adapters.manual_semiauto import ManualWorkflowGitlinkPolicy
from split_gitlink_merge import (
    Conflict,
    GitActionExecutor,
    GitCommandResult as CoreGitCommandResult,
    GitExecutionPort,
    GitlinkMergeContext,
    GitlinkMergeError,
    MergeCommitMessage,
    NonGitlinkConflictError,
    RecursiveGitlinkMergeEngine,
)

from . import common
from . import messages as manual_messages
from .file_conflicts import ManualFileConflictService
from .messages import ManualCommitMessage
from .models import MergeAutomationError, MergeContext, ManualConflictDisposition
from .preflight import ManualPreflightService
from .repository_ops import ManualRepositoryOps
from .session_models import (
    MergeSession,
    PausedConflictRecord,
    RepositoryFrame,
    SessionOutcome,
    SessionPhase,
    SessionStatus,
)
from .session_store import MergeSessionStore, SessionStoreError


class ManualMergePause(MergeAutomationError):
    """Signal that a valid Git conflict was preserved for manual takeover."""

    def __init__(self, session: MergeSession, message: str) -> None:
        self.session = session
        super().__init__(message)


class _ManualOpsGitBackend:
    def __init__(self, ops: ManualRepositoryOps) -> None:
        self.ops = ops

    def run(self, repo_path: Path, args: list[str], *, check: bool = True) -> CoreGitCommandResult:
        result = self.ops.run_git(repo_path, args, check=check)
        normalized = CoreGitCommandResult(
            args=["git", *args],
            cwd=Path(getattr(result, "cwd", repo_path)).resolve(strict=False),
            returncode=int(getattr(result, "returncode", 0)),
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
        )
        if check and normalized.returncode != 0:
            raise GitlinkMergeError(self.ops._format_git_failure(result))
        return normalized


class ManualMergeSessionService:
    def __init__(
        self,
        *,
        ops: ManualRepositoryOps,
        preflight_service: ManualPreflightService,
        file_conflicts_service: ManualFileConflictService,
        store: MergeSessionStore | None = None,
    ) -> None:
        self.ops = ops
        self.preflight = preflight_service
        self.file_conflicts = file_conflicts_service
        self.store = store or MergeSessionStore(
            runner=lambda repo, args: self.ops.run_git(repo, args, check=False)
        )

    def start_merge(self, args: object) -> int:
        try:
            repo = self._prepare_clone_if_requested(args)
            with self.store.command_lock(repo):
                return self._start_merge(args, prepared_repo=repo)
        except (MergeAutomationError, GitlinkMergeError, SessionStoreError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    def resume(self, args: object) -> int:
        try:
            repo = Path(str(getattr(args, "repo", "."))).resolve(strict=False)
            with self.store.command_lock(repo):
                return self._resume(args)
        except (MergeAutomationError, GitlinkMergeError, SessionStoreError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    def status(self, args: object) -> int:
        try:
            return self._status(args)
        except (MergeAutomationError, GitlinkMergeError, SessionStoreError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    def abort(self, args: object) -> int:
        try:
            repo = Path(str(getattr(args, "repo", "."))).resolve(strict=False)
            with self.store.command_lock(repo):
                return self._abort(args)
        except (MergeAutomationError, GitlinkMergeError, SessionStoreError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    def _start_merge(self, args: object, *, prepared_repo: Path) -> int:
        repo = prepared_repo
        self.ops.ensure_git_repo(repo)
        if self.store.load(repo) is not None:
            raise self.ops._automation_error("A manual merge session is already active for this repository.")

        use_current_branch = bool(
            getattr(args, "use_current_branch", False) or getattr(args, "continue_current_branch", False)
        )
        if use_current_branch and getattr(args, "work_branch", None):
            raise self.ops._automation_error("--work-branch cannot be used together with --use-current-branch.")
        base_ref = "HEAD" if use_current_branch else str(getattr(args, "base_remote_branch", "")).strip()
        if not base_ref:
            raise self.ops._automation_error(
                "--base-remote-branch cannot be empty unless --use-current-branch is selected."
            )
        raw_merge_ref = str(getattr(args, "merge_branch", "")).strip()
        if not raw_merge_ref:
            raise self.ops._automation_error("A merge branch is required.")
        merge_ref = (
            raw_merge_ref
            if self.ops.try_resolve_commit(repo, raw_merge_ref) is not None
            else common.normalize_user_merge_ref(raw_merge_ref)
        )
        ignored_paths = tuple(getattr(args, "ignore_root_untracked_path", []) or ())
        self.preflight.preflight_repository(
            repo,
            base_ref,
            [merge_ref],
            ignored_root_untracked_paths=ignored_paths,
        )

        if use_current_branch:
            work_branch = self.ops.current_branch_name(repo)
            if work_branch is None:
                raise self.ops._automation_error("Cannot merge on the current branch from detached HEAD.")
        else:
            work_branch = common.compute_work_branch(getattr(args, "work_branch", None))
            self.ops.create_root_branch(repo, base_ref, work_branch)
            self.ops.update_submodules(repo)

        original_head = self.ops.current_head(repo)
        original_branch = self.ops.current_branch_name(repo)
        target_ref = original_branch or "HEAD"
        merge_target, merge_sha = self._resolve_merge_target(repo, merge_ref)
        message = manual_messages.build_root_commit_message(
            repo_path=repo,
            source_ref=merge_ref,
            target_ref=target_ref,
            source_sha=merge_sha,
            target_sha=original_head,
            work_branch=work_branch,
        )
        frame = RepositoryFrame(
            repo_path=str(repo),
            display_path=".",
            depth=0,
            original_head=original_head,
            original_branch=original_branch,
            ours_sha=original_head,
            theirs_sha=merge_sha,
            expected_merge_head=merge_sha,
            phase=SessionPhase.MERGE_PENDING,
            commit_title=message.title,
            commit_body=message.body,
        )
        session = MergeSession.new(
            root_repo_path=str(repo),
            work_branch=work_branch,
            target_ref=merge_ref,
            base_ref=base_ref,
            merge_sha=merge_sha,
            repo_stack=[frame],
            metadata={
                "merge_target": merge_target,
                "conflict_tool_command": str(
                    getattr(args, "conflict_tool_command", 'code --wait "{file}"')
                ),
                "conflict_tool_timeout_seconds": int(
                    getattr(args, "conflict_tool_timeout_seconds", 0)
                ),
                "confirm_after_conflict_tool": not bool(
                    getattr(args, "no_confirm_after_conflict_tool", False)
                ),
                "report_file": str(getattr(args, "report_file", "merge_manual_todo.txt")),
            },
        )
        session = self.store.save(repo, session)

        try:
            actual_target = self.ops.merge_branch(repo, merge_ref)
            if self.ops.resolve_commit(repo, actual_target) != merge_sha:
                raise self.ops._automation_error("The merge target changed after the session was created.")
            session = self._replace_top_frame(session, phase=SessionPhase.MERGE_ACTIVE)
            session = self.store.save(repo, session)
            context = self._context_from_session(session)
            session = self._resolve_active(repo, session, context, display_path=".", target_ref=target_ref)
            session = self._guard_staged_conflict_markers(session, repo)
            self._commit_frame(repo, session.current_frame)
        except ManualMergePause as pause:
            self._write_pause_report(pause.session)
            print(str(pause), file=sys.stderr)
            print("Manual merge paused. Resolve all conflicts, stage the result, then run resume.")
            return 2
        except (MergeAutomationError, GitlinkMergeError, SessionStoreError) as exc:
            current = self.store.load(repo) or session
            failed = replace(
                current,
                status=SessionStatus.FAILED_FATAL,
                outcome=SessionOutcome.FAILED_FATAL,
                last_error=str(exc),
            )
            try:
                self.store.save(repo, failed)
            except SessionStoreError:
                pass
            raise

        self.store.clear(repo)
        self._remove_pause_report(session)
        print("Recursive merge completed successfully.")
        return 0

    def _resume(self, args: object) -> int:
        root = Path(str(getattr(args, "repo", "."))).resolve(strict=False)
        session = self.store.load(root)
        if session is None:
            raise self.ops._automation_error("No active manual merge session was found.")
        if session.status == SessionStatus.FAILED_FATAL:
            raise self.ops._automation_error(
                "The saved session is in a fatal state; inspect status and abort or restart the merge."
            )
        if not session.repo_stack:
            raise self.ops._automation_error("The saved session has no repository stack.")
        current_root_branch = self.ops.current_branch_name(root)
        if current_root_branch != session.work_branch:
            raise self.ops._automation_error(
                f"Root branch changed since pause: expected {session.work_branch}, found {current_root_branch or 'detached HEAD'}."
            )

        # The previous pause diagnostics stay on the session until resume either
        # finishes (session is deleted) or pauses again with fresh records: a
        # resume that dies halfway must not leave the user with an empty report.
        session = replace(
            session,
            status=SessionStatus.RUNNING,
            phase=SessionPhase.RESUMING,
            outcome=None,
        )
        context = self._context_from_session(session)

        try:
            while session.current_frame is not None:
                frame = session.current_frame
                current_repo = Path(frame.repo_path)
                if frame.phase == SessionPhase.MERGE_PENDING:
                    if self.ops.merge_in_progress(current_repo) or self.ops.get_unmerged_paths(current_repo):
                        session = self._replace_top_frame(session, phase=SessionPhase.MERGE_ACTIVE)
                        session = self.store.save(root, session)
                    else:
                        session = self.store.save(root, session)
                        self.ops.merge_branch(current_repo, frame.theirs_sha or session.target_ref)
                        session = self._replace_top_frame(session, phase=SessionPhase.MERGE_ACTIVE)
                        session = self.store.save(root, session)
                    frame = session.current_frame
                    assert frame is not None

                if self.ops.get_unmerged_paths(current_repo):
                    session = self._resolve_active(
                        current_repo,
                        session,
                        context,
                        display_path=frame.display_path,
                        target_ref=frame.original_branch or session.work_branch,
                    )
                    frame = session.current_frame
                    if frame is None:
                        continue
                    current_repo = Path(frame.repo_path)
                    if self.ops.get_unmerged_paths(current_repo):
                        session = self._pause_current(session, current_repo, "unresolved conflicts remain")
                        raise ManualMergePause(session, "unresolved conflicts remain")

                merge_head = self.ops.read_merge_head(current_repo)
                if merge_head is not None:
                    if frame.expected_merge_head and merge_head != frame.expected_merge_head:
                        raise self.ops._automation_error(
                            f"MERGE_HEAD changed in {current_repo}: expected {frame.expected_merge_head}, found {merge_head}."
                        )
                    session = self.store.save(root, session)
                    session = self._guard_staged_conflict_markers(session, current_repo)
                    self._commit_frame(current_repo, frame)
                else:
                    head = self.ops.current_head(current_repo)
                    ignored_report = (
                        (str(session.metadata.get("report_file", "merge_manual_todo.txt")),)
                        if current_repo.resolve(strict=False) == root
                        else ()
                    )
                    if self.ops.working_tree_has_changes(
                        current_repo,
                        ignored_untracked_paths=ignored_report,
                    ):
                        raise self.ops._automation_error(
                            f"{current_repo} has changes but no active merge; commit or restore them before resume."
                        )
                    if frame.ours_sha and not self.ops.is_ancestor(current_repo, frame.ours_sha, head):
                        raise self.ops._automation_error(f"Current HEAD in {current_repo} no longer contains ours.")
                    if frame.theirs_sha and not self.ops.is_ancestor(current_repo, frame.theirs_sha, head):
                        raise self.ops._automation_error(f"Current HEAD in {current_repo} no longer contains theirs.")

                result_head = self.ops.current_head(current_repo)
                if frame.parent_repo_path and frame.parent_gitlink_path:
                    session = self._replace_top_frame(
                        session,
                        phase=SessionPhase.CHILD_COMMITTED_PENDING_STAGE,
                    )
                    session = self.store.save(root, session)
                    if self.ops.current_branch_name(current_repo) != session.work_branch:
                        self.ops.create_submodule_branch(
                            current_repo,
                            session.work_branch,
                            start_point=result_head,
                        )
                    self.ops.stage_path(Path(frame.parent_repo_path), frame.parent_gitlink_path)
                    session = replace(session, repo_stack=session.repo_stack[:-1])
                    session = self.store.save(root, session)
                    continue

                self.store.clear(root)
                self._remove_pause_report(session)
                print("Manual merge resume completed successfully.")
                return 0
        except ManualMergePause as pause:
            self._write_pause_report(pause.session)
            print(str(pause), file=sys.stderr)
            print("Manual merge is still waiting for conflict resolution.")
            return 2
        except (MergeAutomationError, GitlinkMergeError, SessionStoreError) as exc:
            self._mark_resume_blocked(root, exc)
            raise

        self.store.clear(root)
        self._remove_pause_report(session)
        print("Manual merge resume completed successfully.")
        return 0

    def _status(self, args: object) -> int:
        root = Path(str(getattr(args, "repo", "."))).resolve(strict=False)
        snapshot = self.store.status(root)
        if not snapshot.exists or snapshot.session is None:
            print("No active manual merge session.")
            return 0
        session = snapshot.session
        frame = session.current_frame
        print(f"Session: {session.session_id}")
        print(f"Status: {session.status.value}")
        print(f"Target: {session.target_ref} ({session.merge_sha or 'unknown'})")
        print(f"Work branch: {session.work_branch}")
        print(f"Depth: {len(session.repo_stack)}")
        if frame is None:
            print("Session is invalid: no current repository frame.")
            return 1
        repo = Path(frame.repo_path)
        merge_head = self.ops.read_merge_head(repo)
        unmerged = self.ops.get_unmerged_paths(repo)
        print(f"Current repository: {repo}")
        print(f"Phase: {frame.phase.value}")
        print(f"MERGE_HEAD: {merge_head or 'none'}")
        print(f"Unmerged paths: {len(unmerged)}")
        if merge_head and frame.expected_merge_head and merge_head != frame.expected_merge_head:
            print("Session and repository MERGE_HEAD do not match.", file=sys.stderr)
            return 1
        return 0

    def _abort(self, args: object) -> int:
        root = Path(str(getattr(args, "repo", "."))).resolve(strict=False)
        session = self.store.load(root)
        if session is None:
            raise self.ops._automation_error("No active manual merge session was found.")
        if not bool(getattr(args, "yes", False)):
            print(
                "Abort discards manual conflict edits in this session. Re-run with --yes to confirm.",
                file=sys.stderr,
            )
            return 1
        session = replace(session, phase=SessionPhase.ABORTING)
        session = self.store.save(root, session)
        for frame in self._frames_to_restore(session):
            self._restore_frame(frame)
        self.store.clear(root)
        self._remove_pause_report(session)
        print("Manual merge session aborted.")
        return 0

    def _resolve_active(
        self,
        repo_path: Path,
        session: MergeSession,
        context: MergeContext,
        *,
        display_path: str,
        target_ref: str,
    ) -> MergeSession:
        root = Path(session.root_repo_path)
        state = [session]
        repo = repo_path.resolve(strict=False)

        def persist(updated: MergeSession) -> MergeSession:
            state[0] = self.store.save(root, updated)
            return state[0]

        def prepare_submodule_branch(child_repo: Path, start_point: str) -> None:
            self.ops.create_submodule_branch(child_repo, context.work_branch, start_point=start_point)

        def build_submodule_commit_message(recursive: GitlinkMergeContext) -> MergeCommitMessage:
            nested_display = manual_messages.join_display_path(display_path, recursive.submodule_path)
            message = manual_messages.build_submodule_commit_message(
                repo_path=recursive.submodule_repo,
                parent_repo=recursive.parent_repo,
                display_path=nested_display,
                source_ref=context.merge_branch,
                target_ref=recursive.ours_sha,
                source_sha=recursive.theirs_sha,
                target_sha=recursive.ours_sha,
                base_sha=recursive.base_sha,
                work_branch=context.work_branch,
            )
            return MergeCommitMessage(title=message.title, body=message.body)

        def on_prepare_recursive_merge(recursive: GitlinkMergeContext) -> None:
            child = recursive.submodule_repo.resolve(strict=False)
            message = build_submodule_commit_message(recursive)
            frame = RepositoryFrame(
                repo_path=str(child),
                display_path=manual_messages.join_display_path(display_path, recursive.submodule_path),
                depth=recursive.depth,
                parent_repo_path=str(recursive.parent_repo.resolve(strict=False)),
                parent_gitlink_path=recursive.submodule_path,
                original_head=self.ops.current_head(child),
                original_branch=self.ops.current_branch_name(child),
                ours_sha=recursive.ours_sha,
                theirs_sha=recursive.theirs_sha,
                base_sha=recursive.base_sha,
                expected_merge_head=recursive.theirs_sha,
                phase=SessionPhase.MERGE_PENDING,
                commit_title=message.title,
                commit_body=message.body,
            )
            touched = state[0].touched_frames
            if all(existing.repo_path != frame.repo_path for existing in touched):
                touched = (*touched, frame)
            persist(
                replace(
                    state[0],
                    repo_stack=(*state[0].repo_stack, frame),
                    touched_frames=touched,
                    current_repo_path=str(child),
                )
            )

        def on_gitlink_conflict(event: Any) -> None:
            """Snapshot a submodule before the engine changes it, so abort can undo it.

            ``on_prepare_recursive_merge`` only fires for gitlinks that need a
            recursive merge.  Fast-forward and take-theirs resolutions still
            check the submodule out at a new commit and move its work branch, so
            without this hook ``abort`` would leave them behind.
            """
            path = str(event.data.get("path", "")).strip()
            if not path:
                return
            child = (event.repo_path / path).resolve(strict=False)
            existing = state[0].touched_frames
            if any(frame.repo_path == str(child) for frame in existing):
                return
            was_initialized = (child / ".git").exists()
            original_head = None
            original_branch = None
            if was_initialized:
                original_head = self.ops.current_head(child)
                original_branch = self.ops.current_branch_name(child)
            frame = RepositoryFrame(
                repo_path=str(child),
                display_path=manual_messages.join_display_path(display_path, path),
                depth=len(state[0].repo_stack),
                parent_repo_path=str(event.repo_path.resolve(strict=False)),
                parent_gitlink_path=path,
                original_head=original_head,
                original_branch=original_branch,
                was_initialized=was_initialized,
                phase=SessionPhase.MERGE_PENDING,
            )
            persist(replace(state[0], touched_frames=(*existing, frame)))

        def on_gitlink_resolved(event: Any) -> None:
            top = state[0].current_frame
            path = str(event.data.get("path", ""))
            if (
                top is not None
                and top.parent_repo_path == str(event.repo_path.resolve(strict=False))
                and top.parent_gitlink_path == path
            ):
                persist(
                    replace(
                        state[0],
                        repo_stack=state[0].repo_stack[:-1],
                        current_repo_path=str(event.repo_path.resolve(strict=False)),
                    )
                )

        def handle_file_conflicts(
            actions: GitExecutionPort,
            conflict_repo: Path,
            conflicts: list[Conflict],
            depth: int,
        ) -> None:
            del actions, depth
            classified = self.ops.classify_unmerged_conflicts(conflict_repo)
            manual: list[PausedConflictRecord] = []
            supported: list[str] = []
            for conflict in conflicts:
                current = classified.get(conflict.path)
                if current is None:
                    raise self.ops._automation_error(f"Could not classify conflict path: {conflict.path}")
                if current.disposition == ManualConflictDisposition.FATAL:
                    raise self.ops._automation_error(current.reason or f"Fatal conflict shape: {current.path}")
                if current.disposition == ManualConflictDisposition.MANUAL_TAKEOVER:
                    manual.append(
                        PausedConflictRecord(
                            repo_path=str(conflict_repo.resolve(strict=False)),
                            path=current.path,
                            disposition=current.disposition.value,
                            detail=current.reason,
                        )
                    )
                elif current.disposition == ManualConflictDisposition.SUPPORTED_FILE:
                    supported.append(current.path)
                else:
                    raise self.ops._automation_error(
                        f"Non-file conflict reached the file resolver: {current.path}"
                    )
            if manual:
                paused = self._pause_session(state[0], conflict_repo, tuple(manual), "manual takeover required")
                paused = persist(paused)
                raise ManualMergePause(paused, "manual takeover required")

            unresolved, reason, rollback_requested = self.file_conflicts.resolve_file_conflicts(
                conflict_repo,
                supported,
                context,
            )
            if rollback_requested:
                raise self.ops._automation_error("Rollback is not implicit in the single-branch workflow.")
            if unresolved:
                records = tuple(
                    PausedConflictRecord(
                        repo_path=str(conflict_repo.resolve(strict=False)),
                        path=path,
                        disposition=ManualConflictDisposition.MANUAL_TAKEOVER.value,
                        detail=reason or "file conflict remains unresolved",
                    )
                    for path in unresolved
                )
                paused = persist(self._pause_session(state[0], conflict_repo, records, reason or "unresolved"))
                raise ManualMergePause(paused, reason or "file conflict remains unresolved")

        policy = ManualWorkflowGitlinkPolicy(
            tracked_repo=repo,
            prepare_submodule_branch=prepare_submodule_branch,
            build_submodule_commit_message=build_submodule_commit_message,
            handle_file_conflicts=handle_file_conflicts,
            on_prepare_recursive_merge=on_prepare_recursive_merge,
            on_gitlink_conflict=on_gitlink_conflict,
            on_gitlink_resolved=on_gitlink_resolved,
        )
        engine = RecursiveGitlinkMergeEngine(GitActionExecutor(_ManualOpsGitBackend(self.ops)), policy=policy)
        try:
            engine.resolve_active_conflicts(repo)
        except NonGitlinkConflictError as exc:
            raise self.ops._automation_error(str(exc)) from exc
        return state[0]

    def _guard_staged_conflict_markers(self, session: MergeSession, repo_path: Path) -> MergeSession:
        """Pause instead of committing when the staged result still has markers.

        ``git`` happily records ``<<<<<<<`` / ``>>>>>>>`` lines: a user who runs
        ``git add`` on a half-resolved file and then ``resume`` would otherwise
        get a successful merge commit containing the markers.
        """
        marked = self.ops.find_staged_conflict_marker_paths(repo_path)
        if not marked:
            return session
        records = tuple(
            PausedConflictRecord(
                repo_path=str(repo_path.resolve(strict=False)),
                path=path,
                disposition=ManualConflictDisposition.MANUAL_TAKEOVER.value,
                detail="staged content still contains conflict markers",
            )
            for path in marked
        )
        reason = "staged conflict markers remain"
        paused = self.store.save(
            Path(session.root_repo_path),
            self._pause_session(session, repo_path, records, reason),
        )
        listed = ", ".join(marked)
        raise ManualMergePause(
            paused,
            f"Refusing to commit in {repo_path}: staged content still contains conflict markers ({listed}).",
        )

    def _mark_resume_blocked(self, root: Path, error: Exception) -> None:
        """Record a failed resume as a pause so status and the report stay honest."""
        try:
            current = self.store.load(root)
            if current is None or current.status == SessionStatus.FAILED_FATAL:
                return
            self.store.save(
                root,
                replace(
                    current,
                    status=SessionStatus.PAUSED_MANUAL,
                    phase=SessionPhase.MANUAL_RESOLUTION,
                    outcome=SessionOutcome.PAUSED_MANUAL,
                    paused_reason=str(error),
                    last_error=str(error),
                ),
            )
        except (MergeAutomationError, GitlinkMergeError, SessionStoreError):
            return

    def _pause_current(self, session: MergeSession, repo_path: Path, reason: str) -> MergeSession:
        records = tuple(
            PausedConflictRecord(
                repo_path=str(conflict.repo_path),
                path=conflict.path,
                disposition=conflict.disposition.value,
                detail=conflict.reason,
            )
            for conflict in self.ops.classify_unmerged_conflicts(repo_path).values()
        )
        paused = self._pause_session(session, repo_path, records, reason)
        return self.store.save(Path(session.root_repo_path), paused)

    @staticmethod
    def _pause_session(
        session: MergeSession,
        repo_path: Path,
        conflicts: tuple[PausedConflictRecord, ...],
        reason: str,
    ) -> MergeSession:
        stack = list(session.repo_stack)
        if stack:
            stack[-1] = replace(stack[-1], phase=SessionPhase.MANUAL_RESOLUTION)
        return replace(
            session,
            status=SessionStatus.PAUSED_MANUAL,
            phase=SessionPhase.MANUAL_RESOLUTION,
            outcome=SessionOutcome.PAUSED_MANUAL,
            repo_stack=tuple(stack),
            current_repo_path=str(repo_path.resolve(strict=False)),
            paused_conflicts=conflicts,
            paused_reason=reason,
        )

    @staticmethod
    def _replace_top_frame(session: MergeSession, **changes: Any) -> MergeSession:
        if not session.repo_stack:
            return session
        stack = list(session.repo_stack)
        stack[-1] = replace(stack[-1], **changes)
        return replace(session, repo_stack=tuple(stack), current_repo_path=stack[-1].repo_path)

    def _context_from_session(self, session: MergeSession) -> MergeContext:
        timeout = int(session.metadata.get("conflict_tool_timeout_seconds", 0) or 0)
        return MergeContext(
            work_branch=session.work_branch,
            merge_branch=session.target_ref,
            file_conflict_mode="tool",
            conflict_tool_command=str(
                session.metadata.get("conflict_tool_command", 'code --wait "{file}"')
            ),
            conflict_tool_timeout_seconds=None if timeout <= 0 else timeout,
            confirm_after_conflict_tool=bool(
                session.metadata.get("confirm_after_conflict_tool", False)
            ),
        )

    def _resolve_merge_target(self, repo: Path, merge_ref: str) -> tuple[str, str]:
        self.ops.fetch_ref_if_remote(repo, merge_ref)
        remote, _, branch = merge_ref.partition("/")
        target = common.remote_tracking_ref(merge_ref) if branch and self.ops.remote_exists(repo, remote) else merge_ref
        return target, self.ops.resolve_commit(repo, target)

    def _prepare_clone_if_requested(self, args: object) -> Path:
        clone_values = [
            getattr(args, "clone_branch", None),
            getattr(args, "clone_url", None),
            getattr(args, "clone_path", None),
        ]
        if not any(clone_values):
            return Path(str(getattr(args, "repo", "."))).resolve(strict=False)
        if not all(clone_values):
            raise self.ops._automation_error(
                "Clone before merge requires --clone-branch, --clone-url, and --clone-path together."
            )
        clone_url = str(clone_values[1])
        target = common.resolve_clone_target_path(clone_url, Path(str(clone_values[2])))
        self.ops.run_git(
            Path.cwd(),
            [
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--recurse-submodules",
                "--branch",
                str(clone_values[0]),
                clone_url,
                str(target),
            ],
            stream_output=True,
        )
        return target.resolve(strict=False)

    def _commit_frame(self, repo_path: Path, frame: RepositoryFrame | None) -> None:
        if frame is None:
            raise self.ops._automation_error("Cannot commit without a repository frame.")
        self.ops.commit_if_needed(
            repo_path,
            message=ManualCommitMessage(title=frame.commit_title, body=frame.commit_body),
        )

    @staticmethod
    def _frames_to_restore(session: MergeSession) -> list[RepositoryFrame]:
        """Every repository abort must undo, deepest first.

        The root lives on ``repo_stack``; submodules live on ``touched_frames``.
        ``touched_frames`` is snapshotted at ``gitlink.conflict``, before the
        engine initializes or checks out the submodule, so it wins when the same
        path later appears on the stack with a post-init head.
        """
        by_path: dict[str, RepositoryFrame] = {}
        for frame in (*session.touched_frames, *session.repo_stack):
            by_path.setdefault(frame.repo_path, frame)
        return sorted(
            by_path.values(),
            key=lambda frame: len(Path(frame.repo_path).parts),
            reverse=True,
        )

    def _restore_frame(self, frame: RepositoryFrame) -> None:
        if not frame.was_initialized:
            self._deinit_merge_initialized_submodule(frame)
            return
        repo = Path(frame.repo_path)
        if self.ops.merge_in_progress(repo):
            self.ops.abort_merge(repo)
        if frame.original_branch and self.ops.current_branch_name(repo) != frame.original_branch:
            self.ops.checkout_branch(repo, frame.original_branch)
        elif not frame.original_branch and frame.original_head:
            self.ops.checkout_detached(repo, frame.original_head)
        if frame.original_head:
            self.ops.reset_hard(repo, frame.original_head)

    def _deinit_merge_initialized_submodule(self, frame: RepositoryFrame) -> None:
        parent = frame.parent_repo_path
        path = frame.parent_gitlink_path
        if not parent or not path:
            raise self.ops._automation_error(
                f"Abort cannot deinitialize {frame.repo_path}: missing parent gitlink metadata. "
                "The manual merge session was preserved so the repository can be restored manually "
                "and abort retried."
            )
        print(
            f"Abort is deinitializing {path} because this merge initialized it.",
            file=sys.stderr,
        )
        result = self.ops.run_git(
            Path(parent),
            ["submodule", "deinit", "-f", "--", path],
            check=False,
        )
        if int(getattr(result, "returncode", 0)) != 0:
            raise self.ops._automation_error(
                f"Abort could not deinitialize {frame.repo_path}. "
                "The manual merge session was preserved so abort can be retried.\n"
                f"{self.ops._format_git_failure(result)}"
            )

    def _write_pause_report(self, session: MergeSession) -> None:
        report_name = str(session.metadata.get("report_file", "merge_manual_todo.txt"))
        report = Path(session.root_repo_path) / report_name
        lines = [
            "Manual merge paused",
            "===================",
            "",
            f"Session: {session.session_id}",
            f"Target: {session.target_ref}",
            f"Current repository: {session.current_repo_path or 'unknown'}",
            "",
            "Resolve every unmerged path, run git add/git rm, and then run:",
            f"  python -m manual_merge_split resume --repo {session.root_repo_path}",
            "",
            "Do not start another merge, rebase, reset, or switch the root branch.",
            "Prefer not to commit manually; resume can validate an already-created merge commit if necessary.",
            "",
            "Conflicts:",
        ]
        for conflict in session.paused_conflicts:
            detail = f" — {conflict.detail}" if conflict.detail else ""
            lines.append(f"  - [{conflict.disposition}] {conflict.repo_path}:{conflict.path}{detail}")
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _remove_pause_report(self, session: MergeSession) -> None:
        report_name = str(session.metadata.get("report_file", "merge_manual_todo.txt"))
        report = Path(session.root_repo_path) / report_name
        if report.exists() and not self.ops._is_path_tracked(Path(session.root_repo_path), report_name):
            report.unlink()
