# DevOps Assistant Agent (GitHub + Linear + Slack)

Automate your GitHub → Linear → Slack workflow using Scalekit tools only (no webhooks).

## What it does
- Polls GitHub for open PRs
- When a PR is labeled, creates a linked Linear issue (idempotent, one issue per PR+label)
- Sends a daily Slack digest with open PRs, reviewers needed, and stale PRs

## Quick Start

1. Install dependencies
	```bash
	python3 -m venv .venv
	source .venv/bin/activate
	pip install -r requirements.txt
	```

2. Configure environment
	```bash
	cp .env.example .env
	# Edit .env with your Scalekit, GitHub, Linear, and Slack settings
	```

3. Start the agent (polls GitHub, creates Linear issues, sends Slack digest)
	```bash
	python poller.py
	```

## Files
- `poller.py` – Main agent: GitHub PR polling, Linear issue linking, Slack digest
- `settings.py` – Environment variable loading and validation
- `sk_connectors.py` – Scalekit SDK wrapper, tool execution, idempotency store
- `requirements.txt` – Python dependencies
- `state/pr_linear_links.json` – Local state for PR→Linear mapping
- `.env.example` – Example environment config
- `.gitignore` – Ignore .env, .venv, __pycache__, and state files

## Notes
- Only `poller.py` is needed to run the agent; all other files are support/config
- Make sure your `.env` is correct and all required secrets are set
- `state/pr_linear_links.json` – local idempotency store

## Env vars (.env)
- SCALEKIT_ENV_URL, SCALEKIT_CLIENT_ID, SCALEKIT_CLIENT_SECRET
- GITHUB_REPO_OWNER, GITHUB_REPO_NAME
- SLACK_DIGEST_CHANNEL_ID (destination for daily digest)
- LINEAR_TEAM_ID (default team for new issues)
- LABEL_TO_LINEAR_TEAM (optional JSON mapping, e.g. {"bug":"team-id"})

## Notes
- Idempotency: we store PR→Linear mappings in `state/pr_linear_links.json` and skip duplicates.
- CI status is not fetched in this v1; digest shows basic PR metadata and reviewer needs.
- All third-party calls go via Scalekit Actions tools.

## Tools used (via Scalekit Actions)
- GitHub: `github_pull_requests_list`
- Linear: `linear_issue_create`
- Slack: `slack_send_message`
