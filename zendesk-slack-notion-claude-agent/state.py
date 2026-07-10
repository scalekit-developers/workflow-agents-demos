"""
State management for processed ticket tracking.

Maintains a list of processed ticket IDs to prevent duplicates
across polling cycles. Uses atomic file writes.
"""

import json
import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)


class StateManager:
    """Manage processed ticket state."""

    def __init__(self, state_file: Path = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "processed_tickets.json"
        self.state_file = state_file
        self.max_ids = 5000
        self._processed_ids: Set[str] = set()
        self.load()

    def load(self) -> None:
        """Load processed ticket IDs from disk."""
        if self.state_file.exists():
            try:
                self._processed_ids = set(json.loads(self.state_file.read_text()))
                logger.debug(f"Loaded {len(self._processed_ids)} processed ticket IDs")
            except (json.JSONDecodeError, TypeError):
                logger.warning("State file corrupted, starting fresh")
                self._processed_ids = set()
        else:
            logger.debug("No state file found, starting fresh")
            self._processed_ids = set()

    def save(self) -> None:
        """Save processed ticket IDs to disk (atomic write)."""
        if len(self._processed_ids) > self.max_ids:
            # Keep only the most recent IDs (highest ticket numbers)
            self._processed_ids = set(
                sorted(self._processed_ids, key=lambda x: int(x) if x.isdigit() else 0)[
                    -self.max_ids :
                ]
            )
            logger.info(f"Pruned state to {self.max_ids} most recent IDs")

        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self._processed_ids)))
        tmp.replace(self.state_file)  # atomic on POSIX

    def mark_processed(self, ticket_id: str) -> None:
        """Mark a ticket as processed."""
        self._processed_ids.add(str(ticket_id))
        self.save()

    def is_processed(self, ticket_id: str) -> bool:
        """Check if a ticket has been processed."""
        return str(ticket_id) in self._processed_ids

    def get_unprocessed_tickets(self, tickets: list) -> list:
        """Filter tickets to only unprocessed ones."""
        def _normalize_id(raw) -> str:
            return str(int(raw)) if isinstance(raw, (int, float)) else str(raw)

        return [t for t in tickets if not self.is_processed(_normalize_id(t.get("id", "")))]
