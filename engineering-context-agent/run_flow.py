"""
Engineering Context Agent: GitHub PRs + GitLab Pipeline Status + Jira Issues → Slack DM

For each engineer, the agent:
  1. Fetches their open GitHub PRs (authored or assigned) — live data via Scalekit
  2. Fetches their open GitLab MRs and the latest pipeline status per active branch
  3. Queries Jira with JQL using assignee = currentUser() — works because each
     tool call carries that engineer's own Atlassian OAuth token, not a service account
  4. Synthesises a structured standup digest with an LLM from that live data —
     no static/template fallback, the run fails loudly if the LLM call fails
  5. Posts the digest to the engineer's Slack DM as them, not as a bot

Scalekit Agent Auth handles OAuth for all four connectors per engineer —
token storage, refresh, and delegated identity all go through connect.execute_tool().
No PATs. No service accounts. No manual refresh logic.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py          # run for all configured engineers
"""

import json
import logging
import sys
from datetime import datetime, timezone

import scalekit.client

import settings

settings.validate()


# ── Logging ────────────────────────────────────────────────────────────────────
ICONS = {
    "done":  "✔",  # ✔
    "warn":  "⚠",  # ⚠
    "error": "✖",  # ✖
    "info":  "ℹ",  # ℹ
    "run":   "▶",  # ▶
}


class _ColorFormatter(logging.Formatter):
    """Adds ANSI color per level and a timestamp | LEVEL | message shape."""

    COLORS = {
        logging.DEBUG:    "\033[90m",   # gray
        logging.INFO:     "\033[0m",    # default
        logging.WARNING:  "\033[93m",   # yellow
        logging.ERROR:    "\033[91m",   # red
        logging.CRITICAL: "\033[95m",   # magenta
    }
    RESET = "\033[0m"

    def __init__(self, colorize: bool = True):
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S",
        )
        self.colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not self.colorize:
            return message
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{message}{self.RESET}" if color else message


class _NoiseFilter(logging.Filter):
    """Drops noisy third-party log lines we never want to see (e.g. gRPC AFC chatter)."""

    BLOCKED_SUBSTRINGS = ("AFC is enabled",)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(s in message for s in self.BLOCKED_SUBSTRINGS)


def _setup_logging() -> logging.Logger:
    level = getattr(logging, settings.LOG_LEVEL.strip().upper(), logging.INFO)

    logger = logging.getLogger("engineering-context-agent")
    logger.setLevel(level)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        colorize = sys.stdout.isatty()
        handler.setFormatter(_ColorFormatter(colorize=colorize))
        handler.addFilter(_NoiseFilter())
        logger.addHandler(handler)

    return logger


log = _setup_logging()


# ── Scalekit client ───────────────────────────────────────────────────────────
sk = scalekit.client.ScalekitClient(
    client_id=settings.SCALEKIT_CLIENT_ID,
    client_secret=settings.SCALEKIT_CLIENT_SECRET,
    env_url=settings.SCALEKIT_ENV_URL,
)
connect = sk.connect

GITHUB_CONNECTOR = settings.GITHUB_CONNECTOR
GITLAB_CONNECTOR = settings.GITLAB_CONNECTOR
JIRA_CONNECTOR   = settings.JIRA_CONNECTOR
SLACK_CONNECTOR  = settings.SLACK_CONNECTOR

ALL_CONNECTORS = [GITHUB_CONNECTOR, GITLAB_CONNECTOR, JIRA_CONNECTOR, SLACK_CONNECTOR]


# ── Engineer config ────────────────────────────────────────────────────────────
def _load_engineers() -> list[dict]:
    raw = settings.ENGINEERS_RAW.strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                log.warning("%s  ENGINEERS must be a JSON array of engineer objects", ICONS["warn"])
                return []
            return parsed
        except json.JSONDecodeError as e:
            log.warning("%s  Could not parse ENGINEERS JSON: %s", ICONS["warn"], e)
            return []

    # Single-engineer mode from individual env vars
    # No hardcoded demo defaults: values must come from env.
    engineer_id = settings.ENGINEER_ID.strip()
    if not engineer_id:
        return []

    eng = {
        "id":                  engineer_id,
        "name":                settings.ENGINEER_NAME.strip(),
        "github_username":     settings.GITHUB_USERNAME.strip(),
        "github_repos":        [
            r.strip()
            for r in settings.GITHUB_REPOS.split(",")
            if r.strip()
        ],
        "github_org":          settings.GITHUB_ORG.strip(),
        "gitlab_project_path": settings.GITLAB_PROJECT_PATH.strip(),
        "gitlab_user_id":      settings.GITLAB_USER_ID.strip(),
        "slack_user_id":       settings.SLACK_USER_ID.strip(),
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
        log.warning("%s  [%s] Not authorized for %s. Open:\n    %s\n", ICONS["warn"], connector, identifier, link)
        input("  Press Enter after authorizing in the browser...")
    else:
        log.info("%s  %s (%s) — ACTIVE", ICONS["done"], connector, identifier)


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
            log.warning("%s    Skipping invalid repo path: %s", ICONS["warn"], repo_path)
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
        headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
        json={
            "model": settings.OPENROUTER_MODEL,
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


def build_digest(eng: dict, prs: list, mrs: list, issues: list) -> str:
    """
    Synthesise the standup digest from live PR/MR/Jira data with the LLM.

    No static template or rule-based fallback: if the LLM call fails, the
    exception propagates so the run fails loudly instead of posting
    degraded, non-LLM-formatted content.
    """
    result = _build_digest_with_llm(eng, prs, mrs, issues)
    log.info("%s    LLM digest built from live data", ICONS["done"])
    return result


# ── Step 5: Post to Slack ──────────────────────────────────────────────────────
def post_digest_to_slack(eng: dict, digest: str) -> bool:
    """
    Post the standup digest to the engineer's Slack DM.

    The message comes from the engineer's own Slack account — not a bot.
    Slack treats it as their content: they can reply to themselves, react,
    pin it, or share it in a thread. Bot DMs get ignored; personal DMs get read.
    """
    identifier    = eng["id"]
    slack_user_id = eng.get("slack_user_id", "")

    if not slack_user_id:
        log.warning("%s    No slack_user_id configured for %s — skipping Slack post", ICONS["warn"], eng["name"])
        return False

    try:
        result = connect.execute_tool(
            tool_name="slack_send_message",
            identifier=identifier,
            tool_input={"channel": slack_user_id, "text": digest, "mrkdwn": True},
            connection_name=SLACK_CONNECTOR,
        )
        ts = (result.data or {}).get("timestamp") or (result.data or {}).get("ts") or ""
        log.info("%s    Posted to %s (ts=%s)", ICONS["done"], slack_user_id, ts)
        return True
    except Exception as slack_err:
        err_str = str(slack_err)
        if "token_expired" in err_str or "INVALID_ARGUMENT" in err_str:
            log.warning("%s    Slack token expired — re-authorize:", ICONS["warn"])
            try:
                link = connect.get_authorization_link(
                    connection_name=SLACK_CONNECTOR, identifier=identifier
                ).link
                log.warning("    %s", link)
            except Exception:
                log.warning("    Go to app.scalekit.com → Agent Auth → Connections → %s → re-authorize", SLACK_CONNECTOR)
        else:
            log.error("%s    Slack post failed: %s: %s", ICONS["error"], slack_err.__class__.__name__, err_str[:120])
        return False


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> int:
    if not ENGINEERS:
        log.error("%s  No engineers configured. Set ENGINEERS JSON or ENGINEER_* vars in .env", ICONS["error"])
        return 1

    config_errors: list[str] = []
    for idx, eng in enumerate(ENGINEERS, start=1):
        for err in validate_engineer_config(eng):
            config_errors.append(f"engineer[{idx}] ({eng.get('id', '?')}): {err}")

    if config_errors:
        log.error("%s  Configuration errors detected:", ICONS["error"])
        for err in config_errors:
            log.error("  - %s", err)
        return 1

    today_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info("%s  Engineering Context Agent — %s", ICONS["run"], today_label)
    log.info("   Engineers: %d", len(ENGINEERS))

    empty_connector_results: list[str] = []

    for eng in ENGINEERS:
        eng_name = eng.get("name", eng.get("id", "?"))
        eng_id   = eng["id"]
        log.info("%s", "─" * 60)
        log.info("  Engineer: %s  (id=%s)", eng_name, eng_id)

        # ── Step 0: Auth check ───────────────────────────────────────────────────
        log.info("  Step 0: Checking connector auth")
        for connector in ALL_CONNECTORS:
            ensure_authorized(connector, eng_id)

        # ── Step 1: GitHub PRs ───────────────────────────────────────────────────
        log.info("  Step 1: Fetching GitHub PRs")
        repos = eng.get("github_repos") or []
        github_prs = fetch_github_prs(eng)
        log.info("    Found %d open PR(s) across %d repo(s)", len(github_prs), len(repos))
        for pr in github_prs:
            age = _days_open(pr.get("created_at"))
            stale_flag = f"  {ICONS['warn']} {age}d old" if age > 3 else ""
            log.info("      #%s %s%s", pr.get("number") or pr.get("iid"), pr.get("title", "")[:55], stale_flag)
        if not github_prs:
            empty_connector_results.append(f"{eng_name}: GitHub returned 0 open PRs")

        # ── Step 2: GitLab MRs + pipelines ──────────────────────────────────────
        log.info("  Step 2: Fetching GitLab MRs + pipeline status")
        gitlab_mrs = fetch_gitlab_mrs_and_pipelines(eng)
        log.info("    Found %d open MR(s)", len(gitlab_mrs))
        for mr in gitlab_mrs:
            icon = _pipeline_icon(mr.get("pipeline_status", ""))
            log.info(
                "      !%s %s  pipeline: %s %s",
                mr.get("iid"), mr.get("title", "")[:50], icon, mr.get("pipeline_status", "unknown"),
            )
        if not gitlab_mrs:
            empty_connector_results.append(f"{eng_name}: GitLab returned 0 open MRs")

        # ── Step 3: Jira issues ──────────────────────────────────────────────────
        log.info("  Step 3: Querying Jira (assignee = currentUser())")
        jira_issues = fetch_jira_issues(eng)
        log.info("    Found %d in-progress issue(s)", len(jira_issues))
        for issue in jira_issues:
            fields  = issue.get("fields") or {}
            key     = issue.get("key", "")
            summary = fields.get("summary") or issue.get("summary", "")
            status  = (fields.get("status") or {}).get("name") or ""
            log.info("      %s: %s  [%s]", key, summary[:55], status)
        if not jira_issues:
            empty_connector_results.append(f"{eng_name}: Jira returned 0 in-progress issues")

        # ── Step 4: Build digest ─────────────────────────────────────────────────
        log.info("  Step 4: Building standup digest")
        try:
            digest = build_digest(eng, github_prs, gitlab_mrs, jira_issues)
        except Exception as e:
            log.error("%s    LLM digest failed: %s: %s — no fallback, skipping this engineer", ICONS["error"], e.__class__.__name__, e)
            empty_connector_results.append(f"{eng_name}: LLM digest generation failed")
            continue
        log.info("  ── Digest preview ──\n%s\n  ────────────────────", digest)

        # ── Step 5: Post to Slack ────────────────────────────────────────────────
        log.info("  Step 5: Posting digest to Slack DM")
        if not post_digest_to_slack(eng, digest):
            empty_connector_results.append(f"{eng_name}: Slack delivery failed")

    if empty_connector_results:
        log.error("%s  Run finished with empty or failed connector results:", ICONS["error"])
        for row in empty_connector_results:
            log.error("  - %s", row)
        return 2

    log.info("%s  Done. Ran for %d engineer(s).", ICONS["done"], len(ENGINEERS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
