"""Shared data structures for diffs, findings, and reviews."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    info = "info"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class Category(StrEnum):
    security = "security"
    correctness = "correctness"
    performance = "performance"
    tests = "tests"
    maintainability = "maintainability"


class Verdict(StrEnum):
    approve = "approve"
    comment = "comment"
    request_changes = "request_changes"


class FileStatus(StrEnum):
    added = "added"
    modified = "modified"
    removed = "removed"
    renamed = "renamed"


AGENT_NAMES: tuple[str, ...] = (
    Category.security.value,
    Category.correctness.value,
    Category.performance.value,
    Category.tests.value,
    Category.maintainability.value,
)

SEVERITY_RANK: dict[Severity, int] = {
    Severity.info: 0,
    Severity.low: 1,
    Severity.medium: 2,
    Severity.high: 3,
    Severity.critical: 4,
}

# Multiplier used when ranking. The product with confidence is the sort key.
SEVERITY_WEIGHT: dict[Severity, int] = {
    Severity.info: 1,
    Severity.low: 2,
    Severity.medium: 3,
    Severity.high: 4,
    Severity.critical: 5,
}


class DiffLine(BaseModel):
    kind: str
    old_line: int | None = None
    new_line: int | None = None
    content: str


class Hunk(BaseModel):
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[DiffLine] = Field(default_factory=list)


class FileDiff(BaseModel):
    path: str
    old_path: str | None = None
    status: FileStatus
    hunks: list[Hunk] = Field(default_factory=list)
    is_binary: bool = False
    skipped_reason: str | None = None

    def added_lines(self) -> list[tuple[int, str]]:
        found: list[tuple[int, str]] = []
        for hunk in self.hunks:
            for line in hunk.lines:
                if line.kind == "add" and line.new_line is not None:
                    found.append((line.new_line, line.content))
        return found


class Evidence(BaseModel):
    tool: str
    rule: str
    file: str
    line: int
    message: str
    severity: str = "medium"


class Finding(BaseModel):
    file: str
    line: int = Field(ge=1)
    severity: Severity
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str
    suggested_fix: str | None = None
    title: str = ""
    agent: str = ""
    evidence: str | None = None


class DroppedFinding(BaseModel):
    file: str
    line: int
    category: str
    reason: str
    title: str = ""


class UsageRecord(BaseModel):
    provider: str
    model: str
    agent: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_list_usd: float = 0.0
    billed_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ReviewTarget(BaseModel):
    owner: str | None = None
    repo: str | None = None
    number: int | None = None
    diff_text: str | None = None
    head_sha: str | None = None
    title: str = ""
    body: str = ""
    base_ref: str = ""
    head_ref: str = ""
    html_url: str = ""

    @property
    def coordinates(self) -> str:
        if self.owner and self.repo and self.number is not None:
            return f"{self.owner}/{self.repo}#{self.number}"
        return "local diff"


class ReviewResult(BaseModel):
    summary: str
    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)
    dropped: list[DroppedFinding] = Field(default_factory=list)
    usage: list[UsageRecord] = Field(default_factory=list)
    head_sha: str = ""
    agent_errors: list[dict[str, str]] = Field(default_factory=list)
    files_reviewed: int = 0
    files_skipped: int = 0
    provider: str = "mock"
    model: str = "mock-heuristic-v1"
    title: str = ""
    cached: bool = False


class PublishResult(BaseModel):
    mode: str
    markdown: str
    review_id: int | None = None
    comment_id: int | None = None
    created_review: bool = False
    updated_summary: bool = False
    detail: str = ""


class ChatMessage(BaseModel):
    role: str
    content: str


class Completion(BaseModel):
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    provider: str


class CompletedReview(BaseModel):
    review: ReviewResult
    publish: PublishResult
    target: ReviewTarget


def finding_score(finding: Finding) -> float:
    return float(SEVERITY_WEIGHT[finding.severity]) * finding.confidence
