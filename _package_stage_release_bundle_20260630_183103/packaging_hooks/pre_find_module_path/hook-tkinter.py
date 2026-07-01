"""Do not let PyInstaller exclude tkinter for this local packaging layout."""

from __future__ import annotations


def pre_find_module_path(hook_api):
    return None
