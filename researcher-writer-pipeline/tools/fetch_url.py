import requests
from langchain_core.tools import tool
from readability import Document

@tool
def fetch_url_tool(url: str) -> str:
    """
    Fetch the main readable content from the given URL using readability-lxml.
    Returns the title and main text content (plain text, not HTML).
    """
    try:
        response = requests.get(url, timeout=20)
        if response.status_code != 200:
            return f"Failed to fetch URL: {url} (status {response.status_code})"
        doc = Document(response.text)
        title = doc.title() or ""
        main_html = doc.summary() or ""
        # Strip HTML tags for plain text
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(main_html, "html.parser")
            main_text = soup.get_text(separator="\n", strip=True)
        except ImportError:
            main_text = main_html  # fallback: raw HTML
        # Limit output to first ~2000 chars for brevity
        preview = main_text[:2000]
        return f"Title: {title}\n\nMain content:\n{preview}"
    except Exception as e:
        return f"Error fetching or parsing URL: {e}"
