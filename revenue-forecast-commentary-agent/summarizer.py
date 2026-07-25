"""
Optional LLM polish pass over the rule-based forecast commentary.

Uses an LLM (via OpenRouter) when OPENROUTER_API_KEY is set, and falls back
automatically to the deterministic rule-based commentary from aggregator.py
if the key is missing or the call fails for any reason. The rule-based
commentary is always computed first and passed in here as the source of
truth -- the LLM's job is only to rewrite it more fluently, never to invent
numbers that aren't already in the rule-based draft.
"""

import logging

import requests

logger = logging.getLogger(__name__)

_POLISH_PROMPT = """\
You are helping a RevOps analyst turn a data-backed forecast commentary draft \
into a clear, concise Slack update for the #revenue-ops channel.

Forecast period: {period}

Rule-based draft (contains the only facts and figures you may reference):
{draft}

Rewrite this as a tight, professional Slack message (use Slack mrkdwn: *bold*, \
- bullets). Preserve every number, stage name, and at-risk reason exactly as \
given. Do not invent facts, deals, or figures not present in the draft above. \
Keep it under 200 words.
"""


def polish_commentary(
    rule_based_draft: str,
    forecast_period: str,
    openrouter_api_key: str,
    openrouter_model: str,
) -> str:
    """Return an LLM-polished version of the draft, or the draft unchanged on any failure."""
    if not openrouter_api_key:
        return rule_based_draft

    try:
        return _polish_with_llm(rule_based_draft, forecast_period, openrouter_api_key, openrouter_model)
    except Exception as e:
        logger.warning(f"LLM commentary polish failed ({e}) -- using rule-based draft as-is")
        return rule_based_draft


def _polish_with_llm(draft: str, period: str, api_key: str, model: str) -> str:
    prompt = _POLISH_PROMPT.format(period=period, draft=draft)

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    if not content:
        raise ValueError("LLM returned empty content")
    return content
