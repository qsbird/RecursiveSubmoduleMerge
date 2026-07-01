#!/usr/bin/env python3
"""Compatibility entry point for CI merge review commands."""

from __future__ import annotations

from ci_merge_split.review import main


if __name__ == "__main__":
    raise SystemExit(main())
