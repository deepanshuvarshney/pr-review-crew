"""Pack rendered diffs into chunks that fit a token budget.

Tokens are estimated as characters / 4. A single oversized line is kept whole
so line numbers stay intact; every other piece stays under the budget. When a
file is split, each piece repeats the ``FILE:`` header.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from prcrew.difftext import render_file
from prcrew.models import FileDiff


class DiffChunk(BaseModel):
    index: int
    text: str
    token_estimate: int
    files: list[str] = Field(default_factory=list)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def chunk_files(files: list[FileDiff], max_tokens: int) -> list[DiffChunk]:
    pieces: list[tuple[str, str]] = []
    for file in files:
        pieces.extend(_pieces_for_file(file, max_tokens))
    if not pieces:
        return [DiffChunk(index=1, text="(no reviewable changes)\n", token_estimate=1, files=[])]
    packed: list[DiffChunk] = []
    current: list[str] = []
    current_files: list[str] = []
    current_tokens = 0
    for path, text in pieces:
        tokens = estimate_tokens(text)
        if current and current_tokens + tokens > max_tokens:
            packed.append(_make_chunk(len(packed) + 1, current, current_files, current_tokens))
            current, current_files, current_tokens = [], [], 0
        current.append(text)
        if path not in current_files:
            current_files.append(path)
        current_tokens += tokens
    if current:
        packed.append(_make_chunk(len(packed) + 1, current, current_files, current_tokens))
    return packed


def _make_chunk(index: int, parts: list[str], files: list[str], tokens: int) -> DiffChunk:
    return DiffChunk(index=index, text="".join(parts), token_estimate=tokens, files=files)


def _pieces_for_file(file: FileDiff, max_tokens: int) -> list[tuple[str, str]]:
    rendered = render_file(file)
    if estimate_tokens(rendered) <= max_tokens:
        return [(file.path, rendered)]
    header = f"FILE: {file.path}\nSTATUS: {file.status.value}\n"
    body = rendered.splitlines()
    # Drop the original header lines; we repeat a short header on each piece.
    content = [row for row in body if not row.startswith(("FILE:", "STATUS:", "OLD_PATH:"))]
    pieces: list[tuple[str, str]] = []
    bucket: list[str] = []
    bucket_tokens = estimate_tokens(header)
    for row in content:
        line = row + "\n"
        line_tokens = estimate_tokens(line)
        if bucket and bucket_tokens + line_tokens > max_tokens:
            pieces.append((file.path, header + "".join(bucket)))
            bucket = []
            bucket_tokens = estimate_tokens(header)
        bucket.append(line)
        bucket_tokens += line_tokens
    if bucket:
        pieces.append((file.path, header + "".join(bucket)))
    return pieces
