"""Project error types."""


class PrcrewError(Exception):
    """Base error for expected operational failures."""


class ConfigError(PrcrewError):
    """The config file or an override is invalid."""


class DiffParseError(PrcrewError):
    """The input is not a unified diff we can map onto lines."""


class GitHubError(PrcrewError):
    """The GitHub API returned an error we will not retry."""


class ProviderError(PrcrewError):
    """An LLM provider failed after retries, or was misconfigured."""


class RetryableError(PrcrewError):
    """A transient failure. ``retry_after`` is seconds to wait, when known."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
