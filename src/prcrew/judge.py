"""Lead reviewer.

Verification, dedupe, ranking, the comment cap, and the verdict are code.
A hosted model may draft the prose summary, and if that call fails the
template is used. The model cannot add findings or move line numbers.
"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher

from prcrew.config import Settings
from prcrew.difftext import commentable_lines
from prcrew.llm import LLMProvider, extract_json
from prcrew.log import get_logger
from prcrew.models import (
    SEVERITY_RANK,
    ChatMessage,
    DroppedFinding,
    FileDiff,
    Finding,
    ReviewResult,
    Severity,
    UsageRecord,
    Verdict,
    finding_score,
)
from prcrew.usage import make_usage

logger = get_logger(__name__)


def adjudicate(
    *,
    findings: list[Finding],
    files: list[FileDiff],
    skipped: int,
    errors: list[dict[str, str]],
    usage: list[UsageRecord],
    settings: Settings,
    head_sha: str,
    title: str,
    llm: LLMProvider | None = None,
) -> tuple[ReviewResult, list[UsageRecord]]:
    commentable = commentable_lines(files)
    paths = [file.path for file in files]
    dropped: list[DroppedFinding] = []
    resolved: list[Finding] = []
    for finding in findings:
        path = _resolve_path(finding.file, paths)
        if path is None:
            dropped.append(_drop(finding, "unknown_file"))
            continue
        anchored = finding.model_copy(update={"file": path})
        if anchored.line not in commentable.get(path, set()):
            dropped.append(_drop(anchored, "unverified_line"))
            continue
        resolved.append(anchored)

    resolved.sort(key=finding_score, reverse=True)
    deduped: list[Finding] = []
    for finding in resolved:
        if any(_duplicate(finding, kept) for kept in deduped):
            dropped.append(_drop(finding, "duplicate"))
            continue
        deduped.append(finding)

    kept: list[Finding] = []
    threshold = SEVERITY_RANK[settings.severity_threshold]
    for finding in deduped:
        if finding.confidence < settings.confidence_threshold:
            dropped.append(_drop(finding, "low_confidence"))
            continue
        if SEVERITY_RANK[finding.severity] < threshold:
            dropped.append(_drop(finding, "below_threshold"))
            continue
        kept.append(finding)

    if len(kept) > settings.max_comments:
        for extra in kept[settings.max_comments :]:
            dropped.append(_drop(extra, "over_cap"))
        kept = kept[: settings.max_comments]

    verdict = _verdict(kept, errors)
    summary = _template_summary(
        files_reviewed=len(files),
        skipped=skipped,
        findings=kept,
        dropped=dropped,
        errors=errors,
        verdict=verdict,
        title=title,
    )
    extra_usage: list[UsageRecord] = []
    if llm is not None and llm.name != "mock":
        drafted, record = _draft_summary(llm, settings, summary, kept, verdict)
        if drafted:
            summary = drafted
        if record is not None:
            extra_usage.append(record)

    review = ReviewResult(
        summary=summary,
        verdict=verdict,
        findings=kept,
        dropped=dropped,
        usage=[*usage, *extra_usage],
        head_sha=head_sha,
        agent_errors=errors,
        files_reviewed=len(files),
        files_skipped=skipped,
        provider=settings.provider,
        model=settings.resolved_model(),
        title=title,
    )
    logger.info(
        "judge finished",
        extra={
            "event": "judge_done",
            "verdict": verdict.value,
            "kept": len(kept),
            "dropped": len(dropped),
        },
    )
    return review, extra_usage


def _verdict(findings: list[Finding], errors: list[dict[str, str]]) -> Verdict:
    if any(item.severity in {Severity.high, Severity.critical} for item in findings):
        return Verdict.request_changes
    if any(item.severity == Severity.medium for item in findings):
        return Verdict.comment
    if not findings and errors:
        return Verdict.comment
    return Verdict.approve


def _template_summary(
    *,
    files_reviewed: int,
    skipped: int,
    findings: list[Finding],
    dropped: list[DroppedFinding],
    errors: list[dict[str, str]],
    verdict: Verdict,
    title: str,
) -> str:
    parts: list[str] = []
    if title:
        parts.append(f"Review of {title}.")
    parts.append(f"Reviewed {files_reviewed} file(s) ({skipped} skipped).")
    if errors:
        names = ", ".join(item.get("agent", "agent") for item in errors)
        parts.append(f"Agents that failed and were skipped: {names}.")
    if not findings:
        parts.append("No findings met the severity and confidence thresholds.")
    else:
        counts = Counter(item.category.value for item in findings)
        detail = ", ".join(f"{name} {counts[name]}" for name in sorted(counts))
        parts.append(f"Publishing {len(findings)} comment(s): {detail}.")
        top = max(findings, key=finding_score)
        parts.append(
            f"Highest signal: {top.severity.value} {top.category.value} "
            f"in {top.file}:{top.line} ({top.title})."
        )
    if dropped:
        reasons = Counter(item.reason for item in dropped)
        detail = ", ".join(f"{name} {reasons[name]}" for name in sorted(reasons))
        parts.append(f"Dropped {len(dropped)} candidate(s) before publishing ({detail}).")
    parts.append(f"Verdict: {verdict.value.replace('_', ' ')}.")
    return " ".join(parts)


def _draft_summary(
    llm: LLMProvider,
    settings: Settings,
    template: str,
    findings: list[Finding],
    verdict: Verdict,
) -> tuple[str | None, UsageRecord | None]:
    brief = [
        {
            "file": item.file,
            "line": item.line,
            "severity": item.severity.value,
            "title": item.title,
            "explanation": item.explanation,
        }
        for item in findings
    ]
    messages = [
        ChatMessage(
            role="system",
            content=(
                "You write the pull request review summary for PR Review Crew. "
                'Return JSON {"summary": "..."}. Two short paragraphs. '
                "Do not change the verdict, do not add findings, and do not invent line numbers. "
                f"The verdict is fixed: {verdict.value}."
            ),
        ),
        ChatMessage(
            role="user",
            content=f"Template:\n{template}\n\nFindings:\n{brief}",
        ),
    ]
    try:
        completion = llm.complete(messages, timeout=settings.llm_timeout_seconds)
        payload = extract_json(completion.text)
    except Exception as exc:  # noqa: BLE001 - summary is optional
        logger.warning("summary draft failed", extra={"event": "summary_failed", "error": str(exc)})
        return None, None
    record = make_usage(
        provider=completion.provider,
        model=completion.model,
        agent="judge",
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )
    if isinstance(payload, dict) and isinstance(payload.get("summary"), str):
        text = payload["summary"].strip()
        if text:
            return text, record
    return None, record


def _resolve_path(name: str, paths: list[str]) -> str | None:
    cleaned = name.replace("\\", "/").strip()
    if cleaned in paths:
        return cleaned
    matches = [
        path for path in paths if path.endswith("/" + cleaned) or path.rsplit("/", 1)[-1] == cleaned
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _duplicate(candidate: Finding, kept: Finding) -> bool:
    if candidate.file != kept.file or candidate.category != kept.category:
        return False
    if candidate.line == kept.line:
        return True
    if abs(candidate.line - kept.line) > 3:
        return False
    return SequenceMatcher(None, candidate.explanation, kept.explanation).ratio() >= 0.9


def _drop(finding: Finding, reason: str) -> DroppedFinding:
    return DroppedFinding(
        file=finding.file,
        line=finding.line,
        category=finding.category.value,
        reason=reason,
        title=finding.title,
    )
