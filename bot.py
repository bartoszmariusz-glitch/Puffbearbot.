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

# TUTAJ JEST WKLEJONY TWÓJ DOCELOWY LINK DO MINI-APLIKACJI
SHOP_WEBAPP_URL = "https://twojpuffbearbot.netlify.app"

STOCK_FILE = "stock_v2.json"
PROMOTERS_FILE = "promoters_v2.json"
EARNINGS_FILE = "earnings_v2.json"
USERS_FILE = "users_v2.json"
CLIENT_ORDERS_FILE = "client_orders_v2.json"
BANNED_FILE = "banned_v2.json"
PENDING_ORDERS_FILE = "pending_orders_v2.json"

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

CZESTOCHOWA_POINTS = {
    "point_lisiniec": {
        "name": "Park Lisiniec",
        "url": "https://maps.app.goo.gl/Qthxvwqrsb9jhCZL9?g_st=ipc"
    },
    "point_polnoc": {
        "name": "Północ",
        "url": "https://maps.app.goo.gl/u6D89VCY1u8Txpe47?g_st=ipc"
    },
    "point_centrum": {
        "name": "Centrum",
        "url": "https://maps.app.goo.gl/RT62LASvp6FPSdbs6?g_st=ipc"
    },
    "point_estakada": {
        "name": "Estakada",
        "url": "https://maps.app.goo.gl/nBokZ8UCjk4Hh84J8?g_st=ipc"
    },
    "point_trzech_wieszczow": {
        "name": "Trzech Wieszczów",
        "url": "https://maps.app.goo.gl/MudyD4q2ZW8eDwwc8?g_st=ipc"
    }
}

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
            data[flavor] = 0
            updated = True
    if updated:
        save_stock(data)
    return data

def save_stock(stock_data):
    save_json_file(STOCK_FILE, stock_data)

def get_flavor_stock(flavor_name: str) -> int:
    stock = load_stock()
    return stock.get(flavor_name, 0)

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
    data = load_json_file(PROMOTERS_FILE, default_promoters)
    updated = False
    for code, info in default_promoters.items():
        if code not in data:
            data[code] = info
            updated = True
    if updated:
        save_promoters(data)
    return data

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
    data = load_json_file(EARNINGS_FILE, default_earnings)
    if "sales_by_flavor" not in data:
        data["sales_by_flavor"] = {}
    return data

def save_earnings(earnings_data):
    save_json_file(EARNINGS_FILE, earnings_data)

def add_sale_record(source: str, flavor: str, qty: int, amount: float):
    earnings = load_earnings()
    if source == "bot":
        earnings["bot_sales_total"] += amount
    else:
        earnings["manual_sales_total"] += amount
        
    earnings["total_qty_sold"] += qty
    
    if flavor not in earnings["sales_by_flavor"]:
        earnings["sales_by_flavor"][flavor] = {"qty": 0, "amount": 0.0}
    
    earnings["sales_by_flavor"][flavor]["qty"] += qty
    earnings["sales_by_flavor"][flavor]["amount"] += amount

    earnings["history"].append({
        "source": source,
        "flavor": flavor,
        "qty": qty,
        "amount": amount
    })
    save_earnings(earnings)

def subtract_sale_record(flavor: str, qty: int, amount: float):
    earnings = load_earnings()
    earnings["manual_sales_total"] -= amount
    earnings["total_qty_sold"] = max(0, earnings["total_qty_sold"] - qty)
    
    if flavor in earnings["sales_by_flavor"]:
        earnings["sales_by_flavor"][flavor]["qty"] = max(0, earnings["sales_by_flavor"][flavor]["qty"] - qty)
        earnings["sales_by_flavor"][flavor]["amount"] = max(0.0, earnings["sales_by_flavor"][flavor]["amount"] - amount)

    earnings["history"].append({
        "source": "correction",
        "flavor": flavor,
        "qty": -qty,
        "amount": -amount
    })
    save_earnings(earnings)

def register_user(user_id):
    users = load_json_file(USERS_FILE, [])
    if user_id not in users:
        users.append(user_id)
        save_json_file(USERS_FILE, users)

def is_banned(user_id):
    banned = load_json_file(BANNED_FILE, [])
    return user_id in banned

def add_client_order(user_id, summary_str, status="Oczekujące"):
    orders = load_json_file(CLIENT_ORDERS_FILE, {})
    user_str = str(user_id)
    if user_str not in orders:
        orders[user_str] = []
    orders[user_str].append({
        "details": summary_str,
        "status": status
    })
    if len(orders[user_str]) > 5:
        orders[user_str].pop(0)
    save_json_file(CLIENT_ORDERS_FILE, orders)

def update_client_order_status(user_id, status):
    orders = load_json_file(CLIENT_ORDERS_FILE, {})
    user_str = str(user_id)
    if user_str in orders and orders[user_str]:
        orders[user_str][-1]["status"] = status
        save_json_file(CLIENT_ORDERS_FILE, orders)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

ADMIN_WAITING_FOR_QTY = 100
MANUAL_CHOOSE_FLAVOR, MANUAL_ENTER_QTY, MANUAL_ENTER_AMOUNT, MANUAL_CONFIRM = range(300, 304)
CORRECT_CHOOSE_FLAVOR, CORRECT_ENTER_QTY, CORRECT_ENTER_AMOUNT, CORRECT_CONFIRM = range(400, 404)

def escape_html(text: str) -> str:
    if not text:
        return ""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def escape_md(text: str) -> str:
    if not text:
        return ""
    return str(text).replace('_', '\\_').replace('*', '\\*').replace('`', '\\`').replace('[', '\\[')

def calculate_order_totals(cart, discount_percent=0):
    total_qty = sum(item['qty'] for item in cart)
    standard_price = total_qty * 55 
    
    if total_qty >= 50:
        price_per_item = 29
    elif total_qty >= 20:
        price_per_item = 33
    elif total_qty >= 10:
        price_per_item = 40
    elif total_qty >= 5:
        price_per_item = 45
    elif total_qty >= 3:
        price_per_item = 50
    else:
        price_per_item = 55
        
    total_price = total_qty * price_per_item
    
    discount_amount = 0
    if discount_percent > 0:
        discount_amount = int(round(total_price * (discount_percent / 100)))
        total_price -= discount_amount

    savings = standard_price - total_price
        
    return total_qty, price_per_item, total_price, savings, discount_amount

async def set_bot_menu(application: Application):
    commands = [
        BotCommand("start", "Uruchom PuffBearBot"),
        BotCommand("mojezamowienia", "Sprawdź historię swoich zamówień"),
        BotCommand("help", "Pomoc i informacje"),
        BotCommand("kontakt", "Skontaktuj się z administracją"),
        BotCommand("magazyn", "Panel zarządzania magazynem (Admin)"),
        BotCommand("promotor", "Statystyki kodów promotorów (Admin)"),
        BotCommand("resetkod", "Wyzeruj statystyki danego kodu (Admin)"),
        BotCommand("sprzedaz", "Dodaj sprzedaż ręczną poza botem (Admin)"),
        BotCommand("korekta", "Odejmij omyłkowo dodaną sprzedaż/zarobki (Admin)"),
        BotCommand("zarobki", "Wyświetl szczegółowe zarobki i TOP smaków (Admin)"),
        BotCommand("ogloszenie", "Wyślij ogłoszenie do wszystkich klientów (Admin)"),
        BotCommand("ban", "Zablokuj użytkownika (Admin)"),
        BotCommand("unban", "Odblokuj użytkownika (Admin)"),
        BotCommand("resetzarobki", "Wyzeruj statystyki zarobków (Admin)")
    ]
    await application.bot.set_my_commands(commands)

async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Nie masz uprawnień.")
        return
    return await show_admin_stock_panel(update, context, edit=False)

async def promoters_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Nie masz uprawnień.")
        return
        
    promoters = load_promoters()
    if not promoters:
        await update.message.reply_text("📭 Brak skonfigurowanych kodów promotorów.")
        return
        
    stats_text = "📊 **STATYSTYKI KODÓW TWÓRCÓW**\n\n"
    for code, info in promoters.items():
        stats_text += f"• Kod: `{code}`\n  Twórca: **{info.get('name')}**\n  Użycia: **{info.get('uses', 0)}**\n  Wygenerowany obrót: **{info.get('total_sales', 0):.2f} zł**\n\n"
    
    stats_text += "💡 _Aby wyzerować konkretny kod, użyj komendy:_\n`/resetkod KOD`"
    await update.message.reply_text(stats_text, parse_mode="Markdown")

async def reset_promoter_code_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Nie masz uprawnień.")
        return
        
    args = context.args
    if not args:
        promoters = load_promoters()
        codes_list = ", ".join([f"`{c}`" for c in promoters.keys()])
        await update.message.reply_text(
            "⚠️ **Podaj kod do wyzerowania!**\n\n"
            f"Przykład użycia: `/resetkod JAYSONEK`\n\n"
            f"Dostępne kody:\n{codes_list}",
            parse_mode="Markdown"
        )
        return
        
    code_to_reset = args[0].strip().upper()
    promoters = load_promoters()
    
    if code_to_reset in promoters:
        promoters[code_to_reset]["uses"] = 0
        promoters[code_to_reset]["total_sales"] = 0
        save_promoters(promoters)
        
        await update.message.reply_text(
            f"✅ **Sukces!** Statystyki dla kodu `{code_to_reset}` (Twórca: **{promoters[code_to_reset]['name']}**) zostały wyzerowane.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Nie znaleziono kodu `{code_to_reset}` w bazie promotorów.",
            parse_mode="Markdown"
        )

async def earnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Nie masz uprawnień.")
        return
        
    earnings = load_earnings()
    bot_total = earnings.get("bot_sales_total", 0)
    manual_total = earnings.get("manual_sales_total", 0)
    grand_total = bot_total + manual_total
    total_qty = earnings.get("total_qty_sold", 0)
    sales_by_flavor = earnings.get("sales_by_flavor", {})
    
    text = "📊 **SZCZEGÓŁOWE ZAROBKI I SPRZEDAŻ** 📊\n\n"
    text += "🧪 **Sprzedaż wg smaków:**\n"
    
    if sales_by_flavor:
        for flavor, data in sales_by_flavor.items():
            text += f"• *{escape_md(flavor)}*: **{data['qty']} szt.** — **{data['amount']:.2f} zł**\n"
            
        sorted_flavors = sorted(sales_by_flavor.items(), key=lambda x: x[1]['qty'], reverse=True)
        text += "\n🏆 **TOP 3 NAJCHĘTNIEJ KUPOWANE SMAKI:**\n"
        for i, (flv, d) in enumerate(sorted_flavors[:3], 1):
            text += f"{i}. *{escape_md(flv)}* — {d['qty']} szt.\n"
    else:
        text += "_Brak zarejestrowanych sprzedaży_\n"
        
    text += "\n------------------------------------\n"
    text += f"🤖 Sprzedaż przez bota: **{bot_total:.2f} zł**\n"
    text += f"🤝 Sprzedaż ręczna (poza botem): **{manual_total:.2f} zł**\n"
    text += "------------------------------------\n"
    text += f"💵 **ŁĄCZNY OBRÓT:** **{grand_total:.2f} zł**\n"
    text += f"📦 Łącznie sprzedanych sztuk: **{total_qty}**\n\n"
    text += "💡 _Aby odjąć omyłkowo dodaną sprzedaż: /korekta_\n"
    text += "💡 _Aby wyzerować statystyki zarobków: /resetzarobki_"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def reset_earnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Nie masz uprawnień.")
        return
        
    empty_earnings = {
        "bot_sales_total": 0,
        "manual_sales_total": 0,
        "total_qty_sold": 0,
        "sales_by_flavor": {},
        "history": []
    }
    save_earnings(empty_earnings)
    
    await update.message.reply_text(
        "🔄 **Sukces!** Statystyki zarobków, historii i sprzedanych sztuk zostały wyzerowane.",
        parse_mode="Markdown"
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Nie masz uprawnień.")
        return
        
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ **Podaj treść ogłoszenia!**\n\nPrzykład użycia:\n`/ogloszenie Witajcie! Nowe smaki już w sprzedaży!`",
            parse_mode="Markdown"
        )
        return
        
    announcement_text = "📢 **OGŁOSZENIE OD PUSHBEAR:**\n\n" + " ".join(args)
    users = load_json_file(USERS_FILE, [])
    
    success_count = 0
    fail_count = 0
    
    for uid in users:
        try:
            await context.bot.send_message(chat_id=int(uid), text=announcement_text, parse_mode="Markdown")
            success_count += 1
        except Exception:
            fail_count += 1
            
    await update.message.reply_text(
        f"✅ **Wysłano ogłoszenie!**\n\n• Dostarczono do: **{success_count}** użytkowników\n• Błędy / zablokowani: **{fail_count}**",
        parse_mode="Markdown"
    )

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Nie masz uprawnień.")
        return
        
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Podaj ID użytkownika do zablokowania, np. `/ban 123456789`", parse_mode="Markdown")
        return
        
    target_id = args[0].strip()
    banned = load_json_file(BANNED_FILE, [])
    if target_id not in banned:
        banned.append(target_id)
        save_json_file(BANNED_FILE, banned)
        await update.message.reply_text(f"✅ Użytkownik o ID `{target_id}` został zbanowany.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ Użytkownik o ID `{target_id}` jest już zbanowany.", parse_mode="Markdown")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Nie masz uprawnień.")
        return
        
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Podaj ID użytkownika do odblokowania, np. `/unban 123456789`", parse_mode="Markdown")
        return
        
    target_id = args[0].strip()
    banned = load_json_file(BANNED_FILE, [])
    if target_id in banned:
        banned.remove(target_id)
        save_json_file(BANNED_FILE, banned)
        await update.message.reply_text(f"✅ Użytkownik o ID `{target_id}` został odblokowany.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ Użytkownik o ID `{target_id}` nie był zbanowany.", parse_mode="Markdown")

async def my_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    orders_data = load_json_file(CLIENT_ORDERS_FILE, {})
    
    if user_id not in orders_data or not orders_data[user_id]:
        await update.message.reply_text("📭 Nie masz jeszcze żadnych historii zamówień w tym bocie.")
        return
        
    text = "📦 **TWOJE OSTATNIE ZAMÓWIENIA:**\n\n"
    for idx, ord_info in enumerate(orders_data[user_id], 1):
        status = ord_info.get('status', 'Nieznany')
        text += f"**Zamówienie #{idx}** [Status: *{status}*]\n{ord_info['details']}\n----------------------------------\n"
        
    await update.message.reply_text(text, parse_mode="Markdown")

async def manual_sale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        if update.message:
            await update.message.reply_text("⛔ Nie masz uprawnień.")
        return ConversationHandler.END

    context.user_data.clear()
    keyboard = []
    for idx, flavor in enumerate(FLAVORS_CONFIG):
        stock = get_flavor_stock(flavor)
        keyboard.append([InlineKeyboardButton(f"🧪 {flavor} (Magazyn: {stock})", callback_data=f"man_flavor_{idx}")])
    
    keyboard.append([InlineKeyboardButton("❌ Anuluj", callback_data="man_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🤝 **RĘCZNA SPRZEDAŻ (POZA BOTEM)**\n\n"
        "1️⃣ Wybierz smak, który został sprzedany:"
    )

    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    return MANUAL_CHOOSE_FLAVOR

async def manual_sale_flavor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data
    if data == "man_cancel":
        await query.edit_message_text("❌ Anulowano sprzedaż ręczną.")
        return ConversationHandler.END

    try:
        idx = int(data.replace("man_flavor_", ""))
        flavor_name = FLAVORS_CONFIG[idx]
    except (ValueError, IndexError):
        return MANUAL_CHOOSE_FLAVOR

    context.user_data['manual_flavor'] = flavor_name

    keyboard = [[InlineKeyboardButton("⬅️ Cofnij do wyboru smaku", callback_data="man_back_to_flavor"), InlineKeyboardButton("❌ Anuluj", callback_data="man_cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"📝 Wybrany smak: **{escape_md(flavor_name)}**\n\n"
        f"2️⃣ Wpisz w odpowiedzi **liczbę sztuk**, którą sprzedałeś (np. `1`, `2`, `5`):",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return MANUAL_ENTER_QTY

async def manual_sale_receive_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        return ConversationHandler.END

    text_input = update.message.text.strip()
    try:
        qty = int(text_input)
        if qty <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Podaj poprawną liczbę sztuk większą od zera (np. 1, 2, 3):")
        return MANUAL_ENTER_QTY

    context.user_data['manual_qty'] = qty
    flavor_name = context.user_data.get('manual_flavor')

    await update.message.reply_text(
        f"📦 Wybrano: **{qty} szt.** smaku **{escape_md(flavor_name)}**\n\n"
        f"3️⃣ Wpisz w odpowiedzi **całkowitą kwotę w zł**, jaką zarobiłeś na tej transakcji (np. `55`, `100`, `150`):\n\n"
        f"_(Wpisz np. 'cofnij', aby zmienić liczbę sztuk)_",
        parse_mode="Markdown"
    )
    return MANUAL_ENTER_AMOUNT

async def manual_sale_receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        return ConversationHandler.END

    text_input = update.message.text.strip().lower()
    if text_input in ["cofnij", "wróc", "wroc"]:
        flavor_name = context.user_data.get('manual_flavor')
        await update.message.reply_text(
            f"📝 Wybrany smak: **{escape_md(flavor_name)}**\n\n"
            f"2️⃣ Wpisz nową **liczbę sztuk**: (lub użyj /cancel, aby przerwać)",
            parse_mode="Markdown"
        )
        return MANUAL_ENTER_QTY

    try:
        amount = float(text_input.replace(',', '.'))
        if amount < 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Podaj poprawną kwotę w złotówkach (np. 55 lub 49.50) lub wpisz 'cofnij':")
        return MANUAL_ENTER_AMOUNT

    context.user_data['manual_amount'] = amount
    flavor_name = context.user_data.get('manual_flavor')
    qty = context.user_data.get('manual_qty')

    keyboard = [
        [InlineKeyboardButton("✅ Potwierdź i zapisz", callback_data="man_confirm_yes")],
        [InlineKeyboardButton("🔄 Zmień ilość / cenę (Cofnij)", callback_data="man_back_to_flavor")],
        [InlineKeyboardButton("❌ Anuluj", callback_data="man_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📋 **PODSUMOWANIE TRANSAKCJI RĘCZNEJ:**\n\n"
        f"• Smak: **{escape_md(flavor_name)}**\n"
        f"• Ilość: **{qty} szt.**\n"
        f"• Kwota: **{amount:.2f} zł**\n\n"
        "Czy wszystko się zgadza?",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return MANUAL_CONFIRM

async def manual_sale_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data
    if data == "man_cancel":
        await query.edit_message_text("❌ Anulowano sprzedaż ręczną.")
        return ConversationHandler.END

    if data == "man_back_to_flavor":
        return await manual_sale_start(update, context)

    if data == "man_confirm_yes":
        flavor_name = context.user_data.get('manual_flavor')
        qty = context.user_data.get('manual_qty')
        amount = context.user_data.get('manual_amount')

        stock = load_stock()
        current_stock = stock.get(flavor_name, 0)
        new_stock = max(0, current_stock - qty)
        stock[flavor_name] = new_stock
        save_stock(stock)

        add_sale_record(source="manual", flavor=flavor_name, qty=qty, amount=amount)

        await query.edit_message_text(
            f"✅ **Zarejestrowano sprzedaż ręczną!**\n\n"
            f"• Smak: **{escape_md(flavor_name)}**\n"
            f"• Ilość: **{qty} szt.**\n"
            f"• Kwota: **{amount:.2f} zł**\n"
            f"• Stan magazynowy zaktualizowany: `{current_stock} ➔ {new_stock}`\n\n"
            f"Użyj `/zarobki`, aby sprawdzić aktualny stan finansów.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def correction_sale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        if update.message:
            await update.message.reply_text("⛔ Nie masz uprawnień.")
        return ConversationHandler.END

    context.user_data.clear()
    keyboard = []
    for idx, flavor in enumerate(FLAVORS_CONFIG):
        keyboard.append([InlineKeyboardButton(f"🧪 {flavor}", callback_data=f"corr_flavor_{idx}")])
    
    keyboard.append([InlineKeyboardButton("❌ Anuluj", callback_data="corr_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🛠️ **KOREKTA ZAROBKÓW (ODEJMOWANIE POBŁĘDZIE)**\n\n"
        "Wybierz smak, który chcesz odjąć ze statystyk zarobków:"
    )

    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    return CORRECT_CHOOSE_FLAVOR

async def correction_sale_flavor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data
    if data == "corr_cancel":
        await query.edit_message_text("❌ Anulowano korektę.")
        return ConversationHandler.END

    try:
        idx = int(data.replace("corr_flavor_", ""))
        flavor_name = FLAVORS_CONFIG[idx]
    except (ValueError, IndexError):
        return CORRECT_CHOOSE_FLAVOR

    context.user_data['correct_flavor'] = flavor_name

    keyboard = [[InlineKeyboardButton("⬅️ Cofnij", callback_data="corr_back_to_flavor"), InlineKeyboardButton("❌ Anuluj", callback_data="corr_cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"📝 Wybrany smak do korekty: **{escape_md(flavor_name)}**\n\n"
        f"2️⃣ Wpisz w odpowiedzi **liczbę sztuk**, którą chcesz OJMĄĆ (np. `1`, `2`):",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return CORRECT_ENTER_QTY

async def correction_sale_receive_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        return ConversationHandler.END

    text_input = update.message.text.strip()
    try:
        qty = int(text_input)
        if qty <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Podaj poprawną liczbę sztuk większą od zera (np. 1, 2):")
        return CORRECT_ENTER_QTY

    context.user_data['correct_qty'] = qty
    flavor_name = context.user_data.get('correct_flavor')

    await update.message.reply_text(
        f"📦 Do odjęcia: **{qty} szt.** smaku **{escape_md(flavor_name)}**\n\n"
        f"3️⃣ Wpisz w odpowiedzi **kwotę w zł**, jaką chcesz OJMĄĆ z zarobków (np. `55` lub `100`):\n\n"
        f"_(Wpisz np. 'cofnij', aby zmienić liczbę sztuk)_",
        parse_mode="Markdown"
    )
    return CORRECT_ENTER_AMOUNT

async def correction_sale_receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        return ConversationHandler.END

    text_input = update.message.text.strip().lower()
    if text_input in ["cofnij", "wróc", "wroc"]:
        flavor_name = context.user_data.get('correct_flavor')
        await update.message.reply_text(
            f"📝 Wybrany smak: **{escape_md(flavor_name)}**\n\n"
            f"2️⃣ Wpisz nową **liczbę sztuk** do odjęcia:",
            parse_mode="Markdown"
        )
        return CORRECT_ENTER_QTY

    try:
        amount = float(text_input.replace(',', '.'))
        if amount < 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Podaj poprawną kwotę w złotówkach (np. 55):")
        return CORRECT_ENTER_AMOUNT

    context.user_data['correct_amount'] = amount
    flavor_name = context.user_data.get('correct_flavor')
    qty = context.user_data.get('correct_qty')

    keyboard = [
        [InlineKeyboardButton("✅ Potwierdź i oddejmij", callback_data="corr_confirm_yes")],
        [InlineKeyboardButton("🔄 Zmień (Cofnij)", callback_data="corr_back_to_flavor")],
        [InlineKeyboardButton("❌ Anuluj", callback_data="corr_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📋 **PODSUMOWANIE KOREKTY (ODEJMOWANIA):**\n\n"
        f"• Smak: **{escape_md(flavor_name)}**\n"
        f"• Odjęta ilość: **-{qty} szt.**\n"
        f"• Odjęta kwota: **-{amount:.2f} zł**\n\n"
        "Czy zatwierdzić korektę?",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return CORRECT_CONFIRM

async def correction_sale_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data
    if data == "corr_cancel":
        await query.edit_message_text("❌ Anulowano korektę.")
        return ConversationHandler.END

    if data == "corr_back_to_flavor":
        return await correction_sale_start(update, context)

    if data == "corr_confirm_yes":
        flavor_name = context.user_data.get('correct_flavor')
        qty = context.user_data.get('correct_qty')
        amount = context.user_data.get('correct_amount')

        subtract_sale_record(flavor=flavor_name, qty=qty, amount=amount)

        await query.edit_message_text(
            f"✅ **Wykonano korektę zarobków!**\n\n"
            f"• Smak: **{escape_md(flavor_name)}**\n"
            f"• Odjęto sztuk: **-{qty}**\n"
            f"• Odjęto z obrotu: **-{amount:.2f} zł**\n\n"
            f"Użyj `/zarobki`, aby sprawdzić zaktualizowane finanse.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def show_admin_stock_panel(update_obj, context, edit=False):
    stock = load_stock()
    keyboard = []
    
    for flavor in FLAVORS_CONFIG:
        qty = stock.get(flavor, 0)
        status_emoji = "✅" if qty > 0 else "❌"
        keyboard.append([
            InlineKeyboardButton(f"{status_emoji} {flavor[:10]}", callback_data=f"adm_noop_{flavor}"),
            InlineKeyboardButton("➖ 5", callback_data=f"adm_sub5_{flavor}"),
            InlineKeyboardButton("➕ 5", callback_data=f"adm_add5_{flavor}"),
            InlineKeyboardButton("➕ 10", callback_data=f"adm_add10_{flavor}"),
            InlineKeyboardButton("✏️", callback_data=f"adm_edit_{flavor}"),
            InlineKeyboardButton(f"📦 {qty}", callback_data=f"adm_noop_{flavor}")
        ])
        
    keyboard.append([InlineKeyboardButton("💾 Zapisz stan", callback_data="adm_save_all")])
    keyboard.append([InlineKeyboardButton("🔙 Zamknij panel", callback_data="adm_close")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = "📦 **PANEL ZARZĄDZANIA MAGAZYNEM**\n\nUżyj przycisków, aby zarządzać stanem. Kliknij **Zapisz stan**, aby upewnić się, że zmiany są zapisane:"
    
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
    
    if data == "adm_close":
        try:
            await query.edit_message_text("🔒 Panel magazynowy zamknięty.")
        except Exception:
            pass
        return ConversationHandler.END

    if data == "adm_save_all":
        stock = load_stock()
        save_stock(stock)
        try:
            await query.answer("✅ Stan magazynu został pomyślnie zapisany!", show_alert=True)
        except Exception:
            pass
        return await show_admin_stock_panel(query, context, edit=True)
        
    if data.startswith("adm_noop_"):
        return ADMIN_WAITING_FOR_QTY
        
    if data.startswith("adm_add5_"):
        flavor_name = data.replace("adm_add5_", "")
        stock = load_stock()
        stock[flavor_name] = stock.get(flavor_name, 0) + 5
        save_stock(stock)
        return await show_admin_stock_panel(query, context, edit=True)

    if data.startswith("adm_add10_"):
        flavor_name = data.replace("adm_add10_", "")
        stock = load_stock()
        stock[flavor_name] = stock.get(flavor_name, 0) + 10
        save_stock(stock)
        return await show_admin_stock_panel(query, context, edit=True)

    if data.startswith("adm_sub5_"):
        flavor_name = data.replace("adm_sub5_", "")
        stock = load_stock()
        current_qty = stock.get(flavor_name, 0)
        stock[flavor_name] = max(0, current_qty - 5)
        save_stock(stock)
        return await show_admin_stock_panel(query, context, edit=True)
        
    if data.startswith("adm_edit_"):
        flavor_name = data.replace("adm_edit_", "")
        context.user_data['editing_flavor'] = flavor_name
        
        keyboard = [[InlineKeyboardButton("🔙 Anuluj / Wróć do listy", callback_data="adm_back_to_list")]]
        await query.edit_message_text(
            f"✏️ Edytujesz stan dla: **{escape_md(flavor_name)}**\n\n"
            f"Wpisz w odpowiedzi nową **liczbę sztuk** (np. `15`):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return ADMIN_WAITING_FOR_QTY
        
    if data == "adm_back_to_list":
        return await show_admin_stock_panel(query, context, edit=True)
        
    return ADMIN_WAITING_FOR_QTY

async def admin_receive_new_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        return
        
    flavor_name = context.user_data.get('editing_flavor')
    if not flavor_name:
        await update.message.reply_text("Wystąpił błąd. Wpisz /magazyn, aby otworzyć panel ponownie.")
        return
        
    try:
        new_qty = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Podana ilość musi być cyfrą (np. 10). Spróbuj ponownie:")
        return
        
    stock = load_stock()
    stock[flavor_name] = new_qty
    save_stock(stock)
    
    await update.message.reply_text(f"✅ Zaktualizowano i zapisano! **{flavor_name}** ma teraz **{new_qty} szt.**", parse_mode="Markdown")
    return await show_admin_stock_panel(update, context, edit=False)

async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact_text = (
        "📞 **Kontakt z administracją PuffBear:**\n\n"
        "W razie pytań lub problemów napisz do naszych administratorów:\n"
        "• @puffbearadmin1\n"
        "• @puffbearadmin2"
    )
    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        keyboard = [[InlineKeyboardButton("🔙 Wróć do menu", callback_data="back_to_start")]]
        await query.edit_message_text(contact_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(contact_text, parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    register_user(str(user_id))
    
    if is_banned(str(user_id)):
        await update.message.reply_text("⛔ Twoje konto zostało zablokowane w tym sklepie.")
        return

    keyboard = [
        [InlineKeyboardButton("🛒 Złóż zamówienie (Sklep)", web_app=WebAppInfo(url=SHOP_WEBAPP_URL))],
        [InlineKeyboardButton("📦 Moje zamówienia", callback_data="my_orders")],
        [InlineKeyboardButton("📞 Kontakt / Pomoc", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "🐻💨 **Witaj w oficjalnym sklepie PuffBear!** 💨🐻\n\n"
        "Najlepsze e-liquidy i niezapomniane vape vibes w jednym miejscu.\n\n"
        "💰 **Aktualny cennik / Smart Ceny:**\n"
        "• 1 sztuka: **55 zł / szt.**\n"
        "• 3 sztuki: **50 zł / szt.**\n"
        "• 5+ sztuk: **45 zł / szt.**\n"
        "• 10+ sztuk: **40 zł / szt.**\n"
        "• 20+ sztuk: **33 zł / szt.**\n"
        "• 50+ sztuk: **29 zł / szt.**\n\n"
        "Kliknij przycisk poniżej, aby otworzyć sklep w wygodnym oknie! 👇"
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
        if os.path.exists("produkt.jpg"):
            with open("produkt.jpg", "rb") as photo_file:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photo_file,
                    caption=welcome_text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
    
    return ConversationHandler.END

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await contact_command(update, context)

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(str(user.id)):
        return

    try:
        data_json = update.message.web_app_data.data
        cart = json.loads(data_json)
    except Exception as e:
        logger.error(f"Błąd odczytu danych z WebApp: {e}")
        await update.message.reply_text("❌ Wystąpił błąd podczas przesyłania koszyka ze sklepu.")
        return

    if not cart:
        await update.message.reply_text("🛒 Twój koszyk jest pusty.")
        return

    total_qty, price_per_item, products_price, savings, _ = calculate_order_totals_from_dict(cart)
    
    cart_formatted = "\n".join([f"• {escape_html(flavor)} – {qty} szt." for flavor, qty in cart.items()])
    savings_str = f"\n🎉 <b>Zaoszczędziłeś:</b> {savings} zł!" if savings > 0 else ""
    
    user_mention = f"<a href='tg://user?id={user.id}'>{escape_html(user.first_name)}</a>"
    username_str = f"(@{escape_html(user.username)})" if user.username else ""

    admin_message = (
        "⏳ <b>NOWE ZAMÓWIENIE Z MINI-APLIKACJI</b>\n\n"
        f"👤 <b>Klient:</b> {user_mention} {username_str}\n"
        f"🆔 <b>ID Telegram:</b> <code>{user.id}</code>\n\n"
        f"🧪 <b>Produkty:</b>\n{cart_formatted}\n\n"
        f"📦 <b>Łącznie sztuk:</b> {total_qty}\n"
        f"💰 <b>Wartość produktów:</b> {products_price} zł ({price_per_item} zł/szt){savings_str}\n\n"
        "<i>Wybierz akcję dla tego zamówienia:</i>"
    )

    pending_orders = load_json_file(PENDING_ORDERS_FILE, {})
    pending_orders[str(user.id)] = {
        "cart": [{"flavor": f, "qty": q} for f, q in cart.items()],
        "final_total": products_price,
        "delivery_type": "Mini App WebShop",
        "promoter_code_key": None
    }
    save_json_file(PENDING_ORDERS_FILE, pending_orders)

    order_summary_string = f"🧪 Produkty:\n{cart_formatted}\n📦 Razem: {total_qty} szt. | 💵 Koszt: {products_price} zł"
    add_client_order(user.id, order_summary_string, status="Oczekujące na akceptację")

    keyboard = [
        [
            InlineKeyboardButton("✅ Potwierdź zamówienie", callback_data=f"appr_yes_{user.id}"),
            InlineKeyboardButton("❌ Odrzuć", callback_data=f"appr_no_{user.id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    for admin_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_message,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Nie udało się wysłać powiadomienia do admina {admin_id}: {e}")

    await update.message.reply_text(
        "✅ <b>Twoje zamówienie zostało pomyślnie wysłane do administratora!</b>\n\n"
        "Czekaj na zatwierdzenie. Status możesz sprawdzić w komendzie /mojezamowienia.",
        parse_mode="HTML"
    )

def calculate_order_totals_from_dict(cart_dict):
    total_qty = sum(cart_dict.values())
    standard_price = total_qty * 55
    
    if total_qty >= 50:
        price_per_item = 29
    elif total_qty >= 20:
        price_per_item = 33
    elif total_qty >= 10:
        price_per_item = 40
    elif total_qty >= 5:
        price_per_item = 45
    elif total_qty >= 3:
        price_per_item = 50
    else:
        price_per_item = 55
        
    total_price = total_qty * price_per_item
    savings = standard_price - total_price
    return total_qty, price_per_item, total_price, savings, 0

async def admin_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    if user_id not in ADMIN_CHAT_IDS:
        await query.answer("⛔ Brak uprawnień.", show_alert=True)
        return

    data = query.data
    parts = data.split("_")
    if len(parts) < 3:
        await query.edit_message_text("⚠️ Błąd struktury przycisku.")
        return

    action = parts[1]
    target_client_id = parts[2]

    pending_orders = load_json_file(PENDING_ORDERS_FILE, {})
    order_data = pending_orders.get(target_client_id)

    if action == "no":
        if target_client_id in pending_orders:
            pending_orders.pop(target_client_id, None)
            save_json_file(PENDING_ORDERS_FILE, pending_orders)
            
        update_client_order_status(int(target_client_id), status="Odrzucone")
        await query.edit_message_text("❌ Zamówienie zostało odrzucone.")
        try:
            await context.bot.send_message(
                chat_id=int(target_client_id),
                text="❌ Twoje zamówienie zostało niestety odrzucone przez administratora.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    if action == "yes":
        if not order_data:
            await query.edit_message_text("⚠️ Nie znaleziono danych tego zamówienia (mogło zostać już obsłużone).")
            return

        cart = order_data['cart']
        stock = load_stock()
        low_stock_warnings = []
        
        for item in cart:
            flavor = item['flavor']
            qty_ordered = item['qty']
            if flavor in stock:
                stock[flavor] = max(0, stock[flavor] - qty_ordered)
                if stock[flavor] <= 3:
                    low_stock_warnings.append(f"⚠️ Niski stan! *{flavor}*: pozostało **{stock[flavor]} szt.**")
                    
        save_stock(stock)

        total_qty, price_per_item, products_price, _, _ = calculate_order_totals_from_dict({item['flavor']: item['qty'] for item in cart})
        for item in cart:
            item_subtotal = item['qty'] * price_per_item
            add_sale_record(source="bot", flavor=item['flavor'], qty=item['qty'], amount=item_subtotal)

        pending_orders.pop(target_client_id, None)
        save_json_file(PENDING_ORDERS_FILE, pending_orders)
        
        update_client_order_status(int(target_client_id), status="Zatwierdzone / Przygotowywane")
        
        try:
            old_text = query.message.text if query.message else "Zamówienie"
            await query.edit_message_text(old_text + "\n\n✅ **ZAMÓWIENIE ZATWIERDZONE, MAGAZYN ZAKTUALIZOWANY!**", parse_mode="Markdown")
        except Exception:
            pass

        if low_stock_warnings:
            warning_text = "🚨 **ALARM MAGAZYNOWY:**\n\n" + "\n".join(low_stock_warnings)
            for admin_id in ADMIN_CHAT_IDS:
                try:
                    await context.bot.send_message(chat_id=int(admin_id), text=warning_text, parse_mode="Markdown")
                except Exception:
                    pass

        initial_ai_msg = "Cześć! Twoje zamówienie w PuffBear zostało właśnie zatwierdzone. Gdzie nadamy paczkę / na który punkt dostarczyć?"
        
        try:
            await context.bot.send_message(
                chat_id=int(target_client_id),
                text=f"✅ <b>Twoje zamówienie w PuffBear zostało zatwierdzone przez administratora!</b>\n\n{initial_ai_msg}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Nie udało się wysłać powiadomienia do klienta {target_client_id}: {e}")

async def fallback_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(str(user_id)):
        return
    await update.message.reply_text(
        "ℹ️ Użyj /start, aby otworzyć sklep, lub /kontakt, aby uzyskać pomoc."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message:
            await update.message.reply_text("❌ Przerwano działanie. Wpisz /start.")
    except Exception:
        pass
    return ConversationHandler.END

def main():
    app = Application.builder().token(TOKEN).post_init(set_bot_menu).build()

    admin_stock_conv = ConversationHandler(
        entry_points=[CommandHandler("magazyn", stock_command)],
        states={
            ADMIN_WAITING_FOR_QTY: [
                CallbackQueryHandler(admin_stock_callback, pattern="^(adm_noop_|adm_add5_|adm_add10_|adm_sub5_|adm_edit_|adm_close|adm_back_to_list)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_new_qty)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    manual_sale_conv = ConversationHandler(
        entry_points=[CommandHandler("sprzedaz", manual_sale_start)],
        states={
            MANUAL_CHOOSE_FLAVOR: [
                CallbackQueryHandler(manual_sale_flavor_callback, pattern="^(man_flavor_[0-9]+|man_cancel)$")
            ],
            MANUAL_ENTER_QTY: [
                CallbackQueryHandler(manual_sale_start, pattern="^man_back_to_flavor$"),
                CallbackQueryHandler(manual_sale_flavor_callback, pattern="^man_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, manual_sale_receive_qty)
            ],
            MANUAL_ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, manual_sale_receive_amount)
            ],
            MANUAL_CONFIRM: [
                CallbackQueryHandler(manual_sale_confirm_callback, pattern="^(man_confirm_yes|man_back_to_flavor|man_cancel)$")
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    correction_sale_conv = ConversationHandler(
        entry_points=[CommandHandler("korekta", correction_sale_start)],
        states={
            CORRECT_CHOOSE_FLAVOR: [
                CallbackQueryHandler(correction_sale_flavor_callback, pattern="^(corr_flavor_[0-9]+|corr_cancel)$")
            ],
            CORRECT_ENTER_QTY: [
                CallbackQueryHandler(correction_sale_start, pattern="^corr_back_to_flavor$"),
                CallbackQueryHandler(correction_sale_flavor_callback, pattern="^corr_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, correction_sale_receive_qty)
            ],
            CORRECT_ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, correction_sale_receive_amount)
            ],
            CORRECT_CONFIRM: [
                CallbackQueryHandler(correction_sale_confirm_callback, pattern="^(corr_confirm_yes|corr_back_to_flavor|corr_cancel)$")
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("kontakt", contact_command))
    app.add_handler(CommandHandler("promotor", promoters_stats_command))
    app.add_handler(CommandHandler("resetkod", reset_promoter_code_command))
    app.add_handler(CommandHandler("zarobki", earnings_command))
    app.add_handler(CommandHandler("ogloszenie", broadcast_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("mojezamowienia", my_orders_command))
    app.add_handler(CommandHandler("resetzarobki", reset_earnings_command))
    
    app.add_handler(admin_stock_conv)
    app.add_handler(manual_sale_conv)
    app.add_handler(correction_sale_conv)
    
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    
    app.add_handler(CallbackQueryHandler(admin_approval_callback, pattern="^appr_"))
    app.add_handler(CallbackQueryHandler(start, pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(help_handler, pattern="^help$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text_handler))
    
    print("PuffBearBot z Mini-Aplikacją uruchomiony i gotowy...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
