# Receipts — an evidence-first PR review agent on TrueForge

Most AI code review is unfalsifiable. It says "this may cause a race condition"
and leaves a human to find out. Receipts is a review agent with one rule:

> **A finding is not reported unless a test written and run in a sandbox reproduced it.**

It reads a GitHub pull request, forms hypotheses about defects, proves each one
with three sandboxed runs — **base / head / patched** — pauses for human
approval through a deterministic policy gate, then posts the evidence-backed
findings as a GitHub comment. Every hypothesis is proven, refuted, or explicitly
labelled unverified. Refuted findings are deleted and counted, so you can see
how much noise was suppressed. Nothing reaches GitHub until a human approves
the exact payload. No finding is reported unless reproduced by a failing test
in the sandbox.

## How it uses TrueForge

| Capability | How Receipts uses it |
|---|---|
| MCP tools | GitHub connector reads the PR, its diff, and file contents. DeepWiki for unfamiliar dependencies. |
| Sandbox | Every reproduction runs in an isolated sandbox — the only place agent-written code executes. Each hypothesis gets a three-run differential proof (base/head/patched). |
| Approval gate | `require_approval_for_tools` pauses before any GitHub write. Payload shown in full, policy-checked, before you decide. |
| Subagents | One proof subagent per hypothesis, run sequentially to stay within rate limits. |
| Session continuity | Receipts are persisted to a `runs/` artifact with the session id; `--verify` resumes that session to re-check findings against a new head. |

TrueForge runs the loop. This repo contributes an agent spec, a driver, a
policy, and the receipts store — ≈1,250 lines of Python, none of it
re-implementing a harness.

## Run it

**Prerequisites:** Node 22+, Python 3.10+, a GitHub fine-grained PAT, an
OpenAI-compatible model key, and a Daytona API key (free tier) for the sandbox.

1. **Start the harness.**
```bash
npx @truefoundry/trueforge   # opens http://localhost:8790
```

2. **Configure in the UI** (credentials stay here, never in this repo):
   - Settings → Models → Add custom provider: your base URL and key. Copy the
     model FQN.
   - Settings → Connectors → github: header auth, `Authorization: Bearer <PAT>`.
     Fine-grained, Pull requests and Issues read+write.
   - Settings → Sandbox providers → Daytona: your API key.

3. **Configure this repo.**
```bash
cp .env.example .env   # set TRUEFORGE_MODEL to the FQN from step 2
pip install -r requirements.txt
```

4. **Register the agent and check the safety policy.**
```bash
python -m agent.provision
python -m agent.main --show-gate
```

5. **Review a PR.**
```bash
python -m agent.main --repo OWNER/REPO --pr NUMBER
```

You will see connectors initialise, a sandbox provision, reproductions run, then
the run **stop** with the full comment payload on screen. Type `y` to post or
`n` to deny.

## The review target

PR #6 ([`demo/buggy-metrics-pr`](https://github.com/Sreekarji/trueforge-review-agent/pull/6))
is deliberately buggy and deliberately unmerged. Its existing tests all pass, so
the agent cannot cheat — it has to write new ones. There are three real defects
and at least one plausible-but-wrong hypothesis, making the refutation count
meaningful.

## Safety model

- **The gate is server-side.** `require_approval_for_tools` is enforced by the
  harness, not by this client. A bug in `main.py` cannot post without approval.
- **Least privilege.** Reads are broad; every write tool is named in the gate.
- **Credentials never enter the sandbox.** Model and MCP credentials stay in the
  harness.
- **Everything is logged.** Each run writes `runs/<timestamp>-<session>.jsonl`,
  redacted.
- **Auto-approve is deliberately awkward.** Requires both `--auto-approve` and
  `RECEIPTS_ALLOW_AUTO_APPROVE=1`.

## Qodo Code Review Evidence

Every PR in this repo (#9–#15) went through Qodo's automated review before
merge, and every action-required finding was fixed or addressed before the
merge commit. The representative trail is
[PR #10 — the policy gate](https://github.com/Sreekarji/trueforge-review-agent/pull/10):
Qodo surfaced six bugs spanning correctness, security, and reliability —
including contradictory run evidence (exit_code/outcome mismatch) passing
validation, receipts scope remaining unbound to the review target, the approval
gate crashing instead of pausing, and an ambiguous receipts block being
accepted. All six were resolved before the merge.

## Development

Tests: `python -m pytest -q` (55 passing).