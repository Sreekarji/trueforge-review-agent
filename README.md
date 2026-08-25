# Receipts — an evidence-first PR review agent on TrueForge

Most AI code review is unfalsifiable. It says "this may cause a race condition"
and leaves a human to find out. Receipts is a review agent with one rule:

> **A finding is not reported unless a test written and run in a sandbox reproduced it.**

Every hypothesis is proven, refuted, or explicitly labelled unverified. Refuted
findings are deleted and counted, so you can see how much noise was suppressed.
Nothing reaches GitHub until a human approves the exact payload.

## What the harness does

| Capability | How Receipts uses it |
|---|---|
| MCP tools | GitHub connector reads the PR, its diff, and file contents. DeepWiki for unfamiliar dependencies. |
| Sandbox | Every reproduction runs in an isolated sandbox — the only place agent-written code executes. |
| Approval gate | `require_approval_for_tools` pauses before any GitHub write. Payload shown in full before you decide. |
| Subagents | More than six hypotheses fan out to parallel proof runs. |
| Session state | Each approval round is a new turn chained to the same session. |

TrueForge runs the loop. This repo contributes an agent spec, a driver, and a policy — roughly 600 lines, none of it re-implementing a harness.

## Run it in 5 minutes

### No keys? Watch a recorded run (30 seconds)

```bash
pip install -r requirements.txt
python -m agent.main --replay runs/demo-run.jsonl
```

This re-renders a real run from its audit log: sandbox provisioning, reproductions, the approval gate, and the human decision. No server, no model, no credentials needed.

### Full run

**Prerequisites:** Node 22+, Python 3.10+, a GitHub fine-grained PAT, an OpenAI-compatible model key, and a Daytona API key (free tier) for the sandbox.

1. **Start the harness.**
```bash
   npx @truefoundry/trueforge   # opens http://localhost:8790
```

2. **Configure in the UI** (credentials stay here, never in this repo):
   - Settings → Models → Add custom provider: your base URL and key. Copy the model FQN.
   - Settings → Connectors → github: header auth, `Authorization: Bearer <your PAT>`. Fine-grained, Pull requests and Issues read+write.
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
   python -m agent.main --repo Sreekarji/trueforge-review-agent --pr 2
```

You will see connectors initialise, a sandbox provision, reproductions run, then the run **stop** with the full comment payload on screen. Type `y` to post or `n` to deny.

## The review target

PR #2 (`demo/buggy-metrics-pr`) is deliberately buggy and deliberately unmerged. Its existing tests all pass, so the agent cannot cheat — it has to write new ones. There are three real defects and at least one plausible-but-wrong hypothesis, making the refutation count meaningful.

## Safety model

- **The gate is server-side.** `require_approval_for_tools` is enforced by the harness, not by this client. A bug in `main.py` cannot post without approval.
- **Least privilege.** Reads are broad; every write tool is named in the gate.
- **Credentials never enter the sandbox.** Model and MCP credentials stay in the harness.
- **Everything is logged.** Each run writes `runs/<timestamp>-<session>.jsonl`, redacted.
- **Auto-approve is deliberately awkward.** Requires both `--auto-approve` and `RECEIPTS_ALLOW_AUTO_APPROVE=1`.

## Development

Every change went through a pull request reviewed by Qodo before merge. Tests: `python -m pytest -q`.
