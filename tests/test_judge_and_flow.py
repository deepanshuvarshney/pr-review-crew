from __future__ import annotations

import time
from pathlib import Path

import pytest

from prcrew.config import load_settings, resolve_provider
from prcrew.errors import ProviderError
from prcrew.judge import adjudicate
from prcrew.llm import MockProvider
from prcrew.models import (
    Category,
    ChatMessage,
    Completion,
    Finding,
    ReviewTarget,
    Severity,
    Verdict,
)
from prcrew.service import run_review

SQL = """diff --git a/app/db.py b/app/db.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/app/db.py
@@ -0,0 +1,3 @@
+def get_user(name):
+    cur.execute(f"SELECT * FROM users WHERE name = '{name}'")
+    return cur.fetchone()
"""

DOCS = """diff --git a/app/db.py b/app/db.py
index 1111111..2222222 100644
--- a/app/db.py
+++ b/app/db.py
@@ -1,2 +1,3 @@
+\"\"\"Look up a user by name.\"\"\"
 def get_user(name):
     return name
"""


def _settings(tmp_path: Path, **overrides: object) -> object:
    data = {
        "cache_enabled": False,
        "static_analysis": False,
        "cache_dir": str(tmp_path / "cache"),
        "provider": "mock",
    }
    data.update(overrides)
    return load_settings(tmp_path / "missing.yml", overrides=data)


def test_mock_review_requests_changes_and_suggests_a_fix(tmp_path: Path) -> None:
    demo = Path("examples/demo.diff").read_text(encoding="utf-8")
    completed = run_review(
        ReviewTarget(diff_text=demo),
        _settings(tmp_path),  # type: ignore[arg-type]
        dry_run=True,
    )
    categories = {item.category for item in completed.review.findings}
    assert Category.security in categories
    assert Category.performance in categories
    assert Category.tests in categories
    assert completed.review.verdict == Verdict.request_changes
    security = next(
        item for item in completed.review.findings if item.category == Category.security
    )
    assert security.suggested_fix is not None
    assert security.suggested_fix.startswith("    cur.execute")
    assert "%s" in security.suggested_fix
    assert "```suggestion" in completed.publish.markdown
    assert completed.publish.mode == "dry_run"


def test_docs_only_approves(tmp_path: Path) -> None:
    completed = run_review(
        ReviewTarget(diff_text=DOCS),
        _settings(tmp_path),  # type: ignore[arg-type]
        dry_run=True,
    )
    assert completed.review.findings == []
    assert completed.review.verdict == Verdict.approve


def test_judge_drops_hallucinations_duplicates_and_caps(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_comments=1, confidence_threshold=0.5)  # type: ignore[arg-type]
    files = [
        file
        for file in __import__(
            "prcrew.diffparse", fromlist=["parse_unified_diff"]
        ).parse_unified_diff(SQL)
    ]
    findings = [
        Finding(
            file="app/db.py",
            line=2,
            severity=Severity.high,
            category=Category.security,
            confidence=0.9,
            explanation="Interpolated SQL reaches the driver.",
            title="Interpolated SQL",
            agent="security",
        ),
        Finding(
            file="app/db.py",
            line=2,
            severity=Severity.medium,
            category=Category.security,
            confidence=0.6,
            explanation="Interpolated SQL reaches the driver.",
            title="again",
            agent="security",
        ),
        Finding(
            file="missing.py",
            line=1,
            severity=Severity.critical,
            category=Category.security,
            confidence=0.99,
            explanation="not in the diff",
            title="ghost",
            agent="security",
        ),
        Finding(
            file="app/db.py",
            line=999,
            severity=Severity.high,
            category=Category.correctness,
            confidence=0.9,
            explanation="line does not exist",
            title="ghost line",
            agent="correctness",
        ),
        Finding(
            file="app/db.py",
            line=1,
            severity=Severity.info,
            category=Category.maintainability,
            confidence=0.2,
            explanation="nit",
            title="nit",
            agent="maintainability",
        ),
        Finding(
            file="app/db.py",
            line=3,
            severity=Severity.medium,
            category=Category.tests,
            confidence=0.8,
            explanation="Add a test for the empty name.",
            title="Missing test coverage",
            agent="tests",
        ),
    ]
    review, _usage = adjudicate(
        findings=findings,
        files=files,
        skipped=0,
        errors=[],
        usage=[],
        settings=settings,
        head_sha="abc",
        title="Add query",
        llm=MockProvider(),
    )
    assert len(review.findings) == 1
    assert review.findings[0].severity == Severity.high
    reasons = {item.reason for item in review.dropped}
    assert {"duplicate", "unknown_file", "unverified_line", "over_cap"} <= reasons
    assert review.verdict == Verdict.request_changes


def test_failed_security_agent_does_not_approve(tmp_path: Path) -> None:
    class Flaky:
        name = "mock"
        model = "mock-heuristic-v1"

        def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
            if any(message.content.startswith("AGENT: security") for message in messages):
                raise ProviderError("security down")
            return MockProvider().complete(messages, timeout=timeout)

    completed = run_review(
        ReviewTarget(diff_text=SQL),
        _settings(tmp_path),  # type: ignore[arg-type]
        dry_run=True,
        llm=Flaky(),
    )
    assert completed.review.agent_errors[0]["agent"] == "security"
    assert completed.review.verdict == Verdict.comment
    assert any(item.category == Category.tests for item in completed.review.findings)


def test_agent_timeout_is_isolated(tmp_path: Path) -> None:
    class Slow:
        name = "mock"
        model = "mock-heuristic-v1"

        def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
            time.sleep(5)
            return MockProvider().complete(messages, timeout=timeout)

    completed = run_review(
        ReviewTarget(diff_text=SQL),
        _settings(tmp_path, agent_timeout_seconds=0.2),  # type: ignore[arg-type]
        dry_run=True,
        llm=Slow(),
    )
    assert len(completed.review.agent_errors) == 5
    assert completed.review.verdict == Verdict.comment
    assert completed.review.findings == []


def test_cache_skips_the_model(tmp_path: Path) -> None:
    class Counting(MockProvider):
        calls = 0

        def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
            Counting.calls += 1
            return super().complete(messages, timeout=timeout)

    settings = _settings(tmp_path, cache_enabled=True)  # type: ignore[arg-type]
    target = ReviewTarget(diff_text=SQL)
    first = run_review(target, settings, dry_run=True, llm=Counting())
    calls_after_first = Counting.calls
    assert calls_after_first > 0
    assert first.review.cached is False
    second = run_review(target, settings, dry_run=True, llm=Counting())
    assert Counting.calls == calls_after_first
    assert second.review.cached is True
    assert second.review.verdict == first.review.verdict


def test_disabled_config_skips_work(tmp_path: Path) -> None:
    completed = run_review(
        ReviewTarget(diff_text=SQL),
        _settings(tmp_path, enabled=False),  # type: ignore[arg-type]
        dry_run=True,
    )
    assert completed.publish.mode == "skipped"
    assert completed.review.findings == []


def test_hosted_summary_falls_back_when_the_draft_fails(tmp_path: Path) -> None:
    class DraftFails:
        name = "groq"
        model = "llama"

        def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
            if messages and messages[0].content.startswith("You write"):
                raise ProviderError("summary down")
            return MockProvider().complete(messages, timeout=timeout)

    completed = run_review(
        ReviewTarget(diff_text=SQL),
        _settings(tmp_path, provider="groq"),  # type: ignore[arg-type]
        dry_run=True,
        llm=DraftFails(),
    )
    assert "Reviewed" in completed.review.summary
    assert completed.review.verdict == Verdict.request_changes


def test_hosted_summary_replaces_the_template(tmp_path: Path) -> None:
    class Drafts:
        name = "gemini"
        model = "gemini-2.5-flash"

        def complete(self, messages: list[ChatMessage], *, timeout: float) -> Completion:
            if messages and messages[0].content.startswith("You write"):
                return Completion(
                    text='{"summary": "Ship the parameterized query."}',
                    input_tokens=10,
                    output_tokens=8,
                    model=self.model,
                    provider=self.name,
                )
            return MockProvider().complete(messages, timeout=timeout)

    completed = run_review(
        ReviewTarget(diff_text=SQL),
        _settings(tmp_path, provider="gemini"),  # type: ignore[arg-type]
        dry_run=True,
        llm=Drafts(),
    )
    assert completed.review.summary == "Ship the parameterized query."
    assert any(item.agent == "judge" for item in completed.review.usage)


def test_resolve_auto_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path, provider="auto")  # type: ignore[arg-type]
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert resolve_provider(settings).provider == "mock"  # type: ignore[arg-type]
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert resolve_provider(settings).provider == "gemini"  # type: ignore[arg-type]
    monkeypatch.setenv("GROQ_API_KEY", "q")
    assert resolve_provider(settings).provider == "groq"  # type: ignore[arg-type]


def test_static_evidence_becomes_a_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import subprocess

    monkeypatch.setattr(
        "prcrew.static_analysis.shutil.which",
        lambda name: "/usr/bin/bandit" if name == "bandit" else None,
    )

    source = tmp_path / "app"
    source.mkdir()
    target_file = source / "run.py"
    target_file.write_text("def run(cmd):\n    return cmd\n", encoding="utf-8")
    diff = """diff --git a/app/run.py b/app/run.py
new file mode 100644
--- /dev/null
+++ b/app/run.py
@@ -0,0 +1,2 @@
+def run(cmd):
+    return cmd
"""

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = {
            "results": [
                {
                    "filename": str(target_file),
                    "line_number": 2,
                    "issue_text": "possible shell injection",
                    "test_id": "B602",
                    "issue_severity": "HIGH",
                }
            ]
        }
        return subprocess.CompletedProcess(command, 1, stdout=json.dumps(payload), stderr="")

    completed = run_review(
        ReviewTarget(diff_text=diff),
        _settings(tmp_path, static_analysis=True, static_tools=["bandit"]),  # type: ignore[arg-type]
        dry_run=True,
        local_root=tmp_path,
        static_runner=runner,
    )
    security = [item for item in completed.review.findings if item.category == Category.security]
    assert security
    assert "B602" in security[0].title


def test_low_only_review_approves(tmp_path: Path) -> None:
    diff = """diff --git a/app/note.py b/app/note.py
new file mode 100644
--- /dev/null
+++ b/app/note.py
@@ -0,0 +1,2 @@
+# TODO: explain this constant before release
+VALUE = 1
"""
    completed = run_review(
        ReviewTarget(diff_text=diff),
        _settings(tmp_path),  # type: ignore[arg-type]
        dry_run=True,
    )
    assert completed.review.findings
    assert all(item.severity == Severity.low for item in completed.review.findings)
    assert completed.review.verdict == Verdict.approve
