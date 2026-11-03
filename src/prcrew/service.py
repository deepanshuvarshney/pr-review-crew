"""Run a review from the CLI, the GitHub Action, or the webhook."""

from __future__ import annotations

import os
from pathlib import Path

from prcrew.config import Settings, resolve_provider
from prcrew.errors import PrcrewError
from prcrew.github_api import GitHubClient
from prcrew.graph import Runtime, build_graph, initial_state
from prcrew.llm import LLMProvider, build_llm
from prcrew.log import get_logger
from prcrew.models import CompletedReview, PublishResult, ReviewResult, ReviewTarget, Verdict
from prcrew.publish import render_markdown
from prcrew.static_analysis import Runner

logger = get_logger(__name__)


def run_review(
    target: ReviewTarget,
    settings: Settings,
    *,
    dry_run: bool,
    llm: LLMProvider | None = None,
    github: GitHubClient | None = None,
    local_root: Path | None = None,
    cache_enabled: bool | None = None,
    static_runner: Runner | None = None,
) -> CompletedReview:
    settings = resolve_provider(settings)
    if not settings.enabled:
        review = ReviewResult(
            summary="PR Review Crew is disabled in the config file.",
            verdict=Verdict.approve,
            provider=settings.provider,
            model=settings.resolved_model(),
            title=target.title or target.coordinates,
        )
        publish = PublishResult(
            mode="skipped",
            markdown=render_markdown(review),
            detail="disabled",
        )
        return CompletedReview(review=review, publish=publish, target=target)

    client = github
    if target.number is not None and client is None:
        client = github_client_from_env(settings)
    model = llm if llm is not None else build_llm(settings)
    runtime = Runtime(
        settings=settings,
        llm=model,
        github=client,
        dry_run=dry_run or target.number is None,
        local_root=local_root or Path.cwd(),
        cache_enabled=settings.cache_enabled if cache_enabled is None else cache_enabled,
        static_runner=static_runner,
    )
    # A local diff never posts. An explicit dry_run also forces the printer.
    if dry_run:
        runtime.dry_run = True
    graph = build_graph(runtime)
    final = graph.invoke(initial_state(target))
    if not isinstance(final, dict) or not final.get("review") or not final.get("publish"):
        raise PrcrewError("the review graph finished without a review")
    review = ReviewResult.model_validate(final["review"])
    publish = PublishResult.model_validate(final["publish"])
    resolved_target = ReviewTarget.model_validate(final.get("target") or target.model_dump())
    logger.info(
        "review finished",
        extra={
            "event": "review_done",
            "verdict": review.verdict.value,
            "findings": len(review.findings),
            "mode": publish.mode,
            "cached": review.cached,
        },
    )
    return CompletedReview(review=review, publish=publish, target=resolved_target)


def github_client_from_env(settings: Settings) -> GitHubClient:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise PrcrewError("GITHUB_TOKEN is required to fetch or publish a pull request")
    return GitHubClient(
        token,
        timeout=settings.github_timeout_seconds,
        max_attempts=settings.max_attempts,
    )
