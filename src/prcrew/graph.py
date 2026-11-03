"""LangGraph pipeline: ingest, static analysis, parallel specialists, judge, publish."""

from __future__ import annotations

import hashlib
import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from prcrew.agents import run_specialist
from prcrew.cache import load_review, save_review
from prcrew.coerce import as_dict
from prcrew.config import Settings
from prcrew.diffparse import parse_unified_diff
from prcrew.errors import PrcrewError
from prcrew.filters import partition
from prcrew.github_api import GitHubClient
from prcrew.judge import adjudicate
from prcrew.llm import LLMProvider
from prcrew.log import get_logger
from prcrew.models import (
    Evidence,
    FileDiff,
    Finding,
    ReviewResult,
    ReviewTarget,
    UsageRecord,
)
from prcrew.publish import publish_review
from prcrew.retry import run_with_timeout
from prcrew.static_analysis import Runner, run_static_analysis

logger = get_logger(__name__)


class ReviewState(TypedDict, total=False):
    """Graph state. List fields use an append reducer so parallel agents merge."""

    target: dict[str, Any]
    files: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    findings: Annotated[list[dict[str, Any]], operator.add]
    agent_errors: Annotated[list[dict[str, Any]], operator.add]
    usage: Annotated[list[dict[str, Any]], operator.add]
    review: dict[str, Any]
    publish: dict[str, Any]
    cached: bool


@dataclass
class Runtime:
    settings: Settings
    llm: LLMProvider
    github: GitHubClient | None
    dry_run: bool
    local_root: Path
    cache_enabled: bool
    static_runner: Runner | None = None


def build_graph(runtime: Runtime) -> Any:
    graph: StateGraph[ReviewState] = StateGraph(ReviewState)
    graph.add_node("ingest", _ingest(runtime))
    graph.add_node("static_analysis", _static(runtime))
    graph.add_node("judge", _judge(runtime))
    graph.add_node("publish", _publish(runtime))
    enabled = runtime.settings.agents.enabled()
    for name in enabled:
        graph.add_node(name, _agent(runtime, name))

    graph.add_edge(START, "ingest")
    graph.add_conditional_edges(
        "ingest",
        _route_after_ingest,
        ["static_analysis", "publish"],
    )
    if enabled:
        for name in enabled:
            graph.add_edge("static_analysis", name)
        graph.add_edge(enabled, "judge")
    else:
        graph.add_edge("static_analysis", "judge")
    graph.add_edge("judge", "publish")
    graph.add_edge("publish", END)
    return graph.compile()


def _route_after_ingest(state: ReviewState) -> str:
    if state.get("cached"):
        return "publish"
    return "static_analysis"


def initial_state(target: ReviewTarget) -> dict[str, Any]:
    return {
        "target": target.model_dump(mode="json"),
        "files": [],
        "skipped": [],
        "evidence": [],
        "findings": [],
        "agent_errors": [],
        "usage": [],
        "review": {},
        "publish": {},
        "cached": False,
    }


def _ingest(runtime: Runtime) -> Any:
    def ingest(state: dict[str, Any]) -> dict[str, Any]:
        target = ReviewTarget.model_validate(state["target"])
        if target.number is not None:
            if runtime.github is None or not target.owner or not target.repo:
                raise PrcrewError(
                    "a GitHub client and owner/repo are required to fetch a pull request"
                )
            payload = runtime.github.get_pull(target.owner, target.repo, target.number)
            diff_text = runtime.github.get_pull_diff(target.owner, target.repo, target.number)
            head = as_dict(payload.get("head"))
            base = as_dict(payload.get("base"))
            target = target.model_copy(
                update={
                    "diff_text": diff_text,
                    "head_sha": str(head.get("sha") or ""),
                    "head_ref": str(head.get("ref") or ""),
                    "base_ref": str(base.get("ref") or ""),
                    "title": str(payload.get("title") or ""),
                    "body": str(payload.get("body") or ""),
                    "html_url": str(payload.get("html_url") or ""),
                }
            )
        elif not target.diff_text:
            raise PrcrewError("provide a pull request or a unified diff")
        if not target.head_sha:
            digest = hashlib.sha256((target.diff_text or "").encode("utf-8")).hexdigest()
            target = target.model_copy(update={"head_sha": digest})
        parsed = parse_unified_diff(target.diff_text or "")
        kept, skipped = partition(parsed, runtime.settings.ignore_paths)
        logger.info(
            "ingest finished",
            extra={
                "event": "ingest_done",
                "files": len(kept),
                "skipped": len(skipped),
                "sha": target.head_sha,
            },
        )
        update: dict[str, Any] = {
            "target": target.model_dump(mode="json"),
            "files": [file.model_dump(mode="json") for file in kept],
            "skipped": [file.model_dump(mode="json") for file in skipped],
            "cached": False,
        }
        if runtime.cache_enabled and target.head_sha:
            cached = load_review(
                runtime.settings.cache_dir,
                target.head_sha,
                runtime.settings.fingerprint(),
            )
            if cached is not None:
                logger.info("cache hit", extra={"event": "cache_hit", "sha": target.head_sha})
                update["review"] = cached.model_dump(mode="json")
                update["cached"] = True
        return update

    return ingest


def _static(runtime: Runtime) -> Any:
    def static_analysis(state: dict[str, Any]) -> dict[str, Any]:
        if not runtime.settings.static_analysis:
            return {"evidence": []}
        files = [FileDiff.model_validate(item) for item in state.get("files") or []]
        try:
            evidence = run_static_analysis(
                files,
                runtime.local_root,
                runtime.settings.static_tools,
                timeout=runtime.settings.static_timeout_seconds,
                runner=runtime.static_runner,
            )
        except Exception as exc:  # noqa: BLE001 - tools are optional
            logger.warning(
                "static analysis crashed", extra={"event": "static_failed", "error": str(exc)}
            )
            evidence = []
        return {"evidence": [item.model_dump(mode="json") for item in evidence]}

    return static_analysis


def _agent(runtime: Runtime, name: str) -> Any:
    def node(state: dict[str, Any]) -> dict[str, Any]:
        files = [FileDiff.model_validate(item) for item in state.get("files") or []]
        evidence = [Evidence.model_validate(item) for item in state.get("evidence") or []]

        def invoke() -> tuple[list[Finding], list[UsageRecord]]:
            return run_specialist(name, files, evidence, runtime.llm, runtime.settings)

        try:
            findings, usage = run_with_timeout(invoke, runtime.settings.agent_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - one specialist must not sink the review
            logger.warning(
                "agent failed",
                extra={"event": "agent_failed", "agent": name, "error": str(exc)},
            )
            return {"agent_errors": [{"agent": name, "error": str(exc)}]}
        logger.info(
            "agent finished",
            extra={"event": "agent_done", "agent": name, "findings": len(findings)},
        )
        return {
            "findings": [item.model_dump(mode="json") for item in findings],
            "usage": [item.model_dump(mode="json") for item in usage],
        }

    node.__name__ = name
    return node


def _judge(runtime: Runtime) -> Any:
    def judge(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("cached") and state.get("review"):
            return {}
        target = ReviewTarget.model_validate(state["target"])
        files = [FileDiff.model_validate(item) for item in state.get("files") or []]
        findings = [Finding.model_validate(item) for item in state.get("findings") or []]
        usage = [UsageRecord.model_validate(item) for item in state.get("usage") or []]
        errors = [dict(item) for item in state.get("agent_errors") or []]
        review, _extra = adjudicate(
            findings=findings,
            files=files,
            skipped=len(state.get("skipped") or []),
            errors=errors,
            usage=usage,
            settings=runtime.settings,
            head_sha=target.head_sha or "",
            title=target.title or target.coordinates,
            llm=runtime.llm,
        )
        if runtime.cache_enabled:
            save_review(runtime.settings.cache_dir, review, runtime.settings.fingerprint())
        return {"review": review.model_dump(mode="json")}

    return judge


def _publish(runtime: Runtime) -> Any:
    def publish(state: dict[str, Any]) -> dict[str, Any]:
        review = ReviewResult.model_validate(state["review"])
        target = ReviewTarget.model_validate(state["target"])
        result = publish_review(
            review,
            owner=target.owner,
            repo=target.repo,
            number=target.number,
            dry_run=runtime.dry_run,
            github=runtime.github,
        )
        return {"publish": result.model_dump(mode="json")}

    return publish
