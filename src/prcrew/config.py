"""Load ``.prcrew.yml`` with env and CLI overrides.

Precedence is CLI overrides, then environment variables, then the file, then
built-in defaults. ``ignore_paths`` in the file replaces the default list
rather than extending it. Lockfiles, binaries, and generated files are always
skipped in addition to that list.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from prcrew.errors import ConfigError
from prcrew.models import AGENT_NAMES, Severity

DEFAULT_IGNORE_PATHS: list[str] = [
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
    "Pipfile.lock",
    "uv.lock",
    "*.min.js",
    "*.min.css",
    "**/dist/**",
    "**/build/**",
    "**/vendor/**",
    "**/node_modules/**",
    "**/generated/**",
    "**/*_generated.*",
    "**/*.generated.*",
    "**/*.pb.go",
    "**/*.pb.py",
]

DEFAULT_MODELS: dict[str, str] = {
    "mock": "mock-heuristic-v1",
    "groq": "openai/gpt-oss-120b",
    "gemini": "gemini-2.5-flash",
    "ollama": "llama3.2",
}


class AgentToggles(BaseModel):
    security: bool = True
    correctness: bool = True
    performance: bool = True
    tests: bool = True
    maintainability: bool = True

    def enabled(self) -> list[str]:
        return [name for name in AGENT_NAMES if getattr(self, name)]


class Settings(BaseModel):
    enabled: bool = True
    provider: str = "mock"
    model: str | None = None
    agents: AgentToggles = Field(default_factory=AgentToggles)
    severity_threshold: Severity = Severity.low
    confidence_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    max_comments: int = Field(default=12, ge=0)
    max_diff_tokens: int = Field(default=8000, ge=20)
    ignore_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE_PATHS))
    llm_timeout_seconds: float = Field(default=45.0, gt=0)
    github_timeout_seconds: float = Field(default=30.0, gt=0)
    agent_timeout_seconds: float = Field(default=120.0, gt=0)
    static_timeout_seconds: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=4, ge=1)
    static_analysis: bool = True
    static_tools: list[str] = Field(default_factory=lambda: ["ruff", "bandit", "semgrep"])
    cache_enabled: bool = True
    cache_dir: str = ".prcrew/cache"
    ollama_base_url: str = "http://127.0.0.1:11434"

    def fingerprint(self) -> str:
        """Stable identity of the knobs that change a review."""

        payload = {
            "provider": self.provider,
            "model": self.model,
            "agents": self.agents.model_dump(),
            "severity_threshold": self.severity_threshold.value,
            "confidence_threshold": self.confidence_threshold,
            "max_comments": self.max_comments,
            "max_diff_tokens": self.max_diff_tokens,
            "ignore_paths": self.ignore_paths,
            "static_analysis": self.static_analysis,
            "static_tools": self.static_tools,
        }
        return str(sorted(payload.items()))

    def resolved_model(self) -> str:
        if self.model:
            return self.model
        return DEFAULT_MODELS.get(self.provider, "mock-heuristic-v1")


def _deep_update(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _deep_update(current, value)
        else:
            merged[key] = value
    return merged


def _from_env() -> dict[str, Any]:
    out: dict[str, Any] = {}
    if value := os.environ.get("PRCREW_PROVIDER"):
        out["provider"] = value
    if value := os.environ.get("PRCREW_MODEL"):
        out["model"] = value
    if value := os.environ.get("PRCREW_SEVERITY"):
        out["severity_threshold"] = value
    if value := os.environ.get("PRCREW_MAX_COMMENTS"):
        out["max_comments"] = int(value)
    if value := os.environ.get("PRCREW_CONFIDENCE"):
        out["confidence_threshold"] = float(value)
    if value := os.environ.get("PRCREW_CACHE_DIR"):
        out["cache_dir"] = value
    if value := os.environ.get("OLLAMA_HOST"):
        out["ollama_base_url"] = value
    if value := os.environ.get("PRCREW_DISABLE_AGENTS"):
        agents: dict[str, bool] = {}
        for name in value.split(","):
            cleaned = name.strip()
            if cleaned:
                if cleaned not in AGENT_NAMES:
                    raise ConfigError(f"unknown agent in PRCREW_DISABLE_AGENTS: {cleaned}")
                agents[cleaned] = False
        if agents:
            out["agents"] = agents
    return out


def load_settings(
    path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load settings. A missing file is fine; a present file must be a mapping."""

    config_path = path if path is not None else Path(".prcrew.yml")
    file_data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_path} must be a mapping")
        file_data = loaded
    data = _deep_update(file_data, _from_env())
    if overrides:
        clean = {key: value for key, value in overrides.items() if value is not None}
        data = _deep_update(data, clean)
    try:
        return Settings.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - re-raised as ConfigError
        raise ConfigError(str(exc)) from exc


def resolve_provider(settings: Settings) -> Settings:
    """Turn ``auto`` into a concrete provider based on which free-tier key is set."""

    provider = settings.provider
    if provider == "auto":
        if os.environ.get("GROQ_API_KEY"):
            provider = "groq"
        elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            provider = "gemini"
        else:
            provider = "mock"
    if provider not in DEFAULT_MODELS:
        raise ConfigError(
            f"unknown provider {provider!r}; expected auto, mock, groq, gemini, or ollama"
        )
    model = settings.model or DEFAULT_MODELS[provider]
    return settings.model_copy(update={"provider": provider, "model": model})
