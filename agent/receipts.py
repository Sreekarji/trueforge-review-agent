"""The receipts artifact: persist proven findings so they can be re-checked later."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.table import Table

ARTIFACT_DIR = Path("runs")
SCHEMA = "receipts/v1"
PROVEN_VERDICTS = frozenset({"REGRESSION", "PRE-EXISTING", "UNFIXED", "FIXED", "STILL-FAILING"})
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(value: str) -> str:
    return _UNSAFE.sub("-", value).strip("-").lower() or "unknown"


def artifact_path(repo: str, pr: str, directory: Path = ARTIFACT_DIR) -> Path:
    # Stable hash of the FULL repo identifier so owner/name pairs that slug to
    # the same string (foo/bar-baz vs foo-bar/baz) can never share a path.
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:10]
    return directory / f"receipts-{slug(repo)}-{digest}-pr{slug(str(pr))}.json"


def _normalize_unfixed(receipts: dict[str, Any]) -> dict[str, Any]:
    """UNFIXED findings carry no fix: drop fix_diff and the patched run.

    The receipts/v1 schema allows both fields to be absent for UNFIXED, so a
    stored artifact never implies a fix or a green patched run that does not
    exist. The caller's dict is not mutated.
    """
    findings = receipts.get("findings")
    if not isinstance(findings, list):
        return receipts
    normalized: list[Any] = []
    for finding in findings:
        if isinstance(finding, dict) and str(finding.get("verdict", "")).strip().upper() == "UNFIXED":
            cleaned = {key: value for key, value in finding.items() if key != "fix_diff"}
            runs = cleaned.get("runs")
            if isinstance(runs, dict):
                cleaned["runs"] = {key: value for key, value in runs.items() if key != "patched"}
            normalized.append(cleaned)
        else:
            normalized.append(finding)
    return {**receipts, "findings": normalized}


def save(receipts: dict[str, Any], repo: str, pr: str, session_id: str, directory: Path = ARTIFACT_DIR, path: Path | None = None) -> Path:
    if path is None:
        path = artifact_path(repo, pr, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "repo": receipts.get("repo") or repo,
        "pr": receipts.get("pr") or pr,
        "session_id": session_id,
        "receipts": _normalize_unfixed(receipts),
    }
    path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    return path


def load(path: Path) -> dict[str, Any]:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(artifact, dict) or "receipts" not in artifact:
        raise ValueError(f"{path} is not a receipts artifact")
    return artifact


def proven_findings(receipts: dict[str, Any]) -> list[dict[str, Any]]:
    findings = receipts.get("findings")
    if not isinstance(findings, list):
        return []
    return [f for f in findings if isinstance(f, dict) and str(f.get("verdict", "")).upper() in PROVEN_VERDICTS]


def summary_table(receipts: dict[str, Any], title: str = "Receipts") -> Table:
    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("severity")
    table.add_column("file:line")
    table.add_column("base")
    table.add_column("head")
    table.add_column("patched")
    table.add_column("verdict")
    colours = {"pass": "[green]PASS[/green]", "fail": "[red]FAIL[/red]", None: "[dim]-[/dim]"}
    for finding in receipts.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        runs = finding.get("runs") if isinstance(finding.get("runs"), dict) else {}
        cells = []
        for label in ("base", "head", "patched"):
            run = runs.get(label) if isinstance(runs, dict) else None
            outcome = None
            if isinstance(run, dict):
                outcome = str(run.get("outcome", "")).lower() or None
            cells.append(colours.get(outcome, colours[None]))
        line = finding.get("line")
        where = f"{finding.get('file', '?')}:{line}" if line else str(finding.get("file", "?"))
        table.add_row(str(finding.get("id", "?")), str(finding.get("severity", "?")), where, *cells, str(finding.get("verdict", "?")))
    counts = receipts.get("counts") or {}
    table.caption = (
        f"raised {counts.get('raised', '?')} | confirmed {counts.get('confirmed', '?')} | "
        f"refuted and dropped {counts.get('refuted', '?')} | unverified {counts.get('unverified', '?')}"
    )
    return table


def verification_prompt(artifact: dict[str, Any]) -> str:
    receipts = artifact["receipts"]
    repo = artifact.get("repo", "")
    pr = artifact.get("pr", "")
    findings = proven_findings(receipts)
    lines = [
        f"Re-verify, do not re-review. Earlier in this session you posted "
        f"{len(findings)} proven finding(s) on {repo} #{pr} at head "
        f"{receipts.get('head_sha', 'unknown')}. New commits have landed.",
        "",
        "For each finding below: fetch the CURRENT head version of the file, "
        "rebuild the stored test verbatim in the sandbox, run it, and record the "
        "command and real output. Verdict FIXED if it now passes, STILL-FAILING "
        "if it still fails, UNVERIFIED if you could not run it. Raise no new "
        "hypotheses and reuse the same ids.",
        "",
        "FINDINGS TO RE-CHECK",
    ]
    for finding in findings:
        lines.append(
            f"- {finding.get('id')} | {finding.get('severity')} | "
            f"{finding.get('file')}:{finding.get('line')} | {finding.get('claim')}"
        )
        source = finding.get("test_source")
        if source:
            lines.append(f"  stored test ({finding.get('test_path', 'test.py')}):")
            lines.append("  ```python")
            lines.extend(f"  {row}" for row in str(source).splitlines())
            lines.append("  ```")
    lines += [
        "",
        "Then post ONE verification comment on the pull request: a short verdict, "
        "a table of id / file / claim / before / after, the evidence blocks, and "
        "the receipts block with counts.refuted 0. That call pauses for my approval.",
    ]
    return "\n".join(lines)
