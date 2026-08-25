from __future__ import annotations
import json
from pathlib import Path
import pytest
from agent.main import lookup_call, merge_delta, redact
from agent.provision import SpecError, gated_tools, load_spec

SPEC = Path("agent/agent_spec.yaml")
ENV = {
    "TRUEFORGE_MODEL": "bai/deepseek-v4-flash",
    "TRUEFORGE_GITHUB_CONNECTOR": "github",
    "TRUEFORGE_DEEPWIKI_CONNECTOR": "deepwiki",
}

def test_write_tools_are_gated() -> None:
    _, manifest = load_spec(SPEC, ENV)
    gated = gated_tools(manifest)
    for tool in ("add_issue_comment", "create_issue", "create_pull_request_review"):
        assert tool in gated, f"{tool} must never run without approval"

def test_missing_env_var_fails_loudly() -> None:
    with pytest.raises(SpecError, match="TRUEFORGE_MODEL"):
        load_spec(SPEC, {k: v for k, v in ENV.items() if k != "TRUEFORGE_MODEL"})

def test_sandbox_enabled() -> None:
    _, manifest = load_spec(SPEC, ENV)
    assert manifest["config"]["sandbox"]["enabled"] is True

def test_redact_masks_keys_and_token_shaped_strings() -> None:
    payload = {"Authorization": "Bearer abc", "body": "leaked ghp_deadbeef", "n": 1}
    assert redact(payload) == {"Authorization": "***redacted***", "body": "***redacted***", "n": 1}

def test_merge_delta_reassembles_streamed_tool_arguments() -> None:
    base = {"id": "m1", "content": "", "tool_calls": []}
    merge_delta(base, {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "add_issue_comment", "arguments": '{"bo'}}]})
    merge_delta(base, {"tool_calls": [{"index": 0, "function": {"arguments": 'dy":"hi"}'}}]})
    call = base["tool_calls"][0]
    assert call["function"]["name"] == "add_issue_comment"
    assert json.loads(call["function"]["arguments"]) == {"body": "hi"}

def test_lookup_call_resolves_name_and_redacts_arguments() -> None:
    index = {"m1": {"id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": '{"token":"ghp_x","body":"hi"}'}}]}}
    name, arguments = lookup_call(index, {"id": "c1", "source_event_id": "m1"})
    assert name == "add_issue_comment"
    assert "ghp_x" not in arguments and "***redacted***" in arguments

def test_lookup_call_degrades_gracefully_on_unknown_event() -> None:
    assert lookup_call({}, {"id": "c9", "source_event_id": "nope"})[0] == "<unknown tool>"
