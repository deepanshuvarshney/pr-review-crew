"""Command line: ``prcrew review``, ``prcrew eval``, ``prcrew serve``."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from prcrew import __version__
from prcrew.config import load_settings
from prcrew.errors import PrcrewError
from prcrew.evalharness import render_report, run_eval
from prcrew.log import configure_logging
from prcrew.models import ReviewTarget
from prcrew.service import run_review

_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 2
    configure_logging()
    try:
        return int(args.func(args))
    except PrcrewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prcrew",
        description="Multi-agent pull request review that runs on free-tier models or fully offline.",
    )
    parser.add_argument("--version", action="version", version=f"prcrew {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="review a pull request or a unified diff")
    review.add_argument(
        "target",
        nargs="?",
        help="owner/repo#number, a diff file, or - for stdin",
    )
    review.add_argument("--diff", help="path to a unified diff, or - for stdin")
    review.add_argument("--config", type=Path, help="path to .prcrew.yml")
    review.add_argument("--provider", choices=["auto", "mock", "groq", "gemini", "ollama"])
    review.add_argument("--model")
    review.add_argument("--severity", choices=["info", "low", "medium", "high", "critical"])
    review.add_argument("--max-comments", type=int)
    review.add_argument("--dry-run", action="store_true", help="print Markdown and do not post")
    review.add_argument("--post", action="store_true", help="post even if the default would not")
    review.add_argument("--no-cache", action="store_true")
    review.add_argument("--no-static", action="store_true")
    review.add_argument("--local-root", type=Path)
    review.add_argument("--format", choices=["markdown", "json"], default="markdown")
    review.set_defaults(func=_cmd_review)

    evaluate = sub.add_parser("eval", help="score the crew against the labelled diff dataset")
    evaluate.add_argument("--dataset", type=Path)
    evaluate.add_argument("--provider", choices=["auto", "mock", "groq", "gemini", "ollama"])
    evaluate.add_argument("--model")
    evaluate.add_argument("--config", type=Path)
    evaluate.add_argument("--case", action="append", help="run a single case id")
    evaluate.add_argument("--json", action="store_true")
    evaluate.add_argument("--verbose", action="store_true")
    evaluate.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless macro precision is at least 0.8 and recall at least 0.75",
    )
    evaluate.set_defaults(func=_cmd_eval)

    serve = sub.add_parser("serve", help="GitHub App webhook server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--config", type=Path)
    serve.set_defaults(func=_cmd_serve)
    return parser


def _cmd_review(args: argparse.Namespace) -> int:
    if args.dry_run and args.post:
        raise PrcrewError("--dry-run and --post cannot be combined")
    target = _load_target(args)
    overrides: dict[str, Any] = {}
    if args.provider:
        overrides["provider"] = args.provider
    if args.model:
        overrides["model"] = args.model
    if args.severity:
        overrides["severity_threshold"] = args.severity
    if args.max_comments is not None:
        overrides["max_comments"] = args.max_comments
    if args.no_static:
        overrides["static_analysis"] = False
    if args.no_cache:
        overrides["cache_enabled"] = False
    settings = load_settings(args.config, overrides=overrides)
    if args.dry_run:
        dry_run = True
    elif args.post:
        dry_run = False
    else:
        dry_run = target.number is None
    if not dry_run and target.number is None:
        raise PrcrewError("--post requires a pull request target like owner/repo#123")
    completed = run_review(
        target,
        settings,
        dry_run=dry_run,
        local_root=args.local_root or Path.cwd(),
    )
    if args.format == "json":
        print(
            json.dumps(
                {
                    "review": completed.review.model_dump(mode="json"),
                    "publish": completed.publish.model_dump(mode="json"),
                },
                indent=2,
            )
        )
    else:
        sys.stdout.write(completed.publish.markdown)
        if completed.publish.mode != "dry_run":
            print(f"published: {completed.publish.mode} {completed.publish.detail}")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    overrides: dict[str, Any] = {"static_analysis": False, "cache_enabled": False}
    if args.provider:
        overrides["provider"] = args.provider
    if args.model:
        overrides["model"] = args.model
    settings = load_settings(args.config, overrides=overrides)
    report = run_eval(settings, dataset=args.dataset, only=args.case)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_report(report, verbose=args.verbose))
    if not args.strict:
        return 0
    precision = report.macro_precision
    recall = report.macro_recall
    if precision is None or recall is None or precision < 0.8 or recall < 0.75:
        print(
            f"eval below floor: precision={precision} recall={recall}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError as exc:
        raise PrcrewError("uvicorn is required to serve webhooks") from exc
    from prcrew.server import create_app

    secret = os.environ.get("PRCREW_WEBHOOK_SECRET", "")
    app = create_app(secret, config_path=args.config)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _load_target(args: argparse.Namespace) -> ReviewTarget:
    raw = args.diff or args.target
    if not raw:
        raise PrcrewError("pass a pull request (owner/repo#123) or a diff file")
    if args.diff and args.target:
        raise PrcrewError("pass either a target or --diff, not both")
    match = _TARGET_RE.fullmatch(raw)
    if match:
        return ReviewTarget(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))
    if raw == "-":
        return ReviewTarget(diff_text=sys.stdin.read())
    path = Path(raw)
    if path.is_file():
        return ReviewTarget(diff_text=path.read_text(encoding="utf-8"))
    raise PrcrewError(f"not a pull request reference or a diff file: {raw}")
