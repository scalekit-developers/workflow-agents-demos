import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from scalekit import ScalekitClient
from scalekit.core import ScalekitException

from settings import Settings

log = logging.getLogger("poller")

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
PR_LINKS_FILE = STATE_DIR / "pr_linear_links.json"

# Auth errors from Scalekit/the downstream service that mean the token is
# expired or revoked and the user must re-authorize.
_AUTH_ERROR_SIGNALS = ("invalid_auth", "token_expired", "token_revoked", "not_authed")


class ScalekitConnector:
    def __init__(self):
        self.client = ScalekitClient(
            env_url=Settings.SCALEKIT_ENV_URL,
            client_id=Settings.SCALEKIT_CLIENT_ID,
            client_secret=Settings.SCALEKIT_CLIENT_SECRET,
        )
        self._pr_links = self._load_pr_links()

    def _load_pr_links(self) -> Dict[str, Dict[str, Any]]:
        if not PR_LINKS_FILE.exists():
            PR_LINKS_FILE.write_text("{}")
        try:
            return json.loads(PR_LINKS_FILE.read_text())
        except Exception:
            log.warning("Could not parse %s; starting with empty link map.", PR_LINKS_FILE)
            return {}

    def save_links(self) -> None:
        PR_LINKS_FILE.write_text(json.dumps(self._pr_links, indent=2))

    def get_linear_issue_for_pr(self, key: str) -> Optional[str]:
        entry = self._pr_links.get(key)
        return entry.get("linear_issue_id") if entry else None

    def record_pr_issue(self, key: str, linear_issue_id: str, label: str) -> None:
        self._pr_links[key] = {"linear_issue_id": linear_issue_id, "label": label, "ts": time.time()}
        self.save_links()

    def _maybe_generate_auth_link(self, identifier: str, connection_name: Optional[str], error_str: str) -> None:
        """If the error looks like an expired/revoked token, generate and log a
        re-authorization link so the user knows exactly what to do."""
        if not any(sig in error_str for sig in _AUTH_ERROR_SIGNALS):
            return
        try:
            resp = self.client.actions.get_authorization_link(
                identifier=identifier,
                connection_name=connection_name or None,
            )
            link = getattr(resp, "link", None) or getattr(resp, "url", None) or str(resp)
            expiry = getattr(resp, "expiry", None)
            expiry_str = f" (expires {expiry})" if expiry else ""
            log.error(
                "AUTH EXPIRED for identifier=%s connection=%s — re-authorize here%s:\n  %s",
                identifier, connection_name or "auto", expiry_str, link,
            )
        except Exception:
            log.error(
                "AUTH EXPIRED for identifier=%s connection=%s but could not generate re-auth link: %s",
                identifier, connection_name or "auto", traceback.format_exc(),
            )

    def execute_tool(
        self,
        identifier: str,
        tool: str,
        parameters: Dict[str, Any],
        connection_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        backoff = Settings.RETRY_BACKOFF
        for attempt in range(1, Settings.RETRY_ATTEMPTS + 1):
            try:
                log.debug(
                    "execute_tool attempt=%d tool=%s identifier=%s connection_name=%s params=%s",
                    attempt, tool, identifier, connection_name, parameters,
                )
                resp = self.client.actions.execute_tool(
                    tool_input=parameters,
                    tool_name=tool,
                    identifier=identifier,
                    connection_name=connection_name,
                )
                result = resp.data if hasattr(resp, "data") else resp
                log.debug("Tool %s succeeded (type=%s)", tool, type(result).__name__)
                try:
                    preview = json.dumps(
                        result if isinstance(result, (dict, list)) else str(result), indent=2
                    )[:500]
                    log.debug("Response preview: %s", preview)
                except Exception:
                    pass
                return result

            except ScalekitException as e:
                error_str = str(e).lower()
                log.warning("ScalekitException on attempt %d for tool %s: %s", attempt, tool, e)

                # Check for expired/revoked token — generate re-auth link immediately.
                self._maybe_generate_auth_link(identifier, connection_name, error_str)

                retryable = (
                    any(x in error_str for x in ["timeout", "rate", "connection", "temporary", "unavailable"])
                    and attempt < Settings.RETRY_ATTEMPTS
                )
                if retryable:
                    log.info("Retrying %s after %ds (attempt %d)...", tool, backoff, attempt)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                log.error("Permanent failure for tool %s; returning None. %s", tool, traceback.format_exc())
                return None

            except Exception:
                log.exception("Unexpected exception calling tool %s.", tool)
                return None

        return None


_connector: Optional[ScalekitConnector] = None


def get_connector() -> ScalekitConnector:
    global _connector
    if _connector is None:
        _connector = ScalekitConnector()
    return _connector
