# gmail_api.py — Gmail via Scalekit connector tools only

from __future__ import annotations
from typing import Dict, List

from sk_connectors import execute_tool


def fetch_emails(identifier: str, query: str, max_results: int = 20) -> List[Dict]:
    data = execute_tool(
        "gmail_fetch_mails",
        {"query": query, "max_results": max_results},
        identifier,
    )
    if isinstance(data, dict):
        return data.get("messages") or []
    return []


def get_message(identifier: str, message_id: str) -> Dict:
    data = execute_tool(
        "gmail_get_message_by_id",
        {"message_id": message_id, "format": "full"},
        identifier,
    )
    if isinstance(data, dict):
        return data.get("message") or data
    return {}


def create_draft(identifier: str, to: str, subject: str, body: str) -> Dict:
    attempts = [
        {
            "to": to,
            "subject": subject,
            "body": body,
        },
        {
            "to": to,
            "subject": subject,
            "body": body,
            "content_type": "text/plain",
        },
        {
            "to": [to],
            "subject": subject,
            "body": body,
            "content_type": "text/plain",
        },
    ]
    last_exc: Exception | None = None
    for params in attempts:
        try:
            data = execute_tool("gmail_create_draft", params, identifier)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    return {}


def mark_read(identifier: str, message_id: str) -> None:
    execute_tool(
        "gmail_modify_message_labels",
        {
            "message_id": message_id,
            "remove_label_ids": ["UNREAD"],
        },
        identifier,
    )
