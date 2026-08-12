import hashlib
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import buffer
import drive
import notion_read

LOCAL_TZ = os.environ.get("LOCAL_TZ", "Asia/Kolkata")
DOC_TITLE = "Teaching Notes — Primary Batch"

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _format_session(entry: dict, body: str) -> str:
    date = entry["date"]
    title = entry["title"] or "(untitled)"
    if date:
        heading = f"## {date} — {title}"
    else:
        # Hand-created rows can be missing Date. Still export the session
        # rather than dropping it — just note the gap for a human to notice.
        logger.warning("Entry %s has no Date property", entry["id"])
        heading = f"## {title}"

    lines = [heading]
    if entry["students"]:
        lines.append(f"**Students present:** {', '.join(entry['students'])}")
    lines.append("")
    lines.append(body.strip())
    return "\n".join(lines)


def _render_sessions(entries: list, bodies: list) -> str:
    """Join every session's rendered block. This is the part that gets hashed
    for change detection, so it must contain nothing that varies run-to-run
    when the underlying Notion data hasn't changed — no "last synced" clock.
    """
    sessions = [_format_session(entry, body) for entry, body in zip(entries, bodies)]
    return "\n\n---\n\n".join(sessions)


def _render(entries: list, sessions_text: str) -> str:
    now = datetime.now(ZoneInfo(LOCAL_TZ))
    dates = [e["date"] for e in entries if e["date"]]
    date_range = f"{min(dates)} → {max(dates)}" if dates else "no dates recorded"

    # "Last synced" is deliberately excluded from the hashed content (see
    # _render_sessions) — it changes every run regardless of whether the
    # Notion data did, which would defeat the hash gate below entirely.
    header = (
        f"# {DOC_TITLE}\n\n"
        "Auto-generated from Notion. Do not edit here — edits are overwritten on "
        "next sync.\n"
        f"Last synced: {now.strftime('%Y-%m-%d %H:%M')} {LOCAL_TZ.split('/')[-1]} · "
        f"{len(entries)} sessions · {date_range}\n\n"
        'Each "##" heading below is one class session, oldest first. '
        '"Students present" lists the kids tagged for that session in Notion. '
        "Body is the condensed post-class journal entry.\n"
    )

    return header + "\n---\n\n" + sessions_text + "\n"


def handler(event, context):
    entries = notion_read.list_entries()
    bodies = [notion_read.page_markdown(entry["id"]) for entry in entries]
    sessions_text = _render_sessions(entries, bodies)

    content_hash = hashlib.sha256(sessions_text.encode("utf-8")).hexdigest()
    if content_hash == buffer.get_sync_hash():
        logger.info("No change since last sync (%d sessions), skipping Drive write", len(entries))
        return {"changed": False, "sessions": len(entries)}

    # Drive write before hash write: if the Drive call fails, the stored hash
    # stays at its old value and the next run retries the write. Writing the
    # hash first would mark this run as done even on a failed upload, and the
    # Doc would silently drift out of sync forever.
    doc = _render(entries, sessions_text)
    drive.replace_doc(doc)
    buffer.put_sync_hash(content_hash)

    logger.info("Synced %d sessions to Drive doc %s", len(entries), drive.DRIVE_DOC_ID)
    return {"changed": True, "sessions": len(entries)}
