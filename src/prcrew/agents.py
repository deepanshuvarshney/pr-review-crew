"""Specialist runners. Each one calls the shared LLM interface on diff chunks."""

from __future__ import annotations

from json import JSONDecodeError

from pydantic import ValidationError

from prcrew.chunking import chunk_files
from prcrew.coerce import as_float, as_int
from prcrew.config import Settings
from prcrew.difftext import first_logic_line, has_test_changes
from prcrew.errors import ProviderError
from prcrew.llm import LLMProvider, extract_json
from prcrew.log import get_logger
from prcrew.models import (
    Category,
    ChatMessage,
    Evidence,
    FileDiff,
    Finding,
    Severity,
    UsageRecord,
)
from prcrew.prompts import system_prompt, user_prompt
from prcrew.usage import make_usage

logger = get_logger(__name__)


def run_specialist(
    agent: str,
    files: list[FileDiff],
    evidence: list[Evidence],
    llm: LLMProvider,
    settings: Settings,
) -> tuple[list[Finding], list[UsageRecord]]:
    if not files:
        return [], []
    chunks = chunk_files(files, settings.max_diff_tokens)
    changed = [file.path for file in files]
    logic = first_logic_line(files)
    tests = has_test_changes(files)
    evidence_lines = [_format_evidence(item) for item in evidence[:30]]
    findings: list[Finding] = []
    usage: list[UsageRecord] = []
    for chunk in chunks:
        prompt = user_prompt(
            agent=agent,
            changed_files=changed,
            has_tests=tests,
            first_logic=logic,
            chunk_index=chunk.index,
            chunk_count=len(chunks),
            diff_text=chunk.text,
            evidence_lines=evidence_lines,
        )
        completion = llm.complete(
            [
                ChatMessage(role="system", content=system_prompt(agent)),
                ChatMessage(role="user", content=prompt),
            ],
            timeout=settings.llm_timeout_seconds,
        )
        usage.append(
            make_usage(
                provider=completion.provider,
                model=completion.model,
                agent=agent,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
        )
        findings.extend(
            findings_from_response(completion.text, agent=agent, category=Category(agent))
        )
    return _dedupe(findings), usage


def findings_from_response(text: str, *, agent: str, category: Category) -> list[Finding]:
    try:
        payload = extract_json(text)
    except JSONDecodeError as exc:
        raise ProviderError(f"{agent} returned invalid JSON") from exc
    if isinstance(payload, list):
        raw_items: object = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("findings", [])
    else:
        raise ProviderError(f"{agent} JSON root must be an object or list")
    if not isinstance(raw_items, list):
        raise ProviderError(f"{agent} findings must be a list")
    findings: list[Finding] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        coerced = _coerce_finding(item, agent=agent, category=category)
        if coerced is not None:
            findings.append(coerced)
    return findings


def _coerce_finding(item: dict[str, object], *, agent: str, category: Category) -> Finding | None:
    file_name = item.get("file")
    explanation = item.get("explanation")
    if not isinstance(file_name, str) or not file_name.strip():
        return None
    if not isinstance(explanation, str) or not explanation.strip():
        return None
    line = as_int(item.get("line"))
    if line < 1:
        return None
    severity_raw = str(item.get("severity") or "medium").lower()
    try:
        severity = Severity(severity_raw)
    except ValueError:
        severity = Severity.medium
    confidence = max(0.0, min(1.0, as_float(item.get("confidence"), 0.5)))
    suggested = item.get("suggested_fix")
    fix = None
    if isinstance(suggested, str):
        # Keep leading indentation; GitHub applies the suggestion as the whole line.
        stripped = suggested.strip("\r\n")
        fix = stripped if stripped.strip() else None
    title = item.get("title")
    evidence = item.get("evidence")
    try:
        return Finding(
            file=file_name.strip(),
            line=line,
            severity=severity,
            category=category,
            confidence=confidence,
            explanation=explanation.strip(),
            suggested_fix=fix,
            title=title.strip() if isinstance(title, str) else "",
            agent=agent,
            evidence=evidence.strip() if isinstance(evidence, str) and evidence.strip() else None,
        )
    except ValidationError:
        return None


def _format_evidence(item: Evidence) -> str:
    message = " ".join(item.message.split())
    return f"- {item.tool} | {item.rule} | {item.severity} | {item.file} | {item.line} | {message}"


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (finding.file, finding.line, finding.category.value, finding.title)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique
