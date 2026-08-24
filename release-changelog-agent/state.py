"""
State management for published changelogs.

Records which release versions have already been published, and where, keyed
by repository. Re-running the agent for a version that is already published
reports the existing page instead of creating a duplicate.

As in the sibling agents, this local file is the FAST path, not the only
guard. Confluence publishing is also checked remotely by searching the space
for a page with the same title, so deleting this file does not produce a
duplicate Confluence page -- it only costs one extra search. Notion has no
equivalent exact-title guarantee (titles are not unique there), so the local
record is the primary guard for Notion and is documented as such in the
README.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class StateManager:
    """Tracks published changelog versions per repository."""

    def __init__(self, state_file: Optional[Path] = None):
        if state_file is None:
            state_file = Path(__file__).parent / "state" / "published_releases.json"
        self.state_file = state_file
        # {repo_key: {version: {"confluence": url, "notion": url}}}
        self._state: Dict[str, Dict[str, Dict[str, str]]] = {}
        self.load()

    @staticmethod
    def _key(owner: str, repo: str) -> str:
        return f"{owner}/{repo}".strip().lower()

    def load(self) -> None:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                self._state = raw if isinstance(raw, dict) else {}
                logger.debug(f"Loaded publish state for {len(self._state)} repo(s)")
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, FileNotFoundError):
                logger.warning("State file corrupted or unreadable, starting fresh")
                self._state = {}
        else:
            logger.debug("No state file found, starting fresh")
            self._state = {}

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True))
        tmp.replace(self.state_file)  # atomic on POSIX

    def _versions(self, owner: str, repo: str) -> Dict[str, Dict[str, str]]:
        bucket = self._state.setdefault(self._key(owner, repo), {})
        # Guard against a hand-edited file holding a non-dict here, which
        # would otherwise raise deep inside the run.
        if not isinstance(bucket, dict):
            bucket = {}
            self._state[self._key(owner, repo)] = bucket
        return bucket

    def get_published(self, owner: str, repo: str, version: str) -> Optional[Dict[str, str]]:
        """Return {"confluence": url, "notion": url} for an already-published version."""
        entry = self._versions(owner, repo).get(version)
        return entry if isinstance(entry, dict) else None

    def mark_published(self, owner: str, repo: str, version: str, target: str, url: str) -> None:
        """
        Record that `version` was published to `target` ("confluence"/"notion").

        Saved per target rather than per version, so a run that publishes to
        Confluence and then fails on Notion still records the Confluence page
        and does not re-create it on the retry.
        """
        versions = self._versions(owner, repo)
        entry = versions.get(version)
        if not isinstance(entry, dict):
            entry = {}
            versions[version] = entry
        entry[target] = url
        self.save()
