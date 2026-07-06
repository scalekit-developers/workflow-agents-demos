"""Meeting extraction logic: LLM + rule-based fallback."""
import json
import re
import logging
import requests

logger = logging.getLogger("granola-hubspot")


def extract_with_llm(text: str, api_key: str, model: str) -> dict:
    """Call OpenRouter to extract structured deal info from meeting text."""
    prompt = f"""Extract structured information from this meeting note. Return ONLY valid JSON with these exact keys:
- company (string)
- contact_email (string or null)
- deal_name (string, e.g. "Acme — Q2 Deal")
- deal_stage (one of: appointmentscheduled, qualifiedtobuy, presentationscheduled, decisionmakerboughtin, contractsent, closedwon, closedlost)
- amount (integer USD or null)
- summary (2-3 sentence summary)
- action_items (list of strings)
- next_step (string)
- email_subject (string)
- email_body (string, use \\n for newlines)

Meeting note:
{text}"""

    logger.debug("Calling OpenRouter LLM for extraction")
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}") + 1
    result = json.loads(raw[start:end])
    logger.info("LLM extraction succeeded")
    return result


def extract_rule_based(text: str, title: str = "") -> dict:
    """Fallback parser — extracts key fields from meeting text without an LLM."""
    logger.debug("Using rule-based extraction")
    tl = text.lower()

    # Amount
    m = re.search(r"\$\s*([\d,]+)", text)
    amount = int(m.group(1).replace(",", "")) if m else None

    # Deal stage (check in order of likelihood)
    stage = "appointmentscheduled"
    if "closed won" in tl or "closedwon" in tl:
        stage = "closedwon"
    elif "contract" in tl:
        stage = "contractsent"
    elif "proposal" in tl or "presentation" in tl:
        stage = "presentationscheduled"
    elif "qualified" in tl:
        stage = "qualifiedtobuy"

    # Company — look for capitalized noun near deal/account keywords, else use title
    cm = re.search(
        r"([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*)\s+(?:deal|account|company|team|corp|inc)",
        text,
    )
    company = cm.group(1) if cm else (title.split("—")[0].strip() or "Unknown")

    # Contact email
    em = re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", text)
    contact_email = em.group(0) if em else None

    # Action items — lines starting with a bullet or dash
    action_items = [
        a.strip() for a in re.findall(r"[-•*]\s+(.+)", text) if len(a.strip()) > 5
    ]

    # Summary — first two sentences
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    summary = " ".join(sentences[:2]) if sentences else text[:200]

    next_step = action_items[0] if action_items else "Follow up with the contact"

    email_body = (
        "Hi,\n\n"
        "Thanks for the meeting. Here's a quick recap:\n\n"
        f"{summary}\n\n"
        "Action items:\n"
        + "\n".join(f"• {a}" for a in action_items)
        + f"\n\nNext step: {next_step}\n\nBest regards"
    )

    return {
        "company": company,
        "contact_email": contact_email,
        "deal_name": f"{company} — Deal",
        "deal_stage": stage,
        "amount": amount,
        "summary": summary,
        "action_items": action_items,
        "next_step": next_step,
        "email_subject": f"Follow-up: {company} — next steps",
        "email_body": email_body,
    }


def extract_meeting_info(text: str, title: str = "", api_key: str = "", model: str = "") -> dict:
    """LLM extraction if key is set; rule-based parser otherwise."""
    if api_key:
        try:
            return extract_with_llm(text, api_key, model)
        except Exception as e:
            logger.warning(
                f"LLM extraction failed ({e.__class__.__name__}): {e} — falling back to rule-based"
            )
    return extract_rule_based(text, title)
