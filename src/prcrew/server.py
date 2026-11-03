"""FastAPI webhook for a GitHub App. The signature is required."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from prcrew import __version__
from prcrew.coerce import as_dict
from prcrew.config import load_settings
from prcrew.log import get_logger
from prcrew.models import ReviewTarget
from prcrew.service import run_review

logger = get_logger(__name__)

PullHandler = Callable[[dict[str, Any]], None]


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    if not header.startswith("sha256=") or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = "sha256=" + digest
    return hmac.compare_digest(expected, header)


def create_app(
    secret: str,
    *,
    on_pull_request: PullHandler | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="PR Review Crew", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
        if not secret:
            return JSONResponse({"error": "webhook secret is not configured"}, status_code=503)
        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(secret, body, signature):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return JSONResponse({"ok": True})
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if event != "pull_request":
            return JSONResponse({"ok": True, "ignored": event})
        action = str(payload.get("action") or "")
        if action not in {"opened", "synchronize", "reopened"}:
            return JSONResponse({"ok": True, "ignored": action})
        handler = on_pull_request or _default_handler(config_path)
        background.add_task(_safe, handler, payload)
        return JSONResponse({"ok": True, "queued": True}, status_code=202)

    return app


def _default_handler(config_path: Path | None) -> PullHandler:
    def handle(payload: dict[str, Any]) -> None:
        repository = as_dict(payload.get("repository"))
        pull = as_dict(payload.get("pull_request"))
        full_name = str(repository.get("full_name") or "")
        number = pull.get("number")
        if "/" not in full_name or not isinstance(number, int):
            raise ValueError("webhook payload is missing repository or pull number")
        owner, repo = full_name.split("/", 1)
        settings = load_settings(config_path)
        run_review(
            ReviewTarget(owner=owner, repo=repo, number=number),
            settings,
            dry_run=False,
        )

    return handle


def _safe(handler: PullHandler, payload: dict[str, Any]) -> None:
    try:
        handler(payload)
    except Exception:
        logger.exception("webhook review failed", extra={"event": "webhook_failed"})
