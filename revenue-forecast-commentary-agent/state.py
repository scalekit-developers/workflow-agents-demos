"""
State management for change-detection on the forecast pipeline.

Tracks a content fingerprint of the last posted pipeline snapshot per analyst,
so the agent only posts to Slack when the underlying Salesforce/HubSpot data
has actually changed (new deal, stage move, amount change, at-risk flag
change) -- not just once per calendar period. Google Sheets logging is
append-only and safe to run multiple times regardless of this guard (each
run that reaches Step 4 adds a new snapshot row); the guard only protects the
user-visible Slack post from being duplicated when nothing has changed.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def compute_pipeline_fingerprint(segments: Dict, at_risk_flags: Dict) -> str:
    """
    Build a stable fingerprint of the pipeline snapshot's meaningful content.

    Rounds monetary values to whole dollars so float jitter across runs
    doesn't cause spurious "changes". Sorted keys make the fingerprint
    independent of dict ordering.
    """
    payload = {
        "segments": {
            label: {
                "deal_count": segment.deal_count,
                "total_value": round(segment.total_value),
                "sources": sorted(segment.sources.keys()),
            }
            for label, segment in sorted(segments.items())
        },
        "at_risk": sorted(at_risk_flags.keys()),
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class StateManager:
    """Tracks each analyst's last-posted pipeline fingerprint for change detection."""

    def __init__(self, state_file: Path = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "processed_periods.json"
        self.state_file = state_file
        self._last_fingerprint: Dict[str, str] = {}
        self.load()

    @staticmethod
    def _key(analyst_email: str) -> str:
        return analyst_email.strip().lower()

    def load(self) -> None:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                self._last_fingerprint = raw if isinstance(raw, dict) else {}
                logger.debug(f"Loaded fingerprint state for {len(self._last_fingerprint)} analyst(s)")
            except (json.JSONDecodeError, TypeError):
                logger.warning("State file corrupted, starting fresh")
                self._last_fingerprint = {}
        else:
            logger.debug("No state file found, starting fresh")
            self._last_fingerprint = {}

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._last_fingerprint, indent=2, sort_keys=True))
        tmp.replace(self.state_file)  # atomic on POSIX

    def get_last_fingerprint(self, analyst_email: str) -> Optional[str]:
        return self._last_fingerprint.get(self._key(analyst_email))

    def has_changed(self, analyst_email: str, fingerprint: str) -> bool:
        """True if this fingerprint differs from the last one posted for this analyst."""
        return self.get_last_fingerprint(analyst_email) != fingerprint

    def mark_posted(self, analyst_email: str, fingerprint: str) -> None:
        self._last_fingerprint[self._key(analyst_email)] = fingerprint
        self.save()
