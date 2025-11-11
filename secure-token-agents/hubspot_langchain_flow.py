"""LangChain pipeline demonstration for HubSpot token refresh flow.

Flow:
1. Load env & helpers from naive_env_mail_agent (imports functions).
2. Fetch contacts with current access token.
3. Sleep for configured delay (SIMULATE_EXPIRE_DELAY seconds, default 60).
4. Simulate expiry (corrupt token) and fetch again (triggers refresh path).

Run examples:
    python3 hubspot_langchain_flow.py              # default 60s delay
    SIMULATE_EXPIRE_DELAY=3 python3 hubspot_langchain_flow.py  # quick demo

Requires: langchain-core (lightweight) or langchain.
"""
import os
import time
from typing import Any, Dict

from naive_env_mail_agent import (
    access_token,
    fetch_data,
    simulate_expired_access_token,
    get_access_token,
)

try:
    from langchain_core.runnables import RunnableLambda
except ImportError:
    # Fallback if full langchain installed
    try:
        from langchain.runnables import RunnableLambda  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError("langchain-core or langchain is required. Install with: pip install langchain-core") from e

DELAY = int(os.getenv("SIMULATE_EXPIRE_DELAY", "60"))


def step_fetch_initial(_: Dict[str, Any]) -> Dict[str, Any]:
    print("\n[STEP] Initial fetch using existing access token (if valid)...")
    if not access_token:
        get_access_token()
    fetch_data()
    return {"status": "initial_fetched"}


def step_wait(context: Dict[str, Any]) -> Dict[str, Any]:
    print(f"\n[STEP] Waiting {DELAY}s before simulating token expiry...")
    time.sleep(DELAY)
    return context | {"status": "wait_complete"}


def step_simulate(_: Dict[str, Any]) -> Dict[str, Any]:
    print("\n[STEP] Simulating access token expiry...")
    simulate_expired_access_token()
    return {"status": "expired_simulated"}


def step_fetch_after(_: Dict[str, Any]) -> Dict[str, Any]:
    print("\n[STEP] Fetch after simulated expiry (should refresh token)...")
    fetch_data()
    return {"status": "post_refresh_fetch_complete"}


pipeline = (
    RunnableLambda(step_fetch_initial)
    | RunnableLambda(step_wait)
    | RunnableLambda(step_simulate)
    | RunnableLambda(step_fetch_after)
)


def main():  # pragma: no cover
    print("[INFO] Starting LangChain HubSpot token refresh demonstration pipeline.")
    result = pipeline.invoke({})
    print("\n[RESULT] Pipeline completed:", result)


if __name__ == "__main__":
    main()
