# tools/web_search.py
import os
import requests
from typing import List, Dict
from langchain.tools import tool


def _serpapi_google_search(query: str, num: int = 3, location: str = "United States") -> List[Dict]:
    SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

    """Call SerpAPI Google Search and return a list of {title, link, snippet}."""
    if not SERPAPI_API_KEY:
        return [{"title": "Configuration error", "link": "", "snippet": "Missing SERPAPI_API_KEY in environment."}]

    url = "https://serpapi.com/search"
    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_API_KEY,
        "num": num,
        "location": location,
        "hl": "en",
        "gl": "us",
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        organic = data.get("organic_results", []) or []
        results = []
        for r in organic[:num]:
            results.append({
                "title": r.get("title", ""),
                "link": r.get("link", ""),
                "snippet": r.get("snippet", "") or r.get("snippet_highlighted_words", [""])[0] if r.get("snippet_highlighted_words") else ""
            })
        return results
    except Exception as e:
        return [{"title": "Search error", "link": "", "snippet": str(e)}]

@tool
def web_search_tool(query: str) -> str:
    """
    Search Google via SerpAPI and return the top results (title + URL + snippet).
    Provide a concise, newline-separated summary. Keep URLs visible.
    """
    results = _serpapi_google_search(query=query, num=5)
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        link = r.get("link", "").strip()
        snippet = r.get("snippet", "").strip()
        lines.append(f"{i}. {title}\n   {link}\n   {snippet}")
    return "\n".join(lines) if lines else "No results."
