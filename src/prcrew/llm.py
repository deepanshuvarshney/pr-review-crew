"""One completion interface over Groq, Gemini, Ollama, and the mock provider."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from prcrew.coerce import as_dict, as_int
from prcrew.config import Settings
from prcrew.errors import ProviderError, RetryableError
from prcrew.heuristics import review_diff
from prcrew.log import get_logger
from prcrew.models import ChatMessage, Completion
from prcrew.retry import call_with_retries

logger = get_logger(__name__)

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
_LINE_RE = re.compile(r"^\s*(\d+)\s+\|([+ -])(.*)$")
_EVIDENCE_RE = re.compile(r"^- ([^|]+?) \| ([^|]+?) \| ([^|]+?) \| ([^|]+?) \| (\d+) \| (.*)$")


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
        """Return a completion. Raise ProviderError when the call cannot be used."""


class MockProvider:
    """Heuristic stand-in. Deterministic, offline, and the CI default."""

    name = "mock"
    model = "mock-heuristic-v1"

    def __init__(self, model: str | None = None) -> None:
        if model:
            self.model = model

    def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
        del timeout
        user = next((message.content for message in messages if message.role == "user"), "")
        agent, meta, added, evidence = _parse_review_prompt(user)
        findings = review_diff(agent=agent, added=added, meta=meta, evidence=evidence)
        text = json.dumps({"findings": findings}, ensure_ascii=False)
        input_tokens = _approx_tokens("".join(message.content for message in messages))
        output_tokens = _approx_tokens(text)
        return Completion(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=self.model,
            provider=self.name,
        )


class GroqProvider:
    name = "groq"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float,
        max_attempts: int,
        base_url: str = "https://api.groq.com/openai/v1",
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "prcrew"},
        )

    def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
        del timeout
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 2000,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
        }

        def attempt() -> httpx.Response:
            return _post_json(self._client, "/chat/completions", payload)

        response = call_with_retries(attempt, attempts=self._max_attempts, sleeper=self._sleeper)
        body = _json_object(response)
        try:
            text = str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Groq response did not include a message") from exc
        usage = as_dict(body.get("usage"))
        return Completion(
            text=text,
            input_tokens=as_int(usage.get("prompt_tokens")),
            output_tokens=as_int(usage.get("completion_tokens")),
            model=self.model,
            provider=self.name,
        )


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float,
        max_attempts: int,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
        del timeout
        system = "\n".join(message.content for message in messages if message.role == "system")
        contents = [
            {
                "role": "user" if message.role == "user" else "model",
                "parts": [{"text": message.content}],
            }
            for message in messages
            if message.role != "system"
        ]
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 2000,
                "responseMimeType": "application/json",
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        path = f"/models/{self.model}:generateContent"

        def attempt() -> httpx.Response:
            return _post_json(
                self._client,
                path,
                payload,
                headers={"x-goog-api-key": self._api_key, "User-Agent": "prcrew"},
            )

        response = call_with_retries(attempt, attempts=self._max_attempts, sleeper=self._sleeper)
        body = _json_object(response)
        try:
            text = str(body["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Gemini response did not include text") from exc
        usage = as_dict(body.get("usageMetadata"))
        return Completion(
            text=text,
            input_tokens=as_int(usage.get("promptTokenCount")),
            output_tokens=as_int(usage.get("candidatesTokenCount")),
            model=self.model,
            provider=self.name,
        )


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        timeout: float,
        max_attempts: int,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
        del timeout
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "options": {"temperature": 0.1, "num_predict": 2000},
        }

        def attempt() -> httpx.Response:
            return _post_json(self._client, "/api/chat", payload)

        response = call_with_retries(attempt, attempts=self._max_attempts, sleeper=self._sleeper)
        body = _json_object(response)
        message = as_dict(body.get("message"))
        text = str(message.get("content") or "")
        if not text:
            raise ProviderError("Ollama response did not include a message")
        return Completion(
            text=text,
            input_tokens=as_int(body.get("prompt_eval_count")),
            output_tokens=as_int(body.get("eval_count")),
            model=self.model,
            provider=self.name,
        )


def build_llm(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> LLMProvider:
    provider = settings.provider
    model = settings.resolved_model()
    timeout = settings.llm_timeout_seconds
    attempts = settings.max_attempts
    if provider == "mock":
        return MockProvider(model)
    if provider == "groq":
        key = os.environ.get("GROQ_API_KEY", "")
        if not key:
            raise ProviderError("GROQ_API_KEY is not set")
        return GroqProvider(
            key,
            model,
            timeout=timeout,
            max_attempts=attempts,
            transport=transport,
            sleeper=sleeper,
        )
    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        if not key:
            raise ProviderError("GEMINI_API_KEY is not set")
        return GeminiProvider(
            key,
            model,
            timeout=timeout,
            max_attempts=attempts,
            transport=transport,
            sleeper=sleeper,
        )
    if provider == "ollama":
        return OllamaProvider(
            model,
            base_url=settings.ollama_base_url,
            timeout=timeout,
            max_attempts=attempts,
            transport=transport,
            sleeper=sleeper,
        )
    raise ProviderError(f"unknown provider {provider}")


def extract_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rfind("```")]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def _post_json(
    client: httpx.Client,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        response = client.post(path, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise RetryableError("LLM request timed out") from exc
    except httpx.TransportError as exc:
        raise RetryableError(f"LLM transport error: {exc}") from exc
    if response.status_code in _RETRYABLE_STATUS:
        retry_after = response.headers.get("Retry-After")
        delay = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        raise RetryableError(f"LLM HTTP {response.status_code}", retry_after=delay)
    if response.status_code >= 400:
        raise ProviderError(f"LLM HTTP {response.status_code}: {response.text[:300]}")
    return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise ProviderError("LLM response was not JSON") from exc
    if not isinstance(body, dict):
        raise ProviderError("LLM response was not an object")
    return body


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _parse_review_prompt(
    text: str,
) -> tuple[str, dict[str, str], list[tuple[str, int, str]], list[dict[str, str]]]:
    agent = ""
    meta: dict[str, str] = {}
    section = "meta"
    diff_lines: list[str] = []
    evidence: list[dict[str, str]] = []
    for line in text.splitlines():
        if line.startswith("AGENT:"):
            agent = line.split(":", 1)[1].strip()
            continue
        if line.startswith("DIFF:"):
            section = "diff"
            continue
        if line.startswith("STATIC_EVIDENCE:"):
            section = "evidence"
            continue
        if section == "evidence":
            match = _EVIDENCE_RE.match(line)
            if match:
                evidence.append(
                    {
                        "tool": match.group(1).strip(),
                        "rule": match.group(2).strip(),
                        "severity": match.group(3).strip(),
                        "file": match.group(4).strip(),
                        "line": match.group(5),
                        "message": match.group(6).strip(),
                    }
                )
            continue
        if section == "diff":
            diff_lines.append(line)
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    current_file = ""
    added: list[tuple[str, int, str]] = []
    for line in diff_lines:
        if line.startswith("FILE: "):
            current_file = line[len("FILE: ") :].strip()
            continue
        match = _LINE_RE.match(line)
        if match and current_file and match.group(2) == "+":
            added.append((current_file, int(match.group(1)), match.group(3)))
    return agent, meta, added, evidence
