"""Centralized configuration with fail-fast validation."""
import os
from typing import Optional


class Settings:
    """Load and validate all environment variables at startup."""

    def __init__(self):
        self.SCALEKIT_ENV_URL = os.environ.get("SCALEKIT_ENV_URL")
        self.SCALEKIT_CLIENT_ID = os.environ.get("SCALEKIT_CLIENT_ID")
        self.SCALEKIT_CLIENT_SECRET = os.environ.get("SCALEKIT_CLIENT_SECRET")

        # Identity of the recruiter the agent acts on behalf of.
        # Each connector call is scoped to this identity — never a shared HR bot account.
        # Per-connector overrides exist because a recruiter may have authorized
        # each connector under a different identifier (e.g. different email
        # aliases per service) — each falls back to RECRUITER_USER if unset.
        self.RECRUITER_USER = os.environ.get("RECRUITER_USER")
        self.PANDADOC_USER = os.environ.get("PANDADOC_USER") or self.RECRUITER_USER
        self.SLACK_USER = os.environ.get("SLACK_USER") or self.RECRUITER_USER
        self.GMAIL_USER = os.environ.get("GMAIL_USER") or self.RECRUITER_USER

        self.PANDADOC_CONNECTOR = os.environ.get("PANDADOC_CONNECTOR", "pandadocmcp")
        self.GMAIL_CONNECTOR = os.environ.get("GMAIL_CONNECTOR", "gmail")
        self.SLACK_CONNECTOR = os.environ.get("SLACK_CONNECTOR", "slack")
        # SLACKMCP is a separate connector from SLACK — it's the only one that
        # can read reactions back, which the approval gate polls on.
        self.SLACKMCP_CONNECTOR = os.environ.get("SLACKMCP_CONNECTOR", "slackmcp")
        self.SLACKMCP_USER = os.environ.get("SLACKMCP_USER") or self.RECRUITER_USER

        # Approval gate: require an explicit reaction from the hiring manager
        # before the offer is actually sent to the candidate. Set
        # REQUIRE_APPROVAL=false to skip the gate entirely (old notify-only behavior).
        self.REQUIRE_APPROVAL = os.environ.get("REQUIRE_APPROVAL", "true").lower() not in ("false", "0", "no")
        self.APPROVE_EMOJI = os.environ.get("APPROVE_EMOJI", "white_check_mark")
        self.REJECT_EMOJI = os.environ.get("REJECT_EMOJI", "x")
        self.APPROVAL_POLL_INTERVAL_SECONDS = int(os.environ.get("APPROVAL_POLL_INTERVAL_SECONDS", "30"))
        self.APPROVAL_TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", str(30 * 60)))

        # PandaDoc: a real template is required — PandaDoc's live MCP server does
        # not currently implement Markdown-based document creation (see README
        # "Known limitations"), so there is no template-free path.
        self.PANDADOC_TEMPLATE_UUID = os.environ.get("PANDADOC_TEMPLATE_UUID")
        # Must match a recipient role name that exists on that template.
        # PandaDoc's own default role name is "Client", not "Signer".
        self.PANDADOC_RECIPIENT_ROLE = os.environ.get("PANDADOC_RECIPIENT_ROLE", "Client")

        # Slack: where approval requests are routed. Can be overridden per-request
        # with a specific hiring manager Slack user ID.
        self.SLACK_HIRING_MANAGER_ID = os.environ.get("SLACK_HIRING_MANAGER_ID")
        self.SLACK_APPROVALS_CHANNEL = os.environ.get("SLACK_APPROVALS_CHANNEL")

        self.COMPANY_NAME = os.environ.get("COMPANY_NAME", "Our Company")

        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    def validate(self) -> None:
        """Fail fast if required vars are missing."""
        missing = []

        if not self.SCALEKIT_ENV_URL:
            missing.append("SCALEKIT_ENV_URL")
        if not self.SCALEKIT_CLIENT_ID:
            missing.append("SCALEKIT_CLIENT_ID")
        if not self.SCALEKIT_CLIENT_SECRET:
            missing.append("SCALEKIT_CLIENT_SECRET")

        if not self.RECRUITER_USER:
            missing.append("RECRUITER_USER")
        if not self.PANDADOC_USER:
            missing.append("PANDADOC_USER (or RECRUITER_USER)")
        if not self.SLACK_USER:
            missing.append("SLACK_USER (or RECRUITER_USER)")
        if not self.GMAIL_USER:
            missing.append("GMAIL_USER (or RECRUITER_USER)")

        if not self.PANDADOC_TEMPLATE_UUID:
            missing.append(
                "PANDADOC_TEMPLATE_UUID (a real PandaDoc Template UUID — "
                "not a Document ID; see README 'PandaDoc template setup')"
            )

        # At least one Slack destination is required to route approvals.
        if not self.SLACK_HIRING_MANAGER_ID and not self.SLACK_APPROVALS_CHANNEL:
            missing.append("SLACK_HIRING_MANAGER_ID or SLACK_APPROVALS_CHANNEL")

        if self.REQUIRE_APPROVAL and not self.SLACKMCP_USER:
            missing.append("SLACKMCP_USER (or RECRUITER_USER) — required because REQUIRE_APPROVAL=true")

        if missing:
            raise ValueError(
                f"Missing required env vars:\n"
                f"  {', '.join(missing)}\n"
                f"Copy .env.example to .env and fill in all values."
            )


# Global instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.validate()
    return _settings
