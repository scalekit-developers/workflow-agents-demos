"""
Aggregation logic: turn a Salesforce opportunity record and a batch of Slack
message excerpts into one deal summary, ready to sync to the deal room doc.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# How many Slack excerpts to include in the synced summary. Keeps the comment
# readable; the search itself may return more, but only the top N (assumed
# most relevant, since Slack sorts by relevance by default) are surfaced.
_MAX_SLACK_EXCERPTS = 5

# Excerpts longer than this are truncated with an ellipsis, since deal room
# comments are meant to be a scannable digest, not a full transcript dump.
_MAX_EXCERPT_CHARS = 600


class DealContext:
    """Everything gathered about one opportunity for this sync cycle."""

    def __init__(self, opportunity: Dict):
        self.opportunity = opportunity or {}
        self.slack_excerpts: List[str] = []

    @property
    def opportunity_id(self) -> str:
        return self.opportunity.get("Id", "")

    @property
    def name(self) -> str:
        return self.opportunity.get("Name", "Unknown Opportunity")

    @property
    def account_name(self) -> str:
        account = self.opportunity.get("Account") or {}
        return account.get("Name", "") if isinstance(account, dict) else ""

    @property
    def owner_name(self) -> str:
        owner = self.opportunity.get("Owner") or {}
        return owner.get("Name", "") if isinstance(owner, dict) else ""

    @property
    def stage(self) -> str:
        return self.opportunity.get("StageName", "Unknown")

    @property
    def amount(self) -> Optional[float]:
        amount = self.opportunity.get("Amount")
        return float(amount) if isinstance(amount, (int, float)) else None

    @property
    def close_date(self) -> str:
        return self.opportunity.get("CloseDate", "") or ""

    @property
    def next_step(self) -> str:
        return self.opportunity.get("NextStep", "") or ""

    def add_slack_excerpts(self, excerpts: List[str]) -> None:
        for excerpt in excerpts:
            cleaned = excerpt.strip()
            if cleaned:
                self.slack_excerpts.append(cleaned)

    def has_slack_context(self) -> bool:
        return bool(self.slack_excerpts)


def _format_amount(amount: Optional[float]) -> str:
    if amount is None:
        return "Not set"
    return f"${amount:,.0f}"


def _truncate(text: str, max_chars: int = _MAX_EXCERPT_CHARS) -> str:
    text = " ".join(text.split())  # collapse whitespace/newlines for a compact excerpt
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def build_deal_summary(deal: DealContext, ae_email: str, sync_label: str = "") -> str:
    """
    Render the full deal summary text that gets posted as a Google Drive
    comment on the deal room doc. Plain text (Drive comments do not render
    Markdown), so headings are plain lines rather than "#"/"**" syntax.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    label = f" ({sync_label})" if sync_label else ""

    lines = [f"DEAL ROOM SYNC{label} -- {timestamp}", ""]

    lines.append(f"Opportunity: {deal.name}")
    if deal.account_name:
        lines.append(f"Account: {deal.account_name}")
    lines.append(f"Stage: {deal.stage}")
    lines.append(f"Amount: {_format_amount(deal.amount)}")
    lines.append(f"Close Date: {deal.close_date or 'Not set'}")
    if deal.owner_name:
        lines.append(f"Owner: {deal.owner_name}")
    lines.append(f"Synced by: {ae_email}")
    lines.append("")

    lines.append("Next Steps:")
    lines.append(f"  {deal.next_step or 'No next step recorded in Salesforce.'}")
    lines.append("")

    if deal.has_slack_context():
        lines.append(f"Recent Slack Discussion ({len(deal.slack_excerpts[:_MAX_SLACK_EXCERPTS])} excerpt(s)):")
        for i, excerpt in enumerate(deal.slack_excerpts[:_MAX_SLACK_EXCERPTS], start=1):
            lines.append(f"  [{i}] {_truncate(excerpt)}")
    else:
        lines.append("Recent Slack Discussion: none found for this cycle.")

    return "\n".join(lines)
