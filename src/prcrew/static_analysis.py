"""Optional ruff, bandit, and semgrep evidence for changed lines.

Tools are used only when they are installed and the changed file exists on
disk. Failures are logged and skipped so a missing binary never fails a review.
Findings on lines outside the diff are dropped; pre-existing issues are not
the pull request's to fix.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from prcrew.coerce import as_dict, as_int
from prcrew.difftext import commentable_lines
from prcrew.log import get_logger
from prcrew.models import Evidence, FileDiff

logger = get_logger(__name__)

Runner = Callable[..., subprocess.CompletedProcess[str]]


def run_static_analysis(
    files: list[FileDiff],
    root: Path,
    tools: Sequence[str],
    *,
    timeout: float = 30.0,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[Evidence]:
    run = runner or subprocess.run
    locate = which or shutil.which
    allowed = commentable_lines(files)
    existing = _existing_files(files, root)
    if not existing:
        logger.info(
            "static analysis skipped", extra={"event": "static_skipped", "reason": "no_local_files"}
        )
        return []
    evidence: list[Evidence] = []
    for tool in tools:
        binary = locate(tool)
        if not binary:
            logger.info("static tool missing", extra={"event": "static_missing", "tool": tool})
            continue
        paths = _paths_for_tool(tool, existing)
        if not paths:
            continue
        try:
            completed = run(
                _command(tool, paths),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "static tool failed",
                extra={"event": "static_failed", "tool": tool, "error": str(exc)},
            )
            continue
        if completed.returncode not in {0, 1}:
            logger.warning(
                "static tool exited",
                extra={
                    "event": "static_failed",
                    "tool": tool,
                    "code": completed.returncode,
                    "stderr": (completed.stderr or "")[:300],
                },
            )
            continue
        parsed = _parse_tool(tool, completed.stdout or "")
        for item in parsed:
            aligned = _align_path(item.file, files)
            if aligned is None:
                continue
            if item.line not in allowed.get(aligned, set()):
                continue
            evidence.append(item.model_copy(update={"file": aligned}))
    logger.info(
        "static analysis finished",
        extra={"event": "static_done", "evidence": len(evidence)},
    )
    return evidence[:40]


def _existing_files(files: list[FileDiff], root: Path) -> list[tuple[FileDiff, Path]]:
    found: list[tuple[FileDiff, Path]] = []
    for file in files[:40]:
        candidate = (root / file.path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            found.append((file, candidate))
    return found


def _paths_for_tool(tool: str, existing: list[tuple[FileDiff, Path]]) -> list[Path]:
    if tool in {"ruff", "bandit"}:
        return [path for file, path in existing if path.suffix == ".py"]
    return [path for _, path in existing]


def _command(tool: str, paths: list[Path]) -> list[str]:
    rendered = [str(path) for path in paths]
    if tool == "ruff":
        return ["ruff", "check", "--output-format", "json", *rendered]
    if tool == "bandit":
        return ["bandit", "-f", "json", "-q", *rendered]
    if tool == "semgrep":
        return [
            "semgrep",
            "--quiet",
            "--json",
            "--metrics",
            "off",
            "--config",
            "p/default",
            *rendered,
        ]
    return [tool, *rendered]


def _parse_tool(tool: str, stdout: str) -> list[Evidence]:
    text = stdout.strip()
    if not text:
        return []
    try:
        payload = json.loads(text[:2_000_000])
    except json.JSONDecodeError:
        logger.warning(
            "static tool returned non-json", extra={"event": "static_parse", "tool": tool}
        )
        return []
    if tool == "ruff":
        return _parse_ruff(payload)
    if tool == "bandit":
        return _parse_bandit(payload)
    if tool == "semgrep":
        return _parse_semgrep(payload)
    return []


def _parse_ruff(payload: object) -> list[Evidence]:
    rows: list[object]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("diagnostics"), list):
        rows = payload["diagnostics"]
    else:
        return []
    evidence: list[Evidence] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        location = as_dict(row.get("location"))
        line = as_int(location.get("row") or row.get("line"))
        filename = str(row.get("filename") or row.get("path") or "")
        code = str(row.get("code") or "ruff")
        message = str(row.get("message") or "")
        if line < 1 or not filename:
            continue
        evidence.append(
            Evidence(
                tool="ruff", rule=code, file=filename, line=line, message=message, severity="low"
            )
        )
    return evidence


def _parse_bandit(payload: object) -> list[Evidence]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return []
    evidence: list[Evidence] = []
    for row in payload["results"]:
        if not isinstance(row, dict):
            continue
        line = as_int(row.get("line_number"))
        filename = str(row.get("filename") or "")
        if line < 1 or not filename:
            continue
        evidence.append(
            Evidence(
                tool="bandit",
                rule=str(row.get("test_id") or "bandit"),
                file=filename,
                line=line,
                message=str(row.get("issue_text") or ""),
                severity=str(row.get("issue_severity") or "MEDIUM"),
            )
        )
    return evidence


def _parse_semgrep(payload: object) -> list[Evidence]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return []
    evidence: list[Evidence] = []
    for row in payload["results"]:
        if not isinstance(row, dict):
            continue
        start = as_dict(row.get("start"))
        line = as_int(start.get("line"))
        filename = str(row.get("path") or "")
        extra = as_dict(row.get("extra"))
        if line < 1 or not filename:
            continue
        evidence.append(
            Evidence(
                tool="semgrep",
                rule=str(row.get("check_id") or "semgrep"),
                file=filename,
                line=line,
                message=str(extra.get("message") or ""),
                severity=str(extra.get("severity") or "WARNING"),
            )
        )
    return evidence


def _align_path(reported: str, files: list[FileDiff]) -> str | None:
    cleaned = reported.replace("\\", "/")
    for file in files:
        if cleaned == file.path or cleaned.endswith("/" + file.path):
            return file.path
    basename = cleaned.rsplit("/", 1)[-1]
    matches = [file.path for file in files if file.path.rsplit("/", 1)[-1] == basename]
    if len(matches) == 1:
        return matches[0]
    return None
