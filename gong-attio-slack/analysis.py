"""Call analysis with LLM and rule-based fallback."""
import json
import re
import requests


def analyze_with_llm(transcript: str, call_title: str = "", api_key: str = "", model: str = "") -> dict:
    """Use OpenRouter to extract structured risk signals from call transcript."""
    if not api_key or not model:
        return {}

    prompt = f"""Analyze this sales call transcript. Return ONLY valid JSON with these exact keys:
sentiment (positive/neutral/negative)
sentiment_score (0.0 to 1.0)
objections (list of strings)
competitor_mentions (list of strings)
engagement_level (high/medium/low)
key_concerns (list of top 2-3 concerns)
next_steps (list of agreed actions)
summary (2-3 sentence summary)

Call: {call_title}

Transcript:
{transcript[:4000]}"""

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        # Strip markdown code fences if present
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.rsplit("```", 1)[0]
        content = content.strip()
        return json.loads(content)
    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
        return {}


def analyze_rule_based(transcript: str, call_title: str = "") -> dict:
    """Rule-based fallback analyzer for sentiment and signals."""
    sentiment_score = 0.5
    if any(word in transcript.lower() for word in ["great", "perfect", "excellent", "agreed"]):
        sentiment_score = 0.8
    elif any(word in transcript.lower() for word in ["problem", "issue", "concerned", "hesitant"]):
        sentiment_score = 0.3

    sentiment = "positive" if sentiment_score > 0.6 else ("negative" if sentiment_score < 0.4 else "neutral")

    objections = re.findall(r"(?:concern|issue|problem|hesitation)[:\s]+([^.!?]+)", transcript, re.IGNORECASE)
    competitors = re.findall(r"(?:competitor|alternative|other|using)\s+([A-Za-z][a-zA-Z0-9 ]*)", transcript, re.IGNORECASE)
    engagement = "high" if len(objections) == 0 else ("low" if len(objections) > 3 else "medium")

    return {
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "objections": objections[:3],
        "competitor_mentions": list(set(competitors))[:3],
        "engagement_level": engagement,
        "key_concerns": objections[:2],
        "next_steps": re.findall(r"(?:follow\s+up|next|schedule|send)[:\s]+([^.!?]+)", transcript, re.IGNORECASE)[:2],
        "summary": f"Call on {call_title}. Sentiment: {sentiment}. Engagement: {engagement}.",
    }


def analyze_call(transcript: str, call_title: str = "", api_key: str = "", model: str = "") -> dict:
    """Analyze call with LLM or fallback to rule-based."""
    result = analyze_with_llm(transcript, call_title, api_key, model)
    if result:
        return result
    return analyze_rule_based(transcript, call_title)


def compute_risk_score(analysis: dict) -> float:
    """Compute overall deal risk score (0.0 = safe, 1.0 = high risk)."""
    score = 0.0
    sentiment_score = float(analysis.get("sentiment_score", 0.5))
    score += (1.0 - sentiment_score) * 0.4
    score += (1.0 if analysis.get("engagement_level") == "low" else 0.0) * 0.3
    score += min(len(analysis.get("objections", [])) / 5.0, 1.0) * 0.2
    score += (1.0 if analysis.get("competitor_mentions") else 0.0) * 0.1
    score = max(0.0, min(score, 1.0))
    return round(score, 2)
