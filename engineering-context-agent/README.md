# Engineering Context Agent

Fetches each engineer's open PRs from GitHub, MRs and pipeline status from GitLab, and in-progress Jira issues — then posts a personalised daily standup digest to their Slack DM, sent as them, not as a bot.

Built with [Scalekit Agent Auth](https://scalekit.com) — all OAuth across four systems goes through `connect.execute_tool()`. No PATs. No service accounts. No manual token refresh code.

```
Every morning → GitHub: open PRs authored by or assigned to the engineer
             → GitLab: open MRs + latest pipeline status per branch
             → Jira:   in-progress issues (assignee = currentUser())
             → LLM:    synthesise standup digest
             → Slack:  post to engineer's DM as them
```

A companion blog post walks through the architecture end-to-end: [Engineering Standup Agent: GitHub + GitLab + Jira + Slack with Per-Engineer Delegated Identity](./BLOG.md)

---

## Why per-engineer identity matters

The naive approach — a shared service account with PATs for each system — breaks at team scale:

- `assignee = currentUser()` in Jira JQL resolves to the bot, not the engineer
- GitHub PR filtering by author requires knowing each engineer's username in a separate lookup
- GitLab MR `assignee_id` filter requires a username-to-ID mapping you have to maintain
- Token expiry with no refresh logic means the bot posts nothing on Monday mornings

With Scalekit Agent Auth, each engineer authenticates once per connector. Every subsequent tool call carries their own identity — so `currentUser()` works, author filtering works, and you never maintain a cross-system identity mapping.

---

## Prerequisites

- [Scalekit account](https://scalekit.com) — free tier works
- GitHub account with open PRs
- GitLab account with open MRs
- Jira (Atlassian Cloud) account with in-progress issues
- Slack workspace
- Python 3.11+

---

## Setup

### 1. Set up Scalekit connectors

Go to **app.scalekit.com → Agent Auth → Connections** and create four connectors:

| Connection name | Service | Required scopes |
|---|---|---|
| `github` | GitHub | `repo`, `read:user` |
| `gitlab` | GitLab | `read_api` |
| `jira` | Jira | `read:jira-work`, `read:jira-user` |
| `slack` | Slack | `chat:write`, `im:write` |

> Use `read_api` for GitLab — not `api`. This agent only reads data; `read_api` is the correct least-privilege scope for a read-only digest.

Copy your API credentials from **Settings → API Credentials**.

> **Connector name suffix:** Scalekit may append a random suffix when you create a connector (e.g., `slack-sKfekCVz`). Copy the exact name from the dashboard and set it in `.env`. A mismatch causes `execute_tool()` calls to fail.

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

```bash
SCALEKIT_ENV_URL=https://your-env.scalekit.dev
SCALEKIT_CLIENT_ID=skc_xxxxxxxxxxxx
SCALEKIT_CLIENT_SECRET=your_secret_here

GITHUB_CONNECTOR=github
GITLAB_CONNECTOR=gitlab
JIRA_CONNECTOR=jira
SLACK_CONNECTOR=slack

ENGINEER_ID=eng_alice_123
ENGINEER_NAME=Alice
GITHUB_USERNAME=alice
GITHUB_REPOS=acme-corp/api-gateway,acme-corp/auth-service
GITHUB_ORG=acme-corp
GITLAB_PROJECT_PATH=acme-corp%2Fpayment-service   # URL-encode the slash
GITLAB_USER_ID=12345678                            # GitLab numeric user ID
SLACK_USER_ID=U0XXXXXXXXX                          # Slack member ID
```

**Finding your GitLab user ID:** Go to your GitLab profile → Edit profile → scroll to the bottom → User ID.

**Finding your Slack member ID:** Open Slack → click your profile picture → three-dot menu → Copy member ID.

**URL-encoding GitLab project paths:** Replace `/` with `%2F`. `acme-corp/payment-service` becomes `acme-corp%2Fpayment-service`.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

> **Python version note:** Python 3.11+ is required. If you're on 3.9 or 3.10, some pinned transitive dependencies from `scalekit-sdk-python` may conflict. Use `pyenv` or `conda` to set up a 3.11 environment: `pyenv install 3.11 && pyenv local 3.11`.

### 4. Run

```bash
python run_flow.py
```

The first run checks auth for each connector per engineer. If any are not yet authorized, a magic link is printed — open it, complete the OAuth flow in your browser, press Enter. Every subsequent run goes straight through.

`run_flow.py` is the entire pipeline — all five steps in one file.

---

## Multi-engineer mode

To run for multiple engineers at once, set `ENGINEERS` in `.env` as a JSON array:

```bash
ENGINEERS=[
  {
    "id": "eng_alice_123",
    "name": "Alice",
    "github_username": "alice",
    "github_repos": ["acme-corp/api-gateway", "acme-corp/auth-service"],
    "github_org": "acme-corp",
    "gitlab_project_path": "acme-corp%2Fpayment-service",
    "gitlab_user_id": "12345678",
    "slack_user_id": "U0XXXXXXXXX"
  },
  {
    "id": "eng_bob_456",
    "name": "Bob",
    "github_username": "bob",
    "github_repos": ["acme-corp/frontend"],
    "github_org": "acme-corp",
    "gitlab_project_path": "acme-corp%2Ffrontend",
    "gitlab_user_id": "87654321",
    "slack_user_id": "U1YYYYYYYYY"
  }
]
```

Each engineer's four connectors are authorized independently. When you add a new engineer, the agent prints their four magic links on the first run — they can complete auth from any browser.

---

## LLM digest (optional)

If `OPENROUTER_API_KEY` is set in `.env`, the agent uses an LLM to write each engineer's digest. If the key is not set — or the LLM call fails — it falls back to a structured rule-based formatter automatically.

```bash
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-3-haiku   # default
```

---

## Running on a schedule

**Cron — recommended for production:**

```cron
0 9 * * 1-5  cd /path/to/engineering-context-agent && python run_flow.py >> logs/standup.log 2>&1
```

This runs at 9am every weekday. Scalekit refreshes all OAuth tokens before the tool calls fire — the agent never catches a 401.

**Note on timezones:** If your team spans timezones, run the cron at 9am per timezone, or pass the engineer's timezone into the digest prompt.

---

## How it works

```
Step 0 — Auth check
  For each engineer, Scalekit verifies all four connectors are ACTIVE.
  Prints a magic link for any that need first-time authorization.

Step 1 — GitHub PRs
  Calls github_pull_requests_list for all open PRs per repo, then filters
  locally by user.login and assignee.login to get the engineer's own PRs.
  Loops across all configured repos.

Step 2 — GitLab MRs + pipelines
  Calls gitlab_merge_requests_list with assignee_id and state=opened.
  For each open MR, calls gitlab_pipelines_list with ref=source_branch, per_page=1
  to get the most recent pipeline run status.

Step 3 — Jira
  Calls jira_issues_search with JQL: assignee = currentUser() AND status IN (...)
  currentUser() resolves to *this engineer* because the token is theirs.
  Scalekit resolves the Jira cloud ID automatically — no hardcoded URL construction.

Step 4 — Digest
  LLM (or rule-based fallback) formats all three data sources into a
  standup-format digest: yesterday / today / blockers.

Step 5 — Slack
  Posts digest to the engineer's Slack DM via slack_send_message.
  Message comes from their own account, not a bot.
```

---

## Project structure

```
├── run_flow.py        # main pipeline — all five steps in one file
├── BLOG.md            # companion blog post
├── .env.example       # environment template
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Common issues

**`execute_tool()` fails with a connector error**
Check that the connector name in `.env` matches exactly what's in the Scalekit dashboard (including any random suffix).

**Jira returns no issues**
Confirm the engineer's Jira account has in-progress issues assigned to them. The `currentUser()` JQL function only returns issues assigned to the token owner — if the token isn't fully authorized, it may return 0 results silently.

**GitLab pipeline status is `unknown`**
The project may not have CI/CD configured, or the MR has no recent pipeline run. This is expected for MRs created before a pipeline was attached.

**Python version errors**
Use Python 3.11+. `scalekit-sdk-python` uses type annotations (`str | None`, `list[dict]`) that require 3.10 at minimum, and some transitive dependencies require 3.11.
