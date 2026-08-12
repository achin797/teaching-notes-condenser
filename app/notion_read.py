import logging
import os
import time

import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATA_SOURCE_ID = os.environ["NOTION_DATA_SOURCE_ID"]
# 2026-03-11 or later is required for the /markdown endpoint used below.
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2026-03-11")
# Default is Notion's own unrenamed property name on this data source. If it
# gets renamed in Notion, set this env var to match — no code change needed.
STUDENTS_PROPERTY = os.environ.get("NOTION_STUDENTS_PROPERTY", "Multi-select")

API_BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

logger = logging.getLogger()

# Notion's documented average rate limit is ~3 requests/second. This job makes
# one query call plus one markdown call per session on every run, so it is the
# only caller likely to burst against that ceiling.
_RATE_DELAY_SECONDS = 0.34
_MAX_RETRIES = 3


def _request(method: str, path: str, **kwargs) -> requests.Response:
    """Call the Notion API with rate-limit pacing and retry on 429/5xx."""
    for attempt in range(_MAX_RETRIES):
        time.sleep(_RATE_DELAY_SECONDS)
        resp = requests.request(method, f"{API_BASE}{path}", headers=HEADERS, timeout=20, **kwargs)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            logger.warning("Notion rate limited us, sleeping %.1fs", retry_after)
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500:
            logger.warning("Notion API %s%s -> %s, retrying", method, path, resp.status_code)
            time.sleep(2**attempt)
            continue
        return resp
    return resp  # last attempt's response, even if still failing; caller checks .ok


def _extract_title(properties: dict) -> str:
    title_parts = properties.get("Name", {}).get("title") or []
    return "".join(part.get("plain_text", "") for part in title_parts).strip()


def _extract_date(properties: dict) -> str | None:
    date_obj = properties.get("Date", {}).get("date")
    return date_obj.get("start") if date_obj else None


def _extract_students(properties: dict) -> list:
    options = properties.get(STUDENTS_PROPERTY, {}).get("multi_select") or []
    return [opt.get("name", "") for opt in options if opt.get("name")]


def list_entries() -> list:
    """Return every row in the class-notes data source, oldest session first.

    Each item: {"id", "title", "date" (may be None), "students" (may be [])}.
    Extraction tolerates missing/null fields since hand-edited Notion rows
    can have gaps — one malformed row must never abort the whole sync.
    """
    entries = []
    start_cursor = None
    while True:
        body = {
            "sorts": [{"property": "Date", "direction": "ascending"}],
            "page_size": 100,
        }
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = _request("POST", f"/data_sources/{NOTION_DATA_SOURCE_ID}/query", json=body)
        if not resp.ok:
            print(f"Notion query error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            properties = page.get("properties", {})
            entries.append(
                {
                    "id": page["id"],
                    "title": _extract_title(properties),
                    "date": _extract_date(properties),
                    "students": _extract_students(properties),
                }
            )

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return entries


def page_markdown(page_id: str) -> str:
    """Fetch a page's body content rendered as markdown."""
    resp = _request("GET", f"/pages/{page_id}/markdown")
    if not resp.ok:
        print(f"Notion markdown error {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    data = resp.json()
    if data.get("truncated"):
        logger.warning("Page %s markdown was truncated by Notion", page_id)
    return data.get("markdown", "")
