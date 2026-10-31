"""PuffBear Kontakt — zgłoszenia dla dwóch administratorów."""
import logging
import os
import sqlite3
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("puffbear-kontakt")

TOKEN = os.environ["KONTAKT_BOT_TOKEN"].strip()
ADMINS = {int(v.strip()) for v in os.environ["KONTAKT_ADMIN_ID"].split(",") if v.strip()}
if not ADMINS:
    raise RuntimeError("Brak KONTAKT_ADMIN_ID")
DB = Path(os.environ.get("KONTAKT_DB_PATH", "kontakt_messages.sqlite3"))
DB.parent.mkdir(parents=True, exist_ok=True)


def connect():
    db = sqlite3.connect(DB, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=20000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            assigned_admin INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS ticket_links(
            admin_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            ticket_id INTEGER NOT NULL,
            PRIMARY KEY(admin_id,message_id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS ticket_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_tickets_customer ON tickets(customer_id,status)")
        # Tabela ze starego bota zostaje nietknięta.


def event(db, ticket_id, actor_id, action):
    db.execute("INSERT INTO ticket_events(ticket_id,actor_id,event,created_at) VALUES(?,?,?,?)",
               (ticket_id, actor_id, action, int(time.time())))


def get_ticket(ticket_id):
    with connect() as db:
        return db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()


def open_for_customer(user):
    now = int(time.time())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT * FROM tickets WHERE customer_id=? AND status!='closed'
                            ORDER BY id DESC LIMIT 1""", (user.id,)).fetchone()
        if row:
            db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, row["id"]))
            return row["id"], False
        cur = db.execute("""INSERT INTO tickets(customer_id,customer_name,status,created_at,updated_at)
                            VALUES(?,?,'new',?,?)""", (user.id, user.full_name, now, now))
        ticket_id = cur.lastrowid
        event(db, ticket_id, user.id, "created")
        return ticket_id, True


def link(admin_id, message_id, ticket_id):
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO ticket_links VALUES(?,?,?)",
                   (admin_id, message_id, ticket_id))


def ticket_for_reply(admin_id, message_id):
    with connect() as db:
        row = db.execute("SELECT ticket_id FROM ticket_links WHERE admin_id=? AND message_id=?",
                         (admin_id, message_id)).fetchone()
        return row["ticket_id"] if row else None


def change(ticket_id, admin_id, action):
    """Transakcja atomowa chroni przed równoczesnym przejęciem."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None:
            return "missing"
        if action == "claim":
            if row["status"] == "closed":
                return "closed"
            if row["assigned_admin"] not in (None, admin_id):
                return "taken"
            db.execute("UPDATE tickets SET assigned_admin=?,status='active',updated_at=? WHERE id=?",
                       (admin_id, int(time.time()), ticket_id))
        elif action == "close":
            if row["status"] == "closed":
                return "closed"
            if row["assigned_admin"] != admin_id:
                return "not_owner"
            db.execute("UPDATE tickets SET status='closed',updated_at=? WHERE id=?",
                       (int(time.time()), ticket_id))
        elif action == "reopen":
            if row["status"] != "closed":
                return "already_open"
            db.execute("UPDATE tickets SET status='new',assigned_admin=NULL,updated_at=? WHERE id=?",
                       (int(time.time()), ticket_id))
        else:
            return "unknown"
        event(db, ticket_id, admin_id, action)
        return "ok"


def keyboard(ticket_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🙋 Przejmij", callback_data=f"ticket:claim:{ticket_id}"),
         InlineKeyboardButton("✅ Zamknij", callback_data=f"ticket:close:{ticket_id}")],
        [InlineKeyboardButton("🔄 Otwórz ponownie", callback_data=f"ticket:reopen:{ticket_id}")],
    ])


def summary(row):
    status = {"new": "🆕 Nowe", "active": "🟢 W obsłudze", "closed": "✅ Zamknięte"}.get(row["status"], row["status"])
    owner = str(row["assigned_admin"]) if row["assigned_admin"] else "nieprzypisane"
    return f"🐻 Zgłoszenie #{row['id']}\nStatus: {status}\nObsługuje (Telegram ID): {owner}\nKlient: {row['customer_name']}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if update.effective_user.id in ADMINS:
        await update.message.reply_text(
            "🐻 PuffBear Kontakt — panel zgłoszeń.\n"
            "Nowe sprawy otrzymują obaj administratorzy.\n"
            "Przejmij zgłoszenie, a następnie użyj «Odpowiedz» na wiadomości klienta.\n"
            "/zgloszenia — lista otwartych spraw.")
    else:
        await update.message.reply_text(
            "🐻 PuffBear Kontakt\nNapisz wiadomość, aby utworzyć zgłoszenie do obsługi.")


async def customer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    ticket_id, created = open_for_customer(user)
    row = get_ticket(ticket_id)
    delivered = 0
    for admin_id in ADMINS:
        try:
            header = await context.bot.send_message(
                chat_id=admin_id, text=summary(row),
                reply_markup=keyboard(ticket_id))
            link(admin_id, header.message_id, ticket_id)
            copied = await context.bot.copy_message(
                chat_id=admin_id, from_chat_id=message.chat_id, message_id=message.message_id)
            link(admin_id, copied.message_id, ticket_id)
            delivered += 1
        except TelegramError:
            log.exception("Nie dostarczono zgłoszenia #%s do administratora %s", ticket_id, admin_id)
    if delivered:
        await message.reply_text(
            f"{'✅ Utworzono' if created else '✅ Dodano wiadomość do'} zgłoszenia #{ticket_id}. "
            "Obsługa odpowie tutaj.")
    else:
        await message.reply_text("⚠️ Nie udało się powiadomić obsługi. Spróbuj później.")


async def admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    admin_id = update.effective_user.id
    if not message.reply_to_message:
        await message.reply_text("Odpowiedz na wiadomość z konkretnego zgłoszenia. Lista: /zgloszenia")
        return
    ticket_id = ticket_for_reply(admin_id, message.reply_to_message.message_id)
    if not ticket_id:
        await message.reply_text("Nie znalazłem zgłoszenia. Otwórz /zgloszenia.")
        return
    row = get_ticket(ticket_id)
    if row["status"] == "closed":
        await message.reply_text("To zgłoszenie jest zamknięte. Otwórz je ponownie.")
        return
    if row["assigned_admin"] != admin_id:
        await message.reply_text("Najpierw przejmij zgłoszenie przyciskiem 🙋 Przejmij.")
        return
    try:
        await context.bot.copy_message(
            chat_id=row["customer_id"], from_chat_id=message.chat_id,
            message_id=message.message_id)
        with connect() as db:
            event(db, ticket_id, admin_id, "reply")
            db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (int(time.time()), ticket_id))
        await message.reply_text(f"✅ Odpowiedź wysłana (zgłoszenie #{ticket_id}).")
    except TelegramError:
        log.exception("Nie udało się odpisać na zgłoszenie #%s", ticket_id)
        await message.reply_text("❌ Nie udało się wysłać odpowiedzi.")


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMINS:
        await q.answer("Brak dostępu.", show_alert=True)
        return
    try:
        _, action, raw_id = q.data.split(":")
        ticket_id = int(raw_id)
    except (ValueError, AttributeError):
        await q.answer("Nieprawidłowe zgłoszenie.", show_alert=True)
        return
    result = change(ticket_id, q.from_user.id, action)
    messages = {
        "ok": "Zapisano zmianę.",
        "missing": "Nie znaleziono zgłoszenia.",
        "closed": "Zgłoszenie jest zamknięte.",
        "taken": "Inny administrator już obsługuje tę sprawę.",
        "not_owner": "Tylko przypisany administrator może zamknąć zgłoszenie.",
        "already_open": "Zgłoszenie jest już otwarte.",
    }
    await q.answer(messages.get(result, "Nieznana operacja."), show_alert=result != "ok")
    if result == "ok":
        row = get_ticket(ticket_id)
        try:
            await q.edit_message_text(summary(row), reply_markup=keyboard(ticket_id))
        except TelegramError:
            pass
        if action == "close":
            try:
                await context.bot.send_message(
                    row["customer_id"],
                    f"✅ Zgłoszenie #{ticket_id} zostało zamknięte. "
                    "Jeśli potrzebujesz pomocy, wyślij nową wiadomość.")
            except TelegramError:
                log.exception("Nie udało się powiadomić klienta")


async def tickets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or update.effective_user.id not in ADMINS:
        return
    with connect() as db:
        rows = db.execute("""SELECT * FROM tickets WHERE status!='closed'
                             ORDER BY updated_at DESC LIMIT 20""").fetchall()
    if not rows:
        await update.message.reply_text("📋 Brak otwartych zgłoszeń.")
        return
    for row in rows:
        sent = await update.message.reply_text(summary(row), reply_markup=keyboard(row["id"]))
        link(update.effective_user.id, sent.message_id, row["id"])


async def route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if update.effective_user.id in ADMINS:
        await admin_message(update, context)
    else:
        await customer_message(update, context)


def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("zgloszenia", tickets_command))
    app.add_handler(CallbackQueryHandler(callback, pattern=r"^ticket:(claim|close|reopen):\d+$"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, route))
    log.info("PuffBear Kontakt: %s administratorów", len(ADMINS))
    app.run_polling()


if __name__ == "__main__":
    main()
