#!/usr/bin/env python3
"""Compatibility entry point for offline CI merge HTML review generation."""

from __future__ import annotations

from ci_merge_split.review_html import main


if __name__ == "__main__":
    raise SystemExit(main())
