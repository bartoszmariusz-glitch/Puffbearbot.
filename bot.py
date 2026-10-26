import os
import json
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN")

ADMIN_CHAT_IDS = ["7191240091", "6487236772"]
SHOP_WEBAPP_URL = "https://classy-cucurucho-a3bc50.netlify.app"

STOCK_FILE = "stock_v2.json"
PROMOTERS_FILE = "promoters_v2.json"
EARNINGS_FILE = "earnings_v2.json"
USERS_FILE = "users_v2.json"
CLIENT_ORDERS_FILE = "client_orders_v2.json"
BANNED_FILE = "banned_v2.json"

FLAVORS_CONFIG = [
    "Strawberry raspberry ice", "Lemon mojito", "Prime Strawberry", "Mixed berries",
    "Crazy blueberry", "Forest berries", "Aloe blackcurrant", "Peach lemonade",
    "Kiwi passion fruit guawa", "Dragon fruit ice"
]

def load_json_file(filename, default_val):
    if not os.path.exists(filename):
        save_json_file(filename, default_val)
        return default_val
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_val

def save_json_file(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

def load_earnings():
    return load_json_file(EARNINGS_FILE, {"bot_sales_total": 0, "total_qty_sold": 0})

def save_earnings(data):
    save_json_file(EARNINGS_FILE, data)

def load_promoters():
    return load_json_file(PROMOTERS_FILE, {
        "ALUS10": {"name": "Alus", "uses": 0, "total_sales": 0},
        "JAYSONEK": {"name": "Sikora", "uses": 0, "total_sales": 0},
        "KULT": {"name": "benek", "uses": 0, "total_sales": 0},
        "KULA2026": {"name": "kula", "uses": 0, "total_sales": 0},
        "SWAGMACIEK": {"name": "Majka", "uses": 0, "total_sales": 0},
        "BUWER21": {"name": "małolat", "uses": 0, "total_sales": 0},
        "STACHU10": {"name": "stachu", "uses": 0, "total_sales": 0},
        "DRESIK10": {"name": "Dresik", "uses": 0, "total_sales": 0}
    })

def save_promoters(data):
    save_json_file(PROMOTERS_FILE, data)

def add_client_order(user_id, summary_str):
    orders = load_json_file(CLIENT_ORDERS_FILE, {})
    user_str = str(user_id)
    if user_str not in orders:
        orders[user_str] = []
    orders[user_str].append({"details": summary_str, "status": "Zatwierdzone"})
    save_json_file(CLIENT_ORDERS_FILE, orders)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔲 Otwórz sklep", web_app=WebAppInfo(url=SHOP_WEBAPP_URL))],
        [InlineKeyboardButton("📦 Moje zamówienia", callback_data="my_orders")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "💨 *Heyka! Tu Twój VAPE 24/7 Bot do zamówień!* 💨\n\nKliknij przycisk poniżej, aby otworzyć sklep:"
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

# UNIWERSALNA FUNKCJA ŁAPIĄCA KAŻDE ZAMÓWIENIE
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    # Sprawdzamy tekst z WebApp (tg.sendData) lub ze zwykłej wiadomości tekstowej
    text = ""
    if message.web_app_data:
        text = message.web_app_data.data
    elif message.text:
        text = message.text

    if "ZAMÓWIENIE" in text.upper() or "PRODUKTY:" in text.upper():
        # Rozsyłamy powiadomienie do administratorów
        for admin_id in ADMIN_CHAT_IDS:
            try:
                await context.bot.send_message(
                    chat_id=int(admin_id),
                    text=f"🚨 **NOWE ZAMÓWIENIE!** 🚨\n\n{text}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Błąd wysyłania do admina: {e}")

        # Potwierdzenie dla klienta
        try:
            await message.reply_text("✅ Twoje zamówienie zostało pomyślnie złożone!")
        except Exception:
            pass

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    
    # Ten handler łapie WSZYSTKO: dane z WebApp ORAZ zwykłe wiadomości tekstowe
    app.add_handler(MessageHandler(filters.WEB_APP_DATA | filters.TEXT & ~filters.COMMAND, handle_all_messages))
    
    print("Bot ruszył...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
