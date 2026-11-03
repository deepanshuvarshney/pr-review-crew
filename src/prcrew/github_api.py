"""GitHub REST client with backoff on rate limits and 5xx responses."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from prcrew.errors import GitHubError, RetryableError
from prcrew.log import get_logger
from prcrew.retry import call_with_retries

logger = get_logger(__name__)

_NEXT_RE = re.compile(r"<([^>]+)>\s*;\s*rel=\"next\"")
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        max_attempts: int = 4,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "prcrew",
            },
        )

    def close(self) -> None:
        self._client.close()

    def get_pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        response = self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        data = response.json()
        if not isinstance(data, dict):
            raise GitHubError("pull request payload was not an object")
        return data

    def get_pull_diff(self, owner: str, repo: str, number: int) -> str:
        response = self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        return response.text

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._get_paginated(f"/repos/{owner}/{repo}/issues/{number}/comments")

    def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return _as_object(response)

    def update_issue_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> dict[str, Any]:
        response = self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return _as_object(response)

    def list_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._get_paginated(f"/repos/{owner}/{repo}/pulls/{number}/reviews")

    def create_review(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        event: str,
        comments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "body": body,
            "event": event,
            "comments": comments,
        }
        response = self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json=payload,
        )
        return _as_object(response)

    def update_review(
        self, owner: str, repo: str, number: int, review_id: int, body: str
    ) -> dict[str, Any]:
        response = self._request(
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}",
            json={"body": body},
        )
        return _as_object(response)

    def _get_paginated(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url: str | None = path
        while url:
            response = self._request("GET", url)
            data = response.json()
            if not isinstance(data, list):
                raise GitHubError(f"expected a list from {path}")
            items.extend(item for item in data if isinstance(item, dict))
            url = _next_link(response.headers.get("link"))
        return items

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        def attempt() -> httpx.Response:
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                raise RetryableError(f"GitHub request timed out: {method} {url}") from exc
            except httpx.TransportError as exc:
                raise RetryableError(f"GitHub transport error: {exc}") from exc
            if response.status_code in _RETRYABLE_STATUS or _is_rate_limit(response):
                raise RetryableError(
                    f"GitHub {response.status_code} for {method} {url}",
                    retry_after=_retry_after_seconds(response),
                )
            if response.status_code == 401:
                raise GitHubError("GitHub rejected the token (401)")
            if response.status_code >= 400:
                detail = response.text[:500]
                raise GitHubError(f"GitHub {response.status_code} for {method} {url}: {detail}")
            return response

        return call_with_retries(
            attempt,
            attempts=self._max_attempts,
            sleeper=self._sleeper,
        )


def _as_object(response: httpx.Response) -> dict[str, Any]:
    data = response.json()
    if not isinstance(data, dict):
        raise GitHubError("GitHub payload was not an object")
    return data


def _is_rate_limit(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True
    text = response.text.lower()
    return "rate limit" in text


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            return None
    reset = response.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            return None
    return None


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        match = _NEXT_RE.search(part)
        if match:
            return match.group(1)
    return None
