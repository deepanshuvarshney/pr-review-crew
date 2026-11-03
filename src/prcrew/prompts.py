"""System prompts shared by every hosted provider."""

from __future__ import annotations

_SCHEMA = """\
Return a JSON object and nothing else:
{"findings": [
  {
    "file": "path as shown in DIFF",
    "line": 12,
    "severity": "info|low|medium|high|critical",
    "category": "<your category>",
    "confidence": 0.0,
    "title": "short label",
    "explanation": "why this is a problem in this diff",
    "suggested_fix": "single replacement line, or null"
  }
]}
If nothing in your category is wrong, return {"findings": []}.
Only cite a line number printed in DIFF. Do not invent files or lines.
Prefer added lines (the marker is +). A suggested_fix replaces that single line
and must be a drop-in. Use null when you cannot safely rewrite one line.
Do not repeat secrets in the explanation or the fix.
"""

_FOCI = {
    "security": (
        "You review only security issues: injection, hardcoded secrets, broken "
        "authorization, unsafe deserialization, and command execution."
    ),
    "correctness": (
        "You review only correctness: logic bugs, off-by-one errors, broken edge "
        "cases, swallowed errors, None dereferences, and concurrency mistakes."
    ),
    "performance": (
        "You review only performance: accidental quadratic work, N+1 queries, "
        "and repeated allocation inside loops. Ignore style."
    ),
    "tests": (
        "You review only tests. If the diff changes logic and does not update a "
        "test, say so once and name the case that should be added. If a test file "
        "changed, return no findings."
    ),
    "maintainability": (
        "You review only lightweight maintainability: names, oversized lines, "
        "oversized parameter lists, and TODOs introduced by this diff. Do not "
        "restyle unrelated code and do not repeat functional bugs."
    ),
}


def system_prompt(agent: str) -> str:
    focus = _FOCI.get(agent, "You review the diff for concrete issues.")
    return (
        f"You are the {agent} reviewer for PR Review Crew.\n"
        f"{focus}\n"
        "The category field on every finding must be "
        f"{agent}.\n"
        f"{_SCHEMA}"
    )


def user_prompt(
    *,
    agent: str,
    changed_files: list[str],
    has_tests: bool,
    first_logic: tuple[str, int, str] | None,
    chunk_index: int,
    chunk_count: int,
    diff_text: str,
    evidence_lines: list[str],
) -> str:
    if first_logic is None:
        logic_file, logic_line, logic_text = "none", "0", "none"
    else:
        logic_file, logic_line, logic_text = first_logic[0], str(first_logic[1]), first_logic[2]
    evidence = "\n".join(evidence_lines) if evidence_lines else "- (none)"
    files = ", ".join(changed_files) if changed_files else "(none)"
    return (
        f"AGENT: {agent}\n"
        f"CHANGED_FILES: {files}\n"
        f"HAS_TEST_FILE: {'yes' if has_tests else 'no'}\n"
        f"FIRST_LOGIC_FILE: {logic_file}\n"
        f"FIRST_LOGIC_LINE: {logic_line}\n"
        f"FIRST_LOGIC_TEXT: {logic_text}\n"
        f"CHUNK: {chunk_index}/{chunk_count}\n"
        f"STATIC_EVIDENCE:\n{evidence}\n"
        f"DIFF:\n{diff_text}"
    )
