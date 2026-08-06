#!/usr/bin/env python3
"""
Competitive Intelligence Briefing Agent: Gong -> Notion -> Slack

Runs on behalf of a PMM (Product Marketing Manager): fetches Gong calls with
configured competitor mentions from the last LOOKBACK_DAYS, looks up each
mentioned competitor's battlecard page in Notion, and DMs each affected sales
rep ONE digest message per cycle covering every call and competitor they were
involved with (not one DM per call).

Scalekit Agent Auth handles OAuth for all three connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual
token management, no direct API imports.

Gong availability: as of this build, GONG has zero connected accounts in the
reference Scalekit workspace this agent was developed against. Step 0 and
Step 0.5 report this without crashing (Gong's availability is a per-run data
condition, not a static config error -- see provisioning.py); Step 1 is
where a genuinely unreachable Gong produces a specific, actionable failure
with exit code 1. See README Prerequisites for what connecting GONG requires.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # run one briefing cycle and exit

Exit codes:
  0   = success (calls fetched; at least one rep briefed, or all mentions
        already briefed in a prior run)
  1   = error (config missing, provisioning failed, Gong unreachable, or 5
        consecutive polling errors)
  2   = no data (no calls with any tracked competitor mention found in the
        lookback window)
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import datetime
import signal
import sys
import time
from typing import Dict, List, Optional, Tuple

import scalekit.client
from dotenv import load_dotenv

from aggregator import RepDigest, build_rep_digests, render_digest_text
import config as config_module
from config import Config
from connectors import (
    ConnectorError,
    ConnectorUnavailableError,
    GongConnector,
    NotionConnector,
    SlackConnector,
)
import logging_config
from provisioning import ProvisioningError, verify_notion_battlecards_parent
from state import StateManager, compute_mention_key

load_dotenv()
logger = logging_config.setup_logging(__name__)
config_module.logger = logger

_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    logger.warning("Received signal, shutting down gracefully...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def init_config() -> Config:
    cfg = Config()
    cfg.validate()
    return cfg


def init_scalekit(cfg: Config):
    try:
        sk = scalekit.client.ScalekitClient(
            client_id=cfg.scalekit_client_id,
            client_secret=cfg.scalekit_client_secret,
            env_url=cfg.scalekit_env_url,
        )
        logger.debug("Scalekit client initialized")
        return sk
    except Exception as e:
        logger.error(f"Failed to initialize Scalekit: {e}", exc_info=True)
        sys.exit(1)


def fetch_calls_with_mentions(gong: GongConnector, cfg: Config) -> List[Dict]:
    """
    Step 1: fetch every call in the lookback window and enrich it with
    tracker/participant details. Raises ConnectorUnavailableError /
    ConnectorError to the caller if Gong cannot be reached at all -- this is
    intentionally NOT caught here, so run_flow.main() can distinguish "Gong
    is unreachable" (a specific, actionable failure) from "Gong reached but
    returned zero calls" (a normal no-data outcome) at the call site.

    Malformed individual call records (missing IDs, unparseable dates) are
    the aggregator's concern (see aggregator.build_rep_digests), not this
    function's -- this function's job is only to get raw call data out of
    Gong without silently swallowing a total-fetch failure.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    from_dt = (now - datetime.timedelta(days=cfg.lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_dt = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(f"Fetching Gong calls from {from_dt} to {to_dt} ({cfg.lookback_days} day lookback)")
    calls = gong.list_all_calls(from_dt, to_dt)
    logger.info(f"Gong returned {len(calls)} call(s) in the lookback window")

    if not calls:
        return []

    call_ids = [c.get("id") for c in calls if isinstance(c, dict) and c.get("id")]
    if not call_ids:
        logger.warning("Gong returned calls with no usable call IDs -- treating as zero calls this cycle")
        return []

    details: List[Dict] = []
    batch_size = 20  # conservative batch size for gong_calls_get; Gong's real API caps batch calls
    for i in range(0, len(call_ids), batch_size):
        batch = call_ids[i : i + batch_size]
        try:
            details.extend(gong.get_calls_extensive(batch))
        except ConnectorError as e:
            logger.warning(f"Failed to fetch extensive details for a batch of {len(batch)} call(s): {e}")

    return details


def fetch_transcripts_for_fallback(gong: GongConnector, calls_needing_transcript: List[str]) -> Dict[str, List[Dict]]:
    """
    Fetch transcripts only for calls whose tracker hits didn't already
    resolve a competitor match (an optimization: transcript fetches are the
    heavier call). Returns {call_id: [sentence, ...]}, flattened across
    speakers. A failure here is logged and treated as "no transcript
    available" for those calls rather than aborting the cycle.
    """
    if not calls_needing_transcript:
        return {}

    try:
        raw = gong.get_transcripts(calls_needing_transcript)
    except ConnectorError as e:
        logger.warning(f"Failed to fetch transcripts for {len(calls_needing_transcript)} call(s): {e}")
        return {}

    result: Dict[str, List[Dict]] = {}
    for entry in raw:
        call_id = str(entry.get("callId") or entry.get("call_id") or "")
        if not call_id:
            continue
        sentences = []
        for transcript_part in entry.get("transcript") or []:
            sentences.extend(transcript_part.get("sentences") or [])
        result[call_id] = sentences
    return result


def brief_one_rep(
    cfg: Config,
    notion: NotionConnector,
    slack: SlackConnector,
    state: StateManager,
    digest: RepDigest,
    battlecard_cache: Dict[str, Optional[Dict]],
    notion_parent_page_id: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Process one rep's digest end-to-end: filter out already-briefed mentions
    (idempotency), look up each remaining competitor's battlecard in Notion
    (warn and continue without a link if missing), resolve the rep's Slack
    user ID (skip this rep only, with a warning, if unresolvable), render
    and send the digest DM, and mark the newly-sent mentions as briefed.

    This is the exact, unmodified function exercised by both run_cycle()
    (via real Gong data) and this repo's direct live-test invocation (with a
    synthetic call/mention record standing in for Gong, per the validation
    requirements in this build) -- there is no separate test-only code path.

    Returns (outcome, detail):
      outcome is one of "briefed", "already_briefed", "skipped_no_slack_user",
      "skipped_send_failed".
      detail is a short human-readable reason, used in the final summary log.
    """
    new_mentions = [
        m for m in digest.mentions
        if not state.is_briefed(compute_mention_key(digest.rep_identifier, m.call_id, m.competitor))
    ]

    if not new_mentions:
        logger.info(f"{digest.rep_display_name}: all {len(digest.mentions)} mention(s) already briefed, skipping")
        return "already_briefed", "all mentions already briefed in a prior run"

    filtered_digest = RepDigest(digest.rep_identifier, digest.rep_display_name)
    for m in new_mentions:
        filtered_digest.add(m)

    parent_page_id = notion_parent_page_id or cfg.notion_battlecards_parent_page_id

    battlecard_links: Dict[str, Optional[str]] = {}
    for competitor in filtered_digest.competitors():
        if competitor not in battlecard_cache:
            try:
                battlecard_cache[competitor] = notion.find_battlecard_page(parent_page_id, competitor)
            except ConnectorError as e:
                logger.warning(f"Notion battlecard lookup failed for '{competitor}': {e}")
                battlecard_cache[competitor] = None

        page = battlecard_cache[competitor]
        if page:
            battlecard_links[competitor] = page.get("url")
        else:
            battlecard_links[competitor] = None
            logger.warning(f"No Notion battlecard found for competitor '{competitor}' -- DM will note it's missing")

    try:
        user_id = slack.resolve_user_id(digest.rep_identifier)
    except ConnectorError as e:
        logger.warning(f"Slack lookup failed for rep '{digest.rep_display_name}': {e}")
        user_id = None

    if not user_id:
        logger.warning(
            f"{digest.rep_display_name}: could not resolve a Slack user ID for "
            f"'{digest.rep_identifier}' -- skipping this rep's DM, continuing with others"
        )
        return "skipped_no_slack_user", f"could not resolve Slack user for '{digest.rep_identifier}'"

    text = render_digest_text(filtered_digest, battlecard_links)

    try:
        slack.send_dm(user_id, text)
    except ConnectorError as e:
        logger.warning(f"Failed to send Slack DM to {digest.rep_display_name}: {e}")
        return "skipped_send_failed", f"Slack send failed: {e}"

    logger.info(
        f"[OK] Briefed {digest.rep_display_name} on {len(new_mentions)} mention(s) "
        f"across {len(filtered_digest.competitors())} competitor(s)"
    )
    keys = [compute_mention_key(digest.rep_identifier, m.call_id, m.competitor) for m in new_mentions]
    state.mark_many_briefed(keys)
    return "briefed", f"{len(new_mentions)} mention(s) sent"


def run_cycle(cfg: Config, actions, state: StateManager) -> Optional[int]:
    """
    Run one full briefing cycle. Returns the number of reps with at least
    one new mention this cycle (briefed or already-briefed), or None if
    Gong returned zero calls with any tracked competitor mention -- distinct
    from 0, which cannot actually occur here (a rep entry only exists if a
    mention was found) but is reserved for symmetry with the sibling repos'
    "no data" convention.

    Raises ConnectorUnavailableError / ConnectorError if Gong itself cannot
    be reached -- this propagates up to main(), which is where the specific,
    actionable Gong-unreachable failure and exit code 1 are produced (see
    module docstring and main()).
    """
    gong = GongConnector(actions, cfg.gong_user, cfg.gong_connector, cfg.gong_workspace_id)
    notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
    slack = SlackConnector(actions, cfg.slack_user, cfg.slack_connector)

    logger.info("Step 1: Fetching calls with competitor mentions from Gong")
    calls_with_details = fetch_calls_with_mentions(gong, cfg)

    if not calls_with_details:
        logger.info("No calls found in the lookback window")
        return None

    pmm_domain = cfg.pmm_email.split("@")[-1] if cfg.pmm_email and "@" in cfg.pmm_email else ""

    # Real, verified fallback for rep identification: gong_calls_get never
    # actually returns a populated "parties" list (Scalekit's tool wrapper
    # exposes no contentSelector param to request it -- confirmed against
    # the tool's own published input_schema), but metaData.primaryUserId is
    # reliably present. Resolve the distinct primaryUserIds once via
    # gong_users_get and hand the lookup to build_rep_digests. See
    # aggregator.identify_rep's docstring for the full explanation.
    primary_user_ids = sorted({
        str((c.get("metaData") or c.get("meta") or c).get("primaryUserId"))
        for c in calls_with_details
        if (c.get("metaData") or c.get("meta") or c).get("primaryUserId")
    })
    user_lookup = gong.build_user_lookup(primary_user_ids) if primary_user_ids else {}

    digests = build_rep_digests(calls_with_details, cfg.competitor_names, pmm_domain, user_lookup=user_lookup)

    # Fallback transcript scan only for calls that produced zero tracker-based
    # mentions across every rep -- keeps the common case (tracker hits already
    # resolve mentions) cheap.
    calls_needing_fallback = [
        str((c.get("metaData") or c.get("meta") or c).get("id") or c.get("id"))
        for c in calls_with_details
        if not (c.get("content") or {}).get("trackers") and not c.get("trackers")
    ]
    if calls_needing_fallback:
        transcripts = fetch_transcripts_for_fallback(gong, calls_needing_fallback)
        if transcripts:
            digests = build_rep_digests(
                calls_with_details, cfg.competitor_names, pmm_domain, transcripts, user_lookup=user_lookup
            )

    if not digests:
        logger.info(f"Fetched {len(calls_with_details)} call(s) but none mentioned a tracked competitor "
                    f"({', '.join(cfg.competitor_names)})")
        return None

    logger.info(f"Step 2/3: {len(digests)} rep(s) have competitor mentions this cycle -- looking up battlecards and DMing")

    battlecard_cache: Dict[str, Optional[Dict]] = {}
    outcomes: Dict[str, int] = {"briefed": 0, "already_briefed": 0, "skipped_no_slack_user": 0, "skipped_send_failed": 0}

    for digest in digests.values():
        if _shutdown_requested:
            logger.warning("Shutdown requested mid-cycle -- stopping before processing remaining reps")
            break
        outcome, detail = brief_one_rep(
            cfg, notion, slack, state, digest, battlecard_cache, cfg.notion_battlecards_parent_page_id
        )
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    battlecards_found = sum(1 for v in battlecard_cache.values() if v)
    battlecards_missing = sum(1 for v in battlecard_cache.values() if not v)

    logger.info(
        f"[SUMMARY] {outcomes['briefed']} rep(s) briefed, "
        f"{outcomes['already_briefed']} already briefed (idempotent skip), "
        f"{outcomes['skipped_no_slack_user']} skipped (no Slack user), "
        f"{outcomes['skipped_send_failed']} skipped (send failed), "
        f"{battlecards_found} battlecard(s) found, {battlecards_missing} missing"
    )

    return len(digests)


def main() -> int:
    cfg = init_config()
    sk = init_scalekit(cfg)
    actions = sk.actions
    state = StateManager()

    logger.info("Step 0: Checking connector auth")
    all_active = True
    for connector_name, identifier in cfg.get_connector_users().items():
        conn = _connector_for_check(actions, connector_name, identifier)
        if not conn.check_auth():
            all_active = False

    if not all_active:
        logger.warning(
            "Some connectors are not authorized. Proceeding anyway -- Gong being "
            "unauthorized/unconfigured is a known, handled state (see README); "
            "Step 1 will fail clearly and specifically if Gong truly cannot be reached."
        )

    logger.info("Step 0.5: Verifying Notion battlecards parent page")
    notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
    try:
        verify_notion_battlecards_parent(notion, cfg.notion_battlecards_parent_page_id)
    except ProvisioningError as e:
        logger.error(str(e))
        return 1

    if cfg.polling_mode:
        logger.info(f"Polling mode enabled (interval: {cfg.poll_interval_minutes}m, press Ctrl+C to stop)")
        consecutive_errors = 0
        cycle = 0

        while True:
            if _shutdown_requested:
                logger.info("Graceful shutdown")
                return 130

            cycle += 1
            logger.info(f"Polling cycle #{cycle}")

            try:
                count = run_cycle(cfg, actions, state)
                consecutive_errors = 0
                if count is None:
                    logger.info("No competitor mentions found this cycle")
                else:
                    logger.info(f"[OK] Processed {count} rep(s) with mentions this cycle")
            except (ConnectorUnavailableError, ConnectorError) as e:
                consecutive_errors += 1
                logger.error(f"Gong could not be reached this cycle: {e}")
                if consecutive_errors >= 5:
                    logger.critical("5 consecutive Gong errors, exiting")
                    return 1
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error during cycle: {e}", exc_info=True)
                if consecutive_errors >= 5:
                    logger.critical("5 consecutive errors, exiting")
                    return 1

            for _ in range(cfg.poll_interval_minutes * 60):
                if _shutdown_requested:
                    logger.info("Graceful shutdown")
                    return 130
                time.sleep(1)

    else:
        try:
            count = run_cycle(cfg, actions, state)
        except ConnectorUnavailableError as e:
            logger.error(
                f"Gong is not configured in this Scalekit workspace: {e}\n"
                f"This agent cannot fetch competitor-mention calls until GONG is "
                f"connected. In the Scalekit dashboard, add a Gong connection under "
                f"Agent Auth > Connections, complete its OAuth/API-key flow, then set "
                f"GONG_CONNECTOR to the exact connection name shown there. See README "
                f"Prerequisites for details."
            )
            return 1
        except ConnectorError as e:
            logger.error(f"Gong could not be reached: {e}")
            return 1

        if count is None:
            logger.info("No calls with tracked competitor mentions found in the lookback window")
            return 2
        logger.info(f"[OK] Processed {count} rep(s) with mentions this cycle")
        return 0


def _connector_for_check(actions, connector_name: str, identifier: str):
    """Lightweight wrapper just for the Step 0 auth-check loop."""
    from connectors import Connector
    return Connector(actions, connector_name, identifier)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (signal)")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
