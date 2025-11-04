from langchain_core.tools import tool


@tool
def summarize_tool(research_output: str) -> str:
    """Summarize the research output deterministically (no external API).

    Produces a short bullet list and a one-line conclusion from the input text.
    """
    text = (research_output or "").strip()
    if not text:
        return "No content to summarize."

    # Heuristic: take first 5 sentences/lines as key points
    import re

    # Split by sentences and lines, then pick top fragments
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    parts = [p.strip() for p in parts if p.strip()]
    bullets = parts[:5]
    bullets_fmt = "\n".join(f"- {b}" for b in bullets)

    conclusion = "In summary, the research outlines key advancements and references for follow-up."
    return f"Summary:\n\n{bullets_fmt}\n\n{conclusion}"
