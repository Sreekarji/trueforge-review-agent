from __future__ import annotations
import json
from pathlib import Path
import pytest
from agent import receipts as receipts_store

RECEIPTS = {
    "schema": "receipts/v1", "repo": "Sreekarji/trueforge-review-agent", "pr": 2, "head_sha": "bbb",
    "counts": {"raised": 3, "confirmed": 2, "refuted": 1, "unverified": 0},
    "findings": [
        {"id": "F1", "severity": "high", "file": "metrics/aggregate.py", "line": 42,
         "claim": "mean() returns 0.0 for a single sample", "verdict": "REGRESSION",
         "test_path": "/work/F1/test_f1.py", "test_source": "def test_mean():\n    assert mean([12.5]) == 12.5\n",
         "runs": {"base": {"outcome": "pass"}, "head": {"outcome": "fail"}, "patched": {"outcome": "pass"}}},
        {"id": "F2", "severity": "low", "file": "metrics/io.py", "claim": "could not be reproduced", "verdict": "UNVERIFIED"},
        {"id": "F3", "severity": "medium", "file": "metrics/io.py", "line": 7,
         "claim": "file handle leaks on exception", "verdict": "UNFIXED",
         "test_source": "def test_leak():\n    ...\n", "runs": {"head": {"outcome": "fail"}}},
    ],
}

def test_artifact_path_is_deterministic(tmp_path):
    path = receipts_store.artifact_path("Sreekarji/trueforge-review-agent", "2", Path("runs"))
    assert path == Path("runs/receipts-sreekarji-trueforge-review-agent-735cd76ad3-pr2.json")

def test_save_then_load_round_trips(tmp_path):
    path = receipts_store.save(RECEIPTS, "Sreekarji/trueforge-review-agent", "2", "sess_123", tmp_path)
    artifact = receipts_store.load(path)
    assert artifact["session_id"] == "sess_123"
    assert artifact["receipts"]["findings"][0]["id"] == "F1"

def test_load_rejects_non_artifact(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(ValueError):
        receipts_store.load(path)

def test_only_proven_findings_are_re_verified():
    ids = [f["id"] for f in receipts_store.proven_findings(RECEIPTS)]
    assert ids == ["F1", "F3"]

def test_verification_prompt_carries_stored_tests():
    artifact = {"repo": "Sreekarji/trueforge-review-agent", "pr": 2, "receipts": RECEIPTS}
    prompt = receipts_store.verification_prompt(artifact)
    assert "Re-verify, do not re-review" in prompt
    assert "assert mean([12.5]) == 12.5" in prompt
    assert "F1" in prompt and "F3" in prompt and "F2" not in prompt

def test_summary_table_renders_all_findings():
    table = receipts_store.summary_table(RECEIPTS)
    assert table.row_count == 3

def test_save_respects_explicit_path(tmp_path):
    target = tmp_path / "custom" / "my-artifact.json"
    path = receipts_store.save(RECEIPTS, "Sreekarji/trueforge-review-agent", "2", "s1", path=target)
    assert path == target and target.exists()
