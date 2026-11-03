# Contributing

Thanks for improving PR Review Crew. The project stays on free tiers: do not add a paid API as a required dependency.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Checks

```bash
ruff check src tests
ruff format src tests
mypy src
pytest
prcrew eval --strict
```

Tests must not open network connections. GitHub and LLM calls use `httpx.MockTransport` or the mock provider.

## Adding an eval case

1. Add `evals/dataset/<id>.yml` with a unified diff and labels (`file`, `line`, `category`, `contains`).
2. `contains` must appear on that added line. The harness rejects a label that does not.
3. Run `prcrew eval --verbose --case <id>`.
4. If the case is a bug the mock heuristics should catch, extend `src/prcrew/heuristics.py` and keep the rule narrow.
5. Leave at least one labelled miss in the dataset so recall is not a tautology. `subtle_boundary` is that miss today.

Matching is exact on file, category, and line.

## Adding a provider

Implement `complete(messages, *, timeout) -> Completion` and register it in `build_llm`. Record token counts when the API returns them. Retries go through `call_with_retries` and must not log the API key.

## Pull requests

The mock provider is the CI default. A change that drops macro precision below 0.80 or macro recall below 0.75 fails `prcrew eval --strict`.
