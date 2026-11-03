"""Parse unified diffs into files, hunks, and new-file line numbers.

GitHub inline comments address the new side of a hunk, so every added and
context line records ``new_line``. Deleted lines keep ``old_line`` only.
"""

from __future__ import annotations

import re

from prcrew.errors import DiffParseError
from prcrew.models import DiffLine, FileDiff, FileStatus, Hunk

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(text: str) -> list[FileDiff]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return []
    if "diff --git " not in normalized and not normalized.lstrip().startswith("--- "):
        raise DiffParseError("input does not look like a unified diff")
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    files: list[FileDiff] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            parsed, index = _parse_git_file(lines, index)
            files.append(parsed)
            continue
        if line.startswith("--- "):
            parsed, index = _parse_plain_file(lines, index)
            files.append(parsed)
            continue
        index += 1
    return files


def _parse_git_paths(rest: str) -> tuple[str, str]:
    text = rest.strip()
    if text.startswith('"'):
        tokens = _quoted_tokens(text)
        if len(tokens) != 2:
            raise DiffParseError(f"cannot parse diff header: {rest}")
        return _strip_ab(tokens[0]), _strip_ab(tokens[1])
    marker = " b/"
    found = text.find(marker)
    if not text.startswith("a/") or found < 0:
        raise DiffParseError(f"cannot parse diff header: {rest}")
    return text[2:found], text[found + len(marker) :]


def _quoted_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != '"':
            index += 1
            continue
        index += 1
        chars: list[str] = []
        while index < len(text) and text[index] != '"':
            if text[index] == "\\" and index + 1 < len(text):
                chars.append(text[index + 1])
                index += 2
                continue
            chars.append(text[index])
            index += 1
        tokens.append("".join(chars))
        index += 1
    return tokens


def _strip_ab(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _clean_header_path(raw: str) -> str:
    path = raw.strip().split("\t", 1)[0]
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _parse_git_file(lines: list[str], start: int) -> tuple[FileDiff, int]:
    old, new = _parse_git_paths(lines[start][len("diff --git ") :])
    status = FileStatus.modified
    binary = False
    old_path: str | None = old if old != new else None
    hunks: list[Hunk] = []
    index = start + 1
    while index < len(lines) and not lines[index].startswith("diff --git "):
        line = lines[index]
        if line.startswith("@@"):
            hunk, index = _parse_one_hunk(lines, index)
            hunks.append(hunk)
            continue
        if line.startswith("new file mode"):
            status = FileStatus.added
        elif line.startswith("deleted file mode"):
            status = FileStatus.removed
        elif line.startswith("rename from "):
            status = FileStatus.renamed
            old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            status = FileStatus.renamed
            new = line[len("rename to ") :]
        elif line.startswith("--- /dev/null"):
            status = FileStatus.added
        elif line.startswith("+++ /dev/null"):
            status = FileStatus.removed
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            binary = True
            index += 1
            while index < len(lines) and not lines[index].startswith("diff --git "):
                index += 1
            break
        index += 1
    path = old if new in {"/dev/null", "dev/null"} else new
    if old not in {"/dev/null", "dev/null"} and old != path:
        old_path = old_path or old
        if status == FileStatus.modified:
            status = FileStatus.renamed
    return (
        FileDiff(
            path=path,
            old_path=old_path,
            status=status,
            hunks=hunks,
            is_binary=binary,
        ),
        index,
    )


def _parse_plain_file(lines: list[str], start: int) -> tuple[FileDiff, int]:
    old = _clean_header_path(lines[start][4:])
    index = start + 1
    if index >= len(lines) or not lines[index].startswith("+++ "):
        raise DiffParseError("unified diff is missing a +++ header")
    new = _clean_header_path(lines[index][4:])
    index += 1
    status = FileStatus.modified
    if old == "/dev/null":
        status = FileStatus.added
    elif new == "/dev/null":
        status = FileStatus.removed
    path = old if new == "/dev/null" else new
    hunks: list[Hunk] = []
    while index < len(lines) and not lines[index].startswith("diff --git "):
        if lines[index].startswith("--- ") and hunks:
            break
        if lines[index].startswith("@@"):
            hunk, index = _parse_one_hunk(lines, index)
            hunks.append(hunk)
            continue
        index += 1
    return (
        FileDiff(
            path=path,
            old_path=old if old not in {"/dev/null", path} else None,
            status=status,
            hunks=hunks,
        ),
        index,
    )


def _parse_one_hunk(lines: list[str], start: int) -> tuple[Hunk, int]:
    header = lines[start]
    match = _HUNK_RE.match(header)
    if match is None:
        raise DiffParseError(f"bad hunk header: {header}")
    old_start = int(match.group(1))
    old_count = int(match.group(2) or "1")
    new_start = int(match.group(3))
    new_count = int(match.group(4) or "1")
    index = start + 1
    old_line = old_start
    new_line = new_start
    body: list[DiffLine] = []
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git ") or line.startswith("@@"):
            break
        if line.startswith("--- ") or line.startswith("+++ "):
            break
        if line.startswith("\\"):
            index += 1
            continue
        if line.startswith("+"):
            body.append(DiffLine(kind="add", new_line=new_line, content=line[1:]))
            new_line += 1
        elif line.startswith("-"):
            body.append(DiffLine(kind="del", old_line=old_line, content=line[1:]))
            old_line += 1
        elif line.startswith(" "):
            body.append(
                DiffLine(
                    kind="context",
                    old_line=old_line,
                    new_line=new_line,
                    content=line[1:],
                )
            )
            old_line += 1
            new_line += 1
        elif line == "":
            body.append(DiffLine(kind="context", old_line=old_line, new_line=new_line, content=""))
            old_line += 1
            new_line += 1
        else:
            break
        index += 1
    hunk = Hunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        header=header,
        lines=body,
    )
    return hunk, index
