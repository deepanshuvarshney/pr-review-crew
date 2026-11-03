from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from prcrew.cli import main
from prcrew.config import load_settings
from prcrew.diffparse import parse_unified_diff
from prcrew.errors import PrcrewError, RetryableError
from prcrew.evalharness import run_eval
from prcrew.heuristics import review_diff
from prcrew.log import configure_logging, get_logger
from prcrew.retry import call_with_retries, run_with_timeout
from prcrew.server import create_app, verify_signature
from prcrew.static_analysis import run_static_analysis
from prcrew.usage import make_usage


def test_heuristics_ignore_safe_patterns() -> None:
    assert (
        review_diff(
            agent="security",
            added=[("a.py", 1, 'cur.execute("SELECT * FROM users WHERE name = %s", (name,))')],
            meta={},
            evidence=[],
        )
        == []
    )
    assert (
        review_diff(
            agent="security",
            added=[
                ("a.py", 1, '@app.delete("/admin/users/<int:uid>")'),
                ("a.py", 2, "@login_required"),
                ("a.py", 3, "def delete_user(uid):"),
            ],
            meta={},
            evidence=[],
        )
        == []
    )
    commented = '# API_KEY = "sk-live-ABCDEFGHIJKLMNOPQRSTUV"'
    assert review_diff(agent="security", added=[("a.py", 1, commented)], meta={}, evidence=[]) == []
    assert (
        review_diff(
            agent="security",
            added=[("a.py", 1, 'api_key = "your-api-key-value"')],
            meta={},
            evidence=[],
        )
        == []
    )
    unsafe = review_diff(
        agent="security", added=[("a.py", 4, "data = yaml.load(blob)")], meta={}, evidence=[]
    )
    assert unsafe and unsafe[0]["title"] == "Unsafe YAML load"
    executed = review_diff(
        agent="security", added=[("a.py", 2, "return eval(expr)")], meta={}, evidence=[]
    )
    assert executed and executed[0]["severity"] == "critical"
    assert (
        review_diff(
            agent="performance",
            added=[("a.py", 1, "for item in items:"), ("a.py", 2, "    total += 1")],
            meta={},
            evidence=[],
        )
        == []
    )
    assert (
        review_diff(
            agent="correctness",
            added=[("a.py", 1, "except ValueError:"), ("a.py", 2, "    raise")],
            meta={},
            evidence=[],
        )
        == []
    )


def test_retry_and_timeout_and_usage() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("later", retry_after=0.01)
        return "ok"

    slept: list[float] = []
    assert call_with_retries(flaky, attempts=4, sleeper=slept.append) == "ok"
    assert len(slept) == 2
    with pytest.raises(RetryableError):
        call_with_retries(
            lambda: (_ for _ in ()).throw(RetryableError("no")), attempts=2, sleeper=lambda _d: None
        )
    with pytest.raises(TimeoutError):
        run_with_timeout(lambda: __import__("time").sleep(2), 0.1)
    record = make_usage(
        provider="groq", model="m", agent="security", input_tokens=1_000_000, output_tokens=0
    )
    assert record.estimated_list_usd == pytest.approx(0.59)
    assert record.billed_usd == 0


def test_static_parsers_and_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    diff = """diff --git a/app/run.py b/app/run.py
new file mode 100644
--- /dev/null
+++ b/app/run.py
@@ -0,0 +1,2 @@
+def run(cmd):
+    return cmd
"""
    files = parse_unified_diff(diff)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "run.py").write_text("def run(cmd):\n    return cmd\n", encoding="utf-8")
    monkeypatch.setattr("prcrew.static_analysis.shutil.which", lambda name: None)
    assert run_static_analysis(files, tmp_path, ["ruff"]) == []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        tool = command[0]
        if tool == "ruff":
            payload = [
                {
                    "filename": "app/run.py",
                    "code": "E501",
                    "message": "long",
                    "location": {"row": 2},
                }
            ]
            return subprocess.CompletedProcess(command, 1, stdout=json.dumps(payload), stderr="")
        if tool == "semgrep":
            payload = {
                "results": [
                    {
                        "path": str(tmp_path / "app" / "run.py"),
                        "start": {"line": 99},
                        "check_id": "python.security",
                        "extra": {"message": "nope", "severity": "ERROR"},
                    }
                ]
            }
            return subprocess.CompletedProcess(command, 1, stdout=json.dumps(payload), stderr="")
        return subprocess.CompletedProcess(command, 2, stdout="not json", stderr="boom")

    monkeypatch.setattr("prcrew.static_analysis.shutil.which", lambda name: f"/usr/bin/{name}")
    evidence = run_static_analysis(files, tmp_path, ["ruff", "semgrep", "bandit"], runner=runner)
    assert [item.tool for item in evidence] == ["ruff"]
    assert evidence[0].line == 2


def test_webhook_signature_and_events() -> None:
    secret = "topsecret"
    seen: list[dict[str, object]] = []
    app = create_app(secret, on_pull_request=seen.append)
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    assert client.post("/webhook", content=b"{}").status_code == 401
    unsigned = create_app("", on_pull_request=seen.append)
    assert TestClient(unsigned).post("/webhook", content=b"{}").status_code == 503

    def signed(body: bytes, event: str) -> object:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": f"sha256={digest}", "X-GitHub-Event": event},
        )

    assert signed(b"{}", "ping").status_code == 200
    assert signed(b"not-json", "pull_request").status_code == 400
    ignored = signed(json.dumps({"action": "closed"}).encode(), "pull_request")
    assert ignored.status_code == 200
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 4},
    }
    response = signed(json.dumps(payload).encode(), "pull_request")
    assert response.status_code == 202
    assert seen and seen[0]["action"] == "opened"
    assert verify_signature(
        secret, b"abc", "sha256=" + hmac.new(secret.encode(), b"abc", hashlib.sha256).hexdigest()
    )


def test_cli_review_eval_and_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    diff = tmp_path / "change.diff"
    diff.write_text(Path("examples/demo.diff").read_text(encoding="utf-8"), encoding="utf-8")
    assert (
        main(
            [
                "review",
                str(diff),
                "--no-cache",
                "--no-static",
                "--config",
                str(tmp_path / "none.yml"),
            ]
        )
        == 0
    )
    captured = capsys.readouterr().out
    assert "request changes" in captured
    assert "Interpolated SQL" in captured
    assert main(["review", str(diff), "--format", "json", "--no-cache", "--no-static"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["review"]["verdict"] == "request_changes"
    assert main(["eval", "--strict", "--case", "sql_injection"]) == 0
    assert "security" in capsys.readouterr().out
    assert main(["review"]) == 1
    assert main(["--help"]) == 0

    called: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        called["args"] = args
        called["kwargs"] = kwargs

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setenv("PRCREW_WEBHOOK_SECRET", "s")
    assert main(["serve", "--host", "127.0.0.1", "--port", "9"]) == 0
    assert called["kwargs"]["port"] == 9  # type: ignore[index]


def test_eval_dataset_meets_the_floor(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path / "missing.yml",
        overrides={"provider": "mock", "cache_enabled": False, "static_analysis": False},
    )
    report = run_eval(settings)
    assert report.macro_precision is not None and report.macro_precision >= 0.8
    assert report.macro_recall is not None and report.macro_recall >= 0.75
    subtle = next(case for case in report.cases if case.case_id == "subtle_boundary")
    assert subtle.false_negative
    safe = next(case for case in report.cases if case.case_id == "safe_query")
    assert safe.false_positive == []
    assert safe.false_negative == []


def test_bad_label_anchor_fails(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "bad.yml").write_text(
        "id: bad\ndiff: |\n  diff --git a/a.py b/a.py\n  --- /dev/null\n  +++ b/a.py\n"
        "  @@ -0,0 +1 @@\n  +value = 1\n"
        'labels:\n  - file: a.py\n    line: 1\n    category: security\n    contains: "missing"\n',
        encoding="utf-8",
    )
    settings = load_settings(tmp_path / "missing.yml", overrides={"provider": "mock"})
    with pytest.raises(PrcrewError):
        run_eval(settings, dataset=dataset)


def test_logging_json(capsys: pytest.CaptureFixture[str]) -> None:
    import logging

    try:
        configure_logging("INFO", json_logs=True)
        get_logger("prcrew.demo").info("hello", extra={"event": "demo"})
        line = capsys.readouterr().err.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["message"] == "hello"
        assert payload["event"] == "demo"
        configure_logging("INFO", json_logs=False)
        get_logger("prcrew.demo").info("plain", extra={"event": "demo"})
        assert "plain" in capsys.readouterr().err
    finally:
        logger = logging.getLogger("prcrew")
        logger.handlers.clear()
        logger.propagate = True


def test_project_metadata_is_wired() -> None:
    action = yaml.safe_load(Path("action.yml").read_text(encoding="utf-8"))
    assert action["runs"]["using"] == "composite"
    assert "github-token" in action["inputs"]
    workflow = yaml.safe_load(Path(".github/workflows/pr-review.yml").read_text(encoding="utf-8"))
    assert workflow["jobs"]["review"]["steps"][1]["uses"] == "./"
    ci = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert "3.11" in ci["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    assert "MIT License" in Path("LICENSE").read_text(encoding="utf-8")
