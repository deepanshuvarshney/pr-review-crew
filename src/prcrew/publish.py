"""Render a review as Markdown and publish it to GitHub without duplicating itself.

The summary lives in one issue comment, updated in place. Inline comments are
submitted as a pull request review once per head SHA. GitHub does not let us
edit those inline comments, so a new commit gets a new review and the summary
comment is what stays current.
"""

from __future__ import annotations

from typing import Any

from prcrew.errors import GitHubError
from prcrew.github_api import GitHubClient
from prcrew.log import get_logger
from prcrew.models import Finding, PublishResult, ReviewResult, Verdict
from prcrew.usage import summarize_usage

logger = get_logger(__name__)

SUMMARY_MARKER = "<!-- prcrew:summary -->"


def sha_marker(sha: str) -> str:
    return f"<!-- prcrew:sha:{sha} -->"


def render_markdown(review: ReviewResult, *, inline: bool = True) -> str:
    usage = summarize_usage(review.usage)
    lines = [
        SUMMARY_MARKER,
        sha_marker(review.head_sha or "local"),
        "",
        "## PR Review Crew",
        "",
        f"**Verdict:** {review.verdict.value.replace('_', ' ')}",
        f"**Head:** `{review.head_sha or 'local'}`",
        f"**Provider:** {review.provider} (`{review.model}`)",
        "",
        review.summary,
        "",
    ]
    if review.findings:
        lines.extend(
            [
                "| Severity | Category | Location | Confidence | Finding |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for finding in review.findings:
            title = (finding.title or finding.explanation).replace("|", "/")
            lines.append(
                f"| {finding.severity.value} | {finding.category.value} | "
                f"`{finding.file}:{finding.line}` | {finding.confidence:.2f} | {title} |"
            )
        lines.append("")
    if review.agent_errors:
        failed = ", ".join(item.get("agent", "agent") for item in review.agent_errors)
        lines.append(f"Agents skipped after an error: {failed}.")
        lines.append("")
    lines.extend(
        [
            "### Usage",
            "",
            f"- Calls: {usage['calls']}",
            f"- Tokens: {usage['input_tokens']} in / {usage['output_tokens']} out",
            f"- Estimated list price: ${usage['estimated_list_usd']:.6f}",
            f"- Billed (free tier): ${usage['billed_usd']:.2f}",
            "",
        ]
    )
    if inline and review.findings:
        lines.append("## Inline comments")
        lines.append("")
        for finding in review.findings:
            lines.append(f"### {finding.file}:{finding.line}")
            lines.append("")
            lines.append(comment_body(finding))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def comment_body(finding: Finding) -> str:
    title = finding.title or finding.category.value
    parts = [
        f"**[{finding.severity.value}] {title}**",
        "",
        finding.explanation,
    ]
    if (
        finding.suggested_fix
        and "\n" not in finding.suggested_fix
        and "\r" not in finding.suggested_fix
    ):
        parts.extend(["", "```suggestion", finding.suggested_fix, "```"])
    elif finding.suggested_fix:
        parts.extend(["", "Suggested change:", "", "```", finding.suggested_fix, "```"])
    parts.extend(["", f"_{finding.agent} · confidence {finding.confidence:.2f}_"])
    return "\n".join(parts)


def github_event(verdict: Verdict) -> str:
    if verdict == Verdict.approve:
        return "APPROVE"
    if verdict == Verdict.request_changes:
        return "REQUEST_CHANGES"
    return "COMMENT"


def publish_review(
    review: ReviewResult,
    *,
    owner: str | None,
    repo: str | None,
    number: int | None,
    dry_run: bool,
    github: GitHubClient | None,
) -> PublishResult:
    markdown = render_markdown(review, inline=True)
    summary = render_markdown(review, inline=False)
    if dry_run or github is None or not owner or not repo or number is None:
        logger.info("dry run", extra={"event": "publish_dry_run", "verdict": review.verdict.value})
        return PublishResult(mode="dry_run", markdown=markdown, detail="printed markdown")
    if not review.head_sha:
        raise GitHubError("cannot publish a review without the head SHA")

    comment_id, updated = _upsert_summary(github, owner, repo, number, summary)
    existing = _review_for_sha(github, owner, repo, number, review.head_sha)
    if existing is not None:
        review_id = int(existing["id"])
        github.update_review(owner, repo, number, review_id, summary)
        logger.info(
            "updated existing review",
            extra={"event": "publish_updated", "review_id": review_id, "sha": review.head_sha},
        )
        return PublishResult(
            mode="updated",
            markdown=markdown,
            review_id=review_id,
            comment_id=comment_id,
            created_review=False,
            updated_summary=updated,
            detail="updated the summary for this head SHA",
        )

    comments = [
        {
            "path": finding.file,
            "line": finding.line,
            "side": "RIGHT",
            "body": comment_body(finding),
        }
        for finding in review.findings
    ]
    event = github_event(review.verdict)
    try:
        created = _create_with_approve_fallback(
            github, owner, repo, number, review.head_sha, summary, event, comments
        )
    except GitHubError as exc:
        logger.warning(
            "inline review rejected",
            extra={"event": "publish_inline_failed", "error": str(exc)},
        )
        return PublishResult(
            mode="summary_only",
            markdown=markdown,
            comment_id=comment_id,
            updated_summary=True,
            detail=str(exc),
        )
    raw_id = created.get("id")
    posted_id = raw_id if isinstance(raw_id, int) else None
    logger.info(
        "posted review",
        extra={"event": "publish_posted", "review_id": posted_id, "comments": len(comments)},
    )
    return PublishResult(
        mode="posted",
        markdown=markdown,
        review_id=posted_id,
        comment_id=comment_id,
        created_review=True,
        updated_summary=True,
        detail=f"posted {len(comments)} inline comment(s)",
    )


def _upsert_summary(
    github: GitHubClient, owner: str, repo: str, number: int, body: str
) -> tuple[int | None, bool]:
    comments = github.list_issue_comments(owner, repo, number)
    ours = [item for item in comments if SUMMARY_MARKER in str(item.get("body") or "")]
    if not ours:
        created = github.create_issue_comment(owner, repo, number, body)
        identifier = created.get("id")
        return (int(identifier) if isinstance(identifier, int) else None), False
    latest = ours[-1]
    identifier = latest.get("id")
    if not isinstance(identifier, int):
        return None, False
    github.update_issue_comment(owner, repo, identifier, body)
    return identifier, True


def _review_for_sha(
    github: GitHubClient, owner: str, repo: str, number: int, sha: str
) -> dict[str, Any] | None:
    marker = sha_marker(sha)
    matches = [
        item
        for item in github.list_reviews(owner, repo, number)
        if marker in str(item.get("body") or "")
    ]
    if not matches:
        return None
    return matches[-1]


def _create_with_approve_fallback(
    github: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    sha: str,
    body: str,
    event: str,
    comments: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        return github.create_review(
            owner,
            repo,
            number,
            commit_id=sha,
            body=body,
            event=event,
            comments=comments,
        )
    except GitHubError as exc:
        if event == "APPROVE" and "422" in str(exc):
            logger.info(
                "approve not permitted, posting a comment review",
                extra={"event": "publish_approve_fallback"},
            )
            return github.create_review(
                owner,
                repo,
                number,
                commit_id=sha,
                body=body,
                event="COMMENT",
                comments=comments,
            )
        raise
