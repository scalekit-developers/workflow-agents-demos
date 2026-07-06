"""
Post-Meeting Action Agent: Granola → HubSpot → Gmail → Slack

Scalekit handles OAuth for all four connectors — no manual token management.
LLM extraction (OpenRouter) is used if OPENROUTER_API_KEY is set; falls back
to rule-based parser if the key is missing or the call fails.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py
"""
import sys
from dotenv import load_dotenv
import scalekit.client
from settings import get_settings
from logging_config import setup_logging
from auth import ensure_authorized
from extraction import extract_meeting_info
from connectors.granola import GranolaConnector
from connectors.hubspot import HubSpotConnector
from connectors.slack import SlackConnector
from connectors.gmail import create_draft

load_dotenv()

# Load settings early
try:
    settings = get_settings()
    logger = setup_logging(settings.LOG_LEVEL)
except ValueError as e:
    logger = setup_logging()
    logger.error(str(e))
    sys.exit(1)

# Initialize Scalekit client
sk = scalekit.client.ScalekitClient(
    client_id=settings.SCALEKIT_CLIENT_ID,
    client_secret=settings.SCALEKIT_CLIENT_SECRET,
    env_url=settings.SCALEKIT_ENV_URL,
)
connect = sk.connect


def main() -> int:
    """Run the full pipeline."""
    try:
        # Step 0: Auth check
        logger.info("Step 0: Checking connector authorization")
        ensure_authorized(connect, "granolamcp", settings.GRANOLA_USER)
        ensure_authorized(connect, "hubspot", settings.HUBSPOT_USER)
        ensure_authorized(connect, "gmail", settings.GMAIL_USER)
        ensure_authorized(connect, settings.SLACK_CONNECTOR, settings.SLACK_USER)

        # Step 1: Fetch meetings
        logger.info("Step 1: Fetching meetings from Granola")
        granola = GranolaConnector(connect, settings.GRANOLA_USER)
        meetings = granola.list_meetings(limit=3)

        if not meetings:
            logger.warning("No meetings found in Granola")
            return 2

        logger.info(f"Found {len(meetings)} meeting(s)")

        meeting_data = []
        for m in meetings:
            content = granola.fetch_meeting_content(m["id"], m["title"])
            if content:
                meeting_data.append({**m, "transcript": content})

        if not meeting_data:
            logger.warning("No meetings with content to process")
            return 2

        # Step 2: Extract & sync to HubSpot
        logger.info("Step 2: Extracting info & syncing to HubSpot")
        hubspot = HubSpotConnector(connect, settings.HUBSPOT_USER)
        processed = []

        for m in meeting_data:
            try:
                logger.debug(f"Processing: {m['title']}")
                info = extract_meeting_info(
                    m["transcript"],
                    m["title"],
                    settings.OPENROUTER_API_KEY,
                    settings.OPENROUTER_MODEL,
                )
                logger.info(
                    f"Company: {info['company']} | Stage: {info['deal_stage']} | Amount: ${info['amount'] or 'N/A'}"
                )

                deal_id, deal_name = hubspot.find_or_create_deal(
                    info["company"], info["deal_name"], info["deal_stage"], info["amount"]
                )

                action_str = "\n".join(f"• {a}" for a in info["action_items"])
                try:
                    hubspot.update_deal(
                        deal_id,
                        {
                            "dealstage": info["deal_stage"],
                        },
                    )
                except Exception as e:
                    logger.debug(f"Could not update deal properties: {e}")

                processed.append({**m, **info, "deal_id": deal_id, "deal_name": deal_name})

            except Exception as e:
                logger.error(f"Failed to process {m['title']}: {e}")
                continue

        if not processed:
            logger.warning("No deals were synced")
            return 2

        # Step 3: Create Gmail drafts
        logger.info("Step 3: Creating Gmail drafts")
        for p in processed:
            try:
                to = p.get("contact_email") or settings.GMAIL_USER
                subject = p["email_subject"]
                body = p["email_body"]
                draft = create_draft(connect, settings.GMAIL_USER, to=to, subject=subject, body=body)
                if draft:
                    logger.info(
                        f"Draft created: {to} | Subject: {subject} | id: {draft.get('id')}"
                    )
            except Exception as e:
                logger.error(f"Failed to create draft for {p.get('title')}: {e}")
                continue

        # Step 4: Post to Slack
        logger.info("Step 4: Posting summaries to Slack")
        slack = SlackConnector(connect, settings.SLACK_USER)
        for p in processed:
            try:
                message = slack.format_summary(
                    p["title"],
                    p["summary"],
                    p["next_step"],
                    p.get("action_items", []),
                    p["deal_name"],
                    p["deal_id"],
                )
                slack.send_message(settings.SLACK_CHANNEL, message)
            except Exception as e:
                logger.error(f"Failed to post to Slack for {p.get('title')}: {e}")
                continue

        logger.info("Flow complete ✓")
        return 0

    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
