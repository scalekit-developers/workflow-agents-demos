"""
State management for processed forecast-cycle tracking.

Prevents re-posting the same forecast commentary to Slack for a
(analyst, forecast_period) pair that has already been processed in a prior
run. Google Sheets logging is append-only and safe to run multiple times
(each run adds a new snapshot row), but the state guard still exists to keep
the Slack post -- the user-visible side effect -- from being duplicated on
an accidental re-run within the same period.
"""

import json
import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)


class StateManager:
    """Tracks which (analyst, forecast_period) cycles have already been processed."""

    def __init__(self, state_file: Path = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "processed_periods.json"
        self.state_file = state_file
        self._processed: Set[str] = set()
        self.load()

    @staticmethod
    def _key(analyst_email: str, forecast_period: str) -> str:
        return f"{analyst_email.strip().lower()}::{forecast_period.strip().lower()}"

    def load(self) -> None:
        if self.state_file.exists():
            try:
                self._processed = set(json.loads(self.state_file.read_text()))
                logger.debug(f"Loaded {len(self._processed)} processed period(s)")
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

    def is_processed(self, analyst_email: str, forecast_period: str) -> bool:
        return self._key(analyst_email, forecast_period) in self._processed

    def mark_processed(self, analyst_email: str, forecast_period: str) -> None:
        self._processed.add(self._key(analyst_email, forecast_period))
        self.save()
