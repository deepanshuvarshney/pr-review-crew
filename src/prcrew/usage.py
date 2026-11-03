"""Token accounting. Free-tier providers bill $0; list price is illustrative."""

from __future__ import annotations

from prcrew.models import UsageRecord

# USD per 1,000,000 tokens, (input, output). Figures are public list-price
# ballparks so a run can show relative cost. They are not invoices.
LIST_PRICE_PER_MILLION: dict[str, tuple[float, float]] = {
    "mock": (0.0, 0.0),
    "groq": (0.59, 0.79),
    "gemini": (0.15, 0.60),
    "ollama": (0.0, 0.0),
}

FREE_TIER_PROVIDERS = frozenset(LIST_PRICE_PER_MILLION)


def estimate_list_usd(provider: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = LIST_PRICE_PER_MILLION.get(provider, (0.0, 0.0))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def billed_usd(provider: str, list_usd: float) -> float:
    if provider in FREE_TIER_PROVIDERS:
        return 0.0
    return list_usd


def make_usage(
    *,
    provider: str,
    model: str,
    agent: str,
    input_tokens: int,
    output_tokens: int,
) -> UsageRecord:
    listed = estimate_list_usd(provider, input_tokens, output_tokens)
    return UsageRecord(
        provider=provider,
        model=model,
        agent=agent,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_list_usd=listed,
        billed_usd=billed_usd(provider, listed),
    )


def summarize_usage(records: list[UsageRecord]) -> dict[str, float | int]:
    return {
        "calls": len(records),
        "input_tokens": sum(item.input_tokens for item in records),
        "output_tokens": sum(item.output_tokens for item in records),
        "estimated_list_usd": sum(item.estimated_list_usd for item in records),
        "billed_usd": sum(item.billed_usd for item in records),
    }
