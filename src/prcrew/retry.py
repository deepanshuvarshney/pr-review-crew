"""Exponential backoff and a daemon-thread timeout that does not cancel work."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from prcrew.errors import RetryableError

T = TypeVar("T")


def call_with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 20.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` until it succeeds or retryable failures are exhausted.

    Non-retryable exceptions propagate immediately. ``RetryableError.retry_after``
    overrides the computed delay when the server asks us to wait longer.
    """

    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last: RetryableError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except RetryableError as exc:
            last = exc
            if attempt == attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            if exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            sleeper(delay)
    if last is None:
        raise RuntimeError("retry loop exited without a result")
    raise last


def run_with_timeout(fn: Callable[[], T], timeout: float) -> T:
    """Run ``fn`` on a daemon thread and raise ``TimeoutError`` if it overruns.

    The worker is not killed; HTTP clients bound their own sockets so the thread
    ends. Daemon threads do not block process shutdown.
    """

    if timeout <= 0:
        return fn()
    outcome: dict[str, T] = {}
    error: dict[str, BaseException] = {}

    def target() -> None:
        try:
            outcome["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - propagated to the caller
            error["exc"] = exc

    thread = threading.Thread(target=target, name="prcrew-timeout", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"timed out after {timeout:.1f}s")
    if "exc" in error:
        raise error["exc"]
    return outcome["value"]
