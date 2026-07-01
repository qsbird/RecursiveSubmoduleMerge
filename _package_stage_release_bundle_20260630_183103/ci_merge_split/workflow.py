"""Composition root for split CI workflow services."""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from .messages import CiMessageService
from .publish import CiPublishService
from .report_artifacts import CiReportArtifactsService
from .repository_guards import CiRepositoryGuards
from .snapshot_restore import CiSnapshotRestoreService


@dataclass(frozen=True)
class CiWorkflowServices:
    report_artifacts: CiReportArtifactsService
    guards: CiRepositoryGuards
    snapshots: CiSnapshotRestoreService
    publish: CiPublishService
    messages: CiMessageService

    @classmethod
    def from_module(cls, ci_module: ModuleType) -> "CiWorkflowServices":
        return cls(
            report_artifacts=CiReportArtifactsService(ci_module),
            guards=CiRepositoryGuards(ci_module),
            snapshots=CiSnapshotRestoreService(ci_module),
            publish=CiPublishService(ci_module),
            messages=CiMessageService(ci_module),
        )

    @classmethod
    def from_local(cls) -> "CiWorkflowServices":
        return cls(
            report_artifacts=CiReportArtifactsService(None),
            guards=CiRepositoryGuards(None),
            snapshots=CiSnapshotRestoreService(None),
            publish=CiPublishService(None),
            messages=CiMessageService(None),
        )
