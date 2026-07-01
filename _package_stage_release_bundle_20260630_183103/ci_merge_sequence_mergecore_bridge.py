#!/usr/bin/env python3
"""Run ci_merge_sequence.py while each step uses the mergecore bridge."""

from __future__ import annotations

import sys
from pathlib import Path


CI_PROJECT = Path(r"D:\Git\MergeTool_for_CICD")
if str(CI_PROJECT) not in sys.path:
    sys.path.insert(0, str(CI_PROJECT))

import ci_merge_sequence  # type: ignore  # noqa: E402


ci_merge_sequence.CI_MERGE = Path(__file__).resolve().parent / "ci_merge_mergecore_bridge.py"


if __name__ == "__main__":
    raise SystemExit(ci_merge_sequence.main(sys.argv[1:]))
