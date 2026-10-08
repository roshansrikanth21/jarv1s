"""Poll Telegram and forward allowlisted private chats to the local JARVIS API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.request import Request, urlopen

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("jarvis.telegram")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
JARVIS_URL = os.environ.get("JARVIS_URL", "http://127.0.0.1:8000/api/ask").strip()
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if value.strip()
}


def ask_jarvis(text: str) -> dict:
    body = json.dumps(
        {"message": text, "speak": False, "timeout_s": 120}
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    agent_token = os.environ.get("JARVIS_AGENT_TOKEN", "").strip()
    if agent_token:
        headers["Authorization"] = f"Bearer {agent_token}"

    request = Request(JARVIS_URL, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=150) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JARVIS returned an unexpected response")
    return payload


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "JARVIS bridge is online. Send /id to see your Telegram user ID, "
            "then allow that ID in the bridge launcher."
        )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message and user:
        await message.reply_text(f"Your Telegram user ID is {user.id}.")


async def chat_with_jarvis(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    if chat.type != "private":
        await message.reply_text("Please message me in a private chat.")
        return

    if user.id not in ALLOWED_USER_IDS:
        await message.reply_text(
            f"This account is not allowlisted. Your Telegram user ID is {user.id}. "
            "Stop and restart the bridge, then enter that ID in its prompt."
        )
        return

    try:
        result = await asyncio.to_thread(ask_jarvis, message.text or "")
        reply = str(result.get("reply") or "").strip()
        status = str(result.get("status") or "unknown")
        if status != "ok":
            reply = f"JARVIS ({status})\n{reply}".strip()
        if not reply:
            reply = f"JARVIS returned status: {status}."
    except Exception:
        log.exception("JARVIS request failed")
        reply = (
            "I couldn't reach JARVIS. Check that the backend is running at "
            "127.0.0.1:8000 and that its JARVIS_AGENT_TOKEN matches the bridge."
        )

    # Telegram text messages are limited to 4096 characters.
    for offset in range(0, len(reply), 3900):
        await message.reply_text(reply[offset : offset + 3900])


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN was not supplied by the launcher.")

    logging.basicConfig(level=logging.INFO)
    # HTTPX includes the full request URL in its INFO logs; Telegram puts the bot
    # token in that URL, so never log those requests.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, chat_with_jarvis)
    )
    log.info("Polling Telegram; JARVIS API: %s", JARVIS_URL)
    # A brief outage or a network that comes online late should not permanently
    # kill the bridge during bootstrap. Keep retrying until Telegram is reachable.
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
