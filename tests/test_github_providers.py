from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from prcrew.config import load_settings
from prcrew.errors import GitHubError, ProviderError
from prcrew.github_api import GitHubClient
from prcrew.llm import (
    Completion,
    GeminiProvider,
    GroqProvider,
    MockProvider,
    OllamaProvider,
    build_llm,
    extract_json,
)
from prcrew.models import ChatMessage, ReviewTarget
from prcrew.service import run_review

TODO = """diff --git a/app/note.py b/app/note.py
new file mode 100644
--- /dev/null
+++ b/app/note.py
@@ -0,0 +1,2 @@
+# TODO: explain this constant before release
+VALUE = 1
"""


def _settings(tmp_path: Path, **overrides: object):
    data: dict[str, object] = {
        "cache_enabled": True,
        "static_analysis": False,
        "cache_dir": str(tmp_path / "cache"),
        "provider": "mock",
    }
    data.update(overrides)
    return load_settings(tmp_path / "missing.yml", overrides=data)


def test_publish_updates_the_same_review(tmp_path: Path) -> None:
    box: dict[str, list] = {"comments": [], "reviews": [], "events": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = json.loads(request.content) if request.content and method != "GET" else {}
        if path == "/repos/acme/widgets/pulls/7" and method == "GET":
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text=TODO)
            return httpx.Response(
                200,
                json={
                    "title": "Nit",
                    "html_url": "https://example.test/7",
                    "head": {"sha": "abc123def456", "ref": "nit"},
                    "base": {"ref": "main"},
                },
            )
        if path == "/repos/acme/widgets/issues/7/comments" and method == "GET":
            return httpx.Response(200, json=box["comments"])
        if path == "/repos/acme/widgets/issues/7/comments" and method == "POST":
            comment = {"id": 4, "body": body["body"]}
            box["comments"].append(comment)
            return httpx.Response(201, json=comment)
        if path == "/repos/acme/widgets/issues/comments/4" and method == "PATCH":
            box["comments"][0]["body"] = body["body"]
            return httpx.Response(200, json=box["comments"][0])
        if path == "/repos/acme/widgets/pulls/7/reviews" and method == "GET":
            return httpx.Response(200, json=box["reviews"])
        if path == "/repos/acme/widgets/pulls/7/reviews" and method == "POST":
            box["events"].append(body["event"])
            if body["event"] == "APPROVE":
                return httpx.Response(422, text="approve not allowed")
            review = {"id": 8, "body": body["body"]}
            box["reviews"].append(review)
            return httpx.Response(200, json=review)
        if path == "/repos/acme/widgets/pulls/7/reviews/8" and method == "PUT":
            return httpx.Response(200, json={"id": 8, "body": body["body"]})
        return httpx.Response(404, text=f"unexpected {method} {path}")

    client = GitHubClient(
        "token",
        base_url="https://github.test",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _delay: None,
    )
    target = ReviewTarget(owner="acme", repo="widgets", number=7)
    settings = _settings(tmp_path)
    first = run_review(target, settings, dry_run=False, github=client)
    assert first.publish.mode == "posted"
    assert first.publish.created_review is True
    assert box["events"] == ["APPROVE", "COMMENT"]
    second = run_review(target, settings, dry_run=False, github=client)
    assert second.publish.mode == "updated"
    assert second.review.cached is True
    assert box["events"] == ["APPROVE", "COMMENT"]
    assert len(box["comments"]) == 1


def test_inline_rejection_keeps_the_summary(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/pulls/3") and method == "GET":
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text=TODO)
            return httpx.Response(
                200, json={"title": "Nit", "head": {"sha": "fff", "ref": "n"}, "base": {}}
            )
        if method == "GET":
            return httpx.Response(200, json=[])
        if method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={"id": 1})
        if method == "POST" and path.endswith("/reviews"):
            return httpx.Response(422, text="line not part of diff")
        return httpx.Response(404, text=path)

    client = GitHubClient(
        "t",
        base_url="https://github.test",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _d: None,
    )
    result = run_review(
        ReviewTarget(owner="acme", repo="widgets", number=3),
        _settings(tmp_path, cache_enabled=False),
        dry_run=False,
        github=client,
    )
    assert result.publish.mode == "summary_only"


def test_github_retries_rate_limits_and_stops_on_401() -> None:
    calls = {"n": 0}

    def limited(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                403,
                text="rate limit",
                headers={"X-RateLimit-Remaining": "0", "Retry-After": "0"},
            )
        return httpx.Response(200, json={"title": "ok", "head": {"sha": "abc"}, "base": {}})

    slept: list[float] = []
    client = GitHubClient(
        "t",
        base_url="https://github.test",
        transport=httpx.MockTransport(limited),
        sleeper=slept.append,
        max_attempts=3,
    )
    assert client.get_pull("acme", "widgets", 1)["title"] == "ok"
    assert slept

    def denied(request: httpx.Request) -> httpx.Response:
        calls["denied"] = calls.get("denied", 0) + 1
        return httpx.Response(401, text="nope")

    client = GitHubClient(
        "t",
        base_url="https://github.test",
        transport=httpx.MockTransport(denied),
        sleeper=lambda _d: None,
    )
    with pytest.raises(GitHubError):
        client.get_pull("acme", "widgets", 1)
    assert calls["denied"] == 1


def test_github_paginates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return httpx.Response(200, json=[{"id": 2, "body": "b"}])
        return httpx.Response(
            200,
            json=[{"id": 1, "body": "a"}],
            headers={
                "link": '<https://github.test/repos/acme/widgets/issues/1/comments?page=2>; rel="next"'
            },
        )

    client = GitHubClient(
        "t", base_url="https://github.test", transport=httpx.MockTransport(handler)
    )
    comments = client.list_issue_comments("acme", "widgets", 1)
    assert [item["id"] for item in comments] == [1, 2]


def test_groq_retries_then_reads_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.headers["authorization"] == "Bearer secret"
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == "json_object"
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"findings": []}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4},
            },
        )

    monkeypatch.setenv("GROQ_API_KEY", "secret")
    provider = GroqProvider(
        "secret",
        "llama",
        timeout=5,
        max_attempts=3,
        base_url="https://groq.test/v1",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _d: None,
    )
    completion = provider.complete([ChatMessage(role="user", content="hi")], timeout=5)
    assert isinstance(completion, Completion)
    assert completion.input_tokens == 11
    assert calls["n"] == 2
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderError):
        build_llm(load_settings(Path("/nope.yml"), overrides={"provider": "groq"}))


def test_gemini_and_ollama_parse_usage() -> None:
    def gemini(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "gkey"
        assert request.url.path.endswith(":generateContent")
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": '{"findings": []}'}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
            },
        )

    def ollama(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["format"] == "json"
        return httpx.Response(
            200,
            json={
                "message": {"content": '{"findings": []}'},
                "prompt_eval_count": 3,
                "eval_count": 1,
            },
        )

    gemini_provider = GeminiProvider(
        "gkey",
        "gemini-2.5-flash",
        timeout=5,
        max_attempts=1,
        base_url="https://gemini.test/v1beta",
        transport=httpx.MockTransport(gemini),
        sleeper=lambda _d: None,
    )
    ollama_provider = OllamaProvider(
        "llama3.2",
        base_url="https://ollama.test",
        timeout=5,
        max_attempts=1,
        transport=httpx.MockTransport(ollama),
        sleeper=lambda _d: None,
    )
    assert (
        gemini_provider.complete([ChatMessage(role="user", content="hi")], timeout=5).output_tokens
        == 2
    )
    assert (
        ollama_provider.complete([ChatMessage(role="system", content="s")], timeout=5).input_tokens
        == 3
    )


def test_provider_errors_and_json_fence() -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    provider = GroqProvider(
        "k",
        "m",
        timeout=5,
        max_attempts=2,
        base_url="https://groq.test/v1",
        transport=httpx.MockTransport(bad),
        sleeper=lambda _d: None,
    )
    with pytest.raises(ProviderError):
        provider.complete([ChatMessage(role="user", content="hi")], timeout=5)
    assert extract_json('noise ```json\n{"findings": []}\n```') == {"findings": []}
    first = MockProvider().complete(
        [ChatMessage(role="user", content="AGENT: security\nDIFF:\n")], timeout=1
    )
    second = MockProvider().complete(
        [ChatMessage(role="user", content="AGENT: security\nDIFF:\n")], timeout=1
    )
    assert first.text == second.text


def test_build_llm_requires_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    settings = load_settings(Path("/nope.yml"), overrides={"provider": "gemini"})
    with pytest.raises(ProviderError):
        build_llm(settings)
