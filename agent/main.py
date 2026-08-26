"""Receipts - an evidence-first PR review agent running on TrueForge."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .provision import build_client, load_spec, SpecError
from .trueforge_client import TrueForgeClient, TrueForgeError
from . import receipts as receipts_store
from dataclasses import dataclass
from . import policy
from .policy import Target, Violation

console = Console(highlight=False)
RUNS_DIR = Path("runs")

SENSITIVE_KEY_HINTS = ("token", "key", "secret", "password", "authorization", "pat")
SECRET_PREFIXES = ("ghp_", "github_pat_", "sk-", "dtn_")
MAX_ARG_CHARS = 4000
RECEIPTS_BLOCK = re.compile(r"```receipts\s*\n(.*?)```", re.DOTALL)

REVIEW_PROMPT = (
    "Review pull request #{pr} in {repo}.\n\n"
    "Prove every hypothesis in the sandbox against this PR's head: write a "
    "minimal pytest that fails if and only if the defect is real, and run it. "
    "Classify each result with exactly one verdict: REGRESSION (base PASS, "
    "head FAIL, patched PASS), PRE-EXISTING (base FAIL, head FAIL, patched "
    "PASS), UNFIXED (head FAIL, no working patch - no fix diff), REFUTED "
    "(head PASS - never reported, only counted), or UNVERIFIED (could not be "
    "run - no evidence block). Only REGRESSION and PRE-EXISTING require the "
    "patched run. Fan the proofs out to one subagent per hypothesis. Report "
    "only what you executed, with the real commands and output, and say how "
    "many hypotheses you dropped because the reproduction passed on head.\n\n"
    "Then post the review as a single comment on the pull request, ending with "
    "the machine-readable receipts block. I will approve before anything is "
    "written to GitHub."
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if any(hint in str(key).lower() for hint in SENSITIVE_KEY_HINTS):
                out[key] = "***redacted***"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for prefix in SECRET_PREFIXES:
            if prefix in value:
                return "***redacted***"
        return value
    return value


def merge_delta(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    base["content"] = (base.get("content") or "") + (delta.get("content") or "")
    if delta.get("finish_reason"):
        base["finish_reason"] = delta["finish_reason"]
    fragments = delta.get("tool_calls") or []
    if not fragments:
        return base
    calls: list[dict[str, Any]] = base.setdefault("tool_calls", [])
    for position, fragment in enumerate(fragments):
        index = fragment.get("index", position)
        while len(calls) <= index:
            calls.append({"id": "", "function": {"name": "", "arguments": ""}})
        target = calls[index]
        if fragment.get("id"):
            target["id"] = fragment["id"]
        function = fragment.get("function") or {}
        merged = target.setdefault("function", {"name": "", "arguments": ""})
        if function.get("name"):
            merged["name"] = function["name"]
        if function.get("arguments"):
            merged["arguments"] = (merged.get("arguments") or "") + function["arguments"]
    return base


def lookup_call(index: dict[str, dict[str, Any]], pending_call: dict[str, Any]) -> tuple[str, str]:
    source = index.get(pending_call.get("source_event_id", ""), {})
    for call in source.get("tool_calls", []):
        if call.get("id") == pending_call.get("id"):
            function = call.get("function", {})
            name = function.get("name", "<unknown tool>")
            raw = function.get("arguments", "")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return name, str(raw)[:MAX_ARG_CHARS]
            pretty = json.dumps(redact(parsed), indent=2)
            return name, pretty[:MAX_ARG_CHARS]
    return "<unknown tool>", "<arguments unavailable>"


def lookup_arguments(index: dict[str, dict[str, Any]], pending_call: dict[str, Any]) -> dict[str, Any]:
    source = index.get(pending_call.get("source_event_id", ""), {})
    for call in source.get("tool_calls", []):
        if call.get("id") == pending_call.get("id"):
            raw = (call.get("function") or {}).get("arguments") or ""
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


class AuditLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = path.open("a", encoding="utf-8")

    def write(self, kind: str, payload: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "payload": redact(payload)}
        self._handle.write(json.dumps(record) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def render_event(event: dict[str, Any], index: dict[str, dict[str, Any]]) -> None:
    event_type = event.get("type", "")
    if event_type == "turn.created":
        console.print(f"[dim]turn {event.get('turn_id', '')} started[/dim]")
    elif event_type == "mcp.initialize":
        names = ", ".join(s.get("name", "?") for s in event.get("mcp_servers", []))
        console.print(f"[cyan]connectors ready:[/cyan] {names}")
    elif event_type == "sandbox.created":
        console.print(Panel(f"sandbox id: {event.get('sandbox_id', '?')}", title="[bold green]SANDBOX PROVISIONED[/bold green]", border_style="green"))
    elif event_type == "thread.created":
        console.print(
            f"\n[magenta]|-- proof subagent[/magenta] {event.get('title', '?')} "
            f"[dim]({event.get('thread_id', '?')})[/dim]"
        )
    elif event_type == "thread.done":
        state = event.get("state") or {}
        mark = "[green]done[/green]" if state.get("status") == "done" else "[red]error[/red]"
        console.print(f"[magenta]|-- proof subagent {mark}[/magenta] {event.get('thread_id', '?')}")
    elif event_type == "model.message":
        index[event["id"]] = event
        if event.get("content"):
            sys.stdout.write(event["content"])
            sys.stdout.flush()
    elif event_type == "model.message.delta":
        base = index.get(event.get("id", ""))
        if base is not None:
            merge_delta(base, event)
        if event.get("content") and (event.get("thread_id") or "main") == "main":
            sys.stdout.write(event["content"])
            sys.stdout.flush()
    elif event_type == "tool.response":
        console.print(f"\n[blue]<- tool[/blue] returned")
    elif event_type == "tool.approval_required":
        console.print("\n[bold yellow]-- paused for approval --[/bold yellow]")
    elif event_type == "turn.done":
        state = event.get("state") or {}
        status = state.get("status", "?")
        metrics = state.get("metrics") or {}
        cost = metrics.get("total_cost_in_usd")
        tokens = metrics.get("total_tokens")
        extra = ""
        if tokens is not None:
            extra = f" | {tokens} tokens"
        if cost is not None:
            extra += f" | ${float(cost):.4f}"
        console.print(f"\n[dim]turn finished: {status}{extra}[/dim]")


def show_violations(violations: list[Violation], title: str) -> None:
    table = Table(title=title, show_header=True, header_style="bold red")
    table.add_column("code"); table.add_column("where"); table.add_column("why")
    for v in violations:
        code = f"[bold red]{v.code}[/bold red]" if v.fatal else v.code
        table.add_row(code, v.where, v.message)
    console.print(table)

def show_approval_request(tool_name: str, arguments: str, clean: bool) -> None:
    verdict = ("[green]policy gate: PASSED[/green]" if clean else "[red]policy gate: FAILED[/red]")
    console.print()
    console.print(Panel(
        f"The agent wants to call [bold]{tool_name}[/bold], which writes to GitHub.\nNothing posted yet.\n\n{verdict}",
        title="[bold red]APPROVAL GATE[/bold red]", border_style="red"))
    console.print(Syntax(arguments, "json", theme="ansi_dark", word_wrap=True))


def stream_turn(client: TrueForgeClient, session_id: str, input_items: list[dict[str, Any]], audit: AuditLog) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    index: dict[str, dict[str, Any]] = {}
    required: list[dict[str, Any]] = []
    output = ""
    for event in client.stream_turn(session_id, input_items):
        audit.write("event", event)
        render_event(event, index)
        if event.get("type") == "turn.done":
            state = event.get("state") or {}
            if state.get("status") == "error":
                raise TrueForgeError(f"turn failed: {state.get('message')}")
            required = list(state.get("required_actions") or state.get("requiredActions") or [])
            output = (state.get("output") or {}).get("content") or ""
    return required, index, output


def collect_decisions(
    required: Iterable[dict[str, Any]],
    index: dict[str, dict[str, Any]],
    audit: AuditLog,
    auto_approve: bool,
    target: Target,
    gate: GateState,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    gate.repair_spent = False  # the repair budget is per agent turn, not per call in a batch
    for action in required:
        action_type = action.get("type")
        thread_id = action.get("thread_id") or "main"
        if action_type == "mcp.auth_required":
            urls = ", ".join(s.get("auth_url", "") for s in action.get("mcp_servers", []))
            raise TrueForgeError(f"Connector needs OAuth: {urls}")

        for pending in action.get("tool_calls", []):
            tool_name, arguments = lookup_call(index, pending)

            if action_type == "tool.approval_required":
                raw_arguments = lookup_arguments(index, pending)
                violations = policy.check_payload(tool_name, raw_arguments, target)
                fatal = policy.has_fatal(violations)
                audit.write("policy_check", {
                    "tool": tool_name, "clean": not violations, "fatal": fatal,
                    "violations": [v.render() for v in violations],
                })
                show_approval_request(tool_name, arguments, clean=not violations)
                if violations:
                    show_violations(violations, "Policy gate violations")

                decision = _decide(tool_name, violations, fatal, auto_approve, gate, audit)
                if decision["status"] == "allow":
                    gate.approvals += 1
                    body = policy.body_of(raw_arguments)
                    parsed, _ = policy.extract_receipts(body)
                    if parsed:
                        gate.approved_receipts = parsed
                else:
                    gate.denials += 1
                audit.write("decision", {"tool": tool_name, "decision": policy_safe(decision), "auto": auto_approve})
                items.append({
                    "type": "user.tool_approval",
                    "thread_id": thread_id,
                    "tool_call_id": pending["id"],
                    "approval": decision,
                })

            elif action_type == "tool.response_required":
                console.print(Panel(arguments, title="[bold cyan]AGENT QUESTION[/bold cyan]"))
                answer = console.input("[bold]Your answer:[/bold] ").strip()
                audit.write("answer", {"tool": tool_name, "answer": answer})
                items.append({
                    "type": "user.tool_response",
                    "thread_id": thread_id,
                    "tool_call_id": pending["id"],
                    "content": answer,
                })
    return items


def policy_safe(decision: dict[str, Any]) -> dict[str, Any]:
    if "reason" not in decision:
        return decision
    reason = str(decision["reason"])
    return {**decision, "reason": reason if len(reason) < 400 else reason[:400] + " ..."}


def _decide(
    tool_name: str,
    violations: list[Violation],
    fatal: bool,
    auto_approve: bool,
    gate: GateState,
    audit: AuditLog,
) -> dict[str, Any]:
    if fatal:
        console.print(Panel(
            "This write is refused outright. A fatal violation cannot be repaired by the agent and cannot be approved by a human.",
            title="[bold red]POLICY GATE: REFUSED[/bold red]", border_style="red"))
        gate.policy_denials += 1
        return {"status": "deny", "reason": policy.format_deny_reason(violations)}

    if violations and gate.repairs_left > 0:
        if not gate.repair_spent:
            gate.repair_spent = True
            gate.repairs_left -= 1
        gate.policy_denials += 1
        console.print(Panel(
            f"Auto-denied before a human was asked. The agent gets the violation codes and one chance to repair. Repairs left: {gate.repairs_left}.",
            title="[bold yellow]POLICY GATE: AUTO-DENIED[/bold yellow]", border_style="yellow"))
        return {"status": "deny", "reason": policy.format_deny_reason(violations)}

    if violations:
        console.print("[yellow]Repair budget exhausted. Payload still fails policy.[/yellow]")

    if auto_approve:
        if violations:
            console.print("[red]auto-approve refuses payloads that fail policy; denying[/red]")
            gate.policy_denials += 1
            return {"status": "deny", "reason": policy.format_deny_reason(violations)}
        console.print("[yellow]auto-approve enabled and policy clean; allowing[/yellow]")
        return {"status": "allow"}

    prompt = (
        "\n[bold]Approve this call?[/bold] [green]y[/green] post, [red]n[/red] deny, "
        "[cyan]d F1,F2[/cyan] deny and drop those findings: "
    )
    answer = console.input(prompt).strip()
    lowered = answer.lower()
    if lowered == "y":
        if violations:
            console.print("[red]refused: the policy gate is not overridable from this prompt[/red]")
            gate.policy_denials += 1
            return {"status": "deny", "reason": policy.format_deny_reason(violations)}
        return {"status": "allow"}
    if lowered.startswith("d"):
        identifiers = [part.strip() for part in answer[1:].replace(",", " ").split() if part.strip()]
        if identifiers:
            console.print(f"[cyan]denying and asking for repost without {', '.join(identifiers)}[/cyan]")
            return {"status": "deny", "reason": policy.drop_reason(identifiers)}
    reason = console.input("Reason for denial (optional): ").strip()
    decision: dict[str, Any] = {"status": "deny"}
    if reason:
        decision["reason"] = reason
    return decision


def extract_receipts_block(output: str) -> dict[str, Any] | None:
    """Return the receipts JSON object from a finished review comment, if present."""
    match = RECEIPTS_BLOCK.search(output or "")
    if match is None:
        return None
    try:
        receipts_data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(receipts_data, dict):
        return None
    return receipts_data


@dataclass
class GateState:
    repairs_left: int = 2
    approvals: int = 0
    denials: int = 0
    policy_denials: int = 0
    approved_receipts: dict[str, Any] | None = None
    repair_spent: bool = False


def run_review(
    client: TrueForgeClient,
    agent_name: str,
    prompt: str,
    auto_approve: bool,
    target: Target,
    session_id: str | None = None,
    repairs: int = 2,
    max_pauses: int = 10,
) -> str:
    if session_id:
        console.print(f"[cyan]resuming session[/cyan] {session_id}")
    else:
        session_id = client.create_session(agent_name)["id"]
    safe_session_id = session_id.replace("/", "_").replace(".", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit = AuditLog(RUNS_DIR / f"{stamp}-{safe_session_id}.jsonl")
    gate = GateState(repairs_left=repairs)
    console.print(Panel(
        f"agent: [bold]{agent_name}[/bold]\nsession: {session_id}\n"
        f"target: {target.repo or '?'} #{target.pr or '?'}\naudit log: {audit.path}",
        title="[bold]RECEIPTS[/bold]", border_style="blue"))
    try:
        audit.write("prompt", prompt)
        audit.write("target", {"repo": target.repo, "pr": target.pr, "session_id": session_id})
        input_items: list[dict[str, Any]] = [{"type": "user.message", "content": prompt}]
        output = ""
        for _ in range(max_pauses):
            required, index, output = stream_turn(client, session_id, input_items, audit)
            if not required:
                break
            input_items = collect_decisions(required, index, audit, auto_approve, target, gate)
            if not input_items:
                break
        else:
            raise TrueForgeError(f"Still pausing after {max_pauses} rounds; stopping.")

        if gate.approved_receipts is not None:
            path = receipts_store.save(gate.approved_receipts, target.repo, target.pr, session_id)
            audit.write("receipts_saved", {"path": str(path)})
            console.print(receipts_store.summary_table(gate.approved_receipts, title="Posted receipts"))
            console.print(
                f"[green]receipts saved:[/green] {path}\n"
                f"[dim]re-verify after the author pushes: "
                f"python -m agent.main --repo {target.repo} --pr {target.pr} --verify[/dim]"
            )
        console.print(
            f"[dim]gate: {gate.approvals} approved, {gate.denials} denied "
            f"({gate.policy_denials} by policy before a human was asked)[/dim]"
        )
        return output
    finally:
        audit.close()


def replay(path: Path, delay: float = 0.02) -> None:
    index: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            kind, payload = record["kind"], record["payload"]
            if kind == "event":
                render_event(payload, index)
                time.sleep(delay)
            elif kind == "prompt":
                console.print(Panel(str(payload), title="[bold]PROMPT[/bold]"))
            elif kind == "decision":
                status = payload["decision"]["status"].upper()
                colour = "green" if status == "ALLOW" else "red"
                console.print(f"\n[bold {colour}]HUMAN DECISION: {status}[/bold {colour}] on {payload['tool']}\n")
            elif kind == "policy_check":
                if payload.get("clean"):
                    console.print("[green]policy gate: PASSED[/green]")
                else:
                    console.print("[red]policy gate: FAILED[/red]")
                    for line in payload.get("violations", []):
                        console.print(f"  [red]{line}[/red]")
            elif kind == "answer":
                console.print(f"[cyan]human answered:[/cyan] {payload['answer']}")


def summarise_gate(manifest: dict[str, Any]) -> None:
    table = Table(title="Approval-gated tools", show_header=True)
    table.add_column("Connector")
    table.add_column("Requires approval")
    for server in manifest.get("mcp_servers", []):
        gated = server.get("require_approval_for_tools") or ["(none)"]
        table.add_row(server.get("name", "?"), "\n".join(gated))
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.getenv("REVIEW_REPO", ""))
    parser.add_argument("--pr", default=os.getenv("REVIEW_PR", ""))
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--show-gate", action="store_true")
    parser.add_argument("--show-policy", action="store_true")
    parser.add_argument("--replay", type=Path, default=None)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--receipts", type=Path, default=None)
    parser.add_argument("--auto-approve", action="store_true")
    args = parser.parse_args(argv)

    if args.replay:
        if not args.replay.exists():
            print(f"error: no such run log: {args.replay}", file=sys.stderr)
            return 2
        replay(args.replay)
        return 0

    try:
        agent_name, manifest = load_spec()
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.show_gate:
        summarise_gate(manifest)
        return 0

    if args.show_policy:
        table = Table(title="Verdict rules", show_header=True)
        table.add_column("Verdict")
        table.add_column("Required sandbox outcomes")
        table.add_column("May ship fix diff")
        for verdict, required, allows in policy.rules_summary():
            table.add_row(verdict, required, allows)
        console.print(table)
        return 0

    auto_approve = args.auto_approve
    if auto_approve and os.getenv("RECEIPTS_ALLOW_AUTO_APPROVE") != "1":
        print("error: --auto-approve also requires RECEIPTS_ALLOW_AUTO_APPROVE=1", file=sys.stderr)
        return 2

    prompt = args.prompt
    if prompt is None and not args.verify:
        if not args.repo or not args.pr:
            print("error: pass --repo and --pr, or set them in .env", file=sys.stderr)
            return 2
        prompt = REVIEW_PROMPT.format(repo=args.repo, pr=args.pr)

    target = Target(repo=args.repo, pr=str(args.pr))
    session_id = None

    if args.verify:
        if args.receipts:
            path = args.receipts
        else:
            if not args.repo or not args.pr:
                print("error: --verify needs --repo and --pr (or --receipts) to locate the artifact", file=sys.stderr)
                return 2
            path = receipts_store.artifact_path(target.repo, target.pr)
        if not path.exists():
            print(f"error: no receipts artifact at {path}; run a review first", file=sys.stderr)
            return 2
        try:
            artifact = receipts_store.load(path)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"error: invalid receipts artifact at {path}: {exc}", file=sys.stderr)
            return 2
        try:
            art_repo = str(artifact.get("receipts", {}).get("repo", ""))
            art_pr = str(artifact.get("receipts", {}).get("pr", ""))
            if args.repo and args.pr:
                if art_repo != args.repo or art_pr != str(args.pr):
                    print(f"error: artifact targets {art_repo}#{art_pr} but --repo/--pr specify {args.repo}#{args.pr}", file=sys.stderr)
                    return 2
            else:
                args.repo, args.pr = art_repo, art_pr
                target = Target(repo=art_repo, pr=art_pr)
            session_id = artifact.get("session_id") or None
            prompt = receipts_store.verification_prompt(artifact)
            console.print(receipts_store.summary_table(artifact["receipts"], title=f"Receipts on file ({path})"))
        except (ValueError, json.JSONDecodeError, AttributeError) as exc:
            print(f"error: failed to process receipts artifact at {path}: {exc}", file=sys.stderr)
            return 2

    with build_client() as client:
        if not client.health():
            print(f"error: no TrueForge server at {client.base_url}", file=sys.stderr)
            return 1
        try:
            client.upsert_agent(agent_name, manifest)
            output = run_review(client, agent_name, prompt, auto_approve, target, session_id=session_id)
        except TrueForgeError as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            return 1

    console.print(Panel(output or "(no final output)", title="[bold]RESULT[/bold]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
