"""
Per-employee feedback summarization.

Uses an LLM (via OpenRouter) when OPENROUTER_API_KEY is set, and falls back
automatically to a deterministic, rule-based summary if the key is missing
or the call fails for any reason. Either path summarizes only the real
feedback that was fetched -- never placeholder content.
"""

import logging
from typing import Optional

import requests

from aggregator import EmployeeFeedback

logger = logging.getLogger(__name__)

_SUMMARY_PROMPT = """\
You are helping a manager write a fair, specific performance review summary.

Employee: {name}
Review period: {period}

Average ratings by category:
{ratings_block}

Feedback comments collected from reviewers:
{comments_block}

Write a concise (4-6 sentence) narrative summary covering strengths, growth
areas, and any patterns across reviewers. Be specific and reference the
ratings where relevant. Do not invent facts not present in the feedback above.
"""


def summarize(
    feedback: EmployeeFeedback,
    review_period: str,
    openrouter_api_key: str,
    openrouter_model: str,
) -> str:
    """Summarize one employee's feedback. LLM first, rule-based fallback on any failure."""
    if openrouter_api_key:
        try:
            return _summarize_with_llm(feedback, review_period, openrouter_api_key, openrouter_model)
        except Exception as e:
            logger.warning(f"LLM summarization failed for {feedback.name} ({e}) -- using rule-based summary")

    return _summarize_rule_based(feedback, review_period)


def _summarize_with_llm(
    feedback: EmployeeFeedback,
    review_period: str,
    api_key: str,
    model: str,
) -> str:
    ratings = feedback.average_ratings()
    ratings_block = (
        "\n".join(f"- {field}: {value}/5" for field, value in ratings.items())
        or "(no numeric ratings submitted)"
    )
    comments = feedback.all_comments()
    comments_block = (
        "\n".join(f"- {c}" for c in comments) or "(no written comments submitted)"
    )

    prompt = _SUMMARY_PROMPT.format(
        name=feedback.name,
        period=review_period,
        ratings_block=ratings_block,
        comments_block=comments_block,
    )

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    if not content:
        raise ValueError("LLM returned empty content")
    return content


def _summarize_rule_based(feedback: EmployeeFeedback, review_period: str) -> str:
    """Deterministic summary: ratings table + comment excerpts. No LLM required."""
    lines = []

    overall = feedback.overall_average()
    if overall is not None:
        lines.append(f"Overall average rating for {review_period}: {overall}/5.")

    ratings = feedback.average_ratings()
    if ratings:
        rating_parts = ", ".join(f"{field}: {value}/5" for field, value in ratings.items())
        lines.append(f"By category — {rating_parts}.")

    comments = feedback.all_comments()
    if comments:
        lines.append(f"{len(comments)} written comment(s) were submitted by reviewers:")
        for comment in comments[:5]:
            lines.append(f"  • {comment}")
        if len(comments) > 5:
            lines.append(f"  • ...and {len(comments) - 5} more.")
    else:
        lines.append("No written comments were submitted.")

    if not ratings and not comments:
        return f"No feedback has been submitted yet for {feedback.name} in {review_period}."

    return "\n".join(lines)
