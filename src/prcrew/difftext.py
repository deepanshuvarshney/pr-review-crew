"""Render parsed diffs into the stable prompt format agents and the mock share."""

from __future__ import annotations

import re

from prcrew.models import DiffLine, FileDiff

_LOGIC_RE = re.compile(r"^\s*(def |class |return\b|if |elif |for |while |raise |except |try:)")
_TEST_DIR = {"tests", "test", "__tests__", "spec"}
_TEST_SUFFIXES = (
    ".spec.ts",
    ".spec.js",
    ".spec.tsx",
    ".test.ts",
    ".test.js",
    ".test.tsx",
)


def is_test_path(path: str) -> bool:
    lowered = path.replace("\\", "/")
    name = lowered.rsplit("/", 1)[-1]
    parts = [part for part in lowered.split("/") if part]
    if any(part in _TEST_DIR for part in parts[:-1]):
        return True
    if name.startswith("test_") or name.endswith("_test.py") or name == "test.py":
        return True
    return name.endswith(_TEST_SUFFIXES)


def render_line(line: DiffLine) -> str:
    if line.kind == "add":
        number = line.new_line or 0
        return f"{number:6} |+{line.content}"
    if line.kind == "del":
        number = line.old_line or 0
        return f"{number:6} |-{line.content}"
    number = line.new_line or 0
    return f"{number:6} | {line.content}"


def render_file(file: FileDiff) -> str:
    rows = [f"FILE: {file.path}", f"STATUS: {file.status.value}"]
    if file.old_path and file.old_path != file.path:
        rows.append(f"OLD_PATH: {file.old_path}")
    if not file.hunks:
        rows.append("(no hunks)")
    for hunk in file.hunks:
        rows.append(f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@")
        for line in hunk.lines:
            rows.append(render_line(line))
    return "\n".join(rows) + "\n"


def commentable_lines(files: list[FileDiff]) -> dict[str, set[int]]:
    """New-file line numbers GitHub will accept on the RIGHT side of the diff."""

    found: dict[str, set[int]] = {}
    for file in files:
        numbers: set[int] = set()
        for hunk in file.hunks:
            for line in hunk.lines:
                if (
                    line.kind in {"add", "context"}
                    and line.new_line is not None
                    and line.new_line >= 1
                ):
                    numbers.add(line.new_line)
        found[file.path] = numbers
    return found


def has_test_changes(files: list[FileDiff]) -> bool:
    return any(is_test_path(file.path) for file in files)


def first_logic_line(files: list[FileDiff]) -> tuple[str, int, str] | None:
    for file in files:
        if is_test_path(file.path):
            continue
        for number, text in file.added_lines():
            if _LOGIC_RE.match(text):
                return file.path, number, text.strip()
    return None
