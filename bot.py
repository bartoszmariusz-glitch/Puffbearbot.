
import os
import json
import hmac
import hashlib
import logging
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ==========================================
# KONFIGURACJA
# ==========================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

SHOP_WEBAPP_URL = os.environ.get(
    "SHOP_WEBAPP_URL",
    "https://magnificent-stardust-7d2daa.netlify.app"
).strip()

SHOP_ORIGINS = [
    value.strip().rstrip("/")
    for value in os.environ.get(
        "SHOP_ORIGINS",
        "https://magnificent-stardust-7d2daa.netlify.app"
    ).split(",")
    if value.strip()
]

ADMIN_CHAT_IDS = [
    int(value.strip())
    for value in os.environ.get(
        "ADMIN_CHAT_IDS", ""
    ).split(",")
    if value.strip()
]

DB_PATH = os.environ.get(
    "DB_PATH",
    "puffbear_orders.sqlite3"
)

PAIR_CODE_TTL = 600

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("puffbear")


# ==========================================
# BAZA DANYCH
# ==========================================

def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS pair_codes (
                code TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL
            )
        """)


def new_pair_code(chat_id: int) -> str:
    code = str(secrets.randbelow(900000) + 100000)

    with db() as connection:
        connection.execute(
            "DELETE FROM pair_codes WHERE expires_at < ?",
            (int(time.time()),)
        )

        connection.execute(
            """
            INSERT OR REPLACE INTO pair_codes
            (code, chat_id, expires_at)
            VALUES (?, ?, ?)
            """,
            (
                code,
                chat_id,
                int(time.time()) + PAIR_CODE_TTL
            )
        )

    return code


def consume_pair_code(code: str):
    with db() as connection:
        row = connection.execute(
            """
            SELECT chat_id, expires_at
            FROM pair_codes
            WHERE code = ?
            """,
            (code.strip(),)
        ).fetchone()

        if not row:
            return None

        if row["expires_at"] < int(time.time()):
            connection.execute(
                "DELETE FROM pair_codes WHERE code = ?",
                (code.strip(),)
            )
            return None

        connection.execute(
            "DELETE FROM pair_codes WHERE code = ?",
            (code.strip(),)
        )

        return int(row["chat_id"])


# ==========================================
# TELEGRAM INIT DATA
# ==========================================

def verify_webapp_init_data(init_data: str):
    if not init_data:
        return None

    try:
        values = dict(
            parse_qsl(
                init_data,
                keep_blank_values=True
            )
        )

        received_hash = values.pop("hash", None)

        if not received_hash:
            return None

        auth_date = int(values.get("auth_date", "0"))
        now = int(time.time())

        if auth_date < now - 86400 or auth_date > now + 60:
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(values.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return None

        user = json.loads(values.get("user", "{}"))
        user_id = int(user["id"])

        return {
            "chat_id": user_id,
            "user": user
        }

    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError
    ):
        return None


# ==========================================
# KATALOG AROMATÓW SPOŻYWCZYCH
# ==========================================

ALLOWED_FLAVORS = {
    "Strawberry raspberry ice",
    "Lemon mojito",
    "Prime Strawberry",
    "Mixed berries",
    "Crazy blueberry",
    "Forest berries",
    "Aloe blackcurrant",
    "Peach lemonade",
    "Kiwi passion fruit guawa",
    "Dragon fruit ice",
}

VALID_PROMOS = {
    "ALUS10",
    "JAYSONEK",
    "KULT",
    "KULA2026",
    "SWAGMACIEK",
    "BUWER21",
    "STACHU10",
    "DRESIK10",
}

DELIVERY_ALLOWED = {
    "Paczkomat InPost",
    "Park Lisiniec (Częstochowa)",
    "Północ (Częstochowa)",
    "Centrum (Częstochowa)",
    "Estakada (Częstochowa)",
    "Trzech Wieszczów (Częstochowa)",
}

PAYMENT_ALLOWED = {
    "Przelew / BLIK z góry",
    "Przelew / Blik (z góry)",
    "Gotówka przy odbiorze",
}


def unit_price(quantity: int) -> int:
    if quantity >= 50:
        return 29
    if quantity >= 20:
        return 33
    if quantity >= 10:
        return 40
    if quantity >= 5:
        return 45
    if quantity >= 3:
        return 50
    return 55


def validate_and_price(raw: dict) -> dict:
    raw_items = raw.get("items")

    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Koszyk jest pusty.")

    items = []
    total_qty = 0

    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("Nieprawidłowy produkt.")

        flavor = item.get("flavor")

        try:
            quantity = int(item.get("quantity", 0))
        except (ValueError, TypeError):
            raise ValueError("Nieprawidłowa ilość.")

        if flavor not in ALLOWED_FLAVORS:
            raise ValueError("Nieznany produkt.")

        if quantity < 1 or quantity > 100:
            raise ValueError("Nieprawidłowa liczba sztuk.")

        items.append({
            "flavor": flavor,
            "quantity": quantity
        })

        total_qty += quantity

    if total_qty > 200:
        raise ValueError(
            "Maksymalnie 200 sztuk w jednym zamówieniu."
        )

    price_per_item = unit_price(total_qty)

    promo = str(
        raw.get("promo_code", "Brak")
    ).strip().upper()

    discount = 10 if promo in VALID_PROMOS else 0

    total_price = total_qty * price_per_item

    if discount:
        total_price -= round(
            total_price * discount / 100
        )

    delivery = str(raw.get("delivery", ""))
    payment = str(raw.get("payment", ""))

    if delivery not in DELIVERY_ALLOWED:
        raise ValueError(
            "Nieprawidłowa forma dostawy."
        )

    if payment not in PAYMENT_ALLOWED:
        raise ValueError(
            "Nieprawidłowa forma płatności."
        )

    return {
        "items": items,
        "total_qty": total_qty,
        "unit_price": price_per_item,
        "total_price": total_price,
        "promo_code": promo if discount else "Brak",
        "discount_percent": discount,
        "delivery": delivery,
        "payment": payment,
    }


# ==========================================
# FORMATOWANIE WIADOMOŚCI
# ==========================================

def format_order(
    order_id: int,
    payload: dict,
    user: dict | None = None
) -> str:

    lines = [
        f"🧾 Zamówienie #{order_id}",
        "",
        "Produkty:",
    ]

    for item in payload["items"]:
        lines.append(
            f"• {item['flavor']} — {item['quantity']} szt."
        )

    lines.extend([
        "",
        f"Razem sztuk: {payload['total_qty']}",
        f"Cena za sztukę: {payload['unit_price']} zł",
        f"Kwota końcowa: {payload['total_price']} zł",
        f"Kod promocyjny: {payload['promo_code']}",
        f"Dostawa: {payload['delivery']}",
        f"Płatność: {payload['payment']}",
    ])

    if user:
        full_name = " ".join(
            part
            for part in [
                user.get("first_name", ""),
                user.get("last_name", "")
            ]
            if part
        )

        username = user.get("username")

        lines.extend([
            "",
            f"Klient: {full_name or 'Klient'}",
            (
                f"Username: @{username}"
                if username
                else "Username: brak"
            ),
            f"Telegram ID: {user.get('id', 'brak')}",
        ])

    return "\n".join(lines)


def confirmation_keyboard(order_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Potwierdzam",
                callback_data=f"confirm:{order_id}"
            ),
            InlineKeyboardButton(
                "❌ Anuluj",
                callback_data=f"cancel:{order_id}"
            ),
        ]
    ])


# ==========================================
# /START
# ==========================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat_id = update.effective_chat.id
    code = new_pair_code(chat_id)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🛍 Otwórz sklep PuffBear",
                web_app=WebAppInfo(url=SHOP_WEBAPP_URL)
            )
        ]
    ])

    message = (
        "Witaj w PuffBear! 🐻\n\n"
        "Aromaty spożywcze 30 ml — bez nikotyny.\n\n"
        "Jeśli otwierasz sklep przez zwykły link, "
        "użyj jednorazowego kodu połączenia:\n\n"
        f"🔑 {code}\n\n"
        "Kod jest ważny przez 10 minut. "
        "Nie udostępniaj go innym.\n\n"
        "Możesz też otworzyć sklep przyciskiem poniżej."
    )

    await update.effective_message.reply_text(
        message,
        reply_markup=keyboard
    )


# ==========================================
# ZAPIS ZAMÓWIENIA
# ==========================================

def save_order(
    chat_id: int,
    payload: dict,
    user: dict
) -> int:

    with db() as connection:
        cursor = connection.execute(
            """
            INSERT INTO orders
            (chat_id, payload, status, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                chat_id,
                json.dumps(
                    {
                        "order": payload,
                        "user": user
                    },
                    ensure_ascii=False
                ),
                STATUS_PENDING,
                int(time.time())
            )
        )

        return cursor.lastrowid


async def send_confirmation(
    chat_id: int,
    order_id: int,
    payload: dict,
    user: dict
):
    summary = format_order(
        order_id,
        payload,
        user
    )

    await telegram_app.bot.send_message(
        chat_id=chat_id,
        text=(
            "Sprawdź podsumowanie.\n\n"
            "Administrator otrzyma zamówienie "
            "dopiero po Twoim potwierdzeniu.\n\n"
            + summary
        ),
        reply_markup=confirmation_keyboard(order_id)
    )


# ==========================================
# ZAMÓWIENIE PRZEZ API
# ==========================================

async def order_api(
    payload: dict,
    chat_id: int,
    user: dict | None = None
):
    clean = validate_and_price(payload)
    user = user or {}

    order_id = save_order(
        chat_id,
        clean,
        user
    )

    await send_confirmation(
        chat_id,
        order_id,
        clean,
        user
    )

    return {
        "ok": True,
        "order_id": order_id
    }


# ==========================================
# ZAMÓWIENIE PRZEZ TELEGRAM
# ==========================================

async def order_from_web(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message

    if not message or not message.web_app_data:
        return

    try:
        raw = json.loads(message.web_app_data.data)
        clean = validate_and_price(raw)

        user = (
            update.effective_user.to_dict()
            if update.effective_user
            else {}
        )

        order_id = save_order(
            update.effective_chat.id,
            clean,
            user
        )

        await message.reply_text(
            "Sprawdź podsumowanie.\n\n"
            "Administrator otrzyma zamówienie "
            "dopiero po Twoim potwierdzeniu.\n\n"
            + format_order(order_id, clean, user),
            reply_markup=confirmation_keyboard(order_id)
        )

    except (ValueError, json.JSONDecodeError) as exc:
        await message.reply_text(
            f"Nie udało się przyjąć zamówienia: {exc}"
        )


# ==========================================
# POTWIERDZANIE / ANULOWANIE
# ==========================================

async def order_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not query:
        return

    try:
        action, order_id_text = query.data.split(":", 1)
        order_id = int(order_id_text)
    except (ValueError, AttributeError):
        await query.answer()
        return

    with db() as connection:
        row = connection.execute(
            """
            SELECT chat_id, payload, status
            FROM orders
            WHERE id = ?
            """,
            (order_id,)
        ).fetchone()

        if not row:
            await query.answer(
                "Nie znaleziono zamówienia.",
                show_alert=True
            )
            return

        if int(row["chat_id"]) != int(query.from_user.id):
            await query.answer(
                "To nie jest Twoje zamówienie.",
                show_alert=True
            )
            return

        if row["status"] not in (
            STATUS_PENDING,
            "Oczekujące",
            "OczekujÄce"
        ):
            await query.answer(
                "To zamówienie zostało już obsłużone.",
                show_alert=True
            )
            return

        if action == "cancel":
            connection.execute(
                """
                UPDATE orders
                SET status = ?
                WHERE id = ?
                """,
                (STATUS_CANCELLED, order_id)
            )

            await query.answer()

            await query.edit_message_text(
                f"❌ Zamówienie #{order_id} zostało anulowane."
            )
            return

        if action != "confirm":
            await query.answer()
            return

        saved = json.loads(row["payload"])
        payload = saved["order"]
        user = saved.get("user", {})

        connection.execute(
            """
            UPDATE orders
            SET status = ?
            WHERE id = ?
            """,
            (STATUS_CONFIRMED, order_id)
        )

    await query.answer()

    admin_text = (
        "🚨 NOWE POTWIERDZONE ZAMÓWIENIE 🚨\n\n"
        + format_order(order_id, payload, user)
    )

    delivered = 0

    for admin_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text
            )
            delivered += 1

        except Exception:
            log.exception(
                "Nie udało się powiadomić administratora %s",
                admin_id
            )

    if delivered:
        result_text = (
            "✅ Zamówienie zostało potwierdzone.\n"
            "Administrator otrzymał powiadomienie."
        )
    else:
        result_text = (
            "✅ Zamówienie zostało potwierdzone.\n"
            "Nie udało się wysłać powiadomienia "
            "do administratora. Skontaktuj się z obsługą."
        )

    await query.edit_message_text(result_text)


# ==========================================
# MODEL API
# ==========================================

class OrderRequest(BaseModel):
    items: list[dict] = Field(min_length=1)
    total_qty: int | None = None
    total_price: int | None = None
    promo_code: str = "Brak"
    delivery: str
    payment: str
    init_data: str = ""
    pair_code: str = ""


# ==========================================
# TELEGRAM APPLICATION
# ==========================================

telegram_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .build()
)

telegram_app.add_handler(
    CommandHandler("start", start)
)

telegram_app.add_handler(
    CallbackQueryHandler(
        order_callback,
        pattern=r"^(confirm|cancel):"
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.StatusUpdate.WEB_APP_DATA,
        order_from_web
    )
)


# ==========================================
# FASTAPI LIFESPAN
# ==========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    await telegram_app.initialize()
    await telegram_app.start()

    if telegram_app.updater:
        await telegram_app.updater.start_polling(
            drop_pending_updates=False
        )

    log.info(
        "Bot uruchomiony. Adres sklepu: %s",
        SHOP_WEBAPP_URL
    )

    yield

    if telegram_app.updater:
        await telegram_app.updater.stop()

    await telegram_app.stop()
    await telegram_app.shutdown()


# ==========================================
# FASTAPI
# ==========================================

app = FastAPI(
    title="PuffBear Order API",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=SHOP_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"]
)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/orders")
async def create_order(req: OrderRequest):
    auth = (
        verify_webapp_init_data(req.init_data)
        if req.init_data
        else None
    )

    if auth:
        chat_id = auth["chat_id"]
        user = auth["user"]

    elif req.pair_code:
        chat_id = consume_pair_code(req.pair_code)

        if not chat_id:
            raise HTTPException(
                status_code=401,
                detail=(
                    "Kod połączenia jest nieprawidłowy "
                    "lub wygasł. Otwórz bota i wyślij /start."
                )
            )

        user = {"id": chat_id}

    else:
        raise HTTPException(
            status_code=401,
            detail=(
                "Otwórz bota Telegram i wyślij /start, "
                "aby otrzymać kod połączenia."
            )
        )

    try:
        return await order_api(
            req.model_dump(),
            chat_id,
            user
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc)
        )
