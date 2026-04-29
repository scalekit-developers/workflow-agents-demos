"""
Engineering Context Agent: GitHub PRs + GitLab Pipeline Status + Jira Issues → Slack DM

For each engineer, the agent:
  1. Fetches their open GitHub PRs (authored or assigned)
  2. Fetches their open GitLab MRs and the latest pipeline status per active branch
  3. Queries Jira with JQL using assignee = currentUser() — works because each
     tool call carries that engineer's own Atlassian OAuth token, not a service account
  4. Synthesises a structured standup digest with an LLM (or rule-based fallback)
  5. Posts the digest to the engineer's Slack DM as them, not as a bot

Scalekit Agent Auth handles OAuth for all four connectors per engineer —
token storage, refresh, and delegated identity all go through connect.execute_tool().
No PATs. No service accounts. No manual refresh logic.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py          # run for all configured engineers
"""

import os
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import scalekit.client

load_dotenv()

# ── Scalekit client ───────────────────────────────────────────────────────────
sk = scalekit.client.ScalekitClient(
    client_id=os.environ["SCALEKIT_CLIENT_ID"],
    client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
    env_url=os.environ["SCALEKIT_ENV_URL"],
)
connect = sk.connect

GITHUB_CONNECTOR = os.environ.get("GITHUB_CONNECTOR", "github")
GITLAB_CONNECTOR = os.environ.get("GITLAB_CONNECTOR", "gitlab")
JIRA_CONNECTOR   = os.environ.get("JIRA_CONNECTOR",   "jira")
SLACK_CONNECTOR  = os.environ.get("SLACK_CONNECTOR",  "slack")

ALL_CONNECTORS = [GITHUB_CONNECTOR, GITLAB_CONNECTOR, JIRA_CONNECTOR, SLACK_CONNECTOR]
REQUIRE_NON_EMPTY_CONNECTOR_DATA = os.environ.get(
    "REQUIRE_NON_EMPTY_CONNECTOR_DATA", "true"
).strip().lower() in {"1", "true", "yes", "on"}


# ── Engineer config ────────────────────────────────────────────────────────────
def _load_engineers() -> list[dict]:
    raw = os.environ.get("ENGINEERS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                print("⚠️  ENGINEERS must be a JSON array of engineer objects")
                return []
            return parsed
        except json.JSONDecodeError as e:
            print(f"⚠️  Could not parse ENGINEERS JSON: {e}")
            return []

    # Single-engineer mode from individual env vars
    # No hardcoded demo defaults: values must come from env.
    engineer_id = os.environ.get("ENGINEER_ID", "").strip()
    if not engineer_id:
        return []

    eng = {
        "id":                  engineer_id,
        "name":                os.environ.get("ENGINEER_NAME", "").strip(),
        "github_username":     os.environ.get("GITHUB_USERNAME", "").strip(),
        "github_repos":        [
            r.strip()
            for r in os.environ.get("GITHUB_REPOS", "").split(",")
            if r.strip()
        ],
        "github_org":          os.environ.get("GITHUB_ORG", "").strip(),
        "gitlab_project_path": os.environ.get("GITLAB_PROJECT_PATH", "").strip(),
        "gitlab_user_id":      os.environ.get("GITLAB_USER_ID", "").strip(),
        "slack_user_id":       os.environ.get("SLACK_USER_ID", "").strip(),
    }
    return [eng]


def validate_engineer_config(eng: dict) -> list[str]:
    errors: list[str] = []

    if not eng.get("id"):
        errors.append("id is required")
    if not eng.get("name"):
        errors.append("name is required")
    if not eng.get("github_username"):
        errors.append("github_username is required")

    repos = eng.get("github_repos") or []
    if not repos and not eng.get("github_org"):
        errors.append("either github_repos or github_org is required")

    if not eng.get("gitlab_project_path"):
        errors.append("gitlab_project_path is required")
    if not eng.get("slack_user_id"):
        errors.append("slack_user_id is required")

    return errors


ENGINEERS = _load_engineers()


# ── Auth helpers ───────────────────────────────────────────────────────────────
def ensure_authorized(connector: str, identifier: str) -> None:
    """
    Check connector status for this engineer.
    If not yet authorized, print a magic link and wait for confirmation.

    Each engineer authenticates directly with each provider — their token
    is stored in Scalekit's vault, never in this process.
    """
    resp = connect.get_or_create_connected_account(
        connection_name=connector, identifier=identifier
    )
    if resp.connected_account.status != "ACTIVE":
        link = connect.get_authorization_link(
            connection_name=connector, identifier=identifier
        ).link
        print(f"\n  [{connector}] Not authorized for {identifier}. Open:\n    {link}\n")
        input("  Press Enter after authorizing in the browser...")
    else:
        print(f"  ✓ {connector} ({identifier}) — ACTIVE")


# ── Tool execution ─────────────────────────────────────────────────────────────
def tool(connector: str, tool_name: str, identifier: str, **kwargs) -> dict:
    """
    Execute a Scalekit tool on behalf of a specific engineer.

    identifier=engineer_id means Scalekit uses *that engineer's* OAuth token,
    not a shared service account. This is what makes currentUser() in JQL work,
    PR filtering by author work, and MR assignee filtering work.
    """
    result = connect.execute_tool(
        tool_name=tool_name,
        identifier=identifier,
        tool_input=kwargs,
        connection_name=connector,
    )
    return result.data or {}


# ── Step 1: GitHub PRs ─────────────────────────────────────────────────────────
def fetch_github_prs(eng: dict) -> list[dict]:
    """
    Fetch open PRs authored by or assigned to this engineer across their repos.

    GitHub's `head` parameter filters by branch name, not by author — so we
    fetch all open PRs per repo and filter locally by user.login / assignee.login.
    """
    identifier = eng["id"]
    username   = eng["github_username"]
    repos      = eng.get("github_repos") or []

    if not repos and eng.get("github_org"):
        raw = tool(
            GITHUB_CONNECTOR, "github_user_repos_list",
            identifier, per_page=50, type="all"
        )
        repos = [
            f"{eng['github_org']}/{r['name']}"
            for r in (raw.get("repos") or raw.get("items") or [])
        ]

    prs: list[dict] = []
    for repo_path in repos:
        try:
            owner, repo = repo_path.split("/", 1)
        except ValueError:
            print(f"    ⚠️  Skipping invalid repo path: {repo_path}")
            continue

        raw = tool(
            GITHUB_CONNECTOR, "github_pull_requests_list",
            identifier,
            owner=owner,
            repo=repo,
            state="open",
        )
        all_prs = raw.get("pull_requests") or raw.get("items") or raw.get("data") or raw.get("array") or []
        batch = [
            pr for pr in all_prs
            if (pr.get("user") or {}).get("login", "").lower() == username.lower()
            or (pr.get("assignee") or {}).get("login", "").lower() == username.lower()
        ]

        for pr in batch:
            pr["_repo"] = repo_path
        prs.extend(batch)

    return prs


# ── Step 2: GitLab MRs + pipeline status ──────────────────────────────────────
def fetch_gitlab_mrs_and_pipelines(eng: dict) -> list[dict]:
    """
    Fetch open MRs assigned to this engineer and the latest pipeline status
    for each MR's source branch.

    GitLab has 110 tools in Scalekit's catalogue — one of the richest connector
    surfaces available. We use four of them here: gitlab_merge_requests_list,
    gitlab_pipelines_list, and optionally gitlab_pipeline_get and gitlab_job_log_get.
    """
    identifier   = eng["id"]
    project_path = eng.get("gitlab_project_path", "")
    gitlab_uid   = eng.get("gitlab_user_id", "")

    if not project_path:
        return []

    # Fetch open MRs assigned to this engineer
    mr_params: dict = {"id": project_path, "state": "opened"}
    if gitlab_uid:
        mr_params["assignee_id"] = gitlab_uid

    raw = tool(GITLAB_CONNECTOR, "gitlab_merge_requests_list", identifier, **mr_params)
    mrs = raw.get("merge_requests") or raw.get("data") or raw.get("items") or []
    if isinstance(mrs, dict):
        mrs = list(mrs.values())

    enriched: list[dict] = []
    for mr in mrs:
        source_branch = mr.get("source_branch") or ""
        project_id    = mr.get("project_id") or project_path

        pipeline_status = "unknown"
        pipeline_url    = ""

        if source_branch:
            # Get the latest pipeline for this branch (per_page=1 is intentional —
            # we only need the most recent run)
            p_raw = tool(
                GITLAB_CONNECTOR, "gitlab_pipelines_list",
                identifier,
                id=project_id,
                ref=source_branch,
                per_page=1,
            )
            pipelines = p_raw.get("pipelines") or p_raw.get("data") or p_raw.get("items") or []
            if isinstance(pipelines, dict):
                pipelines = list(pipelines.values())
            if pipelines:
                latest = pipelines[0]
                pipeline_status = latest.get("status", "unknown")
                pipeline_url    = latest.get("web_url") or latest.get("url") or ""

        enriched.append({
            **mr,
            "pipeline_status": pipeline_status,
            "pipeline_url":    pipeline_url,
        })

    return enriched


# ── Step 3: Jira issues ────────────────────────────────────────────────────────
def fetch_jira_issues(eng: dict) -> list[dict]:
    """
    Query Jira for issues assigned to this engineer using JQL.

    The critical detail: assignee = currentUser() is a Jira JQL function that
    resolves to the authenticated user. It only works correctly when the OAuth
    token belongs to the engineer — not a service account.

    With a service account, currentUser() would return the bot, not the engineer,
    and you'd need to maintain a mapping of engineer IDs across three systems.

    Scalekit also handles Jira's cloud ID resolution automatically. The agent
    never constructs https://api.atlassian.com/ex/jira/{cloudId}/... manually —
    Scalekit resolves {{cloud_id}} from the connected account configuration.
    """
    identifier = eng["id"]

    raw = tool(
        JIRA_CONNECTOR, "jira_issues_search",
        identifier,
        jql="assignee = currentUser() AND status NOT IN ('Done', 'Closed', 'Resolved', 'Cancelled') ORDER BY updated DESC",
        maxResults=10,
        fields="summary,status,priority,issuetype,updated",
    )

    issues = (
        raw.get("issues")
        or raw.get("data")
        or raw.get("items")
        or []
    )
    if isinstance(issues, dict):
        issues = list(issues.values())

    return issues


# ── Step 4: Digest synthesis ───────────────────────────────────────────────────
def _build_digest_with_llm(eng: dict, prs: list, mrs: list, issues: list) -> str:
    """Use OpenRouter to write the standup digest from raw data."""
    import requests as http

    def pr_summary(pr: dict) -> dict:
        return {
            "title":  pr.get("title", ""),
            "state":  pr.get("state", "open"),
            "repo":   pr.get("_repo", ""),
            "url":    pr.get("html_url") or pr.get("url") or "",
            "number": pr.get("number") or pr.get("iid"),
            "draft":  pr.get("draft", False),
            "days_open": _days_open(pr.get("created_at")),
        }

    def mr_summary(mr: dict) -> dict:
        return {
            "title":           mr.get("title", ""),
            "source_branch":   mr.get("source_branch", ""),
            "pipeline_status": mr.get("pipeline_status", "unknown"),
            "url":             mr.get("web_url") or mr.get("url") or "",
            "iid":             mr.get("iid"),
            "days_open":       _days_open(mr.get("created_at")),
        }

    def issue_summary(issue: dict) -> dict:
        fields = issue.get("fields") or {}
        return {
            "key":      issue.get("key", ""),
            "summary":  fields.get("summary", issue.get("summary", "")),
            "status":   (fields.get("status") or {}).get("name") or fields.get("status", ""),
            "priority": (fields.get("priority") or {}).get("name") or fields.get("priority", ""),
            "updated":  fields.get("updated") or issue.get("updated", ""),
        }

    prompt = f"""You are a helpful engineering standup assistant.

Write a concise daily standup digest for {eng['name']}.
IMPORTANT: Only use data from the JSON provided below. Do NOT invent, guess, or add any PRs, issues, or MRs that are not in the data.
Keep it under 180 words. Reference real PR/MR titles, issue keys, and pipeline states exactly as they appear in the data.
Flag anything that needs attention: failed pipelines, draft PRs, stale items (>3 days old), P1 issues.

Format exactly as:
• Yesterday / merged or shipped
• Today / what I'm focused on
• Blockers / anything stuck or failing (use "None" if none)

GitHub PRs:
{json.dumps([pr_summary(p) for p in prs], indent=2)}

GitLab MRs and Pipeline Status:
{json.dumps([mr_summary(m) for m in mrs], indent=2)}

Jira Issues In Progress:
{json.dumps([issue_summary(i) for i in issues], indent=2)}
"""

    resp = http.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={
            "model": os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3-haiku"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _days_open(created_at: str | None) -> int:
    if not created_at:
        return 0
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0


def _pipeline_icon(status: str) -> str:
    return {"success": "✅", "failed": "❌", "running": "🔄", "pending": "⏳"}.get(
        status.lower(), "⬜"
    )


def _build_digest_rule_based(eng: dict, prs: list, mrs: list, issues: list) -> str:
    """
    Format the standup digest without an LLM.
    Falls back here when OPENROUTER_API_KEY is not set or the LLM call fails.
    """
    lines = [f"*{eng['name']}'s Daily Standup — {datetime.now(timezone.utc).strftime('%a %d %b')}*\n"]

    # PRs
    if prs:
        lines.append("*GitHub PRs*")
        for pr in prs:
            age   = _days_open(pr.get("created_at"))
            label = pr.get("title", "")[:60]
            repo  = pr.get("_repo", "")
            flags = []
            if pr.get("draft"):
                flags.append("draft")
            if age > 3:
                flags.append(f"{age}d open")
            flag_str = f"  ⚠️ {', '.join(flags)}" if flags else ""
            url  = pr.get("html_url") or pr.get("url") or ""
            iid  = pr.get("number") or pr.get("iid") or ""
            lines.append(f"  • <{url}|#{iid}> {label} `{repo}`{flag_str}")
    else:
        lines.append("*GitHub PRs*\n  No open PRs")

    lines.append("")

    # MRs
    if mrs:
        lines.append("*GitLab MRs*")
        for mr in mrs:
            age    = _days_open(mr.get("created_at"))
            label  = mr.get("title", "")[:60]
            branch = mr.get("source_branch", "")
            status = mr.get("pipeline_status", "unknown")
            icon   = _pipeline_icon(status)
            url    = mr.get("web_url") or mr.get("url") or ""
            iid    = mr.get("iid") or ""
            flags  = []
            if age > 3:
                flags.append(f"{age}d open")
            flag_str = f"  ⚠️ {', '.join(flags)}" if flags else ""
            lines.append(f"  • <{url}|!{iid}> {label}  {icon} `{branch}`{flag_str}")
    else:
        lines.append("*GitLab MRs*\n  No open MRs")

    lines.append("")

    # Jira
    if issues:
        lines.append("*Jira In Progress*")
        for issue in issues:
            fields   = issue.get("fields") or {}
            key      = issue.get("key", "")
            summary  = fields.get("summary") or issue.get("summary", "")
            status   = (fields.get("status") or {}).get("name") or fields.get("status", "")
            priority = (fields.get("priority") or {}).get("name") or ""
            p_flag   = " 🔴" if priority and "highest" in priority.lower() else ""
            lines.append(f"  • *{key}* {summary[:60]}  [{status}]{p_flag}")
    else:
        lines.append("*Jira In Progress*\n  No in-progress issues")

    lines.append("")
    lines.append("_Via Engineering Context Agent · Scalekit Agent Auth_")
    return "\n".join(lines)


def build_digest(eng: dict, prs: list, mrs: list, issues: list) -> str:
    # Skip LLM entirely if there's no real data — prevents hallucinated content
    if not prs and not mrs and not issues:
        print("    (no data — skipping LLM, using rule-based)")
        return _build_digest_rule_based(eng, prs, mrs, issues)

    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            result = _build_digest_with_llm(eng, prs, mrs, issues)
            print("    (LLM digest ✓)")
            return result
        except Exception as e:
            print(f"    ⚠️  LLM failed ({e.__class__.__name__}: {e}) — using rule-based formatter")
    return _build_digest_rule_based(eng, prs, mrs, issues)


# ── Step 5: Post to Slack ──────────────────────────────────────────────────────
def post_digest_to_slack(eng: dict, digest: str) -> None:
    """
    Post the standup digest to the engineer's Slack DM.

    The message comes from the engineer's own Slack account — not a bot.
    Slack treats it as their content: they can reply to themselves, react,
    pin it, or share it in a thread. Bot DMs get ignored; personal DMs get read.
    """
    identifier    = eng["id"]
    slack_user_id = eng.get("slack_user_id", "")

    if not slack_user_id:
        print(f"    ⚠️  No slack_user_id configured for {eng['name']} — skipping Slack post")
        return

    try:
        result = connect.execute_tool(
            tool_name="slack_send_message",
            identifier=identifier,
            tool_input={"channel": slack_user_id, "text": digest, "mrkdwn": True},
            connection_name=SLACK_CONNECTOR,
        )
        ts = (result.data or {}).get("timestamp") or (result.data or {}).get("ts") or ""
        print(f"    ✓ Posted to {slack_user_id} (ts={ts})")
    except Exception as slack_err:
        err_str = str(slack_err)
        if "token_expired" in err_str or "INVALID_ARGUMENT" in err_str:
            print(f"    ⚠️  Slack token expired — re-authorize:")
            try:
                link = connect.get_authorization_link(
                    connection_name=SLACK_CONNECTOR, identifier=identifier
                ).link
                print(f"    {link}")
            except Exception:
                print(f"    Go to app.scalekit.com → Agent Auth → Connections → {SLACK_CONNECTOR} → re-authorize")
        else:
            print(f"    ✗ Slack post failed: {slack_err.__class__.__name__}: {err_str[:120]}")


# ── Main ───────────────────────────────────────────────────────────────────────
if not ENGINEERS:
    print("No engineers configured. Set ENGINEERS JSON or ENGINEER_* vars in .env")
    sys.exit(1)

config_errors: list[str] = []
for idx, eng in enumerate(ENGINEERS, start=1):
    for err in validate_engineer_config(eng):
        config_errors.append(f"engineer[{idx}] ({eng.get('id', '?')}): {err}")

if config_errors:
    print("\nConfiguration errors detected:")
    for err in config_errors:
        print(f"  - {err}")
    sys.exit(1)

today_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
print(f"\n── Engineering Context Agent — {today_label} ──")
print(f"   Engineers: {len(ENGINEERS)}")

empty_connector_results: list[str] = []

for eng in ENGINEERS:
    eng_name = eng.get("name", eng.get("id", "?"))
    eng_id   = eng["id"]
    print(f"\n{'─'*60}")
    print(f"  Engineer: {eng_name}  (id={eng_id})")

    # ── Step 0: Auth check ─────────────────────────────────────────────────────
    print("\n  Step 0: Checking connector auth")
    for connector in ALL_CONNECTORS:
        ensure_authorized(connector, eng_id)

    # ── Step 1: GitHub PRs ─────────────────────────────────────────────────────
    print("\n  Step 1: Fetching GitHub PRs")
    repos = eng.get("github_repos") or []
    github_prs = fetch_github_prs(eng)
    print(f"    Found {len(github_prs)} open PR(s) across {len(repos)} repo(s)")
    for pr in github_prs:
        age = _days_open(pr.get("created_at"))
        stale_flag = f"  ⚠️ {age}d old" if age > 3 else ""
        print(f"      #{pr.get('number') or pr.get('iid')} {pr.get('title','')[:55]}{stale_flag}")
    if not github_prs:
        empty_connector_results.append(f"{eng_name}: GitHub returned 0 open PRs")

    # ── Step 2: GitLab MRs + pipelines ────────────────────────────────────────
    print("\n  Step 2: Fetching GitLab MRs + pipeline status")
    gitlab_mrs = fetch_gitlab_mrs_and_pipelines(eng)
    print(f"    Found {len(gitlab_mrs)} open MR(s)")
    for mr in gitlab_mrs:
        icon = _pipeline_icon(mr.get("pipeline_status", ""))
        print(
            f"      !{mr.get('iid')} {mr.get('title','')[:50]}  "
            f"pipeline: {icon} {mr.get('pipeline_status','unknown')}"
        )
    if not gitlab_mrs:
        empty_connector_results.append(f"{eng_name}: GitLab returned 0 open MRs")

    # ── Step 3: Jira issues ────────────────────────────────────────────────────
    print("\n  Step 3: Querying Jira (assignee = currentUser())")
    jira_issues = fetch_jira_issues(eng)
    print(f"    Found {len(jira_issues)} in-progress issue(s)")
    for issue in jira_issues:
        fields  = issue.get("fields") or {}
        key     = issue.get("key", "")
        summary = fields.get("summary") or issue.get("summary", "")
        status  = (fields.get("status") or {}).get("name") or ""
        print(f"      {key}: {summary[:55]}  [{status}]")
    if not jira_issues:
        empty_connector_results.append(f"{eng_name}: Jira returned 0 in-progress issues")

    # ── Step 4: Build digest ───────────────────────────────────────────────────
    print("\n  Step 4: Building standup digest")
    digest = build_digest(eng, github_prs, gitlab_mrs, jira_issues)
    print(f"\n  ── Digest preview ──\n{digest}\n  ────────────────────")

    # ── Step 5: Post to Slack ──────────────────────────────────────────────────
    print("\n  Step 5: Posting digest to Slack DM")
    post_digest_to_slack(eng, digest)

if REQUIRE_NON_EMPTY_CONNECTOR_DATA and empty_connector_results:
    print("\n✗ Connector data check failed (empty results):")
    for row in empty_connector_results:
        print(f"  - {row}")
    print("Set REQUIRE_NON_EMPTY_CONNECTOR_DATA=false to allow empty connector results.")
    sys.exit(2)

print(f"\n✓ Done. Ran for {len(ENGINEERS)} engineer(s).\n")
