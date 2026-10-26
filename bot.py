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

# ID obu administratorów
ADMIN_CHAT_IDS = ["7191240091", "6487236772"]

# NOWY LINK DO STRONY NA NETLIFY
SHOP_WEBAPP_URL = "https://splendid-pie-735b62.netlify.app"

STOCK_FILE = "stock_v2.json"
PROMOTERS_FILE = "promoters_v2.json"
EARNINGS_FILE = "earnings_v2.json"
USERS_FILE = "users_v2.json"
CLIENT_ORDERS_FILE = "client_orders_v2.json"
BANNED_FILE = "banned_v2.json"

FLAVORS_CONFIG = [
    "Strawberry raspberry ice",
    "Lemon mojito",
    "Prime Strawberry",
    "Mixed berries",
    "Crazy blueberry",
    "Forest berries",
    "Aloe blackcurrant",
    "Peach lemonade",
    "Kiwi passion fruit guawa",
    "Dragon fruit ice"
]

def load_json_file(filename, default_val):
    if not os.path.exists(filename):
        save_json_file(filename, default_val)
        return default_val
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Błąd odczytu {filename}: {e}")
        return default_val

def save_json_file(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        logging.error(f"Błąd zapisu {filename}: {e}")

def load_stock():
    data = load_json_file(STOCK_FILE, {flavor: 20 for flavor in FLAVORS_CONFIG})
    updated = False
    for flavor in FLAVORS_CONFIG:
        if flavor not in data:
            data[flavor] = 20
            updated = True
    if updated:
        save_stock(data)
    return data

def save_stock(stock_data):
    save_json_file(STOCK_FILE, stock_data)

def load_promoters():
    default_promoters = {
        "ALUS10": {"name": "Alus", "discount_percent": 10, "uses": 0, "total_sales": 0},
        "JAYSONEK": {"name": "Sikora", "discount_percent": 10, "uses": 0, "total_sales": 0},
        "KULT": {"name": "benek", "discount_percent": 10, "uses": 0, "total_sales": 0},
        "KULA2026": {"name": "kula", "discount_percent": 10, "uses": 0, "total_sales": 0},
        "SWAGMACIEK": {"name": "Majka", "discount_percent": 10, "uses": 0, "total_sales": 0},
        "BUWER21": {"name": "małolat", "discount_percent": 10, "uses": 0, "total_sales": 0},
        "STACHU10": {"name": "stachu", "discount_percent": 10, "uses": 0, "total_sales": 0},
        "DRESIK10": {"name": "Dresik", "discount_percent": 10, "uses": 0, "total_sales": 0}
    }
    return load_json_file(PROMOTERS_FILE, default_promoters)

def save_promoters(promoters_data):
    save_json_file(PROMOTERS_FILE, promoters_data)

def load_earnings():
    default_earnings = {
        "bot_sales_total": 0,
        "manual_sales_total": 0,
        "total_qty_sold": 0,
        "sales_by_flavor": {},
        "history": []
    }
    return load_json_file(EARNINGS_FILE, default_earnings)

def save_earnings(earnings_data):
    save_json_file(EARNINGS_FILE, earnings_data)

def register_user(user_id):
    users = load_json_file(USERS_FILE, [])
    if user_id not in users:
        users.append(user_id)
        save_json_file(USERS_FILE, users)

def is_banned(user_id):
    banned = load_json_file(BANNED_FILE, [])
    return user_id in banned

def add_client_order(user_id, summary_str, status="Zatwierdzone"):
    orders = load_json_file(CLIENT_ORDERS_FILE, {})
    user_str = str(user_id)
    if user_str not in orders:
        orders[user_str] = []
    orders[user_str].append({
        "details": summary_str,
        "status": status
    })
    if len(orders[user_str]) > 10:
        orders[user_str].pop(0)
    save_json_file(CLIENT_ORDERS_FILE, orders)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_WAITING_FOR_QTY = 100

async def set_bot_menu(application: Application):
    commands = [
        BotCommand("start", "Uruchom sklep"),
        BotCommand("zarobki", "Pulpit obrotów i TOP smaków (Admin)"),
        BotCommand("promotor", "Statystyki kodów promocyjnych (Admin)"),
        BotCommand("magazyn", "Zarządzanie stanem magazynowym (Admin)"),
        BotCommand("mojezamowienia", "Historia zamówień")
    ]
    await application.bot.set_my_commands(commands)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    register_user(str(user_id))
    
    if is_banned(str(user_id)):
        await update.message.reply_text("⛔ Twoje konto zostało zablokowane.")
        return

    keyboard = [
        [InlineKeyboardButton("🔲 Otwórz sklep", web_app=WebAppInfo(url=SHOP_WEBAPP_URL))],
        [InlineKeyboardButton("📞 Kontakt z adminem 1", url="https://t.me/puffbearadmin1")],
        [InlineKeyboardButton("📞 Kontakt z adminem 2", url="https://t.me/puffbearadmin2")],
        [InlineKeyboardButton("📦 Moje zamówienia", callback_data="my_orders")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "💨 *Heyka! Tu Twój VAPE 24/7 Bot do zamówień!* 💨\n\n"
        "Stworzyli mnie po to, żeby zakupy u nas były szybkie, łatwe i przyjemne! 😄\n\n"
        "📦 *Kilka kliknięć i gotowe* — bez stresu, bez czekania.\n"
        "⏰ Jestem dostępny 24/7, więc możesz zamawiać, kiedy tylko chcesz.\n"
        "💰 A najlepsze? Mam dla Ciebie super SMART-ceny!\n"
        "✅ Wybierz to, co Ci się podoba, złóż zamówienie i ciesz się zakupami bez żadnych komplikacji!\n\n"
        "Kliknij przycisk poniżej i złóż zamówienie 24/7! 😎"
    )
    
    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        if query.data == "my_orders":
            return await my_orders_command(update, context)
        await query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

async def my_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    orders_data = load_json_file(CLIENT_ORDERS_FILE, {})
    if user_id not in orders_data or not orders_data[user_id]:
        await update.message.reply_text("📭 Brak historii zamówień.")
        return
    text = "📦 **TWOJE OSTATNIE ZAMÓWIENIA:**\n\n"
    for idx, ord_info in enumerate(orders_data[user_id], 1):
        text += f"**Zamówienie #{idx}**\n{ord_info['details']}\n------------------\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def earnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Brak uprawnień.")
        return
        
    earnings = load_earnings()
    total_sales = earnings.get("bot_sales_total", 0) + earnings.get("manual_sales_total", 0)
    total_qty = earnings.get("total_qty_sold", 0)
    sales_by_flavor = earnings.get("sales_by_flavor", {})
    
    text = "📊 **PULPIT ZAROBKÓW I OBROTU** 📊\n\n"
    if sales_by_flavor:
        text += "🧪 **Sprzedaż wg smaków:**\n"
        for flavor, data in sales_by_flavor.items():
            text += f"• *{flavor}*: **{data['qty']} szt.** — **{data['amount']:.2f} zł**\n"
    else:
        text += "_Brak sprzedanych produktów_\n"
        
    text += f"\n💵 **ŁĄCZNY OBRÓT SKLEPU:** **{total_sales:.2f} zł**\n"
    text += f"📦 **Łącznie sprzedanych sztuk:** {total_qty}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def promoters_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Brak uprawnień.")
        return
        
    promoters = load_promoters()
    stats_text = "📊 **OBRÓT WYGENEROWANY PRZEZ KODY PROMOCYJNE**\n\n"
    for code, info in promoters.items():
        stats_text += f"• Kod: `{code}` (Twórca: **{info.get('name')}**)\n  Użycia: **{info.get('uses', 0)}** | Obroty: **{info.get('total_sales', 0):.2f} zł**\n\n"
    await update.message.reply_text(stats_text, parse_mode="Markdown")

async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Brak uprawnień.")
        return
    return await show_admin_stock_panel(update, context, edit=False)

async def show_admin_stock_panel(update_obj, context, edit=False):
    stock = load_stock()
    keyboard = []
    for flavor in FLAVORS_CONFIG:
        qty = stock.get(flavor, 0)
        keyboard.append([
            InlineKeyboardButton(f"🧪 {flavor[:12]}", callback_data=f"noop_{flavor}"),
            InlineKeyboardButton("➖", callback_data=f"sub_{flavor}"),
            InlineKeyboardButton(f"📦 {qty}", callback_data=f"noop_{flavor}"),
            InlineKeyboardButton("➕", callback_data=f"add_{flavor}")
        ])
    keyboard.append([InlineKeyboardButton("❌ Zamknij", callback_data="close_stock")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "📦 **ZARZĄDZANIE MAGAZYNEM**\nKlikaj plusy i minusy, aby modyfikować stany na bieżąco:"
    
    if edit:
        try:
            await update_obj.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            pass
    else:
        await update_obj.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    return ADMIN_WAITING_FOR_QTY

async def admin_stock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    data = query.data
    
    if data == "close_stock":
        await query.edit_message_text("🔒 Panel magazynowy zamknięty.")
        return ConversationHandler.END

    if data.startswith("add_") or data.startswith("sub_"):
        action, flavor = data.split("_", 1)
        stock = load_stock()
        current = stock.get(flavor, 0)
        if action == "add":
            stock[flavor] = current + 1
        else:
            stock[flavor] = max(0, current - 1)
        save_stock(stock)
        return await show_admin_stock_panel(query, context, edit=True)
        
    return ADMIN_WAITING_FOR_QTY

async def handle_incoming_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    if message_text and "NOWE ZAMÓWIENIE ZE SKLEPU" in message_text:
        try:
            lines = message_text.split("\n")
            total_price = 0
            total_qty = 0
            promo_used = "Brak"
            
            for line in lines:
                if "Kwota do zapłaty:" in line:
                    total_price = float(line.split(":")[-1].replace("zł", "").strip())
                elif "Razem sztuk:" in line:
                    total_qty = int(line.split(":")[-1].strip())
                elif "Kod promocyjny:" in line:
                    promo_used = line.split(":")[-1].strip().upper()

            earnings = load_earnings()
            earnings["bot_sales_total"] += total_price
            earnings["total_qty_sold"] += total_qty
            save_earnings(earnings)

            if promo_used in load_promoters():
                promoters = load_promoters()
                promoters[promo_used]["uses"] += 1
                promoters[promo_used]["total_sales"] += total_price
                save_promoters(promoters)

            add_client_order(update.effective_user.id, message_text)

        except Exception as e:
            logger.error(f"Błąd parsowania zamówienia: {e}")

        for admin_id in ADMIN_CHAT_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=message_text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Błąd wysyłania do admina {admin_id}: {e}")

def main():
    app = Application.builder().token(TOKEN).post_init(set_bot_menu).build()

    stock_conv = ConversationHandler(
        entry_points=[CommandHandler("magazyn", stock_command)],
        states={
            ADMIN_WAITING_FOR_QTY: [CallbackQueryHandler(admin_stock_callback)]
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("zarobki", earnings_command))
    app.add_handler(CommandHandler("promotor", promoters_stats_command))
    app.add_handler(CommandHandler("mojezamowienia", my_orders_command))
    app.add_handler(stock_conv)
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_orders))
    
    print("Bot uruchomiony i gotowy...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
