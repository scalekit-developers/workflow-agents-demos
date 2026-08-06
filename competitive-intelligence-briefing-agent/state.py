"""
State management for idempotency on rep briefing DMs.

Key shape and why
------------------
Each Slack DM this agent sends is a per-rep DIGEST covering every call and
competitor mention that rep had in the current lookback window, not one DM
per call (see aggregator.py). The idempotency question is therefore not
"has this exact digest content been sent before" (a content fingerprint,
like revenue-forecast-commentary-agent's stage-aggregate comparison) but
"has this rep already been told about THIS specific call+competitor
mention" (an identity question, like pto-leave-request-agent's per-request
fingerprint) -- because the right behavior on a re-run with a sliding
lookback window is per-mention suppression, not all-or-nothing digest
suppression:

  - A rep's digest is rebuilt fresh every run from whatever calls currently
    fall inside LOOKBACK_DAYS.
  - A call+competitor mention already DMed in a prior run (still inside the
    window because polling reruns before it ages out) must NOT cause a
    second DM for that same mention.
  - A brand-new call+competitor mention that appears on a later run (a call
    that happened since the last run, or a new competitor mention surfaced
    on a call already seen) SHOULD still reach the rep, even though that
    rep already received an earlier digest containing OTHER, already-seen
    mentions.

A single whole-digest content fingerprint (recompute a hash over everything
currently in-window and compare to "last sent") cannot satisfy the third
requirement: the moment one new mention enters an otherwise-unchanged
window, the fingerprint changes and the ENTIRE digest re-sends, duplicating
DMs for mentions the rep already saw. A per-(rep, call, competitor) key
avoids this: each mention is marked processed independently the first time
it is actually included in a sent DM, so run_flow.py can filter each rep's
candidate mention list down to only the ones not yet marked before building
that run's digest text, and a partially-new digest never re-includes old
mentions or blocks new ones.

Key: sha256(rep_identifier + "::" + gong_call_id + "::" + competitor_name),
normalized (lowercased rep identifier and competitor name). rep_identifier
is whatever value resolved the rep (email preferred, falling back to the
raw name/Gong user ID Gong returned), so the same physical rep is not
double-counted under two different identifier strings within one run, but
is not guaranteed deduplicated across runs if Gong's participant field
format itself changes (documented limitation, see README).
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)


def compute_mention_key(rep_identifier: str, call_id: str, competitor_name: str) -> str:
    """Build a stable idempotency key for one (rep, call, competitor) mention."""
    payload = {
        "rep": rep_identifier.strip().lower(),
        "call_id": str(call_id).strip(),
        "competitor": competitor_name.strip().lower(),
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class StateManager:
    """
    Tracks which (rep, call, competitor) mentions have already been included
    in a sent Slack DM, so re-running the agent (a retried cron job, or a
    polling cycle whose window still overlaps a prior one) never re-DMs a
    rep about a mention they've already been briefed on.
    """

    def __init__(self, state_file: Optional[Path] = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "briefed_mentions.json"
        self.state_file = state_file
        self._briefed: Set[str] = set()
        self.load()

    def load(self) -> None:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                self._briefed = set(raw) if isinstance(raw, list) else set()
                logger.debug(f"Loaded {len(self._briefed)} previously-briefed mention(s)")
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, FileNotFoundError):
                logger.warning("State file corrupted or unreadable, starting fresh")
                self._briefed = set()
        else:
            logger.debug("No state file found, starting fresh")
            self._briefed = set()

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self._briefed), indent=2))
        tmp.replace(self.state_file)  # atomic on POSIX

    def is_briefed(self, key: str) -> bool:
        return key in self._briefed

    def mark_briefed(self, key: str) -> None:
        self._briefed.add(key)
        self.save()

    def mark_many_briefed(self, keys) -> None:
        """Mark a batch of keys briefed with a single disk write (one DM covers many mentions)."""
        self._briefed.update(keys)
        self.save()
