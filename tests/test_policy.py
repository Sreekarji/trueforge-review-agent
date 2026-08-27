from __future__ import annotations
import copy
import json
from agent import policy
from agent.policy import Target, check_payload, check_receipts, extract_receipts

TARGET = Target(repo="Sreekarji/trueforge-review-agent", pr="2")

GOOD_RUN_FAIL = {"cmd": "python -m pytest /work/F1/test_f1.py -q", "exit_code": 1, "outcome": "fail", "tail": "E   AssertionError: assert 0.0 == 12.5\n1 failed in 0.11s"}
GOOD_RUN_PASS = {"cmd": "python -m pytest /work/F1/test_f1.py -q", "exit_code": 0, "outcome": "pass", "tail": "1 passed in 0.09s ................................"}

RECEIPTS = {
    "schema": "receipts/v1", "repo": "Sreekarji/trueforge-review-agent", "pr": 2,
    "base_sha": "aaa", "head_sha": "bbb",
    "counts": {"raised": 3, "confirmed": 1, "refuted": 2, "unverified": 0},
    "findings": [{
        "id": "F1", "severity": "high", "file": "metrics/aggregate.py", "line": 42,
        "claim": "mean() returns 0.0 for a single sample", "verdict": "REGRESSION",
        "test_path": "/work/F1/test_f1.py",
        "test_source": "def test_mean():\n    assert mean([12.5]) == 12.5\n",
        "runs": {"base": GOOD_RUN_PASS, "head": GOOD_RUN_FAIL, "patched": GOOD_RUN_PASS},
        "fix_diff": "--- a/metrics/aggregate.py\n+++ b/metrics/aggregate.py\n@@\n-    return 0.0\n+    return total / len(xs)\n",
    }],
}

def payload(receipts, tool="add_issue_comment", **overrides):
    body = "## Receipts\n\n```receipts\n" + json.dumps(receipts) + "\n```"
    args = {"owner": "Sreekarji", "repo": "trueforge-review-agent", "issue_number": 2, "body": body}
    args.update(overrides)
    return tool, args

def codes(violations): return {v.code for v in violations}

def test_a_fully_proven_report_passes():
    assert check_payload(*payload(RECEIPTS), TARGET) == []

def test_missing_receipts_block_is_denied():
    tool, args = payload(RECEIPTS); args["body"] = "Looks good!"
    assert "NO_RECEIPTS" in codes(check_payload(tool, args, TARGET))

def test_regression_claim_without_passing_base_is_contradicted():
    r = copy.deepcopy(RECEIPTS); r["findings"][0]["runs"]["base"] = GOOD_RUN_FAIL
    assert "VERDICT_CONTRADICTED" in codes(check_receipts(r, TARGET))

def test_fix_diff_without_green_patched_run_is_denied():
    r = copy.deepcopy(RECEIPTS); r["findings"][0]["verdict"] = "UNFIXED"; r["findings"][0]["runs"].pop("patched")
    assert "UNPROVEN_FIX" in codes(check_receipts(r, TARGET))

def test_unfixed_finding_with_null_fix_diff_is_not_unproven_fix():
    # agent_spec.yaml instructs UNFIXED findings to set "fix_diff": null.
    # str(None) == "None" is truthy, so this must not be read as a present diff.
    r = copy.deepcopy(RECEIPTS); r["findings"][0]["verdict"] = "UNFIXED"
    r["findings"][0]["fix_diff"] = None
    assert "UNPROVEN_FIX" not in codes(check_receipts(r, TARGET))

def test_fabricated_evidence_without_real_output_is_denied():
    r = copy.deepcopy(RECEIPTS); r["findings"][0]["runs"]["head"] = {"cmd": "pytest", "exit_code": 1, "outcome": "fail", "tail": "nope"}
    assert "NO_OUTPUT" in codes(check_receipts(r, TARGET))

def test_contradictory_outcome_and_exit_code_are_denied():
    r = copy.deepcopy(RECEIPTS); r["findings"][0]["runs"]["head"] = {"cmd": "python -m pytest t.py -q", "exit_code": 0, "outcome": "fail", "tail": "E   AssertionError: assert 0.0 == 12.5\n1 failed in 0.11s"}
    assert "NO_RUN_OUTCOME" in codes(check_receipts(r, TARGET))

def test_missing_exit_code_is_denied():
    r = copy.deepcopy(RECEIPTS); r["findings"][0]["runs"]["head"] = {"cmd": "python -m pytest t.py -q", "outcome": "fail", "tail": "E   AssertionError: assert 0.0 == 12.5\n1 failed in 0.11s"}
    assert "NO_RUN_OUTCOME" in codes(check_receipts(r, TARGET))

def test_refuted_findings_may_not_be_reported():
    r = copy.deepcopy(RECEIPTS); r["findings"][0]["verdict"] = "REFUTED"
    assert "REFUTED_REPORTED" in codes(check_receipts(r, TARGET))

def test_counts_must_match_the_report():
    r = copy.deepcopy(RECEIPTS); r["counts"]["confirmed"] = 4
    assert "COUNT_MISMATCH" in codes(check_receipts(r, TARGET))

def test_duplicate_finding_ids_are_denied():
    r = copy.deepcopy(RECEIPTS); r["findings"].append(copy.deepcopy(r["findings"][0]))
    r["counts"]["confirmed"] = 2; r["counts"]["raised"] = 4
    assert "DUPLICATE_ID" in codes(check_receipts(r, TARGET))

def test_write_to_another_repo_is_fatal():
    tool, args = payload(RECEIPTS, owner="someone-else")
    violations = check_payload(tool, args, TARGET)
    assert "WRONG_REPO" in codes(violations) and policy.has_fatal(violations)

def test_merge_tool_is_refused_outright():
    tool, args = payload(RECEIPTS, tool="merge_pull_request")
    violations = check_payload(tool, args, TARGET)
    assert codes(violations) == {"OUT_OF_SCOPE_TOOL"} and policy.has_fatal(violations)

def test_credential_in_body_is_fatal():
    tool, args = payload(RECEIPTS); args["body"] += "\ntoken: ghp_abcdefghijklmnopqrstuvwxyz0123\n"
    assert "SECRET_IN_BODY" in codes(check_payload(tool, args, TARGET))


def test_receipts_scope_mismatch_is_fatal():
    r = copy.deepcopy(RECEIPTS); r["repo"] = "other/repo"
    assert "WRONG_REPO" in codes(check_receipts(r, TARGET))

def test_missing_receipts_scope_is_fatal():
    r = copy.deepcopy(RECEIPTS); r.pop("repo", None); r.pop("pr", None)
    assert "SCOPE_MISSING" in codes(check_receipts(r, TARGET))

def test_incomplete_receipts_missing_base_sha_is_denied():
    r = copy.deepcopy(RECEIPTS); r.pop("base_sha")
    assert "MISSING_FIELD" in codes(check_receipts(r, TARGET))

def test_finding_missing_line_is_denied():
    r = copy.deepcopy(RECEIPTS); r["findings"][0].pop("line")
    assert "MISSING_FIELD" in codes(check_receipts(r, TARGET))

def test_trailing_content_after_receipts_block_is_denied():
    tool, args = payload(RECEIPTS); args["body"] += "\nExtra note after the block."
    assert "NO_RECEIPTS" in codes(check_payload(tool, args, TARGET))

def test_multiple_receipts_blocks_are_denied():
    tool, args = payload(RECEIPTS); args["body"] += "\n```receipts\n{\"bad\": true}\n```"
    assert "NO_RECEIPTS" in codes(check_payload(tool, args, TARGET))

def test_missing_owner_and_repo_in_payload_is_fatal():
    tool, args = payload(RECEIPTS); args.pop("owner", None); args.pop("repo", None)
    assert "SCOPE_MISSING" in codes(check_payload(tool, args, TARGET))

def test_missing_issue_number_in_payload_is_fatal():
    tool, args = payload(RECEIPTS); args.pop("issue_number")
    assert "SCOPE_MISSING" in codes(check_payload(tool, args, TARGET))
