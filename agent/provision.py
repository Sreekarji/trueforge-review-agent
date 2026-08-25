"""Resolve agent/agent_spec.yaml against the environment and upsert it."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .trueforge_client import DEFAULT_BASE_URL, TrueForgeClient, TrueForgeError

SPEC_PATH = Path(__file__).with_name("agent_spec.yaml")
_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


class SpecError(RuntimeError):
    pass


def _substitute(node: Any, env: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {key: _substitute(value, env) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute(value, env) for value in node]
    if isinstance(node, str):
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            value = env.get(key, "")
            if not value:
                raise SpecError(f"{key} is required by agent_spec.yaml but is unset or empty.")
            return value
        return _PLACEHOLDER.sub(replace, node)
    return node


def load_spec(path: Path = SPEC_PATH, env: dict[str, str] | None = None) -> tuple[str, dict[str, Any]]:
    environment = dict(os.environ if env is None else env)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "name" not in raw or "manifest" not in raw:
        raise SpecError(f"{path} must define top-level 'name' and 'manifest' keys.")
    resolved = _substitute(raw, environment)
    return resolved["name"], resolved["manifest"]


def gated_tools(manifest: dict[str, Any]) -> set[str]:
    gated: set[str] = set()
    for server in manifest.get("mcp_servers", []):
        gated.update(server.get("require_approval_for_tools", []))
    return gated


def build_client() -> TrueForgeClient:
    return TrueForgeClient(
        base_url=os.getenv("TRUEFORGE_BASE_URL", DEFAULT_BASE_URL),
        token=os.getenv("TRUEFORGE_TOKEN") or None,
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Provision the Receipts agent.")
    parser.add_argument("--print", action="store_true", dest="print_only")
    parser.add_argument("--list-tools", metavar="CONNECTOR")
    args = parser.parse_args(argv)

    try:
        name, manifest = load_spec()
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.print_only:
        print(json.dumps({"name": name, "manifest": manifest}, indent=2))
        return 0

    with build_client() as client:
        if not client.health():
            print(f"error: no TrueForge server at {client.base_url}", file=sys.stderr)
            return 1

        if args.list_tools:
            try:
                for tool in client.list_mcp_tools(args.list_tools):
                    print(tool.get("name", "<unnamed>"))
            except TrueForgeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0

        try:
            agent_id, action = client.upsert_agent(name, manifest)
        except TrueForgeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    print(f"{action} agent {name!r} (id={agent_id})")
    print(f"approval-gated tool selectors: {', '.join(sorted(gated_tools(manifest)))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
