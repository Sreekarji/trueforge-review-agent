"""Deterministic pre-flight checks on the exact payload the agent wants to write."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

SCHEMA = "receipts/v1"
MAX_BODY_CHARS = 12_000
MIN_OUTPUT_CHARS = 20
BODY_KEYS = ("body", "comment", "text", "message")

RECEIPTS_BLOCK = re.compile(r"```receipts\s*\n(.*?)\n```\s*$", re.DOTALL)

SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bdtn_[A-Za-z0-9]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

ALLOWED_WRITE_TOOLS = frozenset({
    "add_issue_comment",
    "create_issue_comment",
    "create_pull_request_review",
    "add_pull_request_review_comment",
})


@dataclass(frozen=True)
class VerdictRule:
    outcomes: dict[str, str]
    allows_fix_diff: bool


VERDICT_RULES: dict[str, VerdictRule] = {
    "REGRESSION": VerdictRule({"base": "pass", "head": "fail", "patched": "pass"}, True),
    "PRE-EXISTING": VerdictRule({"base": "fail", "head": "fail", "patched": "pass"}, True),
    "UNFIXED": VerdictRule({"head": "fail"}, False),
    "FIXED": VerdictRule({"head": "pass"}, False),
    "STILL-FAILING": VerdictRule({"head": "fail"}, False),
    "UNVERIFIED": VerdictRule({}, False),
}

PROVEN_VERDICTS = frozenset(name for name in VERDICT_RULES if name != "UNVERIFIED")


@dataclass(frozen=True)
class Target:
    repo: str = ""
    pr: str = ""


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    where: str = "payload"
    fatal: bool = False

    def render(self) -> str:
        return f"[{self.code}] {self.where}: {self.message}"


def has_fatal(violations: Iterable[Violation]) -> bool:
    return any(v.fatal for v in violations)


def body_of(arguments: dict[str, Any]) -> str:
    for key in BODY_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def extract_receipts(body: str) -> tuple[dict[str, Any], str | None]:
    if body.count("```receipts") != 1:
        return {}, "body must contain exactly one ```receipts fenced block"
    match = RECEIPTS_BLOCK.search(body)
    if match is None:
        return {}, "the ```receipts block must be the last thing in the comment"
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return {}, f"the receipts block is not valid JSON ({exc.msg})"
    if not isinstance(parsed, dict):
        return {}, "the receipts block must be a JSON object"
    return parsed, None


def _outcome(run: Any) -> str | None:
    """'pass'/'fail' only when BOTH the outcome string and a real integer
    exit_code are present AND agree. A missing, boolean, or contradictory
    exit_code is not valid evidence, so it returns None."""
    if not isinstance(run, dict):
        return None
    outcome = str(run.get("outcome", "")).strip().lower()
    code = run.get("exit_code")
    if outcome not in ("pass", "fail") or isinstance(code, bool) or not isinstance(code, int):
        return None
    derived = "pass" if code == 0 else "fail"
    return outcome if derived == outcome else None


def _evidence_violations(run: Any, label: str, where: str) -> list[Violation]:
    if not isinstance(run, dict):
        return [Violation("MISSING_RUN", f"the {label} run is absent", where)]
    found: list[Violation] = []
    if "pytest" not in str(run.get("cmd", "")):
        found.append(Violation("NO_COMMAND", f"the {label} run must record the pytest command", where))
    if len(str(run.get("tail", "")).strip()) < MIN_OUTPUT_CHARS:
        found.append(Violation("NO_OUTPUT", f"the {label} run must record real captured output", where))
    return found


def _finding_violations(finding: dict[str, Any], where: str) -> list[Violation]:
    found: list[Violation] = []
    verdict = str(finding.get("verdict", "")).strip().upper()
    if verdict == "REFUTED":
        return [Violation("REFUTED_REPORTED", "refuted hypotheses are counted, never reported", where)]
    rule = VERDICT_RULES.get(verdict)
    if rule is None:
        return [Violation("BAD_VERDICT", f"verdict {verdict!r} is not valid", where)]
    for key in ("file", "claim"):
        if not str(finding.get(key, "")).strip():
            found.append(Violation("MISSING_FIELD", f"{key} is required", where))
    if not str(finding.get("severity", "")).strip():
        found.append(Violation("MISSING_FIELD", "severity is required", where))
    if verdict != "UNVERIFIED":
        line = finding.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            found.append(Violation("MISSING_FIELD", "line must be a positive integer", where))
    has_diff = finding.get("fix_diff") is not None and bool(str(finding.get("fix_diff", "")).strip())
    if has_diff and not rule.allows_fix_diff:
        found.append(Violation("UNPROVEN_FIX", f"a {verdict} finding may not ship a fix diff", where))
    if verdict in PROVEN_VERDICTS:
        if not str(finding.get("test_source", "")).strip():
            found.append(Violation("NO_TEST_SOURCE", "a proven finding must carry its test_source verbatim", where))
        if not str(finding.get("test_path", "")).strip():
            found.append(Violation("MISSING_FIELD", "test_path is required for a proven finding", where))
    runs = finding.get("runs")
    if rule.outcomes and not isinstance(runs, dict):
        return found + [Violation("NO_RUNS", f"verdict {verdict} requires a runs object", where)]
    for label, expected in rule.outcomes.items():
        run = runs.get(label) if isinstance(runs, dict) else None
        actual = _outcome(run)
        if actual is None:
            found.append(Violation("NO_RUN_OUTCOME", f"the {label} run has no pass/fail outcome", where))
            continue
        if actual != expected:
            found.append(Violation("VERDICT_CONTRADICTED", f"verdict {verdict} requires {label} to {expected}, got {actual}", where))
        found.extend(_evidence_violations(run, label, where))
    return found


def _count_violations(receipts: dict[str, Any], findings: Sequence[Any]) -> list[Violation]:
    counts = receipts.get("counts")
    if not isinstance(counts, dict):
        return [Violation("NO_COUNTS", "receipts.counts is required")]
    def tally(predicate) -> int:
        return sum(1 for f in findings if isinstance(f, dict) and predicate(str(f.get("verdict", "")).strip().upper()))
    proven = tally(lambda v: v in PROVEN_VERDICTS)
    unverified = tally(lambda v: v == "UNVERIFIED")
    found: list[Violation] = []
    if counts.get("confirmed") != proven:
        found.append(Violation("COUNT_MISMATCH", f"counts.confirmed is {counts.get('confirmed')!r} but {proven} proven findings in report"))
    if counts.get("unverified") != unverified:
        found.append(Violation("COUNT_MISMATCH", f"counts.unverified is {counts.get('unverified')!r} but {unverified} in report"))
    raised, refuted = counts.get("raised"), counts.get("refuted")
    if not isinstance(raised, int) or not isinstance(refuted, int):
        found.append(Violation("NO_COUNTS", "counts.raised and counts.refuted must be integers"))
    elif raised != proven + unverified + refuted:
        found.append(Violation("COUNT_MISMATCH", f"counts.raised is {raised} but confirmed+unverified+refuted is {proven+unverified+refuted}"))
    return found


def _receipt_scope_violations(receipts: dict[str, Any], target: "Target") -> list[Violation]:
    if not target.repo:
        return []
    got_repo = str(receipts.get("repo", "")).strip()
    got_pr = str(receipts.get("pr", "")).strip()
    found: list[Violation] = []
    if not got_repo or not got_pr:
        found.append(Violation("SCOPE_MISSING", "receipts must declare repo and pr matching the review", "receipts", fatal=True))
    if got_repo and got_repo != target.repo:
        found.append(Violation("WRONG_REPO", f"receipts scoped to {got_repo!r} but review is {target.repo}", "receipts", fatal=True))
    if got_pr and target.pr and got_pr != str(target.pr):
        found.append(Violation("WRONG_PR", f"receipts scoped to PR #{got_pr} but review is #{target.pr}", "receipts", fatal=True))
    return found


def check_receipts(receipts: dict[str, Any], target: "Target" = Target()) -> list[Violation]:
    found: list[Violation] = []
    if receipts.get("schema") != SCHEMA:
        found.append(Violation("BAD_SCHEMA", f"schema must be {SCHEMA!r}, got {receipts.get('schema')!r}"))
    for key in ("repo", "pr", "base_sha", "head_sha"):
        if not str(receipts.get(key, "")).strip():
            found.append(Violation("MISSING_FIELD", f"receipts.{key} is required", "receipts"))
    found.extend(_receipt_scope_violations(receipts, target))
    findings = receipts.get("findings")
    if not isinstance(findings, list):
        return found + [Violation("NO_FINDINGS", "receipts.findings must be a list")]
    seen: set[str] = set()
    for position, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            found.append(Violation("BAD_FINDING", "each finding must be an object", f"finding {position}"))
            continue
        where = f"finding {finding.get('id') or position}"
        identifier = str(finding.get("id", "")).strip()
        if not identifier:
            found.append(Violation("MISSING_ID", "finding has no id", where))
        elif identifier in seen:
            found.append(Violation("DUPLICATE_ID", f"id {identifier!r} is used twice", where))
        else:
            seen.add(identifier)
        found.extend(_finding_violations(finding, where))
    found.extend(_count_violations(receipts, findings))
    return found


def _target_violations(arguments: dict[str, Any], target: "Target") -> list[Violation]:
    if not target.repo:
        return []
    want_owner, _, want_name = target.repo.partition("/")
    owner, name = str(arguments.get("owner", "")), str(arguments.get("repo", ""))
    found: list[Violation] = []
    if not owner or not name:
        found.append(Violation("SCOPE_MISSING", "the write must declare owner and repo matching the review", fatal=True))
    elif (owner, name) != (want_owner, want_name):
        found.append(Violation("WRONG_REPO", f"targets {owner}/{name} but scoped to {target.repo}", fatal=True))
    number = arguments.get("issue_number") or arguments.get("pull_number") or arguments.get("pullNumber")
    if number is None:
        found.append(Violation("SCOPE_MISSING", "the write must declare the issue/PR number", fatal=True))
    elif target.pr and str(number) != str(target.pr):
        found.append(Violation("WRONG_PR", f"targets #{number} but scoped to #{target.pr}", fatal=True))
    return found


def check_payload(tool_name: str, arguments: dict[str, Any], target: "Target" = Target()) -> list[Violation]:
    if tool_name not in ALLOWED_WRITE_TOOLS:
        return [Violation("OUT_OF_SCOPE_TOOL", f"{tool_name!r} is not a review comment", fatal=True)]
    found = _target_violations(arguments, target)
    body = body_of(arguments)
    if not body.strip():
        return found + [Violation("EMPTY_BODY", "the write carries no comment body")]
    if len(body) > MAX_BODY_CHARS:
        found.append(Violation("BODY_TOO_LONG", f"body is {len(body)} chars, limit is {MAX_BODY_CHARS}"))
    if any(pattern.search(body) for pattern in SECRET_PATTERNS):
        found.append(Violation("SECRET_IN_BODY", "body contains something shaped like a credential", fatal=True))
    receipts, error = extract_receipts(body)
    if error:
        return found + [Violation("NO_RECEIPTS", error)]
    return found + check_receipts(receipts, target)


def format_deny_reason(violations: Sequence[Violation]) -> str:
    lines = [
        "The Receipts policy gate denied this write. It did not reach a human.",
        "Fix every item below, keep the receipts block, and request the same comment tool again.",
        "",
    ]
    lines.extend(f"- {v.render()}" for v in violations)
    return "\n".join(lines)


def drop_reason(identifiers: Sequence[str]) -> str:
    listed = ", ".join(identifiers)
    return (f"The human rejected these findings: {listed}. Remove them from the report and from counts.confirmed, then request the comment again.")


def rules_summary() -> list[tuple[str, str, str]]:
    rows = []
    for verdict, rule in VERDICT_RULES.items():
        required = ", ".join(f"{label} must {outcome}" for label, outcome in rule.outcomes.items())
        rows.append((verdict, required or "(no sandbox evidence required)", "yes" if rule.allows_fix_diff else "no"))
    return rows
