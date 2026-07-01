"""Generate a self-contained offline HTML review page for CI merge reports."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

from .review_common import (
    CONFLICT_STAGE_LABELS,
    build_risk_items,
    format_size,
    is_probably_text,
    line_count,
    read_artifact_text,
    read_git_blob,
    read_git_tree_file,
    read_json,
    read_text,
    render_original_repository_diff,
    report_dir_from_report,
    resolve_expanded_patch_path,
    resolve_patch_path,
    safe_get,
    short_sha,
    text_size_bytes,
    tree_file_exists,
    write_text,
)


DEFAULT_DIFF_CONTEXT_LINES = 3
DEFAULT_EXPANDED_DIFF_CONTEXT_LINES = 20


CSS = """
:root {
  --bg: #eef2f7;
  --surface: #ffffff;
  --surface-soft: #f8fafc;
  --surface-code: #0f172a;
  --text: #111827;
  --muted: #667085;
  --border: #cbd5e1;
  --border-strong: #94a3b8;
  --accent: #2563eb;
  --accent-soft: #dbeafe;
  --danger: #b42318;
  --warn: #b54708;
  --ok: #027a48;
  --add-bg: #e7f8ed;
  --add-line: #cfefdc;
  --add-text: #14532d;
  --del-bg: #fdecec;
  --del-line: #f8d2d2;
  --del-text: #7f1d1d;
  --hunk-bg: #eaf2ff;
  --hunk-text: #1d4ed8;
  --meta-bg: #f1f5f9;
  --focus: #f97316;
  --sidebar-width: 340px;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: "Segoe UI", Arial, sans-serif;
  line-height: 1.5;
}
header {
  padding: 18px 28px;
  background: #20283a;
  color: #fff;
}
header h1 { margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }
header .report-line { margin: 2px 0 4px; }
header .report-link {
  display: inline-block;
  max-width: min(100%, 980px);
  padding: 2px 6px;
  color: #1d4ed8;
  background: #f8fafc;
  border: 1px solid #93c5fd;
  border-radius: 5px;
  font-family: Consolas, "Cascadia Mono", monospace;
  font-size: 13px;
  line-height: 1.35;
  overflow-wrap: anywhere;
  text-decoration: none;
  vertical-align: middle;
}
header .report-link:hover, header .report-link:focus {
  background: #eff6ff;
  border-color: #60a5fa;
  text-decoration: underline;
  outline: none;
}
header .muted { color: #cbd5e1; }
main { padding: 18px 28px 40px; }
a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 3px; }
code, pre { font-family: Consolas, "Cascadia Mono", monospace; }
code {
  padding: 2px 5px;
  border-radius: 4px;
  background: #eef2f7;
}
.panel, .metric, .repo-section, .file-view, .conflict-card, .command-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.panel { padding: 14px; margin: 14px 0; }
.command-panel { padding: 0; }
.command-panel > summary {
  cursor: pointer;
  padding: 14px;
  font-size: 20px;
  font-weight: 700;
}
.command-panel > summary .muted {
  margin-left: 8px;
  font-size: 13px;
  font-weight: 400;
}
.command-panel-body { padding: 0 14px 14px; }
.panel h2, .repo-header h2 { letter-spacing: 0; }
.review-bar {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  padding: 10px 14px;
  margin: -18px -28px 16px;
  background: rgba(255, 255, 255, 0.94);
  border-bottom: 1px solid var(--border);
  backdrop-filter: blur(10px);
}
.review-bar strong { font-size: 14px; }
.review-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.review-button, .segmented button {
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: #fff;
  color: var(--text);
  font: inherit;
  cursor: pointer;
}
.review-button:hover, .segmented button:hover { border-color: var(--accent); }
.segmented {
  display: inline-flex;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: #fff;
}
.segmented button {
  border: 0;
  border-radius: 0;
  border-right: 1px solid var(--border);
}
.segmented button:last-child { border-right: 0; }
.segmented button[aria-pressed="true"] {
  background: var(--accent);
  color: #fff;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
}
.metric { padding: 12px; }
.metric span { display: block; color: var(--muted); font-size: 12px; }
.metric strong { display: block; font-size: 22px; margin-top: 4px; }
.layout {
  display: grid;
  grid-template-columns: minmax(260px, var(--sidebar-width)) 8px minmax(0, 1fr);
  column-gap: 12px;
  align-items: start;
}
.review-content { min-width: 0; }
.sidebar {
  position: sticky;
  top: 58px;
  max-height: calc(100vh - 68px);
  overflow: auto;
}
.sidebar-resizer {
  position: sticky;
  top: 58px;
  height: calc(100vh - 68px);
  border-radius: 999px;
  cursor: col-resize;
  background: transparent;
}
.sidebar-resizer::before {
  content: "";
  display: block;
  width: 3px;
  height: 100%;
  margin: 0 auto;
  border-radius: 999px;
  background: transparent;
}
.sidebar-resizer:hover::before,
.sidebar-resizer:focus::before,
body.sidebar-resizing .sidebar-resizer::before {
  background: var(--border-strong);
}
.sidebar-resizer:focus { outline: 2px solid var(--accent); outline-offset: 2px; }
.nav-list { display: grid; gap: 8px; }
.nav-link {
  display: grid;
  gap: 4px;
  padding: 9px 10px;
  color: var(--text);
  text-decoration: none;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
}
.nav-link:hover, .nav-link:focus { border-color: var(--accent); outline: none; }
.nav-link strong { overflow-wrap: anywhere; word-break: break-word; }
.nav-link span { color: var(--muted); font-size: 12px; }
.nav-file {
  margin-left: 12px;
  border-left: 3px solid var(--accent-soft);
}
.nav-gitlink {
  border-left-color: var(--warn);
  background: #fffaf0;
}
.nav-group {
  margin-top: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
}
.nav-group > summary {
  cursor: pointer;
  padding: 9px 10px;
  font-weight: 700;
}
.nav-group-body {
  display: grid;
  gap: 8px;
  padding: 0 8px 8px;
}
.toolbar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.toolbar input {
  min-width: 260px;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.muted { color: var(--muted); }
.risk { margin: 8px 0; }
.risk-high strong { color: var(--danger); }
.risk-medium strong { color: var(--warn); }
.risk-info strong { color: var(--ok); }
.repo-section { margin: 0 0 18px; overflow: hidden; }
.repo-header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, #fff, #f8fafc);
}
.repo-header h2 { margin: 0; font-size: 19px; }
.repo-meta { text-align: right; color: var(--muted); font-size: 13px; }
.repo-meta span { display: block; }
.repo-body { padding: 14px 16px 16px; }
.details-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
table { width: 100%; border-collapse: collapse; }
th, td { border-top: 1px solid var(--border); padding: 8px; text-align: left; vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
.stat, .raw-json {
  padding: 10px;
  background: var(--surface-soft);
  border: 1px solid var(--border);
  border-radius: 6px;
}
.file-view, .gitlink-section, .patch-file, .conflict-card, .command-card { margin: 12px 0; overflow: hidden; }
.file-view summary, .gitlink-section summary, .conflict-card summary, .command-card summary {
  cursor: pointer;
  padding: 10px 12px;
  font-weight: 600;
  background: var(--surface-soft);
  border-bottom: 1px solid var(--border);
}
.patch-file, .gitlink-section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.patch-file-header {
  display: grid;
  gap: 4px;
  padding: 10px 12px;
  background: var(--surface-soft);
  border-bottom: 1px solid var(--border);
}
.patch-file-title {
  display: flex;
  gap: 8px;
  align-items: center;
  font-weight: 600;
}
.patch-file-meta {
  color: var(--muted);
  font-size: 12px;
}
.file-columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  padding: 12px;
}
.conflict-columns {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  padding: 12px;
}
.review-pane h4, .file-column h4, .conflict-column h4 {
  margin: 0 0 6px;
  font-size: 13px;
  color: var(--muted);
}
.tag {
  display: inline-block;
  min-width: 24px;
  padding: 1px 6px;
  border-radius: 999px;
  background: #eef2f7;
  color: #344054;
  text-align: center;
  font-size: 12px;
  font-weight: 700;
}
.warning {
  padding: 10px 12px;
  margin: 10px 0;
  border: 1px solid #fed7aa;
  border-radius: 8px;
  background: #fff7ed;
  color: #9a3412;
}
.rd-diff-file {
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  background: #fff;
}
.rd-diff-toolbar {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  padding: 8px;
  border-bottom: 1px solid var(--border);
  background: #f6f8fa;
}
.rd-diff-toolbar .segmented button[aria-pressed="true"] {
  background: #1f6feb;
  color: #fff;
}
.rd-context-switcher {
  display: grid;
  gap: 8px;
}
.rd-context-toolbar {
  display: flex;
  justify-content: flex-end;
  padding: 8px 8px 0;
}
.rd-diff-workbench[data-view="unified"] .rd-parallel-view { display: none; }
.rd-diff-workbench[data-view="split"] .rd-inline-view { display: none; }
.rd-text-view-root {
  width: 100%;
  border-collapse: collapse;
  table-layout: auto;
  font-family: Consolas, "Cascadia Mono", monospace;
  font-size: 12.5px;
  line-height: 1.45;
}
.rd-text-view-root td,
.rd-text-view-root th {
  border: 0;
  padding: 0;
  vertical-align: top;
}
.rd-text-view-scroll {
  max-height: 680px;
  overflow: auto;
}
.rd-text-view-content {
  min-width: max-content;
}
.rd-hunk-header {
  background: #f0f5ff;
  color: #315a9f;
  font-weight: 600;
}
.rd-hunk-header-buttons {
  width: 92px;
  border-right: 1px solid #d8e2f2 !important;
  text-align: center;
}
.rd-hunk-header-content {
  padding: 3px 10px !important;
  white-space: pre;
}
.rd-hunk-lines {
  border-top: 1px solid #eef2f7;
}
.rd-line-number {
  width: 46px;
  min-width: 46px;
  padding: 0 8px !important;
  color: #6b7280;
  background: #f8fafc;
  border-right: 1px solid #e5e7eb !important;
  text-align: right;
  user-select: none;
}
.rd-line-number a,
.rd-line-number span {
  display: block;
  color: inherit;
  text-decoration: none;
}
.rd-line-number-empty {
  background: #f3f4f6;
}
.rd-line-content {
  min-width: 560px;
  background: #fff;
}
.rd-line-content-old,
.rd-line-content-new {
  min-width: 360px;
}
.rd-line-text {
  margin: 0;
  padding: 1px 10px;
  white-space: pre;
  word-break: normal;
}
.wrap-enabled .rd-line-text {
  white-space: pre-wrap;
  word-break: break-word;
}
.rd-line-number[data-change="added"] {
  background: #ccf0d8;
  color: #14532d;
}
.rd-line-content[data-change="added"] {
  background: #e6ffed;
  color: #14532d;
}
.rd-line-number[data-change="removed"] {
  background: #ffd7d5;
  color: #7f1d1d;
}
.rd-line-content[data-change="removed"] {
  background: #ffebe9;
  color: #7f1d1d;
}
.rd-line-number[data-change="meta"],
.rd-line-content[data-change="meta"] {
  background: #f1f5f9;
  color: #475569;
}
.rd-parallel-view .rd-hunk-header-buttons {
  width: 46px;
}
.rd-code-viewer {
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  background: #fff;
}
.rd-code-viewer .rd-text-view-scroll {
  max-height: 560px;
}
.rd-code-viewer .rd-line-number {
  width: 56px;
  min-width: 56px;
}
.rd-code-viewer .rd-line-content {
  min-width: 420px;
}
.review-checklist {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 8px;
}
.review-check {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
}
.rd-diff-meta {
  color: var(--muted);
  font-size: 12px;
}
.hidden { display: none !important; }
@media (max-width: 900px) {
  header, main { padding-left: 16px; padding-right: 16px; }
  .review-bar { margin-left: -16px; margin-right: -16px; align-items: flex-start; flex-direction: column; }
  .layout, .details-grid, .file-columns, .conflict-columns { grid-template-columns: 1fr; }
  .sidebar { position: static; max-height: none; }
  .sidebar-resizer { display: none; }
  .repo-header { flex-direction: column; }
  .repo-meta { text-align: left; }
  .toolbar input { min-width: 100%; }
}
"""


SCRIPT = """
(function () {
  function setDiffView(workbench, view) {
    workbench.setAttribute('data-view', view);
    workbench.querySelectorAll('.js-view-toggle').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.getAttribute('data-view') === view));
    });
  }

  function setPatchContext(switcher, context) {
    switcher.setAttribute('data-context', context);
    switcher.querySelectorAll('.js-context-toggle').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.getAttribute('data-context') === context));
    });
    switcher.querySelectorAll('[data-context-panel]').forEach(function (panel) {
      panel.classList.toggle('hidden', panel.getAttribute('data-context-panel') !== context);
    });
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function setupSidebarResize() {
    const layout = document.querySelector('.layout');
    const resizer = document.querySelector('.sidebar-resizer');
    if (!layout || !resizer) {
      return;
    }
    const storageKey = 'ci-review-sidebar-width';
    const minWidth = 260;
    function maxWidth() {
      return Math.max(minWidth, Math.min(620, Math.floor(window.innerWidth * 0.55)));
    }
    function setWidth(value, persist) {
      const width = clamp(Math.round(value), minWidth, maxWidth());
      document.documentElement.style.setProperty('--sidebar-width', width + 'px');
      resizer.setAttribute('aria-valuemin', String(minWidth));
      resizer.setAttribute('aria-valuemax', String(maxWidth()));
      resizer.setAttribute('aria-valuenow', String(width));
      if (persist) {
        try {
          window.localStorage.setItem(storageKey, String(width));
        } catch (_error) {
          // Ignore storage failures in offline HTML contexts.
        }
      }
    }
    try {
      const saved = Number(window.localStorage.getItem(storageKey));
      if (Number.isFinite(saved) && saved > 0) {
        setWidth(saved, false);
      } else {
        setWidth(340, false);
      }
    } catch (_error) {
      setWidth(340, false);
    }
    window.addEventListener('resize', function () {
      const current = Number(resizer.getAttribute('aria-valuenow')) || 340;
      setWidth(current, false);
    });
    resizer.addEventListener('pointerdown', function (event) {
      if (window.innerWidth <= 900) {
        return;
      }
      event.preventDefault();
      resizer.setPointerCapture(event.pointerId);
      document.body.classList.add('sidebar-resizing');
      function onMove(moveEvent) {
        const bounds = layout.getBoundingClientRect();
        setWidth(moveEvent.clientX - bounds.left, true);
      }
      function onUp(upEvent) {
        document.body.classList.remove('sidebar-resizing');
        resizer.releasePointerCapture(upEvent.pointerId);
        resizer.removeEventListener('pointermove', onMove);
        resizer.removeEventListener('pointerup', onUp);
        resizer.removeEventListener('pointercancel', onUp);
      }
      resizer.addEventListener('pointermove', onMove);
      resizer.addEventListener('pointerup', onUp);
      resizer.addEventListener('pointercancel', onUp);
    });
    resizer.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
        return;
      }
      event.preventDefault();
      const current = Number(resizer.getAttribute('aria-valuenow')) || 340;
      setWidth(current + (event.key === 'ArrowRight' ? 24 : -24), true);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    setupSidebarResize();

    const filter = document.getElementById('file-filter');
    if (filter) {
      filter.addEventListener('input', function () {
        const query = filter.value.trim().toLowerCase();
        document.querySelectorAll('[data-filter-key]').forEach(function (node) {
          const key = node.getAttribute('data-filter-key') || '';
          node.classList.toggle('hidden', Boolean(query) && !key.includes(query));
        });
      });
    }

    document.querySelectorAll('.rd-diff-workbench').forEach(function (workbench) {
      setDiffView(workbench, workbench.getAttribute('data-view') || 'unified');
    });
    document.querySelectorAll('.rd-context-switcher').forEach(function (switcher) {
      setPatchContext(switcher, switcher.getAttribute('data-context') || 'compact');
    });
    document.querySelectorAll('.js-view-toggle').forEach(function (button) {
      button.addEventListener('click', function () {
        const workbench = button.closest('.rd-diff-workbench');
        if (workbench) {
          setDiffView(workbench, button.getAttribute('data-view') || 'unified');
        }
      });
    });
    document.querySelectorAll('.js-context-toggle').forEach(function (button) {
      button.addEventListener('click', function () {
        const switcher = button.closest('.rd-context-switcher');
        if (switcher) {
          setPatchContext(switcher, button.getAttribute('data-context') || 'compact');
        }
      });
    });

    const wrapButton = document.querySelector('.js-wrap-toggle');
    if (wrapButton) {
      wrapButton.addEventListener('click', function () {
        const enabled = !document.body.classList.contains('wrap-enabled');
        document.body.classList.toggle('wrap-enabled', enabled);
        wrapButton.setAttribute('aria-pressed', String(enabled));
      });
    }

    document.querySelectorAll('[data-details-action]').forEach(function (button) {
      button.addEventListener('click', function () {
        const open = button.getAttribute('data-details-action') === 'expand';
        document.querySelectorAll('details.nav-group, details.file-view, details.gitlink-section, details.conflict-card, details.command-panel, details.command-card').forEach(function (details) {
          details.open = open;
        });
      });
    });

    document.querySelectorAll('a[href^="#"]').forEach(function (link) {
      link.addEventListener('click', function () {
        const id = link.getAttribute('href').slice(1);
        const target = document.getElementById(id);
        const parentDetails = target ? target.closest('details') : null;
        if (parentDetails) {
          parentDetails.open = true;
        }
        if (target && target.tagName === 'DETAILS') {
          target.open = true;
        }
      });
    });
  });
})();
"""


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def file_language(path: str | None) -> str:
    suffix = Path(path or "").suffix.lower()
    if suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx", ".cu", ".cuh"}:
        return "cpp"
    if suffix in {".py", ".pyw"}:
        return "python"
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return "javascript"
    if suffix in {".json", ".lock"}:
        return "json"
    if suffix in {".xml", ".html", ".htm", ".xaml", ".props", ".targets"}:
        return "xml"
    if suffix in {".yml", ".yaml"}:
        return "yaml"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".css", ".scss", ".less"}:
        return "css"
    return "text"


def changed_file_path(changed_file: dict[str, Any], fallback: str) -> str:
    return str(changed_file.get("path") or changed_file.get("old_path") or fallback)


def path_leaf(value: Any) -> str:
    text = str(value or "").rstrip("/\\")
    if not text:
        return ""
    return re.split(r"[\\/]+", text)[-1]


def repository_display_name(repo: dict[str, Any]) -> str:
    report_path = str(repo.get("path") or "")
    if report_path and report_path != ".":
        return report_path
    repo_name = path_leaf(repo.get("repo_path"))
    return f"{repo_name} (root)" if repo_name else "root repository"


def patch_line_class(line: str) -> str:
    if line == "" or line.startswith("\\ No newline"):
        return "note"
    if line.startswith("@@"):
        return "hunk"
    if line.startswith(("diff --git", "index ", "--- ", "+++ ", "Binary files ")):
        return "meta"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    return "ctx"


PATCH_HUNK_RE = re.compile(r"@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")
GITLINK_PATCH_RE = re.compile(
    r"(?m)^(?:index [0-9a-f]+\.\.[0-9a-f]+ 160000|[-+]Subproject commit [0-9a-f]+)"
)


def is_gitlink_patch_text(text: str | None) -> bool:
    return bool(GITLINK_PATCH_RE.search(text or ""))


def is_gitlink_changed_file(changed_file: dict[str, Any], patch_view: dict[str, Any] | None = None) -> bool:
    kind = str(changed_file.get("kind") or changed_file.get("type") or "").lower()
    if kind in {"gitlink", "gitlink_update", "submodule", "submodule_pointer"}:
        return True
    if "160000" in {
        str(changed_file.get("mode") or ""),
        str(changed_file.get("old_mode") or ""),
        str(changed_file.get("new_mode") or ""),
    }:
        return True
    if patch_view and is_gitlink_patch_text(str(patch_view.get("patch_text") or "")):
        return True
    return False


def collect_gitlink_file_indices(
    repo: dict[str, Any],
    file_patch_views: list[dict[str, Any]],
) -> list[int]:
    patch_views = {view.get("index"): view for view in file_patch_views}
    indices: list[int] = []
    for index, changed_file in enumerate(repo.get("changed_files") or [], start=1):
        if is_gitlink_changed_file(changed_file, patch_views.get(index)):
            indices.append(index)
    return indices


def gitlink_index_set(repo: dict[str, Any]) -> set[int]:
    return {int(index) for index in repo.get("gitlink_file_indices") or []}


def gitlink_count(repo: dict[str, Any]) -> int:
    return len(gitlink_index_set(repo))


def review_path_key(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def review_repo_key(value: Any) -> str:
    return review_path_key(value).casefold()


def rendered_gitlink_conflict_keys(repositories: list[dict[str, Any]]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for repo in repositories:
        repo_keys = {
            review_repo_key(repo.get("repo_path")),
            review_repo_key(repo.get("path")),
        }
        repo_keys.discard("")
        changed_files = repo.get("changed_files") or []
        for index in gitlink_index_set(repo):
            if index < 1 or index > len(changed_files):
                continue
            path = review_path_key(changed_file_path(changed_files[index - 1], f"file-{index}"))
            if not path:
                continue
            for repo_key in repo_keys:
                keys.add((repo_key, path))
    return keys


def is_gitlink_conflict_record(conflict: dict[str, Any]) -> bool:
    conflict_type = str(conflict.get("type") or conflict.get("kind") or "").lower()
    return conflict_type in {"gitlink_conflict", "gitlink"}


def is_rendered_gitlink_conflict(
    conflict_view: dict[str, Any],
    gitlink_keys: set[tuple[str, str]],
) -> bool:
    conflict = conflict_view.get("conflict") or {}
    if not is_gitlink_conflict_record(conflict):
        return False
    repo_key = review_repo_key(conflict.get("repo_path"))
    path = review_path_key(conflict.get("path"))
    return bool(repo_key and path and (repo_key, path) in gitlink_keys)


def filter_rendered_gitlink_conflicts(
    conflicts: list[dict[str, Any]],
    repositories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    gitlink_keys = rendered_gitlink_conflict_keys(repositories)
    if not gitlink_keys:
        return conflicts
    return [
        conflict
        for conflict in conflicts
        if not is_rendered_gitlink_conflict(conflict, gitlink_keys)
    ]


def html_token(value: str | None) -> str:
    text = str(value or "text").lower()
    token = "".join(ch if ch.isalnum() or ch in {"-"} else "-" for ch in text)
    return token.strip("-") or "text"


def display_lines(text: str) -> list[str]:
    if text == "":
        return []
    return text.splitlines()


def rd_change_type(kind: str) -> str:
    if kind == "add":
        return "added"
    if kind == "del":
        return "removed"
    if kind in {"meta", "note"}:
        return "meta"
    return "context"


def render_code_editor(
    text: str | None,
    *,
    language: str = "text",
    empty_text: str = "<empty>",
    aria_label: str = "Code",
    extra_class: str = "",
) -> str:
    value = text if text not in {None, ""} else empty_text
    lines = display_lines(str(value)) or [""]
    rendered_lines = []
    for number, line in enumerate(lines, start=1):
        rendered_lines.append(
            '<tr class="rd-hunk-lines rd-code-lines">'
            f'<td class="rd-line-number"><span>{number}</span></td>'
            '<td class="rd-line-content">'
            f'<pre class="rd-line-text">{esc(line)}</pre>'
            "</td>"
            "</tr>"
        )
    language_class = html_token(language)
    class_text = f"rd-code-viewer rd-lang-{language_class}"
    if extra_class:
        class_text += f" {extra_class}"
    return (
        f'<div class="{esc(class_text)}" data-language="{esc(language)}" role="region" aria-label="{esc(aria_label)}">'
        '<div class="rd-text-view-scroll"><table class="rd-text-view-root">'
        '<tbody class="rd-text-view-content">'
        f"{''.join(rendered_lines)}"
        "</tbody></table></div></div>"
    )


def parse_patch_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    old_line: int | None = None
    new_line: int | None = None
    for index, raw_line in enumerate(display_lines(text), start=1):
        kind = patch_line_class(raw_line)
        old_number: int | str = ""
        new_number: int | str = ""
        sign = ""
        code = raw_line

        if kind == "hunk":
            match = PATCH_HUNK_RE.search(raw_line)
            if match:
                old_line = int(match.group("old"))
                new_line = int(match.group("new"))
        elif kind == "ctx":
            if raw_line.startswith(" "):
                code = raw_line[1:]
                if old_line is not None:
                    old_number = old_line
                    old_line += 1
                if new_line is not None:
                    new_number = new_line
                    new_line += 1
            else:
                kind = "meta"
        elif kind == "del":
            sign = "-"
            code = raw_line[1:] if raw_line.startswith("-") else raw_line
            if old_line is not None:
                old_number = old_line
                old_line += 1
        elif kind == "add":
            sign = "+"
            code = raw_line[1:] if raw_line.startswith("+") else raw_line
            if new_line is not None:
                new_number = new_line
                new_line += 1

        rows.append(
            {
                "index": index,
                "raw": raw_line,
                "code": code,
                "kind": kind,
                "old_number": old_number,
                "new_number": new_number,
                "sign": sign,
            }
        )
    return rows


def render_patch_unified(rows: list[dict[str, Any]]) -> str:
    rendered_rows = []
    for row in rows:
        change = rd_change_type(str(row["kind"]))
        if row["kind"] == "hunk":
            rendered_rows.append(
                '<tr class="rd-hunk-header rd-hunk-header-inline" data-hunk-header="true">'
                '<td class="rd-hunk-header-buttons" colspan="2"></td>'
                f'<td class="rd-hunk-header-content"><pre class="rd-line-text">{esc(row["raw"])}</pre></td>'
                "</tr>"
            )
            continue
        rendered_rows.append(
            f'<tr class="rd-hunk-lines rd-hunk-lines-inline" data-change="{esc(change)}">'
            f'<td class="rd-line-number" data-change="{esc(change)}" data-position="old"><span>{esc(row["old_number"])}</span></td>'
            f'<td class="rd-line-number" data-change="{esc(change)}" data-position="new"><span>{esc(row["new_number"])}</span></td>'
            f'<td class="rd-line-content" data-change="{esc(change)}">'
            f'<pre class="rd-line-text">{esc(row["code"])}</pre>'
            "</td>"
            "</tr>"
        )
    return (
        '<div class="rd-inline-view" data-view-mode="unified" role="region" '
        'aria-label="Unified diff">'
        '<div class="rd-text-view-scroll"><table class="rd-text-view-root">'
        '<tbody class="rd-text-view-content">'
        f"{''.join(rendered_rows)}"
        "</tbody></table></div></div>"
    )


def render_parallel_number_cell(row: dict[str, Any], side: str, change: str, blank: bool) -> str:
    number = row["old_number"] if side == "old" else row["new_number"]
    classes = "rd-line-number"
    if blank:
        classes += " rd-line-number-empty"
    return (
        f'<td class="{esc(classes)}" data-change="{esc(change)}" data-position="{esc(side)}">'
        f'<span>{esc("" if blank else number)}</span>'
        "</td>"
    )


def render_parallel_content_cell(row: dict[str, Any], side: str, change: str, blank: bool) -> str:
    position = "old" if side == "old" else "new"
    classes = f"rd-line-content rd-line-content-{position}"
    if blank:
        classes += " rd-line-number-empty"
    code = "" if blank else str(row["code"])
    return (
        f'<td class="{esc(classes)}" data-change="{esc(change)}" data-position="{esc(position)}">'
        f'<pre class="rd-line-text">{esc(code)}</pre>'
        "</td>"
    )


def render_split_pane(row: dict[str, Any], side: str) -> str:
    kind = str(row["kind"])
    is_old = side == "old"
    blank = (kind == "add" and is_old) or (kind == "del" and not is_old)
    pane_kind = "context"
    if kind == "add" and not is_old:
        pane_kind = "added"
    elif kind == "del" and is_old:
        pane_kind = "removed"
    return render_parallel_number_cell(row, side, pane_kind, blank) + render_parallel_content_cell(
        row,
        side,
        pane_kind,
        blank,
    )


def render_patch_split(rows: list[dict[str, Any]]) -> str:
    rendered_rows = []
    for row in rows:
        kind = str(row["kind"])
        if kind in {"meta", "hunk", "note"}:
            label_class = "rd-hunk-header" if kind == "hunk" else "rd-hunk-lines"
            change = "context" if kind == "hunk" else "meta"
            rendered_rows.append(
                f'<tr class="{label_class} rd-hunk-header-parallel" data-change="{esc(change)}">'
                '<td class="rd-hunk-header-buttons"></td>'
                f'<td class="rd-hunk-header-content" colspan="3"><pre class="rd-line-text">{esc(row["raw"])}</pre></td>'
                "</tr>"
            )
            continue
        change = rd_change_type(kind)
        rendered_rows.append(
            f'<tr class="rd-hunk-lines rd-hunk-lines-parallel" data-change="{esc(change)}">'
            f'{render_split_pane(row, "old")}'
            f'{render_split_pane(row, "new")}'
            "</tr>"
        )
    return (
        '<div class="rd-parallel-view" data-view-mode="split" role="region" '
        'aria-label="Side by side diff">'
        '<div class="rd-text-view-scroll"><table class="rd-text-view-root">'
        '<tbody class="rd-text-view-content">'
        f"{''.join(rendered_rows)}"
        "</tbody></table></div></div>"
    )


def patch_context_label(context_lines: Any, fallback: str) -> str:
    if context_lines is None or context_lines == "":
        return fallback
    try:
        lines = int(context_lines)
    except (TypeError, ValueError):
        return fallback
    return f"{lines} lines"


def render_single_patch_block(
    text: str,
    *,
    empty_text: str = "No patch content is available.",
    context_label: str | None = None,
) -> str:
    if not text:
        return f'<div class="warning">{esc(empty_text)}</div>'
    rows = parse_patch_rows(text)
    meta = f"{len(rows)} patch lines"
    if context_label:
        meta = f"{meta} - {context_label}"
    return (
        f'<div class="rd-diff-file rd-diff-workbench" data-view="unified" data-line-count="{len(rows)}">'
        '<div class="rd-diff-toolbar">'
        '<div class="segmented" role="group" aria-label="Diff view">'
        '<button type="button" class="js-view-toggle" data-view="unified">Unified</button>'
        '<button type="button" class="js-view-toggle" data-view="split">Split</button>'
        "</div>"
        f'<span class="rd-diff-meta">{esc(meta)}</span>'
        "</div>"
        f"{render_patch_unified(rows)}"
        f"{render_patch_split(rows)}"
        "</div>"
    )


def render_patch_block(
    text: str,
    empty_text: str = "No patch content is available.",
    *,
    expanded_text: str | None = None,
    context_lines: Any = None,
    expanded_context_lines: Any = None,
) -> str:
    compact_label = patch_context_label(context_lines, "compact")
    expanded_label = patch_context_label(expanded_context_lines, "expanded")
    if not expanded_text or expanded_text == text:
        return render_single_patch_block(text, empty_text=empty_text, context_label=compact_label)
    return (
        '<div class="rd-context-switcher" data-context="compact">'
        '<div class="rd-context-toolbar">'
        '<div class="segmented" role="group" aria-label="Diff context">'
        f'<button type="button" class="js-context-toggle" data-context="compact">{esc(compact_label)}</button>'
        f'<button type="button" class="js-context-toggle" data-context="expanded">{esc(expanded_label)}</button>'
        "</div>"
        "</div>"
        '<div data-context-panel="compact">'
        f'{render_single_patch_block(text, empty_text=empty_text, context_label=compact_label)}'
        "</div>"
        '<div data-context-panel="expanded" class="hidden">'
        f'{render_single_patch_block(expanded_text, empty_text=empty_text, context_label=expanded_label)}'
        "</div>"
        "</div>"
    )


def render_text_pre(text: str | None, empty_text: str = "<empty>") -> str:
    return render_code_editor(text, empty_text=empty_text)


def render_json_editor(value: Any, *, aria_label: str = "JSON") -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(value)
    return render_code_editor(text, language="json", empty_text="{}", aria_label=aria_label, extra_class="raw-json")


def read_text_if_available(path: Path | None) -> str | None:
    if not path:
        return None
    return read_text(path)


def context_lines_value(report: dict[str, Any], owner: dict[str, Any], owner_key: str, input_key: str, default: int) -> int:
    value = owner.get(owner_key)
    if value is None:
        value = safe_get(report, "input", input_key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def collect_expanded_patch_text(
    report: dict[str, Any],
    repo: dict[str, Any],
    owner: dict[str, Any],
    report_dir: Path,
    expanded_diff_context_lines: int,
    *,
    file_path: str | None = None,
) -> tuple[str | None, str | None]:
    expanded_patch_path = resolve_expanded_patch_path(report, owner, report_dir)
    expanded_patch_text = read_text_if_available(expanded_patch_path)
    if expanded_patch_text is not None:
        return expanded_patch_text, str(expanded_patch_path)
    original_available, original_diff = render_original_repository_diff(
        repo,
        expanded_diff_context_lines,
        file_path=file_path,
    )
    if original_available:
        return original_diff, "original repository"
    return None, None


def read_changed_file_side(
    report: dict[str, Any],
    repo: dict[str, Any],
    changed_file: dict[str, Any],
    side: str,
    report_dir: Path,
) -> tuple[str, str | None]:
    artifact_record = safe_get(changed_file, "content_artifacts", side)
    artifact_path, artifact_text = read_artifact_text(report, artifact_record, report_dir)
    if artifact_text is not None and is_probably_text(artifact_text):
        return f"artifact {artifact_path}", artifact_text

    ref_key = "ours" if side == "ours" else "result"
    path_key = "old_path" if side == "ours" else "path"
    fallback_path_key = "path" if side == "ours" else "old_path"
    repo_path = Path(repo.get("repo_path") or "")
    ref = repo.get(ref_key)
    file_path = changed_file.get(path_key) or changed_file.get(fallback_path_key)
    if repo_path.exists() and ref and file_path and tree_file_exists(repo_path, str(ref), str(file_path)):
        exit_code, output = read_git_tree_file(repo_path, str(ref), str(file_path))
        if exit_code == 0 and is_probably_text(output):
            return "original repository", output
    return "missing", None


def collect_full_file_views(report: dict[str, Any], repo: dict[str, Any], report_dir: Path) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for index, changed_file in enumerate(repo.get("changed_files") or [], start=1):
        path = changed_file_path(changed_file, f"file-{index}")
        ours_source, ours_text = read_changed_file_side(report, repo, changed_file, "ours", report_dir)
        result_source, result_text = read_changed_file_side(report, repo, changed_file, "result", report_dir)
        if ours_text is None and result_text is None:
            continue
        views.append(
            {
                "index": index,
                "path": path,
                "status": str(changed_file.get("status") or ""),
                "old_path": changed_file.get("old_path"),
                "language": file_language(path),
                "ours": ours_text or "",
                "result": result_text or "",
                "ours_source": ours_source,
                "result_source": result_source,
                "line_count": max(line_count(ours_text), line_count(result_text)),
                "size_bytes": max(text_size_bytes(ours_text), text_size_bytes(result_text)),
            }
        )
    return views


def collect_file_patch_views(
    report: dict[str, Any],
    repo: dict[str, Any],
    report_dir: Path,
    expanded_diff_context_lines: int,
) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for index, changed_file in enumerate(repo.get("changed_files") or [], start=1):
        patch_path = resolve_patch_path(report, changed_file, report_dir)
        patch_text = read_text_if_available(patch_path)
        if patch_text is None:
            continue
        path = changed_file_path(changed_file, f"file-{index}")
        expanded_patch_text, expanded_patch_source = collect_expanded_patch_text(
            report,
            repo,
            changed_file,
            report_dir,
            expanded_diff_context_lines,
            file_path=path,
        )
        views.append(
            {
                "index": index,
                "path": path,
                "status": str(changed_file.get("status") or ""),
                "old_path": changed_file.get("old_path"),
                "patch_text": patch_text,
                "expanded_patch_text": expanded_patch_text,
                "patch_path": str(patch_path) if patch_path else None,
                "patch_relative_path": changed_file.get("patch_relative_path"),
                "expanded_patch_source": expanded_patch_source,
                "expanded_patch_path": changed_file.get("expanded_patch_path"),
                "expanded_patch_relative_path": changed_file.get("expanded_patch_relative_path"),
                "context_lines": context_lines_value(
                    report,
                    changed_file,
                    "patch_context_lines",
                    "diff_context_lines",
                    DEFAULT_DIFF_CONTEXT_LINES,
                ),
                "expanded_context_lines": context_lines_value(
                    report,
                    changed_file,
                    "expanded_patch_context_lines",
                    "expanded_diff_context_lines",
                    expanded_diff_context_lines,
                ),
                "line_count": line_count(patch_text),
                "size_bytes": text_size_bytes(patch_text),
                "expanded_line_count": line_count(expanded_patch_text),
                "expanded_size_bytes": text_size_bytes(expanded_patch_text),
            }
        )
    return views


def collect_repository_patch(
    report: dict[str, Any],
    repo: dict[str, Any],
    report_dir: Path,
    diff_context_lines: int,
    expanded_diff_context_lines: int,
) -> tuple[str, str | None, str, str | None, str | None, list[str], int, int]:
    warnings: list[str] = []
    patch_path = resolve_patch_path(report, repo, report_dir)
    patch_text = read_text_if_available(patch_path)
    if patch_text is not None:
        expanded_patch_text, expanded_patch_source = collect_expanded_patch_text(
            report,
            repo,
            repo,
            report_dir,
            expanded_diff_context_lines,
        )
        return (
            patch_text,
            expanded_patch_text,
            "artifact",
            str(patch_path),
            expanded_patch_source,
            warnings,
            context_lines_value(report, repo, "patch_context_lines", "diff_context_lines", diff_context_lines),
            context_lines_value(
                report,
                repo,
                "expanded_patch_context_lines",
                "expanded_diff_context_lines",
                expanded_diff_context_lines,
            ),
        )

    file_patches: list[str] = []
    expanded_file_patches: list[str] = []
    for changed_file in repo.get("changed_files") or []:
        file_patch_path = resolve_patch_path(report, changed_file, report_dir)
        file_patch_text = read_text_if_available(file_patch_path)
        if file_patch_text is not None:
            file_patches.append(file_patch_text)
        path = changed_file_path(changed_file, "")
        expanded_file_patch_text, _expanded_file_patch_source = collect_expanded_patch_text(
            report,
            repo,
            changed_file,
            report_dir,
            expanded_diff_context_lines,
            file_path=path or None,
        )
        if expanded_file_patch_text is not None:
            expanded_file_patches.append(expanded_file_patch_text)
    if file_patches:
        return (
            "\n".join(file_patches),
            "\n".join(expanded_file_patches) if expanded_file_patches else None,
            "file artifacts",
            None,
            "file artifacts" if expanded_file_patches else None,
            warnings,
            context_lines_value(report, repo, "patch_context_lines", "diff_context_lines", diff_context_lines),
            context_lines_value(
                report,
                repo,
                "expanded_patch_context_lines",
                "expanded_diff_context_lines",
                expanded_diff_context_lines,
            ),
        )

    original_available, original_diff = render_original_repository_diff(repo, diff_context_lines)
    if original_available:
        expanded_available, expanded_diff = render_original_repository_diff(repo, expanded_diff_context_lines)
        return (
            original_diff,
            expanded_diff if expanded_available else None,
            "original repository",
            None,
            "original repository" if expanded_available else None,
            warnings,
            diff_context_lines,
            expanded_diff_context_lines,
        )

    warnings.append(
        f"No patch artifact or original repository diff found for {repo.get('path') or repo.get('repo_path')}. "
        f"Original attempt: {original_diff}"
    )
    return "", None, "missing", None, None, warnings, diff_context_lines, expanded_diff_context_lines


def find_stage(conflict: dict[str, Any], stage_number: str) -> dict[str, str] | None:
    for stage in conflict.get("stages") or []:
        if stage.get("stage") == stage_number:
            return stage
    return None


def read_conflict_stage_content(
    report: dict[str, Any],
    conflict: dict[str, Any],
    stage_number: str,
    report_dir: Path,
) -> tuple[str, str]:
    label = CONFLICT_STAGE_LABELS.get(stage_number, f"stage_{stage_number}")
    artifact_record = safe_get(conflict, "artifacts", "stages", label)
    artifact_path, artifact_text = read_artifact_text(report, artifact_record, report_dir)
    if artifact_text is not None:
        return f"artifact {artifact_path}", artifact_text

    stage = find_stage(conflict, stage_number)
    repo_path_value = conflict.get("repo_path")
    repo_path = Path(repo_path_value or "")
    sha = stage.get("sha") if stage else None
    if repo_path_value and repo_path.exists() and sha:
        exit_code, output = read_git_blob(repo_path, sha)
        if exit_code == 0:
            return "original repository", output
    return "unavailable", f"<unable to load {label} content from original repository or artifact>\n"


def collect_conflict_views(report: dict[str, Any], report_dir: Path) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for index, conflict in enumerate(report.get("conflicts") or [], start=1):
        stages = conflict.get("stages") or []
        if not conflict.get("path") or not stages:
            views.append({"index": index, "conflict": conflict, "has_content": False})
            continue
        base_source, base = read_conflict_stage_content(report, conflict, "1", report_dir)
        ours_source, ours = read_conflict_stage_content(report, conflict, "2", report_dir)
        theirs_source, theirs = read_conflict_stage_content(report, conflict, "3", report_dir)
        worktree_path, worktree_text = read_artifact_text(report, safe_get(conflict, "artifacts", "worktree"), report_dir)
        views.append(
            {
                "index": index,
                "conflict": conflict,
                "has_content": True,
                "path": conflict.get("path"),
                "type": conflict.get("type"),
                "language": file_language(conflict.get("path")),
                "base": base,
                "ours": ours,
                "theirs": theirs,
                "base_source": base_source,
                "ours_source": ours_source,
                "theirs_source": theirs_source,
                "worktree": worktree_text,
                "worktree_source": f"artifact {worktree_path}" if worktree_path else None,
                "line_count": max(line_count(base), line_count(ours), line_count(theirs)),
                "size_bytes": max(text_size_bytes(base), text_size_bytes(ours), text_size_bytes(theirs)),
            }
        )
    return views


def collect_review_model(
    report: dict[str, Any],
    report_path: Path,
    *,
    diff_context_lines: int = DEFAULT_DIFF_CONTEXT_LINES,
    expanded_diff_context_lines: int = DEFAULT_EXPANDED_DIFF_CONTEXT_LINES,
) -> dict[str, Any]:
    report_dir = report_dir_from_report(report, report_path)
    repositories: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, repo in enumerate(report.get("repositories") or [], start=1):
        file_patch_views = collect_file_patch_views(report, repo, report_dir, expanded_diff_context_lines)
        gitlink_file_indices = collect_gitlink_file_indices(repo, file_patch_views)
        if file_patch_views:
            repository_patch_path = resolve_patch_path(report, repo, report_dir)
            patch_text = ""
            expanded_patch_text = None
            patch_source = "file artifacts"
            patch_path = str(repository_patch_path) if repository_patch_path else None
            expanded_patch_path = None
            patch_warnings = []
            patch_context_lines = context_lines_value(
                report,
                repo,
                "patch_context_lines",
                "diff_context_lines",
                diff_context_lines,
            )
            expanded_patch_context_lines = context_lines_value(
                report,
                repo,
                "expanded_patch_context_lines",
                "expanded_diff_context_lines",
                expanded_diff_context_lines,
            )
        else:
            (
                patch_text,
                expanded_patch_text,
                patch_source,
                patch_path,
                expanded_patch_path,
                patch_warnings,
                patch_context_lines,
                expanded_patch_context_lines,
            ) = collect_repository_patch(
                report,
                repo,
                report_dir,
                diff_context_lines,
                expanded_diff_context_lines,
            )
        warnings.extend(patch_warnings)
        repositories.append(
            {
                "index": index,
                "path": repo.get("path") or repo.get("repo_path") or f"repo-{index}",
                "display_name": repository_display_name(repo),
                "repo_path": repo.get("repo_path"),
                "strategy": repo.get("strategy"),
                "base": repo.get("base"),
                "ours": repo.get("ours"),
                "theirs": repo.get("theirs"),
                "result": repo.get("result"),
                "changed_files": repo.get("changed_files") or [],
                "commits": repo.get("commits") or [],
                "diff_stat": repo.get("diff_stat") or "",
                "patch_text": patch_text,
                "expanded_patch_text": expanded_patch_text,
                "patch_source": patch_source,
                "patch_path": patch_path,
                "expanded_patch_path": expanded_patch_path,
                "context_lines": patch_context_lines,
                "expanded_context_lines": expanded_patch_context_lines,
                "file_patch_views": file_patch_views,
                "full_file_views": collect_full_file_views(report, repo, report_dir),
                "gitlink_file_indices": gitlink_file_indices,
            }
        )
    conflicts = collect_conflict_views(report, report_dir)
    visible_conflicts = filter_rendered_gitlink_conflicts(conflicts, repositories)
    return {
        "report": report,
        "report_path": report_path,
        "report_dir": report_dir,
        "repositories": repositories,
        "conflicts": visible_conflicts,
        "warnings": warnings,
        "risks": build_risk_items(report),
    }


def render_metrics(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    cards = [
        ("Status", report.get("status")),
        ("Exit", report.get("exit_code")),
        ("Repos", summary.get("merged_repositories")),
        ("Submodules", summary.get("submodules_processed")),
        ("Recursive", summary.get("submodule_recursive_merges")),
        ("Conflicts", summary.get("conflicts")),
    ]
    return "\n".join(
        f'<div class="metric"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>'
        for label, value in cards
    )


def render_risks(risks: list[dict[str, str]]) -> str:
    return "\n".join(
        f'<li class="risk risk-{esc(risk.get("level"))}"><strong>{esc(risk.get("title"))}</strong><br>'
        f'<span>{esc(risk.get("detail"))}</span></li>'
        for risk in risks
    )


def render_repo_nav(repositories: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    gitlink_rows: list[str] = []
    gitlink_keys: list[str] = []
    for repo in repositories:
        repo_gitlinks = gitlink_index_set(repo)
        code_file_count = len(repo.get("changed_files") or []) - len(repo_gitlinks)
        gitlink_summary = f" - {len(repo_gitlinks)} submodule pointers" if repo_gitlinks else ""
        key = f"repo {repo.get('path')} {repo.get('display_name')} {repo.get('strategy')}".lower()
        rows.append(
            f'<a class="nav-link" data-filter-key="{esc(key)}" href="#repo-{repo["index"]}">'
            f'<strong>{esc(repo.get("display_name") or repo.get("path"))}</strong>'
            f'<span>{esc(repo.get("strategy"))} - {code_file_count} files{esc(gitlink_summary)} - {esc(short_sha(repo.get("result")))}</span>'
            "</a>"
        )
        patch_views = {view.get("index"): view for view in repo.get("file_patch_views") or []}
        full_file_views = {view.get("index"): view for view in repo.get("full_file_views") or []}
        for index, changed_file in enumerate(repo.get("changed_files") or [], start=1):
            path = changed_file_path(changed_file, f"file-{index}")
            status = str(changed_file.get("status") or "")
            patch_view = patch_views.get(index)
            full_file_view = full_file_views.get(index)
            is_gitlink = index in repo_gitlinks
            if patch_view:
                href = f'#patch-{repo["index"]}-{index}'
                detail = f"{status} - patch - {patch_view.get('line_count')} lines"
            elif full_file_view:
                href = f'#file-{repo["index"]}-{index}'
                detail = (
                    f"{status} - {full_file_view.get('language')} - {full_file_view.get('line_count')} lines"
                )
            else:
                href = f'#repo-{repo["index"]}'
                detail = f"{status} - changed file"
            file_key = (
                f"file {repo.get('path')} {repo.get('display_name')} {path} {status}"
            ).lower()
            if is_gitlink:
                file_key = f"{file_key} submodule pointer gitlink"
            item = (
                f'<a class="nav-link nav-file{" nav-gitlink" if is_gitlink else ""}" data-filter-key="{esc(file_key)}" '
                f'href="{esc(href)}">'
                f'<strong>{esc(path)}</strong>'
                f'<span>{esc(detail)}</span>'
                "</a>"
            )
            if is_gitlink:
                gitlink_rows.append(item)
                gitlink_keys.append(file_key)
                continue
            rows.append(item)
    for conflict in conflicts:
        item = conflict.get("conflict") or {}
        key = f"conflict {item.get('path')} {item.get('type')}".lower()
        rows.append(
            f'<a class="nav-link" data-filter-key="{esc(key)}" href="#conflict-{conflict["index"]}">'
            f'<strong>Conflict: {esc(item.get("path") or "unknown")}</strong>'
            f'<span>{esc(item.get("type"))}</span>'
            "</a>"
        )
    if gitlink_rows:
        group_key = "submodule pointer gitlink " + " ".join(gitlink_keys)
        rows.append(
            '<details class="nav-group" data-filter-key="'
            f'{esc(group_key)}">'
            f'<summary>Submodule Pointer Changes <span class="muted">{len(gitlink_rows)}</span></summary>'
            f'<div class="nav-group-body">{"".join(gitlink_rows)}</div>'
            "</details>"
        )
    return "\n".join(rows) or '<p class="muted">No repository or conflict entries.</p>'


def render_changed_files(repo: dict[str, Any]) -> str:
    rows: list[str] = []
    patch_views = {view.get("index"): view for view in repo.get("file_patch_views") or []}
    repo_gitlinks = gitlink_index_set(repo)
    for index, changed_file in enumerate(repo.get("changed_files") or [], start=1):
        if index in repo_gitlinks:
            continue
        path = changed_file_path(changed_file, f"file-{index}")
        patch_label = changed_file.get("patch_relative_path") or changed_file.get("patch_path") or ""
        if index in patch_views:
            patch_cell = f'<a href="#patch-{repo["index"]}-{index}"><code>{esc(patch_label or "patch")}</code></a>'
        elif patch_label:
            patch_cell = f"<code>{esc(patch_label)}</code>"
        else:
            patch_cell = ""
        rows.append(
            "<tr>"
            f'<td><span class="tag">{esc(changed_file.get("status") or "")}</span></td>'
            f"<td><code>{esc(path)}</code></td>"
            f"<td>{esc(changed_file.get('old_path') or '')}</td>"
            f"<td>{patch_cell}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4" class="muted">No source file entries.</td></tr>')
    return (
        "<table><thead><tr><th>Status</th><th>Path</th><th>Old Path</th><th>Patch</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_patch_views(repo: dict[str, Any], *, gitlink_only: bool = False) -> str:
    sections: list[str] = []
    repo_gitlinks = gitlink_index_set(repo)
    for view in repo.get("file_patch_views") or []:
        is_gitlink = int(view.get("index") or 0) in repo_gitlinks
        if is_gitlink != gitlink_only:
            continue
        patch_label = view.get("patch_relative_path") or view.get("patch_path") or "patch artifact"
        expanded_label = view.get("expanded_patch_relative_path") or view.get("expanded_patch_source") or ""
        if expanded_label:
            patch_label = f"{patch_label} / expanded: {expanded_label}"
        patch_block = render_patch_block(
            str(view.get("patch_text") or ""),
            expanded_text=view.get("expanded_patch_text"),
            context_lines=view.get("context_lines"),
            expanded_context_lines=view.get("expanded_context_lines"),
        )
        sections.append(
            f'<section class="patch-file" id="patch-{repo["index"]}-{view["index"]}">'
            '<div class="patch-file-header">'
            f'<div class="patch-file-title"><span class="tag">{esc(view.get("status"))}</span>'
            f'<code>{esc(view.get("path"))}</code></div>'
            f'<div class="patch-file-meta">{esc(view.get("line_count"))} patch lines - '
            f'{format_size(view.get("size_bytes"))} - {esc(patch_label)}</div>'
            "</div>"
            f"{patch_block}"
            "</section>"
        )
    if sections:
        return "\n".join(sections)
    if gitlink_only:
        return '<p class="muted">No submodule pointer patch entries.</p>'
    if repo.get("file_patch_views"):
        return '<p class="muted">No source file patch entries.</p>'
    return render_patch_block(
        str(repo.get("patch_text") or ""),
        expanded_text=repo.get("expanded_patch_text"),
        context_lines=repo.get("context_lines"),
        expanded_context_lines=repo.get("expanded_context_lines"),
    )


def render_gitlink_changes(repo: dict[str, Any]) -> str:
    repo_gitlinks = gitlink_index_set(repo)
    if not repo_gitlinks:
        return ""
    rows: list[str] = []
    patch_views = {view.get("index"): view for view in repo.get("file_patch_views") or []}
    for index, changed_file in enumerate(repo.get("changed_files") or [], start=1):
        if index not in repo_gitlinks:
            continue
        path = changed_file_path(changed_file, f"file-{index}")
        patch_view = patch_views.get(index)
        patch_label = changed_file.get("patch_relative_path") or changed_file.get("patch_path") or ""
        if patch_view:
            patch_cell = f'<a href="#patch-{repo["index"]}-{index}"><code>{esc(patch_label or "patch")}</code></a>'
        elif patch_label:
            patch_cell = f"<code>{esc(patch_label)}</code>"
        else:
            patch_cell = ""
        rows.append(
            "<tr>"
            f'<td><span class="tag">{esc(changed_file.get("status") or "")}</span></td>'
            f"<td><code>{esc(path)}</code></td>"
            f"<td><code>{esc(short_sha(changed_file.get('old_sha')))}</code></td>"
            f"<td><code>{esc(short_sha(changed_file.get('new_sha')))}</code></td>"
            f"<td>{patch_cell}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Status</th><th>Path</th><th>Old SHA</th><th>New SHA</th><th>Patch</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return (
        f'<details class="gitlink-section" id="gitlink-{repo["index"]}">'
        f'<summary>Submodule Pointer Changes <span class="muted">{len(rows)}</span></summary>'
        f'<div class="repo-body">{table}{render_patch_views(repo, gitlink_only=True)}</div>'
        "</details>"
    )


def render_file_views(repo: dict[str, Any]) -> str:
    sections: list[str] = []
    for view in repo.get("full_file_views") or []:
        key = f"file {repo.get('path')} {view.get('path')} {view.get('status')} {view.get('language')}".lower()
        language = str(view.get("language") or "text")
        ours_label = f"Ours {view.get('path')}"
        result_label = f"Result {view.get('path')}"
        sections.append(
            f'<details class="file-view" id="file-{repo["index"]}-{view["index"]}" data-filter-key="{esc(key)}">'
            f'<summary><span class="tag">{esc(view.get("status"))}</span> {esc(view.get("path"))} '
            f'<span class="muted">({esc(view.get("language"))}, {esc(view.get("line_count"))} lines, {format_size(view.get("size_bytes"))})</span></summary>'
            '<div class="file-columns">'
            '<div class="review-pane file-column">'
            f'<h4>Ours - {esc(view.get("ours_source"))}</h4>'
            f'{render_code_editor(view.get("ours"), language=language, aria_label=ours_label)}'
            "</div>"
            '<div class="review-pane file-column">'
            f'<h4>Result - {esc(view.get("result_source"))}</h4>'
            f'{render_code_editor(view.get("result"), language=language, aria_label=result_label)}'
            "</div>"
            "</div>"
            "</details>"
        )
    return "\n".join(sections) or '<p class="muted">No full file content artifacts were available.</p>'


def repo_filter_key(repo: dict[str, Any]) -> str:
    parts = [
        "repo",
        str(repo.get("path") or ""),
        str(repo.get("display_name") or ""),
        str(repo.get("strategy") or ""),
    ]
    for changed_file in repo.get("changed_files") or []:
        parts.append(changed_file_path(changed_file, ""))
        parts.append(str(changed_file.get("old_path") or ""))
        parts.append(str(changed_file.get("status") or ""))
    return " ".join(parts).lower()


def render_repo_section(repo: dict[str, Any]) -> str:
    return f"""
<section class="repo-section" id="repo-{repo["index"]}" data-filter-key="{esc(repo_filter_key(repo))}">
  <div class="repo-header">
    <h2>{esc(repo.get("display_name") or repo.get("path"))}</h2>
    <div class="repo-meta">
      <span>strategy: {esc(repo.get("strategy"))}</span>
      <span>result: <code>{esc(short_sha(repo.get("result")))}</code></span>
      <span>patch source: {esc(repo.get("patch_source"))}</span>
    </div>
  </div>
  <div class="repo-body">
    <div class="details-grid">
      <div>
        <p><strong>Repo path:</strong> <code>{esc(repo.get("repo_path"))}</code></p>
        <p><strong>Base:</strong> <code>{esc(short_sha(repo.get("base")))}</code></p>
        <p><strong>Ours:</strong> <code>{esc(short_sha(repo.get("ours")))}</code></p>
      </div>
      <div>
        <p><strong>Theirs:</strong> <code>{esc(short_sha(repo.get("theirs")))}</code></p>
        <p><strong>Result:</strong> <code>{esc(short_sha(repo.get("result")))}</code></p>
        <p><strong>Patch path:</strong> <code>{esc(repo.get("patch_path") or "N/A")}</code></p>
      </div>
    </div>
    <h3>Diff Stat</h3>
    <pre class="stat">{esc(repo.get("diff_stat") or "N/A")}</pre>
    <h3>Changed Files</h3>
    {render_changed_files(repo)}
    <h3>Patch</h3>
    {render_patch_views(repo)}
    {render_gitlink_changes(repo)}
    <h3>Full File Views</h3>
    {render_file_views(repo)}
  </div>
</section>
"""


def render_conflict_section(conflict_view: dict[str, Any]) -> str:
    conflict = conflict_view.get("conflict") or {}
    key = f"conflict {conflict.get('path')} {conflict.get('type')}".lower()
    header = (
        f'<summary><span class="tag">U</span> {esc(conflict.get("path") or "unknown")} '
        f'<span class="muted">({esc(conflict.get("type"))})</span></summary>'
    )
    if not conflict_view.get("has_content"):
        body = f'<div class="repo-body">{render_json_editor(conflict, aria_label="Conflict record")}</div>'
    else:
        language = str(conflict_view.get("language") or "text")
        path_label = str(conflict.get("path") or "unknown")
        base_label = f"Base {path_label}"
        ours_label = f"Ours {path_label}"
        theirs_label = f"Theirs {path_label}"
        worktree_label = f"Worktree {path_label}"
        worktree = ""
        if conflict_view.get("worktree") is not None:
            worktree = (
                '<div class="repo-body">'
                f'<h3>Worktree - {esc(conflict_view.get("worktree_source"))}</h3>'
                f'{render_code_editor(conflict_view.get("worktree"), language=language, aria_label=worktree_label)}'
                "</div>"
            )
        body = (
            '<div class="conflict-columns">'
            '<div class="review-pane conflict-column">'
            f'<h4>Base - {esc(conflict_view.get("base_source"))}</h4>'
            f'{render_code_editor(conflict_view.get("base"), language=language, aria_label=base_label)}'
            "</div>"
            '<div class="review-pane conflict-column">'
            f'<h4>Ours - {esc(conflict_view.get("ours_source"))}</h4>'
            f'{render_code_editor(conflict_view.get("ours"), language=language, aria_label=ours_label)}'
            "</div>"
            '<div class="review-pane conflict-column">'
            f'<h4>Theirs - {esc(conflict_view.get("theirs_source"))}</h4>'
            f'{render_code_editor(conflict_view.get("theirs"), language=language, aria_label=theirs_label)}'
            "</div>"
            "</div>"
            f"{worktree}"
        )
    return f'<details class="conflict-card" id="conflict-{conflict_view["index"]}" data-filter-key="{esc(key)}">{header}{body}</details>'


def render_commands(report: dict[str, Any]) -> str:
    sections: list[str] = []
    for index, command in enumerate(report.get("commands") or [], start=1):
        cmd = " ".join(str(part) for part in command.get("cmd") or [])
        status = "ok" if command.get("exit_code") == 0 else "failed"
        body = (
            f"cwd: {command.get('cwd')}\n"
            f"exit_code: {command.get('exit_code')}\n"
            f"duration_ms: {command.get('duration_ms')}\n"
            f"allow_failure: {command.get('allow_failure')}\n"
            f"timed_out: {command.get('timed_out')}\n\n"
            f"$ {cmd}\n\n"
            f"stdout:\n{command.get('stdout') or ''}\n\n"
            f"stderr:\n{command.get('stderr') or ''}"
        )
        sections.append(
            f'<details class="command-card"><summary>{index}. {esc(status)} - {esc(cmd[:160])}</summary>'
            f'<div class="repo-body">{render_code_editor(body, language="text", aria_label=f"Command {index}")}</div></details>'
        )
    return "\n".join(sections) or '<p class="muted">No command records.</p>'


def render_review_checklist() -> str:
    items = [
        "Changed files reviewed",
        "Patch hunks reviewed",
        "Conflicts and errors reviewed",
        "Publish and restore state reviewed",
        "Command log reviewed",
    ]
    return "".join(
        f'<label class="review-check"><input type="checkbox"> <span>{esc(item)}</span></label>' for item in items
    )


def render_html(
    report: dict[str, Any],
    report_path: Path,
    *,
    title: str = "CI Merge Code Review",
    diff_context_lines: int = DEFAULT_DIFF_CONTEXT_LINES,
    expanded_diff_context_lines: int = DEFAULT_EXPANDED_DIFF_CONTEXT_LINES,
) -> str:
    model = collect_review_model(
        report,
        report_path,
        diff_context_lines=diff_context_lines,
        expanded_diff_context_lines=expanded_diff_context_lines,
    )
    resolved = report.get("resolved") or {}
    output = report.get("output") or {}
    repositories = model["repositories"]
    conflicts = model["conflicts"]
    warnings = model["warnings"]
    warning_items = "".join(f'<li class="warning">{esc(item)}</li>' for item in warnings)
    repo_sections = "\n".join(render_repo_section(repo) for repo in repositories)
    conflict_sections = "\n".join(render_conflict_section(conflict) for conflict in conflicts)
    gitlink_file_count = sum(gitlink_count(repo) for repo in repositories)
    changed_file_count = sum(len(repo.get("changed_files") or []) for repo in repositories) - gitlink_file_count
    command_count = len(report.get("commands") or [])
    resolved_report_path = report_path.resolve(strict=False)
    try:
        report_href = resolved_report_path.as_uri()
    except ValueError:
        report_href = str(resolved_report_path)
    if not conflict_sections:
        conflict_sections = '<p class="muted">No conflict entries.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>{CSS}</style>
</head>
<body>
  <header>
    <h1>{esc(title)}</h1>
    <div class="report-line">Report: <a class="report-link" href="{esc(report_href)}">{esc(resolved_report_path)}</a></div>
    <div class="muted">Self-contained offline HTML. GitLab Rapid Diffs-style review workbench with no external CSS, JavaScript, CDN, or network resources.</div>
  </header>
  <main>
    <div class="review-bar">
      <strong>Rapid Diffs Review Workbench - {len(repositories)} repos - {changed_file_count} files - {gitlink_file_count} submodule pointers - {len(conflicts)} conflicts</strong>
      <div class="review-actions">
        <button type="button" class="review-button js-wrap-toggle" aria-pressed="false">Wrap</button>
        <button type="button" class="review-button" data-details-action="expand">Expand all</button>
        <button type="button" class="review-button" data-details-action="collapse">Collapse all</button>
      </div>
    </div>
    <section class="panel">
      <h2>Overview</h2>
      <div class="metrics">{render_metrics(report)}</div>
      <p><strong>Source:</strong> <code>{esc(short_sha(resolved.get("source_sha")))}</code></p>
      <p><strong>Target:</strong> <code>{esc(short_sha(resolved.get("target_sha")))}</code></p>
      <p><strong>Result:</strong> <code>{esc(short_sha(resolved.get("result_sha")))}</code></p>
      <p><strong>Output dir:</strong> <code>{esc(output.get("output_dir") or "")}</code></p>
    </section>
    <section class="panel">
      <h2>Automatic Review Flags</h2>
      <ul>{render_risks(model["risks"])}</ul>
      {"<h3>Artifact Warnings</h3><ul>" + warning_items + "</ul>" if warnings else ""}
    </section>
    <div class="layout">
      <aside class="panel sidebar">
        <h2>Files</h2>
        <div class="toolbar">
          <input id="file-filter" type="search" placeholder="Filter path, repo, status, conflict">
        </div>
        <div class="nav-list">{render_repo_nav(repositories, conflicts)}</div>
      </aside>
      <div class="sidebar-resizer" role="separator" aria-label="Resize Files navigation" aria-orientation="vertical" tabindex="0"></div>
      <div class="review-content">
        <section class="panel">
          <h2>Repositories</h2>
          {repo_sections or '<p class="muted">No repository entries.</p>'}
        </section>
        <section class="panel">
          <h2>Conflicts And Errors</h2>
          {conflict_sections}
          <h3>Errors</h3>
          {render_json_editor(report.get("errors") or [], aria_label="Errors")}
          <h3>Restore</h3>
          {render_json_editor(report.get("restore") or {}, aria_label="Restore")}
          <h3>Publish</h3>
          {render_json_editor(report.get("publish") or {}, aria_label="Publish")}
        </section>
        <details class="panel command-panel">
          <summary>Commands <span class="muted">{command_count} records, collapsed by default</span></summary>
          <div class="command-panel-body">{render_commands(report)}</div>
        </details>
      </div>
    </div>
  </main>
  <script>{SCRIPT}</script>
</body>
</html>
"""


def default_output_path(report_path: Path, *, codemirror_only: bool = False) -> Path:
    return report_path.with_name("merge-codemirror-review.html" if codemirror_only else "merge-diff-review.html")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a self-contained offline HTML page from a CI merge report.")
    parser.add_argument("--report", required=True, help="Path to merge-report.json")
    parser.add_argument("--output", help="Output HTML path. Defaults to merge-diff-review.html next to the report.")
    parser.add_argument("--title", default="CI Merge Code Review", help="HTML page title")
    parser.add_argument(
        "--codemirror-only",
        action="store_true",
        help="Accepted for compatibility. The offline renderer always uses built-in views.",
    )
    parser.add_argument(
        "--diff-context-lines",
        "--diff_context_lines",
        dest="diff_context_lines",
        type=int,
        default=DEFAULT_DIFF_CONTEXT_LINES,
        help="Fallback git diff context lines when patch artifacts are missing.",
    )
    parser.add_argument(
        "--expanded-diff-context-lines",
        "--expanded_diff_context_lines",
        dest="expanded_diff_context_lines",
        type=int,
        default=DEFAULT_EXPANDED_DIFF_CONTEXT_LINES,
        help="Expanded fallback git diff context lines when expanded patch artifacts are missing.",
    )
    return parser.parse_args(argv)


def generate_html_file(
    report_path: Path,
    output_path: Path | None = None,
    *,
    title: str = "CI Merge Code Review",
    diff_context_lines: int = DEFAULT_DIFF_CONTEXT_LINES,
    expanded_diff_context_lines: int = DEFAULT_EXPANDED_DIFF_CONTEXT_LINES,
    codemirror_only: bool = False,
) -> Path:
    report_path = report_path.expanduser().resolve(strict=False)
    report = read_json(report_path)
    output = (
        output_path.expanduser().resolve(strict=False)
        if output_path
        else default_output_path(report_path, codemirror_only=codemirror_only)
    )
    html_text = render_html(
        report,
        report_path,
        title=title,
        diff_context_lines=diff_context_lines,
        expanded_diff_context_lines=expanded_diff_context_lines,
    )
    write_text(output, html_text)
    return output


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    report_path = Path(args.report).expanduser().resolve(strict=False)
    if not report_path.exists():
        print(f"report not found: {report_path}", file=sys.stderr)
        return 2
    if args.diff_context_lines < 0:
        print("--diff-context-lines must be >= 0", file=sys.stderr)
        return 2
    if args.expanded_diff_context_lines < args.diff_context_lines:
        print("--expanded-diff-context-lines must be >= --diff-context-lines", file=sys.stderr)
        return 2
    output_path = Path(args.output).expanduser().resolve(strict=False) if args.output else None
    try:
        generated = generate_html_file(
            report_path,
            output_path,
            title=args.title,
            diff_context_lines=args.diff_context_lines,
            expanded_diff_context_lines=args.expanded_diff_context_lines,
            codemirror_only=args.codemirror_only,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
