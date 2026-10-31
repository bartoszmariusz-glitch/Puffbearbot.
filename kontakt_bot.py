
import os
import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["KONTAKT_BOT_TOKEN"]
ADMIN_ID = int(os.environ["KONTAKT_ADMIN_ID"])

# Powiązanie wiadomości administratora z klientami
message_map = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text(
            "🐻 Panel PuffBear Kontakt aktywny!\n"
            "Odpowiadaj na przekazane wiadomości klientów."
        )
    else:
        await update.message.reply_text(
            "🐻 Witaj w PuffBear Kontakt!\n\n"
            "Napisz swoją wiadomość, a obsługa odpowie "
            "Ci tutaj na Telegramie."
        )


async def customer_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.message

    if not message or message.chat.type != "private":
        return

    if user.id == ADMIN_ID:
        return

    header = await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "🐻 NOWA WIADOMOŚĆ\n\n"
            f"Klient: {user.full_name}\n"
            f"Username: @{user.username or 'brak'}\n"
            f"ID: {user.id}\n\n"
            "Odpowiedz na skopiowaną wiadomość poniżej."
        ),
    )

    copied = await context.bot.copy_message(
        chat_id=ADMIN_ID,
        from_chat_id=message.chat_id,
        message_id=message.message_id,
    )

    message_map[copied.message_id] = user.id


async def admin_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message or message.chat.type != "private":
        return

    if not message.reply_to_message:
        await message.reply_text(
            "Odpowiedz bezpośrednio na wiadomość klienta."
        )
        return

    original_id = message.reply_to_message.message_id
    customer_id = message_map.get(original_id)

    if not customer_id:
        await message.reply_text(
            "Nie znaleziono klienta. "
            "Poproś go o wysłanie nowej wiadomości."
        )
        return

    try:
        await context.bot.copy_message(
            chat_id=customer_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        await message.reply_text("✅ Odpowiedź wysłana.")
    except Exception:
        logging.exception("Błąd wysyłania odpowiedzi")
        await message.reply_text(
            "❌ Nie udało się wysłać odpowiedzi."
        )


def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            route_message,
        )
    )

    print("🐻 PuffBear Kontakt uruchomiony!")
    app.run_polling()


async def route_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user.id == ADMIN_ID:
        await admin_reply(update, context)
    else:
        await customer_message(update, context)


if __name__ == "__main__":
    main()
