"""Composition root for split manual workflow services."""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from .commands import ManualCommandService
from .cross_submodule_moves import CrossSubmoduleMoveService
from .file_conflicts import ManualFileConflictService
from .guided_commands import ManualGuidedCommandService
from .preflight import ManualPreflightService
from .repository_ops import ManualRepositoryOps


@dataclass(frozen=True)
class ManualWorkflowServices:
    preflight: ManualPreflightService
    file_conflicts: ManualFileConflictService
    cross_submodule_moves: CrossSubmoduleMoveService
    repository_ops: ManualRepositoryOps
    guided_commands: ManualGuidedCommandService
    commands: ManualCommandService

    @classmethod
    def from_module(cls, manual_module: ModuleType) -> "ManualWorkflowServices":
        return cls(
            preflight=ManualPreflightService(manual_module),
            file_conflicts=ManualFileConflictService(manual_module),
            cross_submodule_moves=CrossSubmoduleMoveService(manual_module),
            repository_ops=ManualRepositoryOps(manual_module),
            guided_commands=ManualGuidedCommandService(manual_module),
            commands=ManualCommandService(manual_module),
        )

    @classmethod
    def from_local(cls) -> "ManualWorkflowServices":
        return cls(
            preflight=ManualPreflightService(),
            file_conflicts=ManualFileConflictService(),
            cross_submodule_moves=CrossSubmoduleMoveService(),
            repository_ops=ManualRepositoryOps(),
            guided_commands=ManualGuidedCommandService(),
            commands=ManualCommandService(),
        )
