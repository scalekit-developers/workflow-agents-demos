"""poller.py - Scalekit-only DevOps Assistant

Replaces webhook approach by polling GitHub PRs via Scalekit tools and
creating Linear issues when new labels appear that haven't yet been mapped.

Idempotency: state/pr_linear_links.json stores per-PR+label Linear issue IDs.

Logging:
- All output goes through the standard `logging` module instead of print().
- Console output is color-highlighted (yellow=WARNING, red=ERROR, red-bg=CRITICAL)
  when running in a real terminal; plain text otherwise (e.g. when piped to a file).
- A rotating log file is also written to logs/poller.log so history survives
  terminal restarts.
- Every external call (GitHub, Linear, Slack, disk I/O) is wrapped in
  try/except so a single failure is logged clearly and the poller keeps running
  instead of crashing the whole process.
"""
import time
import datetime as dt
import json
import logging
import logging.handlers
import os
import sys
import traceback
from typing import Any, Dict, List, Set

from settings import Settings
from sk_connectors import get_connector


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

class ColorFormatter(logging.Formatter):
    """Formatter that highlights WARNING/ERROR/CRITICAL with ANSI colors
    when writing to an interactive terminal. Falls back to plain text for
    non-tty destinations (files, pipes, CI logs) so nothing gets garbled."""

    COLORS = {
        logging.DEBUG: "\033[2;37m",        # dim grey
        logging.INFO: "\033[36m",           # cyan
        logging.WARNING: "\033[1;33m",      # bold yellow
        logging.ERROR: "\033[1;31m",        # bold red
        logging.CRITICAL: "\033[1;97;41m",  # bold white on red background
    }
    RESET = "\033[0m"

    def __init__(self, fmt: str, datefmt: str, colorize: bool):
        super().__init__(fmt, datefmt)
        self.colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not self.colorize:
            return message
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{message}{self.RESET}" if color else message


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("poller")
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False

    if logger.handlers:
        return logger  # already configured, e.g. on module re-import

    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColorFormatter(fmt, datefmt, colorize=sys.stdout.isatty()))
    logger.addHandler(console_handler)

    try:
        log_dir = os.path.join(os.path.dirname(__file__), "logs")
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "poller.log"), maxBytes=2_000_000, backupCount=5
        )
        file_handler.setFormatter(ColorFormatter(fmt, datefmt, colorize=False))
        logger.addHandler(file_handler)
    except OSError as e:
        # Don't let a logging-setup problem stop the poller from starting.
        logger.warning("Could not set up file logging (%s); continuing with console-only logging.", e)

    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# Config / env validation
# ---------------------------------------------------------------------------

LINEAR_IDENTIFIER = os.getenv("LINEAR_IDENTIFIER", "")
SLACK_IDENTIFIER = os.getenv("SLACK_IDENTIFIER", "")
GITHUB_IDENTIFIER = os.getenv("GITHUB_IDENTIFIER", "")

if not LINEAR_IDENTIFIER or not SLACK_IDENTIFIER or not GITHUB_IDENTIFIER:
    log.critical(
        "Missing required environment variable(s): %s",
        ", ".join(
            name
            for name, val in (
                ("LINEAR_IDENTIFIER", LINEAR_IDENTIFIER),
                ("SLACK_IDENTIFIER", SLACK_IDENTIFIER),
                ("GITHUB_IDENTIFIER", GITHUB_IDENTIFIER),
            )
            if not val
        ),
    )
    raise ValueError("LINEAR_IDENTIFIER, SLACK_IDENTIFIER, and GITHUB_IDENTIFIER must be set in .env")

POLL_INTERVAL = 30  # seconds

STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "pr_linear_links.json")


# ---------------------------------------------------------------------------
# Digest helpers (from digest.py)
# ---------------------------------------------------------------------------

# Tools used (via Scalekit execute_tool):
# github_pull_requests_list – list open PRs
# linear_issue_create – create Linear issue
# slack_send_message – optional notifications

def list_open_prs_digest(identifier: str) -> List[Dict[str, Any]]:
    """Fetch open PRs for the digest. Returns [] on any failure so the
    digest can still be sent (just noting there's nothing to report)."""
    try:
        conn = get_connector()
    except Exception:
        log.exception("Failed to obtain connector while building digest.")
        return []

    owner, repo = Settings.GITHUB_REPO_OWNER, Settings.GITHUB_REPO_NAME
    params = {"owner": owner, "repo": repo, "state": "open"}
    try:
        prs_resp = conn.execute_tool(
            identifier=identifier,
            tool="github_pull_requests_list",
            parameters=params,
            connection_name=Settings.GITHUB_CONNECTION_NAME or None,
        ) or {}
    except Exception:
        log.exception("github_pull_requests_list failed while building digest for %s/%s.", owner, repo)
        return []

    for key in ("array", "items", "pull_requests", "data"):
        if key in prs_resp:
            value = prs_resp[key]
            if isinstance(value, list):
                return value
            log.warning("Digest PR response field '%s' was not a list (got %s); ignoring.", key, type(value).__name__)
            return []

    log.warning("Digest PR response had none of the expected keys (array/items/pull_requests/data): %s", list(prs_resp.keys()))
    return []


def format_digest(prs: List[Dict[str, Any]], pr_linear_links: dict) -> str:
    if not prs:
        return "Daily DevOps Digest:\n- No open PRs."

    lines = ["Daily DevOps Digest:"]
    today = dt.datetime.now(dt.timezone.utc).date()

    for pr in prs:
        try:
            title = pr.get("title") or pr.get("head", {}).get("ref")
            number = int(pr.get("number") or 0)
            url = pr.get("html_url")
            reviewers = [r.get("login") for r in (pr.get("requested_reviewers") or [])]

            updated_raw = pr.get("updated_at", "")
            updated_norm = updated_raw.replace("Z", "+00:00") if updated_raw else ""
            try:
                updated_date = dt.datetime.fromisoformat(updated_norm).date() if updated_norm else None
            except ValueError:
                log.warning("PR #%s has an unparsable updated_at value (%r); treating as unknown.", number, updated_raw)
                updated_date = None

            stale = " (stale)" if (updated_date and (today - updated_date).days >= Settings.DIGEST_STALE_DAYS) else ""
            reviewer_text = ", ".join(reviewers) if reviewers else "none"

            pr_key_prefix = f"{Settings.GITHUB_REPO_OWNER}/{Settings.GITHUB_REPO_NAME}#{number}:"
            linked_issues = [v["linear_issue_id"] for k, v in pr_linear_links.items() if k.startswith(pr_key_prefix)]
            linear_text = f" | Linear: {', '.join(linked_issues)}" if linked_issues else ""

            ci_status = " | CI: (not implemented)"
            lines.append(f"- #{number} {title}{stale} | reviewers: {reviewer_text}{linear_text}{ci_status} | {url}")
        except Exception:
            log.exception("Skipping malformed PR entry in digest: %r", pr)
            continue

    return "\n".join(lines)


def post_slack_digest(identifier: str, text: str) -> None:
    try:
        conn = get_connector()
    except Exception:
        log.exception("Failed to obtain connector; daily digest was NOT sent to Slack.")
        return

    params = {"channel": Settings.SLACK_DIGEST_CHANNEL_ID, "text": text}
    try:
        result = conn.execute_tool(
            identifier=identifier,
            tool="slack_send_message",
            parameters=params,
            connection_name=Settings.SLACK_CONNECTION_NAME or None,
        )
        if result is None:
            log.error(
                "Slack digest NOT sent — tool returned None. "
                "Check SLACK_IDENTIFIER / SLACK_CONNECTION_NAME and that the account is active."
            )
        else:
            log.info("Slack digest sent successfully.")
    except Exception:
        log.exception("Failed to send Slack digest to channel %s.", Settings.SLACK_DIGEST_CHANNEL_ID)


# ---------------------------------------------------------------------------
# Core polling logic
# ---------------------------------------------------------------------------

def fetch_open_prs() -> List[Dict[str, Any]]:
    """Fetch open PRs for the main poll loop. Returns [] on failure rather
    than raising, so a transient GitHub/Scalekit outage doesn't kill the poller."""
    try:
        conn = get_connector()
    except Exception:
        log.exception("Failed to obtain connector; skipping this poll cycle.")
        return []

    log.info("Fetching PRs for %s/%s", Settings.GITHUB_REPO_OWNER, Settings.GITHUB_REPO_NAME)
    params = {"owner": Settings.GITHUB_REPO_OWNER, "repo": Settings.GITHUB_REPO_NAME, "state": "open"}

    try:
        resp = conn.execute_tool(
            identifier=GITHUB_IDENTIFIER,
            tool="github_pull_requests_list",
            parameters=params,
            connection_name=Settings.GITHUB_CONNECTION_NAME or None,
        ) or {}
    except Exception:
        log.exception("github_pull_requests_list failed for %s/%s; skipping this poll cycle.",
                       Settings.GITHUB_REPO_OWNER, Settings.GITHUB_REPO_NAME)
        return []

    log.debug("Raw PR fetch response: %s", resp)

    prs: List[Dict[str, Any]] = []
    for key in ("array", "items", "pull_requests", "data"):
        if key in resp:
            value = resp[key]
            if isinstance(value, list):
                prs = value
            else:
                log.warning("PR response field '%s' was not a list (got %s); treating as no PRs.", key, type(value).__name__)
            break
    else:
        log.warning("PR response had none of the expected keys (array/items/pull_requests/data): %s", list(resp.keys()))

    log.info("Found %d open PR(s).", len(prs))
    return prs


def build_label_key(full_name: str, number: int, label: str) -> str:
    return f"{full_name}#{number}:{label}".lower()


def process_labels(pr: Dict[str, Any], existing_keys: Set[str]) -> None:
    try:
        conn = get_connector()
    except Exception:
        log.exception("Failed to obtain connector; skipping label processing for this PR.")
        return

    full_name = f"{Settings.GITHUB_REPO_OWNER}/{Settings.GITHUB_REPO_NAME}"

    raw_number = pr.get("number")
    try:
        number = int(raw_number) if raw_number is not None else None
    except (TypeError, ValueError):
        log.warning("PR has a non-numeric 'number' field (%r); using raw value as fallback.", raw_number)
        number = raw_number

    title = str(pr.get("title") or "")
    url = str(pr.get("html_url") or "")
    labels = [str(l.get("name")) for l in pr.get("labels", []) if l.get("name")]
    log.info("Processing PR #%s: '%s' with labels: %s", number, title, labels)

    for label in labels:
        try:
            _process_single_label(conn, full_name, number, title, url, label)
        except Exception:
            # One bad label should never stop the rest of the PR's labels
            # (or the rest of the polling cycle) from being processed.
            log.exception("Unexpected error processing label '%s' on PR #%s; continuing with next label.", label, number)
            continue


def _process_single_label(conn, full_name: str, number: Any, title: str, url: str, label: str) -> None:
    key = build_label_key(full_name, number, label)

    try:
        linear_issue_id = conn.get_linear_issue_for_pr(key)
    except Exception:
        log.exception("Failed to check idempotency state for key '%s'; skipping this label to avoid duplicate issues.", key)
        return

    if linear_issue_id:
        log.info("Already linked Linear issue for key: %s (id: %s)", key, linear_issue_id)
        return

    team_id = str(Settings.LABEL_TO_LINEAR_TEAM.get(label) or Settings.LINEAR_TEAM_ID)
    if not team_id:
        log.warning("No team_id configured for label '%s'; skipping.", label)
        return

    safe_title = title.replace("{", "(").replace("}", ")")
    safe_label = label.replace("{", "(").replace("}", ")")
    safe_descr = f"Auto-created from PR #{number} in {full_name}\nURL: {url}\nLabel: {safe_label}"
    issue_params = {
        "title": f"PR: {safe_title} [{safe_label}]",
        "description": safe_descr,
        "teamId": team_id,
    }

    try:
        log.info("Creating Linear issue for key: %s", key)
        result = conn.execute_tool(
            identifier=LINEAR_IDENTIFIER,
            tool="linear_issue_create",
            parameters=issue_params,
            connection_name=Settings.LINEAR_CONNECTION_NAME or None,
        )
        log.debug("Linear issue create result: %s", result)
    except Exception:
        log.exception("Failed to create Linear issue for key '%s'.", key)
        return

    linear_issue_id = None
    if isinstance(result, dict):
        try:
            linear_issue_id = result["data"]["issueCreate"]["issue"].get("id")
        except (KeyError, TypeError, AttributeError):
            pass
        if not linear_issue_id:
            linear_issue_id = result.get("id") or result.get("issue_id") or (result.get("data") or {}).get("id")

    if not linear_issue_id:
        log.error("Linear issue creation for key '%s' returned no usable issue id. Response was: %s", key, result)
        return

    try:
        conn.record_pr_issue(key, linear_issue_id, label)
        log.info("Recorded Linear issue %s for key: %s", linear_issue_id, key)
    except Exception:
        # The issue WAS created in Linear but we failed to persist that fact.
        # Flag this loudly since the next poll could otherwise create a duplicate.
        log.error(
            "Linear issue %s was created for key '%s' but recording it to local state FAILED. "
            "A duplicate issue may be created on the next poll. Error: %s",
            linear_issue_id, key, traceback.format_exc(),
        )

    slack_msg = f"Linked Linear issue {linear_issue_id} for PR #{number} [{label}]"
    slack_params = {"channel": Settings.SLACK_DIGEST_CHANNEL_ID, "text": slack_msg}
    try:
        slack_result = conn.execute_tool(
            identifier=SLACK_IDENTIFIER,
            tool="slack_send_message",
            parameters=slack_params,
            connection_name=Settings.SLACK_CONNECTION_NAME or None,
        )
        if slack_result is None:
            log.error(
                "Slack notification NOT sent — tool returned None. "
                "Check SLACK_IDENTIFIER and that only one Slack account is connected for that identifier."
            )
        else:
            log.info("Slack notification sent.")
    except Exception:
        log.exception("Failed to send Slack notification for key '%s' (Linear issue was still created/recorded).", key)


def loop_once() -> None:
    prs = fetch_open_prs()
    existing: Set[str] = set()  # placeholder; connector handles per-key checks
    log.info("Looping over %d PR(s)...", len(prs))

    for pr in prs:
        pr_number = pr.get("number", "?")
        try:
            process_labels(pr, existing)
        except Exception:
            # Defense in depth: process_labels already guards its own internals,
            # but make sure a truly unexpected error on one PR never takes down
            # the whole polling cycle.
            log.exception("Unexpected error while processing PR #%s; continuing with next PR.", pr_number)
            continue


def load_pr_linear_links() -> dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        log.info("No existing state file at %s yet; starting with empty link map.", STATE_PATH)
        return {}
    except json.JSONDecodeError:
        log.error("State file at %s is corrupted/unparsable JSON; treating as empty. Manual recovery may be needed.", STATE_PATH)
        return {}
    except OSError:
        log.exception("Failed to read state file at %s; treating as empty.", STATE_PATH)
        return {}


def run_forever() -> None:
    log.info("DevOps poller started (Scalekit-only). Press Ctrl+C to stop.")
    last_digest_day = None

    while True:
        start = time.time()
        try:
            loop_once()

            now = dt.datetime.now(dt.timezone.utc)
            today = now.date()
            if last_digest_day != today:
                log.info("Sending daily Slack digest...")
                pr_linear_links = load_pr_linear_links()
                prs = list_open_prs_digest(GITHUB_IDENTIFIER)
                digest_text = format_digest(prs, pr_linear_links)
                post_slack_digest(SLACK_IDENTIFIER, digest_text)
                log.info("Daily digest cycle complete.")
                last_digest_day = today

        except KeyboardInterrupt:
            # Let this propagate so the outer caller can shut down cleanly.
            raise
        except Exception:
            # Catch-all so one bad cycle (network blip, malformed response,
            # unexpected Scalekit error, etc.) never crashes the whole poller.
            log.error("Loop error - this cycle failed but the poller will keep running:\n%s", traceback.format_exc())

        elapsed = time.time() - start
        sleep_for = max(1, POLL_INTERVAL - elapsed)
        log.info("Loop took %.2fs, sleeping for %.2fs.", elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received; shutting down poller cleanly.")
        sys.exit(0)
    except Exception:
        # Last-resort safety net: log the full traceback clearly before exiting
        # with a non-zero code so process managers (systemd, supervisord, etc.)
        # know it failed.
        log.critical("Poller crashed with an unhandled error:\n%s", traceback.format_exc())
        sys.exit(1)