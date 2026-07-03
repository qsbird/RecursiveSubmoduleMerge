"""Tkinter dialog for confirming and editing publish plan before execution.

This module provides a :class:`PublishConfirmDialog` that displays the
``PublishPlanItem`` entries from a ``PublishResult``, allowing the user to
add or remove items from the push set before confirming execution.
"""

from __future__ import annotations

from tkinter import ttk
from typing import Any

from .publish import PublishPlanItem, PublishResult


LANGUAGE_EN = "en"
LANGUAGE_ZH = "zh"

DIALOG_TEXT: dict[str, dict[str, str]] = {
    "dialog_title": {
        LANGUAGE_EN: "Confirm Publish",
        LANGUAGE_ZH: "确认发布",
    },
    "label_repos_to_push": {
        LANGUAGE_EN: "Repositories to push:",
        LANGUAGE_ZH: "待推送仓库：",
    },
    "col_display_path": {
        LANGUAGE_EN: "Path",
        LANGUAGE_ZH: "路径",
    },
    "col_commit": {
        LANGUAGE_EN: "Commit",
        LANGUAGE_ZH: "Commit",
    },
    "col_remote_state": {
        LANGUAGE_EN: "Remote State",
        LANGUAGE_ZH: "远端状态",
    },
    "col_push": {
        LANGUAGE_EN: "Push?",
        LANGUAGE_ZH: "推送？",
    },
    "button_select_all": {
        LANGUAGE_EN: "Select All",
        LANGUAGE_ZH: "全选",
    },
    "button_deselect_all": {
        LANGUAGE_EN: "Deselect All",
        LANGUAGE_ZH: "全不选",
    },
    "button_confirm": {
        LANGUAGE_EN: "Confirm Push",
        LANGUAGE_ZH: "确认推送",
    },
    "button_cancel": {
        LANGUAGE_EN: "Cancel",
        LANGUAGE_ZH: "取消",
    },
    "message_nothing_selected": {
        LANGUAGE_EN: "No repositories selected for push.",
        LANGUAGE_ZH: "没有选中任何仓库进行推送。",
    },
    "status_up_to_date": {
        LANGUAGE_EN: "up to date",
        LANGUAGE_ZH: "已是最新",
    },
    "status_fast_forward": {
        LANGUAGE_EN: "fast forward",
        LANGUAGE_ZH: "快进",
    },
    "status_missing": {
        LANGUAGE_EN: "new branch",
        LANGUAGE_ZH: "新分支",
    },
    "status_unknown": {
        LANGUAGE_EN: "unknown",
        LANGUAGE_ZH: "未知",
    },
}


def _t(key: str, language: str = LANGUAGE_EN, **kwargs: Any) -> str:
    translations = DIALOG_TEXT.get(key)
    if translations is None:
        return key.format(**kwargs)
    template = translations.get(language) or translations[LANGUAGE_EN]
    return template.format(**kwargs)


def _remote_state_display(state: str, language: str = LANGUAGE_EN) -> str:
    mapping = {
        "up_to_date": _t("status_up_to_date", language),
        "fast_forward": _t("status_fast_forward", language),
        "missing": _t("status_missing", language),
    }
    return mapping.get(state, _t("status_unknown", language))


def short_sha(value: str) -> str:
    return value[:12]


class PublishConfirmDialog:
    """Modal dialog showing the publish plan with checkboxes for each item.

    Usage::

        dialog = PublishConfirmDialog(parent, check_result, language="zh")
        parent.wait_window(dialog.top)
        if dialog.confirmed:
            selected = dialog.selected_display_paths()
    """

    def __init__(
        self,
        parent: Any,
        check_result: PublishResult,
        *,
        language: str = LANGUAGE_EN,
    ) -> None:
        self.check_result = check_result
        self.language = language
        self.confirmed = False

        import tkinter as tk

        self.tk = tk
        self.top = tk.Toplevel(parent)
        self.top.title(_t("dialog_title", language))
        self.top.geometry("720x480")
        self.top.transient(parent)
        self.top.grab_set()

        self._push_vars: dict[str, tk.BooleanVar] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        tk = self.tk
        ttk_module = ttk

        main = ttk_module.Frame(self.top, padding=12)
        main.pack(fill="both", expand=True)

        # Label
        ttk_module.Label(main, text=_t("label_repos_to_push", self.language)).pack(anchor="w")

        # Treeview with checkboxes
        tree_frame = ttk_module.Frame(main)
        tree_frame.pack(fill="both", expand=True, pady=(8, 0))

        columns = ("display_path", "commit", "remote_state")
        self.tree = ttk_module.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="none",
        )
        self.tree.heading("display_path", text=_t("col_display_path", self.language))
        self.tree.heading("commit", text=_t("col_commit", self.language))
        self.tree.heading("remote_state", text=_t("col_remote_state", self.language))
        self.tree.column("display_path", width=300, minwidth=200)
        self.tree.column("commit", width=120, minwidth=80)
        self.tree.column("remote_state", width=120, minwidth=80)

        scrollbar = ttk_module.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Checkbox column via a separate frame below the tree
        check_frame = ttk_module.Frame(main)
        check_frame.pack(fill="x", pady=(8, 0))

        # Insert items
        for item in self.check_result.items:
            remote_display = _remote_state_display(item.remote_state, self.language)
            iid = self.tree.insert(
                "",
                "end",
                values=(item.display_path, short_sha(item.desired_sha), remote_display),
            )
            var = tk.BooleanVar(value=not item.skipped)
            self._push_vars[item.display_path] = var

            cb = ttk_module.Checkbutton(
                check_frame,
                text=f"{item.display_path} ({remote_display})",
                variable=var,
            )
            cb.pack(anchor="w")

        # Select All / Deselect All buttons
        btn_frame = ttk_module.Frame(main)
        btn_frame.pack(fill="x", pady=(8, 0))

        ttk_module.Button(btn_frame, text=_t("button_select_all", self.language), command=self._select_all).pack(
            side="left", padx=(0, 8)
        )
        ttk_module.Button(btn_frame, text=_t("button_deselect_all", self.language), command=self._deselect_all).pack(
            side="left"
        )

        # Confirm / Cancel buttons
        action_frame = ttk_module.Frame(main)
        action_frame.pack(fill="x", pady=(12, 0))

        ttk_module.Button(action_frame, text=_t("button_confirm", self.language), command=self._confirm).pack(
            side="right", padx=(8, 0)
        )
        ttk_module.Button(action_frame, text=_t("button_cancel", self.language), command=self._cancel).pack(
            side="right"
        )

    def _select_all(self) -> None:
        for var in self._push_vars.values():
            var.set(True)

    def _deselect_all(self) -> None:
        for var in self._push_vars.values():
            var.set(False)

    def _confirm(self) -> None:
        if not self.selected_display_paths():
            from tkinter import messagebox

            messagebox.showwarning(
                _t("dialog_title", self.language),
                _t("message_nothing_selected", self.language),
                parent=self.top,
            )
            return
        self.confirmed = True
        self.top.destroy()

    def _cancel(self) -> None:
        self.confirmed = False
        self.top.destroy()

    def selected_display_paths(self) -> set[str]:
        return {
            display_path
            for display_path, var in self._push_vars.items()
            if var.get()
        }