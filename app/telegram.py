import os

import requests

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
ALLOWED_CHAT_ID = os.environ["ALLOWED_CHAT_ID"]

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def verify_secret(headers: dict) -> bool:
    # Telegram sends this header on every webhook POST; header lookup must be
    # case-insensitive since API Gateway / Lambda Function URLs may lowercase keys.
    incoming = None
    for key, value in headers.items():
        if key.lower() == "x-telegram-bot-api-secret-token":
            incoming = value
            break
    return incoming == TELEGRAM_WEBHOOK_SECRET


def is_allowed_chat(chat_id) -> bool:
    return str(chat_id) == str(ALLOWED_CHAT_ID)


def send_message(chat_id, text: str) -> None:
    # Telegram caps sendMessage text at 4096 chars; truncate defensively so a
    # reply never fails outright (the Notion row itself holds the full content).
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"
    resp = requests.post(
        f"{API_BASE}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    resp.raise_for_status()
