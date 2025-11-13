"""poller.py - Scalekit-only DevOps Assistant

Replaces webhook approach by polling GitHub PRs via Scalekit tools and
creating Linear issues when new labels appear that haven't yet been mapped.

Idempotency: state/pr_linear_links.json stores per-PR+label Linear issue IDs.
"""
import time
import datetime as dt
from typing import Dict, Any, List, Set
from settings import Settings
from sk_connectors import get_connector
import threading

# --- Digest helpers (from digest.py) ---
def list_open_prs_digest(identifier) -> List[Dict[str, Any]]:
    conn = get_connector()
    owner, repo = Settings.GITHUB_REPO_OWNER, Settings.GITHUB_REPO_NAME
    params = {"owner": owner, "repo": repo, "state": "open"}
    prs_resp = conn.execute_tool(
        identifier=identifier,
        tool="github_pull_requests_list",
        parameters=params,
    ) or {}
    if "array" in prs_resp:
        return prs_resp["array"]
    if "items" in prs_resp:
        return prs_resp["items"]
    if "pull_requests" in prs_resp:
        return prs_resp["pull_requests"]
    if "data" in prs_resp:
        return prs_resp["data"]
    return []

def format_digest(prs: List[Dict[str, Any]], pr_linear_links: dict) -> str:
    if not prs:
        return "Daily DevOps Digest:\n- No open PRs."
    lines = ["Daily DevOps Digest:"]
    today = dt.datetime.utcnow().date()
    for pr in prs:
        title = pr.get("title") or pr.get("head", {}).get("ref")
        number = pr.get("number")
        url = pr.get("html_url")
        reviewers = [r.get("login") for r in (pr.get("requested_reviewers") or [])]
        updated_raw = pr.get("updated_at", "")
        updated_norm = updated_raw.replace("Z", "+00:00") if updated_raw else ""
        try:
            updated_date = dt.datetime.fromisoformat(updated_norm).date() if updated_norm else None
        except Exception:
            updated_date = None
        stale = " (stale)" if (updated_date and (today - updated_date).days >= Settings.DIGEST_STALE_DAYS) else ""
        reviewer_text = ", ".join(reviewers) if reviewers else "none"
        # Find Linear issue(s) for this PR
        pr_key_prefix = f"{Settings.GITHUB_REPO_OWNER}/{Settings.GITHUB_REPO_NAME}#{number}:"
        linked_issues = [v["linear_issue_id"] for k, v in pr_linear_links.items() if k.startswith(pr_key_prefix)]
        linear_text = f" | Linear: {', '.join(linked_issues)}" if linked_issues else ""
        # Placeholder for CI status (could be fetched via status API)
        ci_status = " | CI: (not implemented)"
        lines.append(f"- #{number} {title}{stale} | reviewers: {reviewer_text}{linear_text}{ci_status} | {url}")
    return "\n".join(lines)

def post_slack_digest(identifier, text: str):
    conn = get_connector()
    params = {"channel": Settings.SLACK_DIGEST_CHANNEL_ID, "text": text}
    try:
        result = conn.execute_tool(
            identifier=identifier,
            tool="slack_send_message",
            parameters=params,
        )
        print(f"[poller] Slack digest send result: {result}")
    except Exception as e:
        print(f"[poller] Slack digest send error: {e}")

import os
LINEAR_IDENTIFIER = os.getenv("LINEAR_IDENTIFIER", "")
SLACK_IDENTIFIER = os.getenv("SLACK_IDENTIFIER", "")
GITHUB_IDENTIFIER = os.getenv("GITHUB_IDENTIFIER", "")
if not LINEAR_IDENTIFIER or not SLACK_IDENTIFIER or not GITHUB_IDENTIFIER:
    raise ValueError("LINEAR_IDENTIFIER, SLACK_IDENTIFIER, and GITHUB_IDENTIFIER must be set in .env")
POLL_INTERVAL = 30  # seconds

# Tools used (via Scalekit execute_tool):
# github_pull_requests_list – list open PRs
# linear_issue_create – create Linear issue
# slack_send_message – optional notifications

def fetch_open_prs() -> List[Dict[str, Any]]:
    conn = get_connector()
    print(f"[poller] Fetching PRs for {Settings.GITHUB_REPO_OWNER}/{Settings.GITHUB_REPO_NAME}")
    params = {"owner": Settings.GITHUB_REPO_OWNER, "repo": Settings.GITHUB_REPO_NAME, "state": "open"}
    resp = conn.execute_tool(
        identifier=GITHUB_IDENTIFIER,
        tool="github_pull_requests_list",
        parameters=params,
    ) or {}
    print(f"[poller] Raw PR fetch response: {resp}")
    # Support all possible keys
    if "array" in resp:
        prs = resp["array"]
    elif "items" in resp:
        prs = resp["items"]
    elif "pull_requests" in resp:
        prs = resp["pull_requests"]
    elif "data" in resp:
        prs = resp["data"]
    else:
        prs = []
    print(f"[poller] Found {len(prs)} open PRs.")
    return prs


def build_label_key(full_name: str, number: int, label: str) -> str:
    return f"{full_name}#{number}:{label}".lower()


def process_labels(pr: Dict[str, Any], existing_keys: Set[str]):
    conn = get_connector()
    full_name = f"{Settings.GITHUB_REPO_OWNER}/{Settings.GITHUB_REPO_NAME}"
    # Normalize fields from Scalekit/GitHub response
    raw_number = pr.get("number")
    try:
        number = int(raw_number) if raw_number is not None else None
    except Exception:
        # fallback to string-based number
        number = raw_number
    title = str(pr.get("title") or "")
    url = str(pr.get("html_url") or "")
    # labels may contain characters that break templates; coerce to str
    labels = [str(l.get("name")) for l in pr.get("labels", []) if l.get("name")]
    print(f"[poller] Processing PR #{number}: '{title}' with labels: {labels}")

    for label in labels:
        key = build_label_key(full_name, number, label)
        # Check idempotency: only create if not already linked
        linear_issue_id = conn.get_linear_issue_for_pr(key)
        if linear_issue_id:
            print(f"[poller] Already linked Linear issue for key: {key} (id: {linear_issue_id})")
            continue  # already processed this label
        team_id = str(Settings.LABEL_TO_LINEAR_TEAM.get(label) or Settings.LINEAR_TEAM_ID)
        if not team_id:
            print(f"[poller] No team_id configured for label {label}; skipping")
            continue

        # sanitize fields to avoid template rendering issues in Scalekit tools
        safe_title = title.replace("{", "(").replace("}", ")")
        safe_label = label.replace("{", "(").replace("}", ")")
        safe_descr = f"Auto-created from PR #{number} in {full_name}\nURL: {url}\nLabel: {safe_label}"
        issue_params = {
            "title": f"PR: {safe_title} [{safe_label}]",
            "description": safe_descr,
            "teamId": team_id,
        }
        try:
            print(f"[poller] Creating Linear issue for key: {key} with params: {issue_params}")
            result = conn.execute_tool(identifier=LINEAR_IDENTIFIER, tool="linear_issue_create", parameters=issue_params)
            print(f"[poller] Linear issue create result: {result}")
        except Exception as e:
            print(f"[poller] Linear issue create error: {e}")
            continue
        # Extract Linear issue id from nested response
        linear_issue_id = None
        if result:
            # Try to extract from common Linear API response structure
            if "data" in result and "issueCreate" in result["data"] and "issue" in result["data"]["issueCreate"]:
                linear_issue_id = result["data"]["issueCreate"]["issue"].get("id")
            if not linear_issue_id:
                linear_issue_id = result.get("id") or result.get("issue_id") or result.get("data", {}).get("id")
        if linear_issue_id:
            conn.record_pr_issue(key, linear_issue_id, label)
            print(f"[poller] Recorded Linear issue {linear_issue_id} for key: {key}")
            slack_msg = f"Linked Linear issue {linear_issue_id} for PR #{number} [{label}]"
            slack_params = {"channel": Settings.SLACK_DIGEST_CHANNEL_ID, "text": slack_msg}
            try:
                slack_result = conn.execute_tool(
                    identifier=SLACK_IDENTIFIER,
                    tool="slack_send_message",
                    parameters=slack_params,
                )
                print(f"[poller] Slack send result: {slack_result}")
            except Exception as e:
                print(f"[poller] Slack send error: {e}")
        else:
            print(f"[poller] Failed to create Linear issue for key: {key}")


def loop_once():
    prs = fetch_open_prs()
    existing = set()  # placeholder; connector handles per-key checks
    print(f"[poller] Looping over {len(prs)} PRs...")
    for pr in prs:
        process_labels(pr, existing)


def run_forever():
    print("▶️  DevOps poller started (Scalekit-only). Press Ctrl+C to stop.")
    last_digest_day = None
    # Load pr_linear_links.json for digest
    import json
    import os as _os
    state_path = _os.path.join(_os.path.dirname(__file__), "state", "pr_linear_links.json")
    def load_pr_linear_links():
        try:
            with open(state_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    while True:
        start = time.time()
        try:
            loop_once()
            # Send daily digest at midnight UTC (or first run)
            now = dt.datetime.utcnow()
            today = now.date()
            if last_digest_day != today:
                print("[poller] Sending daily Slack digest...")
                pr_linear_links = load_pr_linear_links()
                prs = list_open_prs_digest(GITHUB_IDENTIFIER)
                digest_text = format_digest(prs, pr_linear_links)
                post_slack_digest(SLACK_IDENTIFIER, digest_text)
                print("[poller] Daily digest sent.")
                last_digest_day = today
        except KeyboardInterrupt:
            print("[poller] KeyboardInterrupt, exiting.")
            raise
        except Exception as e:
            print(f"⚠️  Loop error: {e}")
        elapsed = time.time() - start
        print(f"[poller] Loop took {elapsed:.2f}s, sleeping for {max(1, POLL_INTERVAL - elapsed):.2f}s.")
        sleep_for = max(1, POLL_INTERVAL - elapsed)
        time.sleep(sleep_for)

if __name__ == "__main__":
    run_forever()
