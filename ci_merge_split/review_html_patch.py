from __future__ import annotations

import re
from typing import Any, TypedDict


class PatchRow(TypedDict):
    index: int
    raw: str
    code: str
    kind: str
    old_number: int | str
    new_number: int | str
    sign: str


PATCH_HUNK_RE = re.compile(r"@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")


def display_lines(text: str) -> list[str]:
    if text == "":
        return []
    return text.splitlines()


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


def parse_patch_rows(text: str) -> list[dict[str, Any]]:
    rows: list[PatchRow] = []
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
