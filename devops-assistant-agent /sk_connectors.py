import time
from pathlib import Path
from typing import Any, Dict, Optional
from scalekit import ScalekitClient
from scalekit.core import ScalekitException
from settings import Settings

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
PR_LINKS_FILE = STATE_DIR / "pr_linear_links.json"

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
            import json
            return json.loads(PR_LINKS_FILE.read_text())
        except Exception:
            return {}

    def save_links(self):
        import json
        PR_LINKS_FILE.write_text(json.dumps(self._pr_links, indent=2))

    def get_linear_issue_for_pr(self, key: str) -> Optional[str]:
        entry = self._pr_links.get(key)
        return entry.get("linear_issue_id") if entry else None

    def record_pr_issue(self, key: str, linear_issue_id: str, label: str):
        self._pr_links[key] = {"linear_issue_id": linear_issue_id, "label": label, "ts": time.time()}
        self.save_links()

    def execute_tool(self, identifier: str, tool: str, parameters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        backoff = Settings.RETRY_BACKOFF
        for attempt in range(1, Settings.RETRY_ATTEMPTS + 1):
            try:
                print(f"🔧 [SkConnector] execute_tool attempt={attempt} tool={tool} identifier={identifier} params={parameters}")
                resp = self.client.actions.execute_tool(tool_input=parameters, tool_name=tool, identifier=identifier)
                result = resp.data if hasattr(resp, 'data') else resp
                print(f"✅ [SkConnector] {tool} response type={type(result)}")
                # Optionally pretty-print small responses
                try:
                    import json
                    preview = json.dumps(result if isinstance(result, (dict, list)) else str(result), indent=2)[:1000]
                    print(f"📦 [SkConnector] response preview: {preview}")
                except Exception:
                    pass
                return result
            except ScalekitException as e:
                import traceback
                print(f"❌ [SkConnector] ScalekitException on attempt {attempt}: {e}")
                traceback.print_exc()
                msg = str(e).lower()
                retryable = any(x in msg for x in ["timeout", "rate", "connection", "temporary", "unavailable"]) and attempt < Settings.RETRY_ATTEMPTS
                if retryable:
                    print(f"⏳ retrying after {backoff}s...")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                print("⛔ permanent failure returning None")
                return None
            except Exception as e:
                import traceback
                print(f"❌ [SkConnector] Unexpected exception calling tool {tool}: {e}")
                traceback.print_exc()
                return None

_connector: Optional[ScalekitConnector] = None

def get_connector() -> ScalekitConnector:
    global _connector
    if _connector is None:
        _connector = ScalekitConnector()
    return _connector
