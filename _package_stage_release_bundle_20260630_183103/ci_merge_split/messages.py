"""CI branch naming and commit-message boundary."""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any

from . import message_builder


@dataclass(frozen=True)
class CiMessageService:
    """Facade for branch names and merge commit messages."""

    ci_module: ModuleType | None

    def __post_init__(self) -> None:
        return None

    def branch_for_depth(self, ctx: Any, depth: int) -> str:
        return message_builder.branch_for_depth(ctx, depth)

    def build_merge_commit_message(self, ctx: Any, **kwargs: Any) -> Any:
        return message_builder.build_merge_commit_message(ctx, **kwargs)

    def root_merge_title(self, ctx: Any, source_sha: str | None = None, target_sha: str | None = None) -> str:
        return message_builder.root_merge_title(ctx, source_sha, target_sha)

    def commit_body(self, fields: list[tuple[str, Any]]) -> str:
        return message_builder.commit_body(fields)

    def compact_label(self, value: str | None, *, max_chars: int = 48, fallback: str = "unknown") -> str:
        return message_builder.compact_label(value, max_chars=max_chars, fallback=fallback)

    def conflict_path_lines(self, resolved_gitlinks: list[dict[str, Any]] | None) -> list[str]:
        return message_builder.conflict_path_lines(resolved_gitlinks)
