"""Receipts - an evidence-first PR review agent running on TrueForge."""

from __future__ import annotations

import argparse
import json
import os
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

console = Console(highlight=False)
RUNS_DIR = Path("runs")

SENSITIVE_KEY_HINTS = ("token", "key", "secret", "password", "authorization", "pat")
SECRET_PREFIXES = ("ghp_", "github_pat_", "sk-", "dtn_")
MAX_ARG_CHARS = 4000

REVIEW_PROMPT = (
    "Review pull request #{pr} in {repo}.\n\n"
    "Prove every hypothesis three times in the sandbox: the reproduction must "
    "PASS against the base branch version of the file, FAIL against this PR's "
    "head, and PASS again once your proposed fix is applied. Fan the proofs out "
    "to one subagent per hypothesis. Report only what you executed, with the "
    "real commands and output, and say how many hypotheses you dropped because "
    "the reproduction passed on head.\n\n"
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


def show_approval_request(tool_name: str, arguments: str) -> None:
    console.print()
    console.print(Panel(f"The agent wants to call [bold]{tool_name}[/bold], which writes to GitHub.\nNothing has been posted yet.", title="[bold red]APPROVAL GATE[/bold red]", border_style="red"))
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


def collect_decisions(required: Iterable[dict[str, Any]], index: dict[str, dict[str, Any]], audit: AuditLog, auto_approve: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for action in required:
        action_type = action.get("type")
        thread_id = action.get("thread_id") or "main"
        if action_type == "mcp.auth_required":
            urls = ", ".join(s.get("auth_url", "") for s in action.get("mcp_servers", []))
            raise TrueForgeError(f"Connector needs OAuth: {urls}")
        for pending in action.get("tool_calls", []):
            tool_name, arguments = lookup_call(index, pending)
            if action_type == "tool.approval_required":
                show_approval_request(tool_name, arguments)
                if auto_approve:
                    console.print("[yellow]auto-approve enabled; allowing[/yellow]")
                    decision: dict[str, Any] = {"status": "allow"}
                else:
                    answer = console.input("\n[bold]Approve this call?[/bold] [green]y[/green] to post, [red]n[/red] to deny: ").strip().lower()
                    if answer == "y":
                        decision = {"status": "allow"}
                    else:
                        reason = console.input("Reason for denial (optional): ").strip()
                        decision = {"status": "deny"}
                        if reason:
                            decision["reason"] = reason
                audit.write("decision", {"tool": tool_name, "decision": decision, "auto": auto_approve})
                items.append({"type": "user.tool_approval", "thread_id": thread_id, "tool_call_id": pending["id"], "approval": decision})
            elif action_type == "tool.response_required":
                console.print(Panel(arguments, title="[bold cyan]AGENT QUESTION[/bold cyan]"))
                answer = console.input("[bold]Your answer:[/bold] ").strip()
                audit.write("answer", {"tool": tool_name, "answer": answer})
                items.append({"type": "user.tool_response", "thread_id": thread_id, "tool_call_id": pending["id"], "content": answer})
    return items


def run_review(client: TrueForgeClient, agent_name: str, prompt: str, auto_approve: bool, max_pauses: int = 10) -> str:
    session = client.create_session(agent_name)
    session_id = session["id"]
    safe_session_id = session_id.replace("/", "_").replace(".", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit = AuditLog(RUNS_DIR / f"{stamp}-{safe_session_id}.jsonl")
    console.print(Panel(f"agent: [bold]{agent_name}[/bold]\nsession: {session_id}\naudit log: {audit.path}", title="[bold]RECEIPTS[/bold]", border_style="blue"))
    try:
        audit.write("prompt", prompt)
        input_items: list[dict[str, Any]] = [{"type": "user.message", "content": prompt}]
        for _ in range(max_pauses):
            required, index, output = stream_turn(client, session_id, input_items, audit)
            if not required:
                return output
            input_items = collect_decisions(required, index, audit, auto_approve)
            if not input_items:
                return output
        raise TrueForgeError(f"Still pausing after {max_pauses} rounds; stopping.")
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
    parser.add_argument("--replay", type=Path, default=None)
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

    auto_approve = args.auto_approve
    if auto_approve and os.getenv("RECEIPTS_ALLOW_AUTO_APPROVE") != "1":
        print("error: --auto-approve also requires RECEIPTS_ALLOW_AUTO_APPROVE=1", file=sys.stderr)
        return 2

    prompt = args.prompt
    if prompt is None:
        if not args.repo or not args.pr:
            print("error: pass --repo and --pr, or set them in .env", file=sys.stderr)
            return 2
        prompt = REVIEW_PROMPT.format(repo=args.repo, pr=args.pr)

    with build_client() as client:
        if not client.health():
            print(f"error: no TrueForge server at {client.base_url}", file=sys.stderr)
            return 1
        try:
            client.upsert_agent(agent_name, manifest)
            output = run_review(client, agent_name, prompt, auto_approve)
        except TrueForgeError as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            return 1

    console.print(Panel(output or "(no final output)", title="[bold]RESULT[/bold]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
