"""Score published findings against the labelled diff dataset."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from prcrew.config import Settings, resolve_provider
from prcrew.diffparse import parse_unified_diff
from prcrew.errors import PrcrewError
from prcrew.models import Category, Finding, ReviewTarget
from prcrew.service import run_review

_CATEGORIES = [item.value for item in Category]


@dataclass
class Label:
    file: str
    line: int
    category: str
    contains: str = ""


@dataclass
class CaseResult:
    case_id: str
    true_positive: list[Label] = field(default_factory=list)
    false_positive: list[Finding] = field(default_factory=list)
    false_negative: list[Label] = field(default_factory=list)


@dataclass
class Scores:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        total = self.tp + self.fp
        if total == 0:
            return None
        return self.tp / total

    @property
    def recall(self) -> float | None:
        total = self.tp + self.fn
        if total == 0:
            return None
        return self.tp / total


@dataclass
class EvalReport:
    cases: list[CaseResult]
    per_category: dict[str, Scores]
    micro: Scores
    macro_precision: float | None
    macro_recall: float | None
    provider: str
    model: str

    def to_dict(self) -> dict[str, Any]:
        def pack(scores: Scores) -> dict[str, Any]:
            return {
                "tp": scores.tp,
                "fp": scores.fp,
                "fn": scores.fn,
                "precision": scores.precision,
                "recall": scores.recall,
            }

        return {
            "provider": self.provider,
            "model": self.model,
            "per_category": {name: pack(scores) for name, scores in self.per_category.items()},
            "micro": pack(self.micro),
            "macro_precision": self.macro_precision,
            "macro_recall": self.macro_recall,
            "cases": [
                {
                    "id": case.case_id,
                    "tp": len(case.true_positive),
                    "fp": len(case.false_positive),
                    "fn": len(case.false_negative),
                }
                for case in self.cases
            ],
        }


def dataset_root() -> Path:
    packaged = Path(__file__).resolve().parents[2] / "evals" / "dataset"
    if packaged.is_dir():
        return packaged
    cwd = Path.cwd() / "evals" / "dataset"
    if cwd.is_dir():
        return cwd
    raise PrcrewError("labelled eval dataset not found at evals/dataset")


def load_cases(dataset: Path | None = None) -> list[dict[str, Any]]:
    root = dataset or dataset_root()
    if not root.is_dir():
        raise PrcrewError(f"dataset directory not found: {root}")
    cases: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.yml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise PrcrewError(f"{path.name} must be a mapping")
        loaded["id"] = str(loaded.get("id") or path.stem)
        cases.append(loaded)
    if not cases:
        raise PrcrewError(f"no cases in {root}")
    return cases


def run_eval(
    settings: Settings,
    *,
    dataset: Path | None = None,
    only: list[str] | None = None,
    line_tolerance: int = 0,
) -> EvalReport:
    settings = resolve_provider(
        settings.model_copy(update={"cache_enabled": False, "static_analysis": False})
    )
    cases = load_cases(dataset)
    if only:
        wanted = set(only)
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            raise PrcrewError("no eval cases matched --case")
    results: list[CaseResult] = []
    for case in cases:
        results.append(_score_case(case, settings, line_tolerance))
    return _aggregate(results, settings.provider, settings.resolved_model())


def render_report(report: EvalReport, *, verbose: bool = False) -> str:
    lines = [
        f"provider: {report.provider} ({report.model})",
        "",
        "| Category | TP | FP | FN | Precision | Recall |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, scores in report.per_category.items():
        lines.append(
            f"| {name} | {scores.tp} | {scores.fp} | {scores.fn} | "
            f"{_fmt(scores.precision)} | {_fmt(scores.recall)} |"
        )
    lines.append(
        f"| micro | {report.micro.tp} | {report.micro.fp} | {report.micro.fn} | "
        f"{_fmt(report.micro.precision)} | {_fmt(report.micro.recall)} |"
    )
    lines.append(
        f"| macro |  |  |  | {_fmt(report.macro_precision)} | {_fmt(report.macro_recall)} |"
    )
    if verbose:
        lines.append("")
        for case in report.cases:
            if not case.false_positive and not case.false_negative:
                continue
            lines.append(f"## {case.case_id}")
            for label in case.false_negative:
                lines.append(f"- miss {label.category} {label.file}:{label.line}")
            for finding in case.false_positive:
                lines.append(
                    f"- extra {finding.category.value} {finding.file}:{finding.line} {finding.title}"
                )
    return "\n".join(lines) + "\n"


def _score_case(case: dict[str, Any], settings: Settings, tolerance: int) -> CaseResult:
    case_id = str(case["id"])
    diff = str(case.get("diff") or "")
    labels = [_label(item, diff, case_id) for item in case.get("labels") or []]
    completed = run_review(
        ReviewTarget(diff_text=diff), settings, dry_run=True, cache_enabled=False
    )
    predicted = list(completed.review.findings)
    used: set[int] = set()
    result = CaseResult(case_id=case_id)
    for label in labels:
        match_at = _match_label(label, predicted, used, tolerance)
        if match_at is None:
            result.false_negative.append(label)
            continue
        used.add(match_at)
        result.true_positive.append(label)
    for index, finding in enumerate(predicted):
        if index not in used:
            result.false_positive.append(finding)
    return result


def _label(item: object, diff: str, case_id: str) -> Label:
    if not isinstance(item, dict):
        raise PrcrewError(f"{case_id} has a label that is not a mapping")
    label = Label(
        file=str(item.get("file") or ""),
        line=int(item.get("line") or 0),
        category=str(item.get("category") or ""),
        contains=str(item.get("contains") or ""),
    )
    if not label.file or label.line < 1 or label.category not in _CATEGORIES:
        raise PrcrewError(f"{case_id} has an invalid label: {item}")
    if label.contains:
        content = _line_content(diff, label.file, label.line)
        if label.contains not in content:
            raise PrcrewError(
                f"{case_id} label {label.file}:{label.line} does not contain {label.contains!r} "
                f"(line is {content!r})"
            )
    return label


def _line_content(diff: str, path: str, line: int) -> str:
    for file in parse_unified_diff(diff):
        if file.path != path:
            continue
        for number, text in file.added_lines():
            if number == line:
                return text
        for hunk in file.hunks:
            for diff_line in hunk.lines:
                if diff_line.new_line == line:
                    return diff_line.content
    return ""


def _match_label(
    label: Label, predicted: list[Finding], used: set[int], tolerance: int
) -> int | None:
    for index, finding in enumerate(predicted):
        if index in used:
            continue
        if finding.category.value != label.category or finding.file != label.file:
            continue
        if abs(finding.line - label.line) <= tolerance:
            return index
    return None


def _aggregate(results: list[CaseResult], provider: str, model: str) -> EvalReport:
    per = {name: Scores() for name in _CATEGORIES}
    micro = Scores()
    for case in results:
        for label in case.true_positive:
            per[label.category].tp += 1
            micro.tp += 1
        for finding in case.false_positive:
            per[finding.category.value].fp += 1
            micro.fp += 1
        for label in case.false_negative:
            per[label.category].fn += 1
            micro.fn += 1
    precisions = [scores.precision for scores in per.values() if scores.precision is not None]
    recalls = [scores.recall for scores in per.values() if scores.recall is not None]
    macro_precision = sum(precisions) / len(precisions) if precisions else None
    macro_recall = sum(recalls) / len(recalls) if recalls else None
    return EvalReport(
        cases=results,
        per_category=per,
        micro=micro,
        macro_precision=macro_precision,
        macro_recall=macro_recall,
        provider=provider,
        model=model,
    )


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"
