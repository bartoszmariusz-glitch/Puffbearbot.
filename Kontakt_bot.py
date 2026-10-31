"""PuffBear Kontakt — two-admin Telegram support bot.

Required environment variables:
  KONTAKT_BOT_TOKEN: Telegram BotFather token
  KONTAKT_ADMIN_ID: comma-separated numeric Telegram user IDs
Optional:
  KONTAKT_DB_PATH: SQLite file path (use a Railway volume path for persistence)
"""

import logging
import os
import sqlite3
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("puffbear-kontakt")

TOKEN = os.environ["KONTAKT_BOT_TOKEN"].strip()
ADMIN_IDS = set()
for item in os.environ["KONTAKT_ADMIN_ID"].split(","):
    item = item.strip()
    if item:
        ADMIN_IDS.add(int(item))
if not ADMIN_IDS:
    raise ValueError("KONTAKT_ADMIN_ID musi zawierać co najmniej jeden Telegram ID")

DB_PATH = Path(os.environ.get("KONTAKT_DB_PATH", "kontakt_messages.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def connect_db():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("""CREATE TABLE IF NOT EXISTS message_links (
        admin_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        customer_id INTEGER NOT NULL,
        PRIMARY KEY (admin_id, message_id)
    )""")
    return connection


def save_link(admin_id: int, message_id: int, customer_id: int):
    with connect_db() as db:
        db.execute("INSERT OR REPLACE INTO message_links VALUES (?, ?, ?)",
                   (admin_id, message_id, customer_id))


def get_customer(admin_id: int, message_id: int):
    with connect_db() as db:
        row = db.execute(
            "SELECT customer_id FROM message_links WHERE admin_id=? AND message_id=?",
            (admin_id, message_id),
        ).fetchone()
    return row[0] if row else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text(
            "🐻 PuffBear Kontakt — panel administratora.\n"
            "Odpowiadaj na wiadomości klientów przez funkcję «Odpowiedz»."
        )
    else:
        await update.message.reply_text(
            "🐻 Witaj w PuffBear Kontakt!\n\n"
            "Napisz swoją wiadomość, a obsługa odpowie Ci tutaj."
        )


async def customer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not message or update.effective_chat.type != "private":
        return

    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            username = f"@{user.username}" if user.username else "brak"
            header = await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    "🐻 NOWA WIADOMOŚĆ\n\n"
                    f"Klient: {user.full_name}\n"
                    f"Username: {username}\n"
                    f"ID klienta: {user.id}\n\n"
                    "Odpowiedz na wiadomość poniżej."
                ),
            )
            copied = await context.bot.copy_message(
                chat_id=admin_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
            save_link(admin_id, copied.message_id, user.id)
            # The header can also be used for replying.
            save_link(admin_id, header.message_id, user.id)
            sent += 1
        except TelegramError:
            logger.exception("Nie udało się przekazać wiadomości do administratora %s", admin_id)

    if sent:
        await message.reply_text("✅ Wiadomość przekazana do obsługi PuffBear.")
    else:
        await message.reply_text("❌ Nie udało się przekazać wiadomości. Spróbuj ponownie później.")


async def admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or update.effective_chat.type != "private":
        return
    if not message.reply_to_message:
        await message.reply_text("Aby odpisać klientowi, użyj «Odpowiedz» na jego wiadomości.")
        return
    admin_id = update.effective_user.id
    customer_id = get_customer(admin_id, message.reply_to_message.message_id)
    if customer_id is None:
        await message.reply_text("❌ Nie znalazłem klienta dla tej wiadomości.")
        return
    try:
        await context.bot.copy_message(
            chat_id=customer_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        await message.reply_text("✅ Odpowiedź wysłana klientowi.")
    except TelegramError:
        logger.exception("Błąd wysyłania odpowiedzi do klienta %s", customer_id)
        await message.reply_text("❌ Nie udało się wysłać odpowiedzi klientowi.")


async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS:
        await admin_message(update, context)
    else:
        await customer_message(update, context)


def main():
    with connect_db():
        pass
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, route_message))
    logger.info("PuffBear Kontakt uruchomiony (%s administratorów)", len(ADMIN_IDS))
    app.run_polling()


if __name__ == "__main__":
    main()
