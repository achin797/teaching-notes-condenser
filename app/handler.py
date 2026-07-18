import base64
import json
import logging

import buffer
import condense
import notion
import telegram

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_HELP_TEXT = (
    "Send your raw class notes as one or more messages, then send /done to "
    "condense them and add them to Notion.\n\n"
    "Commands:\n"
    "/start, /help — show this message\n"
    "/done — finalize the buffered notes\n"
    "/quit — discard the buffered notes and start over"
)

_OK = {"statusCode": 200, "body": ""}


def _parse_body(event: dict) -> dict:
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body) if body else {}


def handler(event, context):
    headers = event.get("headers") or {}

    # 1. Verify the webhook secret so random internet POSTs to the public
    # Function URL are ignored rather than processed.
    if not telegram.verify_secret(headers):
        logger.warning("Rejected request with missing/invalid webhook secret")
        return _OK

    try:
        update = _parse_body(event)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Rejected request with unparseable body")
        return _OK

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text")
    update_id = update.get("update_id")

    if chat_id is None or text is None:
        # Non-text update (photo, sticker, edited_message, etc.) — nothing to do.
        return _OK

    # 3. Only respond to the owner's chat.
    if not telegram.is_allowed_chat(chat_id):
        telegram.send_message(chat_id, "Not authorized.")
        return _OK

    # 4. Idempotency: Telegram retries the webhook if it doesn't get a fast 200,
    # which would otherwise create duplicate Notion rows on a slow Bedrock call.
    if update_id is not None and buffer.is_duplicate_update(update_id):
        logger.info("Skipping already-processed update_id=%s", update_id)
        return _OK

    stripped = text.strip()

    if stripped in ("/start", "/help"):
        telegram.send_message(chat_id, _HELP_TEXT)
        return _OK

    if stripped == "/quit":
        chunks = buffer.get_and_clear_buffer(chat_id)
        if not chunks:
            telegram.send_message(chat_id, "Nothing buffered — already a clean slate.")
        else:
            telegram.send_message(chat_id, "Buffer cleared. Send new notes whenever you're ready.")
        return _OK

    if stripped == "/done":
        chunks = buffer.get_and_clear_buffer(chat_id)
        if not chunks:
            telegram.send_message(
                chat_id, "No notes buffered yet — send your raw notes first."
            )
            return _OK
        raw_notes = "\n".join(chunks)
        try:
            condensed = condense.condense(raw_notes)
            url = notion.create_entry(condensed=condensed, raw_notes=raw_notes)
            telegram.send_message(chat_id, f"✅ Added to Notion\n{url}")
        except Exception:
            logger.exception("Failed to condense/write notes for chat_id=%s", chat_id)
            telegram.send_message(
                chat_id,
                "⚠️ Something went wrong condensing/saving those notes. "
                "Your raw notes were not saved — please resend them.",
            )
        return _OK

    # Any other text: buffer it, since a full set of raw notes usually arrives
    # as several Telegram messages (Telegram caps a single message at 4096 chars).
    count = buffer.append_chunk(chat_id, text)
    telegram.send_message(chat_id, f"Got it ({count} message(s) buffered) — send /done when finished.")
    return _OK
