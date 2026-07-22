"""
State management for processed review-cycle tracking.

Prevents re-notifying a manager on Slack for a review period that has
already been summarized and written to Notion in a prior run.
"""

import json
import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)


class StateManager:
    """Tracks which (manager, review_period) cycles have already been processed."""

    def __init__(self, state_file: Path = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "processed_cycles.json"
        self.state_file = state_file
        self._processed: Set[str] = set()
        self.load()

    @staticmethod
    def _key(manager_email: str, review_period: str) -> str:
        return f"{manager_email.strip().lower()}::{review_period.strip().lower()}"

    def load(self) -> None:
        if self.state_file.exists():
            try:
                self._processed = set(json.loads(self.state_file.read_text()))
                logger.debug(f"Loaded {len(self._processed)} processed cycle(s)")
            except (json.JSONDecodeError, TypeError):
                logger.warning("State file corrupted, starting fresh")
                self._processed = set()
        else:
            logger.debug("No state file found, starting fresh")
            self._processed = set()

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self._processed), indent=2))
        tmp.replace(self.state_file)  # atomic on POSIX

    def is_processed(self, manager_email: str, review_period: str) -> bool:
        return self._key(manager_email, review_period) in self._processed

    def mark_processed(self, manager_email: str, review_period: str) -> None:
        self._processed.add(self._key(manager_email, review_period))
        self.save()
