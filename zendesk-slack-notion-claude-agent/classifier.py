"""
Ticket classification using LLM or rule-based fallback.

Classifies tickets into category + severity.
Gracefully falls back if LLM is unavailable.
"""

import os
import json
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class TicketClassifier:
    """Classify support tickets by category and severity."""

    VALID_CATEGORIES = {"billing", "bug", "feature_request", "how_to", "account_issue"}
    VALID_SEVERITIES = {"P0", "P1", "P2", "P3"}

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

    def classify(self, subject: str, description: str) -> Dict:
        """
        Classify a ticket into category + severity.
        Returns {"category": "...", "severity": "P0"-"P3", "summary": "...", "suggested_response": "..."}.
        """
        if self.api_key:
            try:
                return self._classify_with_llm(subject, description)
            except Exception as e:
                logger.warning(f"LLM classification failed ({e.__class__.__name__}: {e}) -- using rule-based")
                return self._classify_rule_based(subject, description)
        else:
            logger.debug("No LLM API key, using rule-based classification")
            return self._classify_rule_based(subject, description)

    def _classify_with_llm(self, subject: str, description: str) -> Dict:
        """Use OpenRouter LLM for classification."""
        import requests as http

        prompt = f"""You are a support ticket classifier. Analyze this ticket and return ONLY valid JSON with these exact keys:
- category (one of: billing, bug, feature_request, how_to, account_issue)
- severity (one of: P0, P1, P2, P3)
  P0 = service down / data loss / security breach affecting multiple users
  P1 = major feature broken, workaround exists but painful
  P2 = minor issue, cosmetic, or single-user impact
  P3 = question, enhancement idea, or low-impact ask
- summary (one sentence explaining the core issue)
- suggested_response (2-3 sentence draft reply to the customer)

Ticket subject: {subject}

Ticket description:
{description[:3000]}"""

        resp = http.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # Extract JSON from markdown code fences if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        start, end = raw.find("{"), raw.rfind("}") + 1
        result = json.loads(raw[start:end])

        # Validate fields
        if result.get("category") not in self.VALID_CATEGORIES:
            result["category"] = "account_issue"
        if result.get("severity") not in self.VALID_SEVERITIES:
            result["severity"] = "P2"

        logger.debug("LLM classification OK")
        return result

    def _classify_rule_based(self, subject: str, description: str) -> Dict:
        """Keyword-based fallback classifier."""
        text = f"{subject} {description}".lower()

        # Category detection
        if any(w in text for w in ("invoice", "charge", "billing", "refund", "payment", "subscription", "plan", "upgrade")):
            category = "billing"
        elif any(w in text for w in ("error", "bug", "crash", "broken", "not working", "500", "exception", "fails", "failure")):
            category = "bug"
        elif any(w in text for w in ("feature", "request", "would be nice", "suggest", "enhancement", "wishlist", "add support")):
            category = "feature_request"
        elif any(w in text for w in ("how to", "how do i", "tutorial", "guide", "documentation", "help me", "instructions")):
            category = "how_to"
        else:
            category = "account_issue"

        # Severity detection
        if any(w in text for w in ("down", "outage", "data loss", "security", "breach", "critical", "emergency", "all users")):
            severity = "P0"
        elif any(w in text for w in ("broken", "can't login", "cannot access", "blocking", "urgent", "major")):
            severity = "P1"
        elif any(w in text for w in ("minor", "cosmetic", "typo", "slow", "intermittent")):
            severity = "P2"
        else:
            severity = "P3" if category in ("feature_request", "how_to") else "P2"

        return {
            "category": category,
            "severity": severity,
            "summary": f"{category.replace('_', ' ').title()} ticket: {subject[:80]}",
            "suggested_response": "Thank you for reaching out. Our team is reviewing your request and will follow up shortly.",
        }
