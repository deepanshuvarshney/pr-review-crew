"""Deterministic reviewers used by the mock provider.

These are intentionally narrow. They exist so CI, demos, and the eval harness
can run with no API key and still exercise the same parse, judge, and publish
path as a hosted model. A subtle bug that does not match a rule is a real miss.
"""

from __future__ import annotations

import re
from typing import Any

from prcrew.models import Category, Severity

_SQL = re.compile(r"\b(select|insert|update|delete|drop|alter)\b", re.IGNORECASE)
_SECRET_ASSIGN = re.compile(
    r"(?i)\b(api_key|apikey|secret|password|token|access_key)\b\s*=\s*[\"']([^\"']+)[\"']"
)
_KNOWN_SECRET = re.compile(r"AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}")
_PLACEHOLDER = re.compile(r"example|placeholder|changeme|your[-_]|xxx+|<[^>]+>|\$\{", re.IGNORECASE)
_ROUTE = re.compile(r"@(?:app|router|blueprint)\.(?:route|get|post|put|patch|delete)\s*\(")
_SENSITIVE_ROUTE = re.compile(r"admin|internal|delete", re.IGNORECASE)
_AUTH = re.compile(
    r"login_required|require_auth|current_user|Depends\s*\(|authorize|permission|is_authenticated|\bjwt\b"
)
_ASSIGN_LOOKUP = re.compile(r"(\w+)\s*=\s*(?:get_|find_|fetch_)\w+\s*\(")


def review_diff(
    *,
    agent: str,
    added: list[tuple[str, int, str]],
    meta: dict[str, str],
    evidence: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if agent == Category.security.value:
        return _dedupe(_security(added) + _from_evidence(evidence, Category.security))
    if agent == Category.correctness.value:
        return _dedupe(_correctness(added) + _from_evidence(evidence, Category.correctness))
    if agent == Category.performance.value:
        return _dedupe(_performance(added) + _from_evidence(evidence, Category.performance))
    if agent == Category.tests.value:
        return _tests(meta)
    if agent == Category.maintainability.value:
        return _dedupe(_style(added) + _from_evidence(evidence, Category.maintainability))
    return []


def _security(added: list[tuple[str, int, str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    by_file: dict[str, list[tuple[int, str]]] = {}
    for path, line, text in added:
        by_file.setdefault(path, []).append((line, text))
        if _is_comment(text):
            continue
        if _sql_injection(text):
            findings.append(
                _finding(
                    path,
                    line,
                    Severity.high,
                    Category.security,
                    0.91,
                    "Interpolated SQL",
                    "This builds a SQL statement with string interpolation. "
                    "A quote in the value can change the query. "
                    "Pass the value as a bound parameter instead.",
                    _suggest_parameterized(text),
                )
            )
        secret = _secret_finding(text)
        if secret is not None:
            findings.append(
                _finding(path, line, Severity.critical, Category.security, 0.93, *secret)
            )
        if re.search(r"\bpickle\.loads?\s*\(", text) or re.search(r"\bmarshal\.loads?\s*\(", text):
            findings.append(
                _finding(
                    path,
                    line,
                    Severity.high,
                    Category.security,
                    0.9,
                    "Unsafe deserialization",
                    "Unpickling bytes from a request or a store lets a payload run code. "
                    "Read a schema-checked format such as JSON instead.",
                    None,
                )
            )
        if (
            re.search(r"\byaml\.load\s*\(", text)
            and "SafeLoader" not in text
            and "safe_load" not in text
        ):
            findings.append(
                _finding(
                    path,
                    line,
                    Severity.high,
                    Category.security,
                    0.88,
                    "Unsafe YAML load",
                    "yaml.load without SafeLoader can construct arbitrary objects. Use yaml.safe_load.",
                    None,
                )
            )
        if re.search(r"\b(eval|exec)\s*\(", text):
            findings.append(
                _finding(
                    path,
                    line,
                    Severity.critical,
                    Category.security,
                    0.9,
                    "Dynamic code execution",
                    "eval or exec on data that is not a literal runs attacker-controlled code. "
                    "Parse a restricted syntax or dispatch on an allow-list.",
                    None,
                )
            )
        if "os.system" in text or re.search(r"shell\s*=\s*True", text):
            findings.append(
                _finding(
                    path,
                    line,
                    Severity.critical,
                    Category.security,
                    0.92,
                    "Command injection",
                    "A shell is invoked with a string that can include untrusted input. "
                    "Pass an argument list to subprocess.run and keep shell disabled.",
                    _suggest_subprocess(text),
                )
            )
    for path, rows in by_file.items():
        route_lines = [
            (line, text)
            for line, text in rows
            if _ROUTE.search(text) and _SENSITIVE_ROUTE.search(text)
        ]
        if not route_lines:
            continue
        if any(_AUTH.search(text) for _, text in rows):
            continue
        line, _text = route_lines[0]
        findings.append(
            _finding(
                path,
                line,
                Severity.high,
                Category.security,
                0.74,
                "Missing authorization",
                "This route mutates or exposes a privileged path and the added lines "
                "do not check an authenticated principal or a permission dependency.",
                None,
            )
        )
    return findings


def _correctness(added: list[tuple[str, int, str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    by_file: dict[str, list[tuple[int, str]]] = {}
    for path, line, text in added:
        by_file.setdefault(path, []).append((line, text))
        if _is_comment(text):
            continue
        off = re.match(r"(\s*)return\s+(\w+)\[len\(\2\)\]\s*$", text)
        if off or re.search(r"range\(\s*len\([^)]*\)\s*\+\s*1", text):
            suggestion = None
            if off:
                suggestion = f"{off.group(1)}return {off.group(2)}[-1]"
            findings.append(
                _finding(
                    path,
                    line,
                    Severity.high,
                    Category.correctness,
                    0.86,
                    "Off-by-one index",
                    "This indexes one past the end of the sequence. "
                    "The last element is at len(items) - 1, or items[-1].",
                    suggestion,
                )
            )
    for path, rows in by_file.items():
        findings.extend(_bare_except(path, rows))
        findings.extend(_none_deref(path, rows))
        findings.extend(_race(path, rows))
    return findings


def _bare_except(path: str, rows: list[tuple[int, str]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for index, (line, text) in enumerate(rows):
        if not re.match(r"\s*except\s*(Exception)?\s*:\s*$", text):
            continue
        if not re.match(r"\s*except\s*:\s*$", text) and not re.match(
            r"\s*except\s+Exception\s*:\s*$", text
        ):
            continue
        for _, nxt in rows[index + 1 : index + 3]:
            stripped = nxt.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped == "pass":
                found.append(
                    _finding(
                        path,
                        line,
                        Severity.medium,
                        Category.correctness,
                        0.84,
                        "Swallowed exception",
                        "This handler catches a broad exception and ignores it, so callers "
                        "cannot tell a failure from an empty result. Catch the specific error "
                        "and log or re-raise it.",
                        None,
                    )
                )
            break
    return found


def _none_deref(path: str, rows: list[tuple[int, str]]) -> list[dict[str, Any]]:
    pending: dict[str, int] = {}
    found: list[dict[str, Any]] = []
    for line, text in rows:
        if re.search(r"\bis\s+None\b|\bis\s+not\s+None\b", text):
            for name in list(pending):
                if re.search(rf"\b{name}\b", text):
                    pending.pop(name, None)
            continue
        match = _ASSIGN_LOOKUP.search(text.strip())
        if match:
            pending[match.group(1)] = line
            continue
        for name in list(pending):
            if re.search(rf"\b{name}\.", text) and "None" not in text:
                found.append(
                    _finding(
                        path,
                        line,
                        Severity.high,
                        Category.correctness,
                        0.72,
                        "Possible None dereference",
                        f"`{name}` comes from a lookup that can return None, and this line "
                        "reads an attribute without a guard. Check for None before using it.",
                        None,
                    )
                )
                pending.pop(name, None)
    return found


def _race(path: str, rows: list[tuple[int, str]]) -> list[dict[str, Any]]:
    texts = [text for _, text in rows]
    joined = "\n".join(texts)
    if "Lock" in joined:
        return []
    if not any("Thread(" in text or "threading." in text for text in texts):
        return []
    for line, text in rows:
        if "+=" in text or ".append(" in text:
            return [
                _finding(
                    path,
                    line,
                    Severity.medium,
                    Category.correctness,
                    0.7,
                    "Unsynchronized shared update",
                    "This mutates shared state from a thread without a lock. "
                    "Concurrent increments can lose updates. Guard the update with a Lock.",
                    None,
                )
            ]
    return []


def _performance(added: list[tuple[str, int, str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    by_file: dict[str, list[tuple[int, str]]] = {}
    for path, line, text in added:
        by_file.setdefault(path, []).append((line, text))
    for path, rows in by_file.items():
        loop_indent: int | None = None
        for line, text in rows:
            if _is_comment(text):
                continue
            indent = len(text) - len(text.lstrip(" "))
            if re.match(r"\s*(for|while)\b", text):
                loop_indent = indent
                continue
            if loop_indent is None:
                continue
            if text.strip() and indent <= loop_indent:
                loop_indent = None
                if re.match(r"\s*(for|while)\b", text):
                    loop_indent = indent
                continue
            if re.search(
                r"\.(query|execute|filter|fetchone|fetchall)\s*\(|requests\.(get|post)\s*\(", text
            ):
                findings.append(
                    _finding(
                        path,
                        line,
                        Severity.medium,
                        Category.performance,
                        0.78,
                        "Query inside a loop",
                        "This calls the database or the network once per iteration. "
                        "Fetch the rows in one query with an IN clause or a join, then index them in memory.",
                        None,
                    )
                )
            elif re.search(r"\+=\s*(?!\s*\d)", text):
                findings.append(
                    _finding(
                        path,
                        line,
                        Severity.low,
                        Category.performance,
                        0.7,
                        "Repeated allocation in a loop",
                        "Concatenating a string or a list with += copies the left side on every pass. "
                        "Append to a list and join once, or use a builder.",
                        None,
                    )
                )
    return findings


def _style(added: list[tuple[str, int, str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path, line, text in added:
        if re.search(r"\b(TODO|FIXME)\b", text):
            findings.append(
                _finding(
                    path,
                    line,
                    Severity.low,
                    Category.maintainability,
                    0.6,
                    "Leftover TODO",
                    "This change adds a TODO or FIXME with no owner. "
                    "Either do the work in this pull request or link a tracked issue.",
                    None,
                )
            )
        if not _is_comment(text) and len(text) > 120:
            findings.append(
                _finding(
                    path,
                    line,
                    Severity.low,
                    Category.maintainability,
                    0.58,
                    "Line exceeds 120 characters",
                    "This added line is long enough to hide the behavior. "
                    "Break the expression or extract a named helper.",
                    None,
                )
            )
        signature = re.match(r"\s*def\s+\w+\s*\((.*)\)\s*:", text)
        if signature and signature.group(1).count(",") >= 5:
            findings.append(
                _finding(
                    path,
                    line,
                    Severity.low,
                    Category.maintainability,
                    0.57,
                    "Function takes many parameters",
                    "This new function takes more than six parameters. "
                    "Group the related values into a small object so call sites stay readable.",
                    None,
                )
            )
    return findings


def _tests(meta: dict[str, str]) -> list[dict[str, Any]]:
    if meta.get("HAS_TEST_FILE") == "yes":
        return []
    path = meta.get("FIRST_LOGIC_FILE", "none")
    if path in {"", "none"}:
        return []
    try:
        line = int(meta.get("FIRST_LOGIC_LINE", "0"))
    except ValueError:
        return []
    if line < 1:
        return []
    text = meta.get("FIRST_LOGIC_TEXT", "the new logic")
    return [
        _finding(
            path,
            line,
            Severity.medium,
            Category.tests,
            0.64,
            "Missing test coverage",
            f"This diff changes logic (`{text}`) and does not update a test file. "
            "Add a test for the happy path and one edge case, including the failure path.",
            None,
        )
    ]


def _from_evidence(evidence: list[dict[str, str]], category: Category) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in evidence:
        mapped = _evidence_category(item.get("tool", ""), item.get("rule", ""))
        if mapped != category:
            continue
        try:
            line = int(item.get("line", "0"))
        except ValueError:
            continue
        path = item.get("file", "")
        if line < 1 or not path:
            continue
        severity = _severity_from_tool(item.get("severity", "medium"))
        tool = item.get("tool", "tool")
        rule = item.get("rule", "")
        message = item.get("message", "")
        found.append(
            _finding(
                path,
                line,
                severity,
                category,
                0.8,
                f"{tool} {rule}",
                f"{tool} reported {rule} on a changed line: {message}".strip(),
                None,
                evidence=f"{tool}:{rule}",
            )
        )
    return found


def _evidence_category(tool: str, rule: str) -> Category | None:
    if tool == "bandit":
        return Category.security
    if tool == "semgrep":
        return Category.security
    if tool == "ruff":
        if rule.startswith("S"):
            return Category.security
        if rule.startswith("PERF"):
            return Category.performance
        if rule.startswith(("F", "B")):
            return Category.correctness
        return Category.maintainability
    return None


def _severity_from_tool(value: str) -> Severity:
    lowered = value.lower()
    if lowered in {"critical"}:
        return Severity.critical
    if lowered in {"high", "error"}:
        return Severity.high
    if lowered in {"medium", "warning"}:
        return Severity.medium
    if lowered in {"low"}:
        return Severity.low
    return Severity.info


def _sql_injection(text: str) -> bool:
    if not _SQL.search(text):
        return False
    if re.search(r"\bf[\"']", text) and "{" in text:
        return True
    if ".format(" in text:
        return True
    if re.search(r"[\"']\s*\+", text) or re.search(r"\+\s*[\"']", text):
        return True
    return re.search(r"[\"'][^\"']*%[sd][^\"']*[\"']\s*%", text) is not None


def _suggest_parameterized(text: str) -> str | None:
    if "execute" not in text:
        return None
    match = re.search(r"f([\"'])(?P<sql>.*)\1", text)
    if match is None:
        return None
    sql = match.group("sql")
    names = re.findall(r"\{(\w+)\}", sql)
    if not names:
        return None
    rewritten = re.sub(r"['\"]?\{\w+\}['\"]?", "%s", sql)
    indent = text[: len(text) - len(text.lstrip())]
    if len(names) == 1:
        args = f"({names[0]},)"
    else:
        args = "(" + ", ".join(names) + ")"
    return f'{indent}cur.execute("{rewritten}", {args})'


def _suggest_subprocess(text: str) -> str | None:
    match = re.search(r"os\.system\(\s*f[\"'].*\{(\w+)\}.*[\"']\s*\)", text)
    if match is None:
        return None
    indent = text[: len(text) - len(text.lstrip())]
    var = match.group(1)
    if "ping" in text:
        return f'{indent}subprocess.run(["ping", "-c", "1", {var}], check=False)'
    return f"{indent}subprocess.run([{var}], check=False)"


def _secret_finding(text: str) -> tuple[str, str, str | None] | None:
    known = _KNOWN_SECRET.search(text)
    assigned = _SECRET_ASSIGN.search(text)
    raw_value = ""
    if assigned:
        raw_value = assigned.group(2)
    elif known:
        raw_value = known.group(0)
    else:
        return None
    if len(raw_value) < 12 or _PLACEHOLDER.search(raw_value):
        return None
    suggestion = None
    if assigned:
        name = assigned.group(1)
        indent = text[: len(text) - len(text.lstrip())]
        suggestion = f'{indent}{name} = os.environ["{name.upper()}"]'
    return (
        "Hardcoded secret",
        "A credential is committed in source. Rotate it, remove it from history, "
        "and read it from the environment or a secret manager.",
        suggestion,
    )


def _is_comment(text: str) -> bool:
    return text.strip().startswith("#")


def _finding(
    path: str,
    line: int,
    severity: Severity,
    category: Category,
    confidence: float,
    title: str,
    explanation: str,
    suggested_fix: str | None,
    evidence: str | None = None,
) -> dict[str, Any]:
    return {
        "file": path,
        "line": line,
        "severity": severity.value,
        "category": category.value,
        "confidence": confidence,
        "title": title,
        "explanation": explanation,
        "suggested_fix": suggested_fix,
        "evidence": evidence,
    }


def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in findings:
        key = (str(item["file"]), int(item["line"]), str(item["category"]), str(item["title"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique
