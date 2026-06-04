"""Tkinter window icon helper for the manual desktop GUI."""

from __future__ import annotations

from typing import Any, Callable


WINDOW_ICON_PNG_BASE64 = (
    "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"
    "rkJggg=="
)


def apply_window_icon(
    root: Any,
    *,
    photo_image_factory: Callable[..., Any] | None = None,
) -> bool:
    if photo_image_factory is None:
        import tkinter as tk

        photo_image_factory = tk.PhotoImage
    try:
        image = photo_image_factory(master=root, data=WINDOW_ICON_PNG_BASE64)
        root.iconphoto(True, image)
        root._merge_tool_window_icon = image
    except Exception:  # noqa: BLE001 - icon setup must never block the GUI.
        return False
    return True
