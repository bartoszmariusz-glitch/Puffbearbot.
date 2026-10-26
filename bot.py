import os
import json
import hmac
import hashlib
import logging
import secrets
import sqlite3
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    ContextTypes
)
from contextlib import asynccontextmanager

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Superfajnyikochanybot").lstrip("@")
SHOP_WEBAPP_URL = os.environ.get("SHOP_WEBAPP_URL", "https://classy-cucurucho-a3bc50.netlify.app")
# Comma-separated Telegram numeric chat IDs, e.g. "123456789,987654321"
ADMIN_CHAT_IDS = [
    int(x.strip()) for x in os.environ.get("ADMIN_CHAT_IDS", "").split(",")
    if x.strip()
]
DB_PATH = os.environ.get("DB_PATH", "puffbear_orders.sqlite3")
PAIR_CODE_TTL = 10 * 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("puffbear")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS pair_codes (
            code TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, expires_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OczekujÄce',
            created_at INTEGER NOT NULL
        )""")

def new_pair_code(chat_id: int) -> str:
    code = f"{secrets.randbelow(900000) + 100000}"
    with db() as conn:
        conn.execute("DELETE FROM pair_codes WHERE expires_at < ?", (int(time.time()),))
        conn.execute("INSERT OR REPLACE INTO pair_codes(code, chat_id, expires_at) VALUES(?,?,?)",
                     (code, chat_id, int(time.time()) + PAIR_CODE_TTL))
    return code

def consume_pair_code(code: str):
    with db() as conn:
        row = conn.execute(
            "SELECT chat_id, expires_at FROM pair_codes WHERE code = ?", (code.strip(),)
        ).fetchone()
        if not row or row["expires_at"] < int(time.time()):
            if row:
                conn.execute("DELETE FROM pair_codes WHERE code = ?", (code.strip(),))
            return None
        conn.execute("DELETE FROM pair_codes WHERE code = ?", (code.strip(),))
        return int(row["chat_id"])

def verify_webapp_init_data(init_data: str):
    """Validate Telegram Mini App initData and return authenticated user data."""
    if not init_data:
        return None
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = values.pop("hash", None)
        if not received_hash:
            return None
        auth_date = int(values.get("auth_date", "0"))
        if auth_date < int(time.time()) - 86400 or auth_date > int(time.time()) + 60:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        user = json.loads(values.get("user", "{}"))
        user_id = int(user["id"])
        return {"chat_id": user_id, "user": user}
    except (ValueError, KeyError, json.JSONDecodeError, TypeError):
        return None

def validate_and_price(raw: dict) -> dict:
    """Rebuild order from allowed products; never trust browser price totals."""
    allowed_flavors = {
        "Strawberry raspberry ice", "Lemon mojito", "Prime Strawberry",
        "Mixed berries", "Crazy blueberry", "Forest berries",
        "Aloe blackcurrant", "Peach lemonade",
        "Kiwi passion fruit guawa", "Dragon fruit ice"
    }
    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Koszyk jest pusty.")
    items, total_qty = [], 0
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("NieprawidÅowy produkt.")
        flavor = item.get("flavor")
        try:
            qty = int(item.get("quantity", 0))
        except (ValueError, TypeError):
            raise ValueError("NieprawidÅowa iloÅÄ.")
        if flavor not in allowed_flavors or qty < 1 or qty > 100:
            raise ValueError("NieprawidÅowy produkt lub iloÅÄ.")
        items.append({"flavor": flavor, "quantity": qty})
        total_qty += qty
    if total_qty > 200:
        raise ValueError("Maksymalna liczba sztuk w jednym zamÃ³wieniu to 200.")

    if total_qty >= 50: unit_price = 29
    elif total_qty >= 20: unit_price = 33
    elif total_qty >= 10: unit_price = 40
    elif total_qty >= 5: unit_price = 45
    elif total_qty >= 3: unit_price = 50
    else: unit_price = 55

    valid_promos = {
        "ALUS10", "JAYSONEK", "KULT", "KULA2026",
        "SWAGMACIEK", "BUWER21", "STACHU10", "DRESIK10"
    }
    promo = str(raw.get("promo_code", "Brak")).strip().upper()
    discount = 10 if promo in valid_promos else 0
    total_price = total_qty * unit_price
    if discount:
        total_price -= round(total_price * discount / 100)

    delivery_allowed = {
        "Paczkomat InPost", "Park Lisiniec (CzÄstochowa)",
        "PÃ³Ånoc (CzÄstochowa)", "Centrum (CzÄstochowa)",
        "Estakada (CzÄstochowa)", "Trzech WieszczÃ³w (CzÄstochowa)"
    }
    payment_allowed = {"Przelew / Blik (z gÃ³ry)", "GotÃ³wka przy odbiorze"}
    delivery = str(raw.get("delivery", ""))
    payment = str(raw.get("payment", ""))
    if delivery not in delivery_allowed or payment not in payment_allowed:
        raise ValueError("NieprawidÅowa dostawa lub pÅatnoÅÄ.")
    return {
        "items": items, "total_qty": total_qty, "unit_price": unit_price,
        "total_price": total_price, "promo_code": promo if discount else "Brak",
        "discount_percent": discount, "delivery": delivery, "payment": payment
    }

def format_order(order_id: int, payload: dict, user: dict | None = None) -> str:
    lines = [f"ð§¾ ZamÃ³wienie #{order_id}", "", "Produkty:"]
    lines += [f"â¢ {x['flavor']} â {x['quantity']} szt." for x in payload["items"]]
    lines += [
        "", f"Razem sztuk: {payload['total_qty']}",
        f"Cena za sztukÄ (prÃ³g iloÅciowy): {payload['unit_price']} zÅ",
        f"Kwota koÅcowa: {payload['total_price']} zÅ",
        f"Kod promocyjny: {payload['promo_code']}",
        f"Dostawa: {payload['delivery']}",
        f"PÅatnoÅÄ: {payload['payment']}"
    ]
    if user:
        full_name = " ".join(x for x in [user.get("first_name", ""), user.get("last_name", "")] if x).strip()
        lines += ["", f"Klient: {full_name or 'Klient'}",
                  f"Username: @{user['username']}" if user.get("username") else "Username: brak",
                  f"Telegram ID: {user.get('id', 'brak')}"]
    return "\n".join(lines)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    code = new_pair_code(chat_id)
    keyboard = [[InlineKeyboardButton("ð OtwÃ³rz sklep PuffBear", web_app=WebAppInfo(url=SHOP_WEBAPP_URL))]]
    await update.effective_message.reply_text(
        "Witaj w PuffBear!\n\n"
        f"JeÅli otwierasz sklep przez zwykÅy link, uÅ¼yj jednorazowego kodu poÅÄczenia: {code}\n"
        "Kod jest waÅ¼ny przez 10 minut. Nie udostÄpniaj go innym.\n\n"
        "MoÅ¼esz teÅ¼ otworzyÄ sklep przyciskiem poniÅ¼ej.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def order_from_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.web_app_data:
        return
    try:
        raw = json.loads(msg.web_app_data.data)
        payload = validate_and_price(raw)
        user = update.effective_user.to_dict() if update.effective_user else {}
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO orders(chat_id, payload, status, created_at) VALUES(?,?,?,?)",
                (update.effective_chat.id, json.dumps({"order": payload, "user": user}, ensure_ascii=False),
                 "OczekujÄce", int(time.time()))
            )
            order_id = cur.lastrowid
        summary = format_order(order_id, payload, user)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("â Potwierdzam zamÃ³wienie", callback_data=f"confirm:{order_id}"),
            InlineKeyboardButton("â Anuluj", callback_data=f"cancel:{order_id}")
        ]])
        await msg.reply_text(
            "SprawdÅº podsumowanie. ZamÃ³wienie trafi do administratora dopiero po Twoim potwierdzeniu.\n\n"
            + summary,
            reply_markup=keyboard
        )
    except (ValueError, json.JSONDecodeError) as exc:
        await msg.reply_text(f"Nie udaÅo siÄ przyjÄÄ zamÃ³wienia: {exc}")

async def order_api(payload: dict, chat_id: int, user: dict | None = None):
    clean = validate_and_price(payload)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO orders(chat_id, payload, status, created_at) VALUES(?,?,?,?)",
            (chat_id, json.dumps({"order": clean, "user": user or {}}, ensure_ascii=False),
             "OczekujÄce", int(time.time()))
        )
        order_id = cur.lastrowid
    summary = format_order(order_id, clean, user)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("â Potwierdzam zamÃ³wienie", callback_data=f"confirm:{order_id}"),
        InlineKeyboardButton("â Anuluj", callback_data=f"cancel:{order_id}")
    ]])
    await telegram_app.bot.send_message(
        chat_id=chat_id,
        text="SprawdÅº podsumowanie. Administrator otrzyma zamÃ³wienie dopiero po Twoim potwierdzeniu.\n\n" + summary,
        reply_markup=keyboard
    )
    return {"ok": True, "order_id": order_id}

async def order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        action, order_id_str = query.data.split(":", 1)
        order_id = int(order_id_str)
    except (ValueError, AttributeError):
        return
    with db() as conn:
        row = conn.execute("SELECT chat_id, payload, status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            await query.edit_message_text("Nie znaleziono zamÃ³wienia.")
            return
        if int(row["chat_id"]) != int(query.from_user.id):
            await query.answer("To nie jest Twoje zamÃ³wienie.", show_alert=True)
            return
        if row["status"] != "OczekujÄce":
            await query.edit_message_text(f"To zamÃ³wienie ma juÅ¼ status: {row['status']}.")
            return
        saved = json.loads(row["payload"])
        payload, user = saved["order"], saved.get("user", {})
        if action == "cancel":
            conn.execute("UPDATE orders SET status='Anulowane' WHERE id=?", (order_id,))
            await query.edit_message_text(f"ZamÃ³wienie #{order_id} zostaÅo anulowane.")
            return
        if action != "confirm":
            return
        conn.execute("UPDATE orders SET status='Potwierdzone' WHERE id=?", (order_id,))
    await query.edit_message_text("â DziÄkujemy! PotwierdziÅeÅ zamÃ³wienie. Administrator otrzymaÅ powiadomienie.")
    admin_text = "ð¨ NOWE POTWIERDZONE ZAMÃWIENIE ð¨\n\n" + format_order(order_id, payload, user)
    failed = []
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_text)
        except Exception:
            log.exception("Nie udaÅo siÄ wysÅaÄ zamÃ³wienia do administratora %s", admin_id)
            failed.append(admin_id)
    if failed:
        log.error("Nie udaÅo siÄ powiadomiÄ wszystkich administratorÃ³w dla zamÃ³wienia %s", order_id)

class OrderRequest(BaseModel):
    items: list[dict] = Field(min_length=1)
    total_qty: int | None = None
    total_price: int | None = None
    promo_code: str = "Brak"
    delivery: str
    payment: str
    init_data: str = ""
    pair_code: str = ""

telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CallbackQueryHandler(order_callback, pattern=r"^(confirm|cancel):"))
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, order_from_web))

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    if telegram_app.updater:
        await telegram_app.updater.start_polling(drop_pending_updates=False)
    yield
    if telegram_app.updater:
        await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(title="PuffBear Order API", lifespan=lifespan)
# Set SHOP_ORIGINS to a comma-separated list of exact public site origins.
# Example: https://your-shop.netlify.app,https://your-custom-domain.pl
allowed_origins = [
    origin.strip() for origin in os.environ.get(
        "SHOP_ORIGINS", "https://classy-cucurucho-a3bc50.netlify.app"
    ).split(",") if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/api/orders")
async def create_order(req: OrderRequest):
    auth = verify_webapp_init_data(req.init_data) if req.init_data else None
    if auth:
        chat_id = auth["chat_id"]
        user = auth["user"]
    elif req.pair_code:
        chat_id = consume_pair_code(req.pair_code)
        if not chat_id:
            raise HTTPException(status_code=401, detail="Kod poÅÄczenia jest nieprawidÅowy lub wygasÅ. OtwÃ³rz bota i uÅ¼yj /start, aby dostaÄ nowy kod.")
        # The user has linked this browser to the Telegram chat by entering a one-time code.
        user = {"id": chat_id}
    else:
        raise HTTPException(status_code=401, detail="OtwÃ³rz bota i wpisz /start, a nastÄpnie uÅ¼yj kodu poÅÄczenia.")
    try:
        payload = req.model_dump()
        result = await order_api(payload, chat_id, user)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
