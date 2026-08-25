from __future__ import annotations
import json
from pathlib import Path
import pytest
from agent import receipts
from agent.main import lookup_call, merge_delta, persist_receipts, redact
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


def test_artifact_path_hashes_full_repo_identifier() -> None:
    left = receipts.artifact_path("foo/bar-baz", "1")
    right = receipts.artifact_path("foo-bar/baz", "1")
    assert left != right, "slug collision must not produce the same artifact path"
    assert left.name.startswith("receipts-foo-bar-baz-")


def test_persist_receipts_extracts_block_and_saves(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    def fake_save(data, repo, pr, session_id, directory=Path("runs")):
        captured["data"] = data
        captured["repo"] = repo
        captured["pr"] = pr
        return tmp_path / "artifact.json"
    monkeypatch.setattr(receipts, "save", fake_save)
    body = "## Review\n\n```receipts\n" + json.dumps({"schema": "receipts/v1", "counts": {}, "findings": []}) + "\n```"
    persist_receipts(body, "owner/repo", "7", "sess-1")
    assert captured["data"]["schema"] == "receipts/v1"
    assert captured["repo"] == "owner/repo" and captured["pr"] == "7"


def test_persist_receipts_ignores_comment_without_block(monkeypatch) -> None:
    monkeypatch.setattr(receipts, "save", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not save")))
    persist_receipts("Just a normal comment.", "owner/repo", "7", "sess-1")


def test_unfixed_findings_drop_fix_diff_and_patched_run(tmp_path) -> None:
    payload = {"schema": "receipts/v1", "findings": [
        {"id": "U1", "verdict": "UNFIXED",
         "fix_diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-foo\n+bar",
         "runs": {"head": {"outcome": "fail"}, "patched": {"outcome": "pass"}}},
        {"id": "R1", "verdict": "REGRESSION",
         "fix_diff": "--- a/x\n+++ b/x",
         "runs": {"head": {}, "base": {}, "patched": {}}},
    ]}
    stored = receipts.load(receipts.save(payload, "owner/repo", "3", "s1", directory=tmp_path))["receipts"]
    unfixed = stored["findings"][0]
    assert "fix_diff" not in unfixed
    assert "patched" not in unfixed["runs"]
    regression = stored["findings"][1]
    assert regression["fix_diff"] and "patched" in regression["runs"]
