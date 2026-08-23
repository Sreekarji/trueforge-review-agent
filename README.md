# TrueForge Review Agent

**AI-powered code review agent built on TrueForge for the Agent Harness Hackathon**

## What It Does

TrueForge Review Agent is an autonomous code review assistant. Point it at any GitHub repository and it will:

- 🔍 **Explore the codebase** — list repository structure, read source files, and trace the architecture of the main components.
- 🐛 **Find bugs** — detect logic errors, tensor/shape mistakes, device and gradient issues, security holes, and performance bottlenecks.
- 📊 **Produce structured reviews** — every finding is severity-ranked (Critical / High / Medium / Low) with the affected file, approximate line number, a code snippet, an explanation, and a suggested fix.
- 🧠 **Ground the review with research** — enrich findings with up-to-date documentation and web context before finalizing recommendations.
- ✅ **Respect the human-in-the-loop** — reviews are presented for approval before anything is posted back to GitHub as an issue or comment.

## How to Run with TrueForge

### Prerequisites

- Python 3.10+
- A [TrueForge](https://trueforge.example) installation (agent harness runtime)
- MCP servers configured for **GitHub**, **Exa**, and **DeepWiki** (see below)
- A GitHub Personal Access Token with `repo` and `issues:write` scopes

### Setup

```bash
# 1. Clone the repo
git clone https://github.com/Sreekarji/trueforge-review-agent.git
cd trueforge-review-agent

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
export GITHUB_TOKEN=ghp_xxx          # GitHub PAT (repo, issues:write)
export EXA_API_KEY=xxx               # Exa search API key
export DEEPWIKI_TOKEN=xxx            # DeepWiki access token

# 4. Register MCP servers in your TrueForge config (trueforge.yaml)
mcp:
  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_TOKEN}
  exa:
    command: npx
    args: ["-y", "mcp-exa"]
    env:
      EXA_API_KEY: ${EXA_API_KEY}
  deepwiki:
    command: npx
    args: ["-y", "@deepwiki/mcp-server"]
    env:
      DEEPWIKI_TOKEN: ${DEEPWIKI_TOKEN}
```

### Running the agent

```bash
# Review a repository and print the report
trueforge run agent --repo https://github.com/owner/repo

# Review only specific files
trueforge run agent --repo https://github.com/owner/repo --files "src/model.py,src/train.py"

# Generate a report and save it locally (no posting to GitHub)
trueforge run agent --repo https://github.com/owner/repo --output review.md

# Full flow: review, ask for approval, then post as a GitHub issue
trueforge run agent --repo https://github.com/owner/repo --post-issue
```

The agent always **asks for explicit approval before posting** any comments or issues to GitHub.

## MCP Tools Used

The agent uses three MCP (Model Context Protocol) servers to gather context and act on repositories:

| MCP Server | Role in the Agent |
|---|---|
| **GitHub** | Lists repository contents, reads source files, fetches metadata, and (with approval) posts issues and PR comments. The primary data source for the review. |
| **Exa** | Web search and page fetch — used to research libraries, reproduce known CVEs, check current best practices, and validate whether a flagged issue is a real-world problem. |
| **DeepWiki** | Deep documentation retrieval — used to look up framework internals (e.g., PyTorch module behavior, library APIs) so findings are grounded in official docs rather than guesswork. |

## Agent Flow

```text
User: "Review https://github.com/owner/repo"
  │
  ▼
[1] GitHub  ──► list repo structure, read main source files
  │
  ▼
[2] DeepWiki ─► fetch framework/library docs for referenced APIs
  │
  ▼
[3] Exa     ──► search for known issues / best practices / CVEs
  │
  ▼
[4] Analyze ──► bug hunting, code-quality pass, performance review
  │
  ▼
[5] Report  ──► severity-ranked findings + suggested fixes
  │
  ▼
[6] Ask user for approval to post → GitHub issue / PR comments
```

## Project Structure

```text
trueforge-review-agent/
├── agent/          # Agent logic (review pipeline)
├── tools/          # MCP client wrappers (github, exa, deepwiki)
├── config/         # Agent prompts and TrueForge config
├── tests/          # Unit tests
├── trueforge.yaml  # TrueForge + MCP server configuration
└── requirements.txt
```

## License

MIT
