import warnings

# Suppress asyncio 'event loop is closed' warnings from noisy SDK cleanup
warnings.filterwarnings("ignore", message=".*Event loop is closed.*", category=RuntimeWarning)

import os
import time
import json
import requests
from typing import Set, Any
from dotenv import load_dotenv


"""
Freshdesk → Google ADK Customer Feedback Automation Agent

Main polling and automation logic for:
- Polling Freshdesk for resolved tickets
- Fetching latest CSAT survey result
- Using Google ADK SequentialAgent to decide and execute actions (reply, close, reopen)
- Deduplication and robust logging
"""

# Safe import of google_adk components; handle when SDK isn't installed in the active venv
try:
    from google.adk.agents import SequentialAgent, LlmAgent
    from google.adk import runners
    from google.adk.models import Gemini
except Exception as _adk_err:
    print(f"[WARN] google.adk not available yet: {_adk_err}")
    SequentialAgent = None
    LlmAgent = None
    runners = None
    Gemini = None

load_dotenv()

# Ensure GOOGLE_API_KEY is set for Google ADK SDK compatibility
if os.getenv("GOOGLE_ADK_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_ADK_API_KEY")

FRESHDESK_DOMAIN = os.getenv("FRESHDESK_DOMAIN")
FRESHDESK_API_KEY = os.getenv("FRESHDESK_API_KEY")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
STATE_FILE = os.path.join(os.path.dirname(__file__), "processed_tickets.json")

FRESHDESK_BASE = f"https://{FRESHDESK_DOMAIN}/api/v2"
FRESHDESK_AUTH = (FRESHDESK_API_KEY, "X")

 # Default Gemini model for ADK (can override in .env)
GEMINI_MODEL = os.getenv("GOOGLE_ADK_MODEL", "gemini-2.0-flash")

 # --- Freshdesk API helpers ---
def fd_get_tickets():
    """Fetch all tickets from Freshdesk (with requester info)."""
    url = f"{FRESHDESK_BASE}/tickets?include=requester"
    resp = requests.get(url, auth=FRESHDESK_AUTH)
    resp.raise_for_status()
    return resp.json()

def fd_update_ticket(ticket_id, data):
    """Update a Freshdesk ticket (e.g., status)."""
    url = f"{FRESHDESK_BASE}/tickets/{ticket_id}"
    resp = requests.put(url, json=data, auth=FRESHDESK_AUTH)
    resp.raise_for_status()
    return resp.json()

def fd_reply_ticket(ticket_id, body, public=True):
    """Post a public reply to a Freshdesk ticket."""
    url = f"{FRESHDESK_BASE}/tickets/{ticket_id}/reply"
    data = {"body": body}
    resp = requests.post(url, json=data, auth=FRESHDESK_AUTH)
    resp.raise_for_status()
    return resp.json()

def fd_get_survey(ticket_id: str):
    """Fetch the latest CSAT survey result for a Freshdesk ticket."""
    url = f"https://{FRESHDESK_DOMAIN}/helpdesk/tickets/{ticket_id}/surveys.json"
    try:
        resp = requests.get(url, auth=FRESHDESK_AUTH)
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, list):
            sorted_surveys = sorted(
                [s for s in data if isinstance(s, dict) and s.get("survey_result")],
                key=lambda s: s["survey_result"].get("created_at", ""),
                reverse=True
            )
            if sorted_surveys:
                return sorted_surveys[0]["survey_result"]
        return None
    except Exception:
        return None


# --- Google ADK helpers ---
def _run_with_runner(seq_agent: Any, prompt: str):
    """Run the agent using the InMemoryRunner.run_debug and return text output if any."""
    if runners is None:
        raise RuntimeError("google.adk.runners not available")
    try:
        runner = runners.InMemoryRunner(agent=seq_agent, app_name="agents")
        import inspect as _inspect, asyncio as _asyncio
        events_or_coro = runner.run_debug(user_messages=prompt, quiet=True)
        if _inspect.iscoroutine(events_or_coro):
            try:
                events = _asyncio.run(events_or_coro)
            except Exception as _e:
                print(f"[DEBUG] run_debug await failed: {_e}")
                return None
        else:
            events = events_or_coro
        texts = []
        for ev in events:
            content = getattr(ev, "content", None)
            if not content:
                continue
            parts = getattr(content, "parts", [])
            for p in parts:
                t = getattr(p, "text", None)
                if t:
                    texts.append(t)
        return "\n".join(texts) if texts else None
    except Exception as e:
        print(f"[DEBUG] InMemoryRunner.run_debug raised: {e}")
        raise


def _strip_code_fences(s: str) -> str:
    """Remove markdown code fences from a string (for JSON parsing)."""
    s = s.strip()
    if s.startswith('```json'):
        s = s[len('```json'):]
    elif s.startswith('```'):
        s = s[len('```'):]
    if s.endswith('```'):
        s = s[:-3]
    return s.strip()


def get_adk_decision(ticket_id: str, survey_result: dict, email: str | None) -> dict | None:
    """
    Use Google ADK SequentialAgent to decide what action to take based on the survey result.
    Returns a dict with keys: feedback_received, rating, action, raw.
    """
    if not (SequentialAgent and LlmAgent and Gemini):
        print("[ERROR] google.adk SDK not available. Install google-adk in your venv.")
        return None

    instruction = (
        "You are a Freshdesk ticket feedback decision agent. "
        "Given the ticket id, requester email, and the latest CSAT survey result (JSON), decide what action to take. "
        "The survey_result object may contain keys like id, rating (103 means satisfied, -103 means not satisfied), created_at, etc. "
        "Output ONLY JSON with keys: feedback_received (true/false), rating (103|-103|null), action (\"thank_and_close\"|\"reopen_and_apologize\"|null)."
    )

    candidates = [os.getenv("GOOGLE_ADK_MODEL") or GEMINI_MODEL, "gemini-1.5-pro-001", "gemini-1.0-pro", "gemini-pro", "gemini-1.5-flash"]
    last_error = None
    for model_name in [m for m in candidates if m]:
        try:
            print(f"[DEBUG] ADK model candidate: {model_name}")
            writer = LlmAgent(
                name="Decision",
                model=Gemini(model=model_name),
                instruction=instruction,
                description="Decides action based on survey result",
                output_key="decision",
            )
            seq = SequentialAgent(
                name=f"fd_decision_seq_{ticket_id}",
                sub_agents=[writer],
                description="Freshdesk CSAT decision pipeline",
            )
            prompt = (
                f"Ticket ID: {ticket_id}. Requester: {email}. Survey Result JSON:\n" + json.dumps(survey_result or {}, ensure_ascii=False)
            )
            raw = _run_with_runner(seq, prompt)
            print(f"[DEBUG] ADK raw output: {raw}")
            if not raw:
                continue
            if isinstance(raw, str):
                try:
                    parsed = json.loads(_strip_code_fences(raw))
                except Exception:
                    parsed = {"feedback_received": False, "rating": None, "action": None, "raw": raw}
            elif isinstance(raw, dict):
                parsed = raw.get("decision") or raw
            else:
                parsed = {"feedback_received": False, "rating": None, "action": None, "raw": str(raw)}

            # Normalize
            rating = parsed.get("rating")
            if isinstance(rating, str):
                low = rating.strip().lower()
                if low in ("satisfied", "positive", "good", "happy"):
                    rating = 103
                elif low in ("not_satisfied", "negative", "bad", "unhappy"):
                    rating = -103
            if isinstance(rating, str) and rating.lstrip("-").isdigit():
                rating = int(rating)
            action = parsed.get("action")
            feedback_received = bool(parsed.get("feedback_received"))
            result = {"feedback_received": feedback_received, "rating": rating, "action": action, "raw": parsed}
            print(f"[DEBUG] ADK parsed decision: {result}")
            return result
        except Exception as e:
            last_error = e
            print(f"[WARN] ADK candidate {model_name} failed: {e}")
            continue
    if last_error:
        print(f"[ERROR] All ADK model candidates failed. Last error: {last_error}")
    return None


def load_state() -> Set[str]:
    """Load processed ticket IDs from local state file."""
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE) as f:
        return set(json.load(f))

def save_state(state: Set[str]):
    """Save processed ticket IDs to local state file."""
    with open(STATE_FILE, "w") as f:
        json.dump(list(state), f)

def process_ticket(ticket, state: Set[str]):
    """
    Main ticket processing logic:
    - Skip if already processed or not resolved
    - Fetch survey result
    - Use ADK to decide action
    - Reply and update ticket as needed
    - Deduplicate
    """
    ticket_id = str(ticket["id"])
    if ticket_id in state:
        return
    if ticket["status"] != 4:  # 4 = Resolved
        return
    requester_email = (ticket.get("requester") or {}).get("email")
    survey_result = fd_get_survey(ticket_id)
    decision = get_adk_decision(ticket_id, survey_result, requester_email)
    if not decision or not decision.get("feedback_received"):
        return
    rating = decision.get("rating")
    action = decision.get("action")
    try:
        if action == "thank_and_close" or rating == 103:
            fd_reply_ticket(ticket_id, "Thank you for your feedback! We're glad you are satisfied.")
            fd_update_ticket(ticket_id, {"status": 5})  # Closed
        elif action == "reopen_and_apologize" or rating == -103:
            fd_reply_ticket(ticket_id, "We're sorry to hear that. We're reopening this ticket and will follow up to make it right.")
            fd_update_ticket(ticket_id, {"status": 2})  # Open
        else:
            return
    except Exception as e:
        print(f"[ERROR] Failed to process ADK action for ticket {ticket_id}: {e}")
    state.add(ticket_id)
    save_state(state)

def poll():
    """Main polling loop: fetch tickets and process them forever."""
    state = load_state()
    while True:
        try:
            tickets = fd_get_tickets()
            for ticket in tickets:
                process_ticket(ticket, state)
        except Exception as e:
            print(f"[ERROR] Polling error: {e}")
    # Sleep between polling cycles
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    poll()
