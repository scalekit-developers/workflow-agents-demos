"""
State management for processed (opportunity, sync-cycle) tracking.

Prevents redundant Drive-comment syncs for an opportunity that has already
been synced this cycle (e.g. re-running the agent within the same day/hour
in one-time mode).
"""

import json
import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)


class StateManager:
    """Tracks which (opportunity_id, sync_cycle) pairs have already been synced."""

    def __init__(self, state_file: Path = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "synced_cycles.json"
        self.state_file = state_file
        self._processed: Set[str] = set()
        self.load()

    @staticmethod
    def _key(opportunity_id: str, sync_cycle: str) -> str:
        return f"{opportunity_id.strip().lower()}::{sync_cycle.strip().lower()}"

    def load(self) -> None:
        if self.state_file.exists():
            try:
                self._processed = set(json.loads(self.state_file.read_text()))
                logger.debug(f"Loaded {len(self._processed)} synced cycle(s)")
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

    def is_processed(self, opportunity_id: str, sync_cycle: str) -> bool:
        return self._key(opportunity_id, sync_cycle) in self._processed

    def mark_processed(self, opportunity_id: str, sync_cycle: str) -> None:
        self._processed.add(self._key(opportunity_id, sync_cycle))
        self.save()
