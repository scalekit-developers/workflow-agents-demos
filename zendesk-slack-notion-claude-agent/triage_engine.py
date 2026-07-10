"""
Core triage pipeline.

Orchestrates the complete ticket triage workflow:
1. Fetch tickets
2. Classify
3. Search KB
4. Route to Slack
5. Update Zendesk
"""

import logging
import time
from typing import Dict, List

logger = logging.getLogger(__name__)


class TriageEngine:
    """Orchestrate ticket triage pipeline."""

    def __init__(
        self,
        zendesk_conn,
        slack_conn,
        notion_conn,
        classifier,
        state_manager,
        config,
    ):
        self.zendesk = zendesk_conn
        self.slack = slack_conn
        self.notion = notion_conn
        self.classifier = classifier
        self.state = state_manager
        self.config = config

    def triage_ticket(self, ticket: Dict) -> bool:
        """
        Run the full triage pipeline on a single ticket.
        Returns True on success, False on failure.
        """
        from formatter import build_slack_message, build_zendesk_internal_note

        raw_id = ticket.get("id", "?")
        ticket_id = str(int(raw_id)) if isinstance(raw_id, (int, float)) else str(raw_id)
        subject = ticket.get("subject") or ticket.get("raw_subject") or "No subject"
        description = ticket.get("description") or ""

        # Mark as processed BEFORE triaging (prevents duplicates on crash)
        self.state.mark_processed(ticket_id)

        if not description:
            full = self.zendesk.get_ticket(ticket_id)
            description = full.get("description") or ""
            ticket = {**ticket, **full}

        # ── Step 2: Classify ──────────────────────────────────────────────
        logger.info(f"Step 2: Classifying ticket #{ticket_id}")

        classification = self.classifier.classify(subject, description)
        cat = classification["category"]
        sev = classification["severity"]
        logger.info(f"Category: {cat} | Severity: {sev} | Subject: {subject[:60]}")

        # ── Step 3: Search Notion KB ──────────────────────────────────────
        logger.debug(f"Step 3: Searching Notion KB")
        kb_articles: List[Dict] = []
        if cat in ("how_to", "bug", "account_issue"):
            search_query = classification.get("summary") or subject
            pages = self.notion.search_kb(search_query, self.config.notion_db_id)
            kb_articles = self.notion.extract_articles(pages)

            if kb_articles:
                logger.info(f"Found {len(kb_articles)} matching KB article(s)")
                for art in kb_articles[:3]:
                    logger.debug(f"  • {art['title']}")
            else:
                logger.debug(f"No matching KB articles found")
        else:
            logger.debug(f"Skipped KB search (category '{cat}' does not require it)")

        # ── Step 4: Route to Slack ────────────────────────────────────────
        logger.debug(f"Step 4: Routing to Slack")
        channel = self.config.channel_map.get(cat, self.config.fallback_channel)
        message = build_slack_message(ticket, classification, kb_articles, self.config.severity_priority)
        result = self.slack.send_message(channel, message)
        if result:
            logger.info(f"Routed to Slack channel: {channel}")
        else:
            if channel != self.config.fallback_channel:
                logger.debug(f"Retrying on fallback channel...")
                result = self.slack.send_message(self.config.fallback_channel, message)
            if not result:
                logger.warning(f"Failed to route to Slack")

        # ── Step 5: Update Zendesk ────────────────────────────────────────
        logger.debug(f"Step 5: Updating Zendesk ticket")
        priority = self.config.severity_priority.get(sev, "normal")
        tags = [f"auto_category:{cat}", f"auto_severity:{sev}", "triaged_by_agent"]

        self.zendesk.update_ticket(int(ticket_id), tags, priority)

        note = build_zendesk_internal_note(classification, kb_articles, self.config.severity_priority)
        self.zendesk.add_internal_note(str(ticket_id), note)

        logger.info(f"✓ Ticket #{ticket_id} triaged successfully")
        return True

    def run_once(self) -> int:
        """
        Run a single triage cycle.
        Returns number of tickets processed, or -1 on error.
        """
        logger.info("Step 1: Fetching new Zendesk tickets")

        tickets = self.zendesk.search_tickets()
        unprocessed = self.state.get_unprocessed_tickets(tickets)

        if not unprocessed:
            logger.info("No new tickets found")
            return 0

        logger.info(f"Found {len(unprocessed)} new ticket(s)")

        for i, ticket in enumerate(unprocessed):
            try:
                self.triage_ticket(ticket)
            except Exception as e:
                logger.exception(f"Error triaging ticket: {e}")
                continue

            # Brief pause between tickets to respect API rate limits
            if i < len(unprocessed) - 1:
                time.sleep(1.5)

        return len(unprocessed)
