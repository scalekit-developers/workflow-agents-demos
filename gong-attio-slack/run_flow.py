"""
Deal Intelligence Agent: Gong to Attio to Slack

Fetches yesterday's sales calls from Gong, cross-references deal data from Attio,
computes risk scores, and posts prioritized risk report to Slack DM.

All OAuth handled by Scalekit Agent Auth. LLM analysis via OpenRouter with
rule-based fallback. No hardcoded data, no manual token management.

Setup:
  cp .env.example .env
  pip install -r requirements.txt
  python run_flow.py
"""
import sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import scalekit.client

from settings import get_settings
from logging_config import setup_logging
from auth import ensure_authorized
from analysis import analyze_call, compute_risk_score
from connectors.gong import GongConnector
from connectors.attio import AttioConnector
from connectors.slack import SlackConnector


def _init():
    """Initialize settings, logging, and Scalekit client."""
    load_dotenv()
    try:
        settings = get_settings()
        logger = setup_logging(settings.LOG_LEVEL)
    except ValueError as e:
        logger = setup_logging()
        logger.error(str(e))
        sys.exit(1)

    sk = scalekit.client.ScalekitClient(
        client_id=settings.SCALEKIT_CLIENT_ID,
        client_secret=settings.SCALEKIT_CLIENT_SECRET,
        env_url=settings.SCALEKIT_ENV_URL,
    )
    return settings, logger, sk.connect


def main() -> int:
    """Run the full pipeline."""
    settings, logger, connect = _init()

    try:
        # Step 0: Auth check
        logger.info("Step 0: Checking connector authorization")
        ensure_authorized(connect, settings.GONG_CONNECTOR, settings.GONG_USER)
        ensure_authorized(connect, settings.ATTIO_CONNECTOR, settings.ATTIO_USER)
        ensure_authorized(connect, settings.SLACK_CONNECTOR, settings.SLACK_USER)

        # Step 1: Fetch calls from Gong
        logger.info("Step 1: Fetching calls from Gong")
        gong = GongConnector(connect, settings.GONG_USER)
        calls = gong.list_calls(limit=10)

        if not calls:
            logger.warning("No calls found in Gong")
            return 2

        logger.info(f"Found {len(calls)} call(s)")

        # Step 2: Analyze calls and enrich with Attio data
        logger.info("Step 2: Analyzing calls and fetching deal data")
        attio = AttioConnector(connect, settings.ATTIO_USER)
        calls_analysis = []

        for call in calls:
            try:
                logger.debug(f"Processing: {call.get('title', 'Unknown')}")
                call_id = call.get("id")
                call_title = call.get("title", "")
                company_name = call.get("company", "")
                transcript = call.get("transcript", "")

                if not transcript:
                    logger.debug(f"Skipping {call_title}: no transcript")
                    continue

                # Analyze call
                analysis = analyze_call(
                    transcript,
                    call_title,
                    settings.OPENROUTER_API_KEY,
                    settings.OPENROUTER_MODEL,
                )
                risk_score = compute_risk_score(analysis)

                # Fetch deal data from Attio
                deals = attio.search_deals(company_name, limit=3)
                deal_id = deals[0].get("id") if deals else None
                deal_name = deals[0].get("name") if deals else company_name

                logger.info(
                    f"Call: {call_title} | Company: {company_name} | "
                    f"Risk: {risk_score} | Sentiment: {analysis.get('sentiment')}"
                )

                calls_analysis.append({
                    "call_id": call_id,
                    "company": company_name,
                    "deal_id": deal_id,
                    "deal_name": deal_name,
                    "risk_score": risk_score,
                    "sentiment": analysis.get("sentiment"),
                    "sentiment_score": analysis.get("sentiment_score"),
                    "engagement_level": analysis.get("engagement_level"),
                    "objections": analysis.get("objections", []),
                    "competitor_mentions": analysis.get("competitor_mentions", []),
                    "summary": analysis.get("summary", ""),
                })

            except Exception as e:
                logger.error(f"Failed to process {call.get('title', 'Unknown')}: {e}")
                continue

        if not calls_analysis:
            logger.warning("No calls successfully analyzed")
            return 2

        # Step 3: Post risk report to Slack
        logger.info("Step 3: Posting risk report to Slack")
        slack = SlackConnector(connect, settings.SLACK_USER)
        report = slack.format_risk_report(calls_analysis)

        try:
            result = slack.send_dm(settings.SLACK_DM_USER, report)
            logger.info(f"Report posted to Slack (ts={result.get('ts', 'N/A')})")
        except Exception as e:
            logger.error(f"Failed to post to Slack: {e}")
            return 1

        logger.info("Flow complete")
        return 0

    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception:
        logger.exception("Pipeline failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
