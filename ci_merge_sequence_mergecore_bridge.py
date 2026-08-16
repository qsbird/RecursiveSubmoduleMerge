#!/usr/bin/env python3
"""Run external ``ci_merge_sequence.py`` while each step uses the mergecore bridge."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

from ci_merge_split.legacy import CI_LEGACY_PROJECT_ENVVAR, resolve_ci_project_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validation bridge for exercising the external ci_merge_sequence module against mergecore.",
    )
    parser.add_argument(
        "--ci-project",
        dest="ci_project",
        help=f"Path to the external CI merge project. Overrides {CI_LEGACY_PROJECT_ENVVAR}.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    parsed, passthrough = parser.parse_known_args(argv)
    project_path = resolve_ci_project_path(parsed.ci_project)
    if project_path is None:
        raise RuntimeError(
            "external ci_merge_sequence project is not configured; "
            f"set {CI_LEGACY_PROJECT_ENVVAR} or pass --ci-project explicitly"
        )
    # The external sequence launches the per-repository bridge as a child
    # process, so preserve an explicitly supplied project path across that
    # process boundary.
    os.environ[CI_LEGACY_PROJECT_ENVVAR] = str(project_path)
    if str(project_path) not in sys.path:
        sys.path.insert(0, str(project_path))
    ci_merge_sequence = importlib.import_module("ci_merge_sequence")
    ci_merge_sequence.CI_MERGE = Path(__file__).resolve().parent / "ci_merge_mergecore_bridge.py"
    return ci_merge_sequence.main(passthrough)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
