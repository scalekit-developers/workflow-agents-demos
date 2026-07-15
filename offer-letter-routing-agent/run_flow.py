"""
Offer Letter & Comp Routing Agent: PandaDoc → Slack (approval gate) → Gmail

Scalekit handles OAuth for all connectors, scoped to the requesting
recruiter's own identity — never a shared HR bot account.

Flow:
  1. Validate the offer request (candidate, comp, dates)
  2. Generate the offer document in PandaDoc from a template (left in Draft —
     not sent yet)
  3. Post an approval request to the hiring manager in Slack and POLL for a
     ✅/❌ reaction before doing anything the candidate would see. This is a
     real gate: send happens only after approval, not just a notification
     that happened to arrive after the fact.
  4. If approved: send the document for e-signature, then email the
     candidate the actual signable link. If rejected or the approval times
     out, the document stays in Draft in PandaDoc and nothing goes to the
     candidate.

Set REQUIRE_APPROVAL=false in .env to skip the gate and go back to
notify-only behavior (post to Slack, send regardless).

Requires a real PandaDoc template UUID (PANDADOC_TEMPLATE_UUID) — PandaDoc's
Markdown-based document creation tool is not currently implemented on
PandaDoc's live MCP server (confirmed 2026-07-15; see README "Known
limitations"), so there is no template-free fallback path.

The approval gate requires a SLACKMCP connection in Scalekit (distinct from
the plain SLACK connector) — only SLACKMCP can read reactions back. See
README "Approval gate setup".

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py --candidate-first Alex --candidate-last Chen \\
      --email alex.chen@example.com --role "Staff Engineer" \\
      --salary 180000 --start-date 2026-08-03
"""
import sys
import time
import argparse
from dotenv import load_dotenv
import scalekit.client
from settings import get_settings
from logging_config import setup_logging
from auth import ensure_authorized
from validation import validate_offer_request, ValidationError
from approval_gate import wait_for_approval
from connectors.pandadoc import PandaDocConnector
from connectors.slack import SlackConnector
from connectors.slack_mcp import SlackMCPConnector
from connectors.gmail import send_message as gmail_send_message


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

    try:
        sk = scalekit.client.ScalekitClient(
            client_id=settings.SCALEKIT_CLIENT_ID,
            client_secret=settings.SCALEKIT_CLIENT_SECRET,
            env_url=settings.SCALEKIT_ENV_URL,
        )
    except Exception as e:
        logger.error(
            f"Failed to connect to Scalekit at {settings.SCALEKIT_ENV_URL}: {e}\n"
            f"Check SCALEKIT_ENV_URL, SCALEKIT_CLIENT_ID, and SCALEKIT_CLIENT_SECRET in .env"
        )
        sys.exit(1)

    return settings, logger, sk.connect


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send an offer letter and route it for approval.")
    parser.add_argument("--candidate-first", required=True, help="Candidate first name")
    parser.add_argument("--candidate-last", required=True, help="Candidate last name")
    parser.add_argument("--email", required=True, help="Candidate email address")
    parser.add_argument("--role", required=True, help="Role title, e.g. 'Staff Engineer'")
    parser.add_argument("--salary", required=True, help="Base salary, e.g. 180000 or 180k")
    parser.add_argument("--start-date", required=True, help="Start date, YYYY-MM-DD")
    parser.add_argument(
        "--hiring-manager",
        default=None,
        help="Slack user ID to route approval to (overrides SLACK_HIRING_MANAGER_ID)",
    )
    return parser.parse_args()


def main() -> int:
    """Run the full pipeline."""
    args = parse_args()  # parsed first so --help works without needing .env configured
    settings, logger, connect = _init()

    try:
        # Step 0: Validate input
        logger.info("Step 0: Validating offer request")
        try:
            offer = validate_offer_request(
                {
                    "candidate_first_name": args.candidate_first,
                    "candidate_last_name": args.candidate_last,
                    "candidate_email": args.email,
                    "role_title": args.role,
                    "base_salary": args.salary,
                    "start_date": args.start_date,
                }
            )
        except ValidationError as e:
            logger.error(f"Invalid offer request: {e}")
            return 1

        logger.info(
            f"Candidate: {offer.candidate_first_name} {offer.candidate_last_name} | "
            f"Role: {offer.role_title} | Salary: {offer.base_salary} | Start: {offer.start_date}"
        )

        # Step 1: Auth check — every call below is scoped to the recruiter's own identity
        # (per-connector, since a recruiter may have authorized each service under
        # a different identifier)
        logger.info("Step 1: Checking connector authorization")
        ensure_authorized(connect, settings.PANDADOC_CONNECTOR, settings.PANDADOC_USER)
        ensure_authorized(connect, settings.SLACK_CONNECTOR, settings.SLACK_USER)
        ensure_authorized(connect, settings.GMAIL_CONNECTOR, settings.GMAIL_USER)
        if settings.REQUIRE_APPROVAL:
            ensure_authorized(connect, settings.SLACKMCP_CONNECTOR, settings.SLACKMCP_USER)

        # Step 2: Generate the offer document in PandaDoc (left in Draft)
        logger.info("Step 2: Generating offer document in PandaDoc")
        pandadoc = PandaDocConnector(connect, settings.PANDADOC_USER, settings.PANDADOC_CONNECTOR)
        doc_name = f"Offer - {offer.candidate_first_name} {offer.candidate_last_name} - {offer.role_title}"

        document = pandadoc.create_from_template(
            template_uuid=settings.PANDADOC_TEMPLATE_UUID,
            name=doc_name,
            candidate_email=offer.candidate_email,
            candidate_first_name=offer.candidate_first_name,
            candidate_last_name=offer.candidate_last_name,
            recipient_role=settings.PANDADOC_RECIPIENT_ROLE,
            tokens={
                "candidate_name": f"{offer.candidate_first_name} {offer.candidate_last_name}",
                "role_title": offer.role_title,
                "base_salary": offer.base_salary,
                "start_date": offer.start_date,
                "company_name": settings.COMPANY_NAME,
            },
        )

        if not document or not document.get("id"):
            logger.error("Failed to create offer document — aborting")
            return 1

        document_id = document["id"]
        document_url = document.get("document_url", "")

        # PandaDoc processes newly-created documents asynchronously (status
        # starts as "Uploaded" and moves to "Draft" once ready) — sending
        # before that transition completes can fail, so poll briefly first.
        logger.debug("Waiting for PandaDoc to finish processing the document")
        for attempt in range(10):
            status = pandadoc.get_status(document_id)
            if status and status.lower() != "uploaded":
                break
            time.sleep(1)
        else:
            logger.warning(
                f"Document {document_id} still 'Uploaded' after 10s — continuing anyway"
            )

        destination = args.hiring_manager or settings.SLACK_HIRING_MANAGER_ID or settings.SLACK_APPROVALS_CHANNEL
        slack = SlackConnector(connect, settings.SLACK_USER, settings.SLACK_CONNECTOR)

        if settings.REQUIRE_APPROVAL:
            # Step 3: Post approval request and BLOCK until the hiring manager
            # reacts — nothing is sent to the candidate until this resolves.
            logger.info("Step 3: Requesting approval from hiring manager in Slack")
            if not destination:
                logger.error(
                    "REQUIRE_APPROVAL=true but no Slack destination configured "
                    "(SLACK_HIRING_MANAGER_ID or SLACK_APPROVALS_CHANNEL) — aborting"
                )
                return 1

            slack_mcp = SlackMCPConnector(connect, settings.SLACKMCP_USER, settings.SLACKMCP_CONNECTOR)
            message = slack.format_approval_request(
                candidate_name=f"{offer.candidate_first_name} {offer.candidate_last_name}",
                role_title=offer.role_title,
                base_salary=offer.base_salary,
                start_date=offer.start_date,
                document_id=document_id,
                document_url=document_url,
            )
            posted = slack_mcp.send_message(destination, message)
            if not posted or not posted.get("message_ts"):
                logger.error("Failed to post approval request to Slack — aborting")
                return 1

            logger.info(
                f"Waiting up to {settings.APPROVAL_TIMEOUT_SECONDS}s for a "
                f":{settings.APPROVE_EMOJI}: or :{settings.REJECT_EMOJI}: reaction"
            )
            result = wait_for_approval(
                get_reactions=lambda: slack_mcp.get_reaction_emojis(destination, posted["message_ts"]),
                approve_emoji=settings.APPROVE_EMOJI,
                reject_emoji=settings.REJECT_EMOJI,
                poll_interval_seconds=settings.APPROVAL_POLL_INTERVAL_SECONDS,
                timeout_seconds=settings.APPROVAL_TIMEOUT_SECONDS,
            )

            if result.timed_out:
                slack.send_message(
                    destination,
                    f"⏱️ Offer for {offer.candidate_first_name} {offer.candidate_last_name} "
                    f"timed out waiting for approval — document left in Draft in PandaDoc.",
                )
                logger.warning("Approval timed out — offer NOT sent to candidate")
                return 3
            if not result.approved:
                slack.send_message(
                    destination,
                    f"🚫 Offer for {offer.candidate_first_name} {offer.candidate_last_name} "
                    f"was rejected — document left in Draft in PandaDoc.",
                )
                logger.warning("Approval rejected — offer NOT sent to candidate")
                return 4
        else:
            logger.warning("REQUIRE_APPROVAL=false — skipping approval gate")
            if destination:
                message = slack.format_approval_request(
                    candidate_name=f"{offer.candidate_first_name} {offer.candidate_last_name}",
                    role_title=offer.role_title,
                    base_salary=offer.base_salary,
                    start_date=offer.start_date,
                    document_id=document_id,
                    document_url=document_url,
                )
                slack.send_message(destination, message)

        # Step 4: Send for e-signature (only reached if approved, or gate disabled)
        logger.info("Step 4: Sending offer document to candidate for e-signature")
        sent = pandadoc.send(
            document_id=document_id,
            subject=f"Your offer from {settings.COMPANY_NAME}: {offer.role_title}",
            message=(
                f"Hi {offer.candidate_first_name}, we're excited to offer you the "
                f"{offer.role_title} role. Please review and sign at your convenience."
            ),
        )
        if not sent:
            logger.error("Failed to send document — it remains in draft in PandaDoc")
            # Not fatal: the doc exists and can be sent manually.

        # Step 5: Email the candidate the actual offer document link
        logger.info("Step 5: Emailing the offer document to the candidate")
        gmail_send_message(
            connect,
            settings.GMAIL_USER,
            connection_name=settings.GMAIL_CONNECTOR,
            to=offer.candidate_email,
            subject=f"Your offer from {settings.COMPANY_NAME}: {offer.role_title}",
            body=(
                f"Hi {offer.candidate_first_name},\n\n"
                f"Congratulations! We're excited to offer you the {offer.role_title} role "
                f"at {settings.COMPANY_NAME}.\n\n"
                f"Your offer document is ready for review and e-signature:\n"
                f"{document_url or '(check your email from PandaDoc for the signing link)'}\n\n"
                f"Please also check your inbox for a separate email from PandaDoc with the "
                f"secure signing link.\n\n"
                f"Welcome to the team!\n\n"
                f"{settings.COMPANY_NAME}"
            ),
        )

        logger.info(f"Offer flow complete ✓ (document_id={document_id})")
        return 0

    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception:
        logger.exception("Pipeline failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
