"""Cache a finished review by head SHA and the config fingerprint."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from prcrew.log import get_logger
from prcrew.models import ReviewResult

logger = get_logger(__name__)


def cache_file(cache_dir: str, sha: str, fingerprint: str) -> Path:
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
    safe_sha = "".join(char for char in sha if char.isalnum())[:64]
    return Path(cache_dir) / f"{safe_sha}-{digest}.json"


def load_review(cache_dir: str, sha: str, fingerprint: str) -> ReviewResult | None:
    path = cache_file(cache_dir, sha, fingerprint)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        review = ReviewResult.model_validate(data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("cache read failed", extra={"event": "cache_read_failed", "error": str(exc)})
        return None
    return review.model_copy(update={"cached": True})


def save_review(cache_dir: str, review: ReviewResult, fingerprint: str) -> None:
    if not review.head_sha:
        return
    path = cache_file(cache_dir, review.head_sha, fingerprint)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(review.model_dump_json(indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "cache write failed", extra={"event": "cache_write_failed", "error": str(exc)}
        )
