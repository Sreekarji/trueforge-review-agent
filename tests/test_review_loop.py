from __future__ import annotations
import json
from pathlib import Path
import pytest
from agent import receipts
from agent.main import lookup_call, merge_delta, redact
from agent.provision import SpecError, gated_tools, load_spec

SPEC = Path("agent/agent_spec.yaml")
ENV = {
    "TRUEFORGE_MODEL": "deep32/deepseek-v4-flash",
    "TRUEFORGE_GITHUB_CONNECTOR": "github",
    "TRUEFORGE_DEEPWIKI_CONNECTOR": "deepwiki",
}

def test_write_tools_are_gated() -> None:
    _, manifest = load_spec(SPEC, ENV)
    gated = gated_tools(manifest)
    for tool in ("add_issue_comment", "create_issue", "create_issue_comment", "create_pull_request_review"):
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


def test_iteration_limit_leaves_room_for_three_runs_per_hypothesis() -> None:
    _, manifest = load_spec(SPEC, ENV)
    assert manifest["config"]["iteration_limit"] >= 100


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


def _clean_receipts_payload() -> dict:
    return {
        "schema": "receipts/v1", "repo": "Sreekarji/trueforge-review-agent", "pr": 2,
        "base_sha": "aaa", "head_sha": "bbb",
        "counts": {"raised": 1, "confirmed": 1, "refuted": 0, "unverified": 0},
        "findings": [{
            "id": "F1", "severity": "high", "file": "a.py", "line": 1,
            "claim": "mean() mishandles a single sample", "verdict": "REGRESSION",
            "test_path": "/work/F1/t.py", "test_source": "def test_mean():\n    assert mean([12.5]) == 12.5\n",
            "runs": {
                "head": {"cmd": "python -m pytest /work/F1/t.py -q", "exit_code": 1, "outcome": "fail", "tail": "E   AssertionError: assert 0.0 == 12.5\n1 failed in 0.11s"},
                "base": {"cmd": "python -m pytest /work/F1/t.py -q", "exit_code": 0, "outcome": "pass", "tail": "1 passed in 0.09s ................................"},
                "patched": {"cmd": "python -m pytest /work/F1/t.py -q", "exit_code": 0, "outcome": "pass", "tail": "1 passed in 0.09s ................................"},
            },
            "fix_diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-    return 0.0\n+    return total / len(xs)\n",
        }],
    }


def _approval_action(pending_id: str, source_event_id: str) -> dict:
    return {"type": "tool.approval_required", "thread_id": "t1", "tool_calls": [{"id": pending_id, "source_event_id": source_event_id}]}


def test_collect_decisions_auto_denies_violation_through_repair_budget(tmp_path, monkeypatch) -> None:
    from agent.main import AuditLog, GateState, collect_decisions
    from agent.policy import Target
    index = {"m1": {"id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(
        {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": "no receipts block here"})}}]}}
    audit = AuditLog(tmp_path / "audit.jsonl")
    gate = GateState()
    shown: list[bool] = []
    def fake_show(tool_name, arguments, clean):
        shown.append(clean)
    monkeypatch.setattr("agent.main.show_approval_request", fake_show)
    items = collect_decisions([_approval_action("c1", "m1")], index, audit, auto_approve=False,
                              target=Target(repo="Sreekarji/trueforge-review-agent", pr="2"), gate=gate)
    assert items[0]["approval"]["status"] == "deny"
    assert "policy" in items[0]["approval"]["reason"].lower()
    assert shown == [False]          # panel shown with FAILED verdict, no human prompt yet
    assert gate.policy_denials == 1  # consumed one repair
    assert gate.repairs_left == 1


def test_collect_decisions_refuses_human_override_when_repairs_exhausted(tmp_path, monkeypatch) -> None:
    from agent.main import AuditLog, GateState, collect_decisions
    from agent.policy import Target
    index = {"m1": {"id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(
        {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": "no receipts block here"})}}]}}
    audit = AuditLog(tmp_path / "audit.jsonl")
    gate = GateState(repairs_left=0)
    monkeypatch.setattr("agent.main.console.input", lambda prompt="": "y")
    items = collect_decisions([_approval_action("c1", "m1")], index, audit, auto_approve=False,
                              target=Target(repo="Sreekarji/trueforge-review-agent", pr="2"), gate=gate)
    assert items[0]["approval"]["status"] == "deny"  # human 'y' cannot override policy
    assert "policy" in items[0]["approval"]["reason"].lower()
    assert gate.denials == 1
    assert gate.policy_denials == 1  # refused human override is a policy-caused denial


def test_repair_budget_decrements_once_per_batch(tmp_path) -> None:
    from agent.main import AuditLog, GateState, collect_decisions
    from agent.policy import Target
    bad = {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": "no receipts block here"}
    index = {"m1": {"id": "m1", "tool_calls": [
        {"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(bad)}},
        {"id": "c2", "function": {"name": "add_issue_comment", "arguments": json.dumps(bad)}},
    ]}}
    action = {"type": "tool.approval_required", "thread_id": "t1", "tool_calls": [
        {"id": "c1", "source_event_id": "m1"}, {"id": "c2", "source_event_id": "m1"}]}
    audit = AuditLog(tmp_path / "audit.jsonl")
    gate = GateState()
    items = collect_decisions([action], index, audit, auto_approve=False,
                              target=Target(repo="Sreekarji/trueforge-review-agent", pr="2"), gate=gate)
    assert [i["approval"]["status"] for i in items] == ["deny", "deny"]
    assert gate.repairs_left == 1      # decremented ONCE for the batch, not per call
    assert gate.policy_denials == 2    # every policy-caused denial still counted


def test_drop_command_preserves_finding_id_case(tmp_path, monkeypatch) -> None:
    from agent.main import AuditLog, GateState, collect_decisions
    from agent.policy import Target
    index = {"m1": {"id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(
        {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": "no receipts block here"})}}]}}
    audit = AuditLog(tmp_path / "audit.jsonl")
    gate = GateState(repairs_left=0)
    monkeypatch.setattr("agent.main.console.input", lambda prompt="": "d F1,F2")
    items = collect_decisions([_approval_action("c1", "m1")], index, audit, auto_approve=False,
                              target=Target(repo="Sreekarji/trueforge-review-agent", pr="2"), gate=gate)
    assert items[0]["approval"]["status"] == "deny"
    reason = items[0]["approval"]["reason"]
    assert "F1, F2" in reason       # original case preserved
    assert "f1" not in reason       # not lowercased
    assert gate.denials == 1


def test_auto_approve_refusal_counts_policy_denial(tmp_path) -> None:
    from agent.main import AuditLog, GateState, collect_decisions
    from agent.policy import Target
    index = {"m1": {"id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(
        {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": "no receipts block here"})}}]}}
    audit = AuditLog(tmp_path / "audit.jsonl")
    gate = GateState(repairs_left=0)
    items = collect_decisions([_approval_action("c1", "m1")], index, audit, auto_approve=True,
                              target=Target(repo="Sreekarji/trueforge-review-agent", pr="2"), gate=gate)
    assert items[0]["approval"]["status"] == "deny"
    assert gate.policy_denials == 1  # auto-approve rejection is a policy-caused denial
    assert gate.denials == 1


def test_run_review_resumes_given_session(tmp_path, monkeypatch) -> None:
    from agent.main import run_review
    from agent.policy import Target
    monkeypatch.setattr("agent.main.RUNS_DIR", tmp_path)
    seen: list[tuple] = []
    class FakeClient:
        def create_session(self, name):
            seen.append(("create", name))
            return {"id": "brand-new"}
        def stream_turn(self, session_id, input_items):
            seen.append(("stream", session_id))
            yield {"type": "turn.done", "state": {"status": "done", "required_actions": [], "output": {"content": "done"}}}
    out = run_review(FakeClient(), "agent", "p", False, Target(repo="owner/repo", pr="1"), session_id="orig-sess")
    assert out == "done"
    assert ("create", "agent") not in seen      # session resumed, not created
    assert ("stream", "orig-sess") in seen


def test_run_review_persists_approved_receipts(tmp_path, monkeypatch) -> None:
    from agent.main import run_review
    from agent.policy import Target
    monkeypatch.setattr("agent.main.RUNS_DIR", tmp_path)
    captured: dict = {}
    def fake_save(receipts, repo, pr, session_id, directory=Path("runs"), path=None):
        captured.update(receipts=receipts, repo=repo, pr=pr, session_id=session_id, path=path)
        return tmp_path / "artifact.json"
    monkeypatch.setattr("agent.main.receipts_store.save", fake_save)
    body = "## Receipts\n\n```receipts\n" + json.dumps(_clean_receipts_payload()) + "\n```"
    args = {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": body}
    action = {"type": "tool.approval_required", "thread_id": "t1", "tool_calls": [{"id": "c1", "source_event_id": "m1"}]}
    index = {"m1": {"id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(args)}}]}}
    class FakeClient:
        def __init__(self):
            self.calls = 0
        def create_session(self, name):
            return {"id": "sess-1"}
        def stream_turn(self, session_id, input_items):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "model.message", "id": "m1", "tool_calls": [
                    {"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(args)}}]}
                yield {"type": "turn.done", "state": {"status": "done", "required_actions": [action], "output": {"content": "paused"}}}
            else:
                yield {"type": "turn.done", "state": {"status": "done", "required_actions": [], "output": {"content": "posted"}}}
    out = run_review(FakeClient(), "agent", "p", True, Target(repo="Sreekarji/trueforge-review-agent", pr="2"), session_id="sess-1")
    assert out == "posted"
    assert captured["repo"] == "Sreekarji/trueforge-review-agent"
    assert captured["pr"] == "2"
    assert captured["session_id"] == "sess-1"
    assert captured["receipts"]["findings"][0]["id"] == "F1"
    assert captured["path"] is None  # canonical path (no custom save_path supplied)


def test_run_review_saves_to_custom_path(tmp_path, monkeypatch) -> None:
    from agent.main import run_review
    from agent.policy import Target
    monkeypatch.setattr("agent.main.RUNS_DIR", tmp_path)
    captured: dict = {}
    def fake_save(receipts, repo, pr, session_id, directory=Path("runs"), path=None):
        captured["path"] = path
        return path or (tmp_path / "artifact.json")
    monkeypatch.setattr("agent.main.receipts_store.save", fake_save)
    body = "## Receipts\n\n```receipts\n" + json.dumps(_clean_receipts_payload()) + "\n```"
    args = {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": body}
    action = {"type": "tool.approval_required", "thread_id": "t1", "tool_calls": [{"id": "c1", "source_event_id": "m1"}]}
    index = {"m1": {"id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(args)}}]}}
    class FakeClient:
        def __init__(self): self.calls = 0
        def create_session(self, name): return {"id": "sess-1"}
        def stream_turn(self, session_id, input_items):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "model.message", "id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(args)}}]}
                yield {"type": "turn.done", "state": {"status": "done", "required_actions": [action], "output": {"content": "paused"}}}
            else:
                yield {"type": "turn.done", "state": {"status": "done", "required_actions": [], "output": {"content": "posted"}}}
    custom = tmp_path / "custom" / "my.json"
    run_review(FakeClient(), "agent", "p", True, Target(repo="Sreekarji/trueforge-review-agent", pr="2"), session_id="sess-1", save_path=custom)
    assert captured["path"] == custom


def test_collect_decisions_passes_clean_flag_and_allows(tmp_path, monkeypatch) -> None:
    from agent.main import AuditLog, GateState, collect_decisions
    from agent.policy import Target
    body = "## Receipts\n\n```receipts\n" + json.dumps(_clean_receipts_payload()) + "\n```"
    index = {"m1": {"id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(
        {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": body})}}]}}
    audit = AuditLog(tmp_path / "audit.jsonl")
    gate = GateState()
    captured: dict = {}
    def fake_show(tool_name, arguments, clean):
        captured["clean"] = clean
    monkeypatch.setattr("agent.main.show_approval_request", fake_show)
    items = collect_decisions([_approval_action("c1", "m1")], index, audit, auto_approve=True,
                              target=Target(repo="Sreekarji/trueforge-review-agent", pr="2"), gate=gate)
    assert items[0]["approval"]["status"] == "allow"
    assert captured["clean"] is True
    assert gate.approvals == 1
    assert gate.approved_receipts is not None  # receipts extracted from the approved body


def _verify_artifact(tmp_path, repo, pr, session_id="sess_1"):
    payload = {"schema": "receipts/v1", "repo": repo, "pr": pr, "base_sha": "aaa", "head_sha": "bbb",
               "counts": {"raised": 0, "confirmed": 0, "refuted": 0, "unverified": 0}, "findings": []}
    return receipts.save(payload, repo, str(pr), session_id, directory=tmp_path)


def test_verify_rejects_artifact_target_mismatch(monkeypatch, tmp_path) -> None:
    from agent.main import main
    monkeypatch.setattr("agent.main.load_dotenv", lambda: None)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    path = _verify_artifact(tmp_path, "other/repo", 9)
    def boom(*a, **k):
        raise AssertionError("must not connect to the server on a mismatched artifact")
    monkeypatch.setattr("agent.main.build_client", boom)
    rc = main(["--verify", "--receipts", str(path), "--repo", "Sreekarji/trueforge-review-agent", "--pr", "2"])
    assert rc == 2


def test_verify_derives_target_from_artifact(monkeypatch, tmp_path) -> None:
    from agent.main import main
    monkeypatch.setattr("agent.main.load_dotenv", lambda: None)
    monkeypatch.delenv("REVIEW_REPO", raising=False)
    monkeypatch.delenv("REVIEW_PR", raising=False)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    path = _verify_artifact(tmp_path, "Sreekarji/trueforge-review-agent", 2)
    class FakeClient:
        base_url = "http://fake"
        def health(self): return False
        def __enter__(self): return self
        def __exit__(self, *exc): return None
    monkeypatch.setattr("agent.main.build_client", lambda: FakeClient())
    rc = main(["--verify", "--receipts", str(path)])
    assert rc == 1  # got past artifact processing with derived target, then no server


def test_verify_invalid_artifact_is_a_friendly_error(monkeypatch, tmp_path) -> None:
    from agent.main import main
    monkeypatch.setattr("agent.main.load_dotenv", lambda: None)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    junk = tmp_path / "junk.json"
    junk.write_text("{not valid json", encoding="utf-8")
    def boom(*a, **k):
        raise AssertionError("must not connect to the server for an invalid artifact")
    monkeypatch.setattr("agent.main.build_client", boom)
    rc = main(["--verify", "--receipts", str(junk), "--repo", "Sreekarji/trueforge-review-agent", "--pr", "2"])
    assert rc == 2


def test_decide_eoferror_on_approval_prompt_auto_denies(tmp_path, monkeypatch) -> None:
    from agent.main import AuditLog, GateState, collect_decisions
    from agent.policy import Target
    index = {"m1": {"id": "m1", "tool_calls": [{"id": "c1", "function": {"name": "add_issue_comment", "arguments": json.dumps(
        {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": "no receipts block here"})}}]}}
    audit = AuditLog(tmp_path / "audit.jsonl")
    gate = GateState(repairs_left=0)
    call_count = 0
    def raise_eof(prompt=""):
        nonlocal call_count
        call_count += 1
        raise EOFError
    monkeypatch.setattr("agent.main.console.input", raise_eof)
    items = collect_decisions([_approval_action("c1", "m1")], index, audit, auto_approve=False,
                              target=Target(repo="Sreekarji/trueforge-review-agent", pr="2"), gate=gate)
    assert items[0]["approval"]["status"] == "deny"
    assert items[0]["approval"]["reason"] == "stdin closed"


def test_replay_renders_target_and_receipts_saved(tmp_path, monkeypatch) -> None:
    from agent.main import replay
    log = tmp_path / "run.jsonl"
    import json as _json
    lines = [
        _json.dumps({"kind": "target", "payload": {"repo": "owner/repo", "pr": "1", "session_id": "s1"}}),
        _json.dumps({"kind": "receipts_saved", "payload": {"path": "runs/artifact.json"}}),
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    printed: list[str] = []
    monkeypatch.setattr("agent.main.console.print", lambda msg, **kw: printed.append(str(msg)))
    replay(log, delay=0)
    assert any("owner/repo" in p for p in printed)
    assert any("runs/artifact.json" in p for p in printed)


def test_unverified_finding_without_line_passes_policy() -> None:
    from agent.policy import check_receipts, Target
    import copy
    r = {
        "schema": "receipts/v1", "repo": "Sreekarji/trueforge-review-agent", "pr": "2",
        "base_sha": "aaa", "head_sha": "bbb",
        "counts": {"raised": 1, "confirmed": 0, "refuted": 0, "unverified": 1},
        "findings": [{
            "id": "U1", "severity": "low", "file": "metrics/io.py",
            "claim": "could not be reproduced in sandbox", "verdict": "UNVERIFIED",
        }],
    }
    violations = check_receipts(r, Target(repo="Sreekarji/trueforge-review-agent", pr="2"))
    codes = {v.code for v in violations}
    assert "MISSING_FIELD" not in codes


def test_create_session_raises_on_missing_id(monkeypatch) -> None:
    from agent.trueforge_client import TrueForgeClient, TrueForgeError
    import unittest.mock as mock
    client = TrueForgeClient.__new__(TrueForgeClient)
    client._http = None
    with mock.patch.object(client, "_request", return_value={"data": {}}):
        with pytest.raises(TrueForgeError, match="no 'id'"):
            client.create_session("agent")


def test_stream_turn_wraps_httpx_error(monkeypatch) -> None:
    from agent.trueforge_client import TrueForgeClient, TrueForgeError
    import httpx, unittest.mock as mock
    client = TrueForgeClient.__new__(TrueForgeClient)
    http_mock = mock.MagicMock()
    http_mock.stream.side_effect = httpx.ReadTimeout("timed out")
    client._http = http_mock
    with pytest.raises(TrueForgeError, match="stream_turn network error"):
        list(client.stream_turn("sess", []))
