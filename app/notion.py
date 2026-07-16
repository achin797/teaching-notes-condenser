import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")
LOCAL_TZ = os.environ.get("LOCAL_TZ", "Asia/Kolkata")

API_BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# Notion caps a single rich_text object's text.content at 2000 chars.
_CHUNK_SIZE = 2000


def chunk(text: str) -> list:
    """Split text into a list of Notion rich_text objects, each <= 2000 chars."""
    if not text:
        return [{"text": {"content": ""}}]
    return [
        {"text": {"content": text[i : i + _CHUNK_SIZE]}}
        for i in range(0, len(text), _CHUNK_SIZE)
    ]


def _body_paragraphs(text: str) -> list:
    """Paragraph blocks for the page body, chunked to Notion's 2000-char limit."""
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [piece]},
        }
        for piece in chunk(text)
    ]


def create_entry(condensed: str, raw_notes: str) -> str:
    """Create a Notion page in the class-notes database. Returns the new page's URL."""
    now = datetime.now(ZoneInfo(LOCAL_TZ))
    # Title defaults to the month name followed by the day number, e.g. "July 16".
    title = f"{now.strftime('%B')} {now.day}"

    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Date": {"date": {"start": now.strftime("%Y-%m-%d")}},
            "Raw notes": {"rich_text": chunk(raw_notes)},
        },
        "children": _body_paragraphs(condensed),
    }

    resp = requests.post(f"{API_BASE}/pages", headers=HEADERS, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()["url"]
