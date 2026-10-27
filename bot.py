import os
import json
import hmac
import hashlib
import logging
import secrets
import sqlite3
import time
import unicodedata
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ['BOT_TOKEN']
SHOP_WEBAPP_URL = os.environ.get('SHOP_WEBAPP_URL', 'https://magnificent-stardust-7d2daa.netlify.app').strip()
SHOP_ORIGINS = [x.strip().rstrip('/') for x in os.environ.get('SHOP_ORIGINS', SHOP_WEBAPP_URL).split(',') if x.strip()]
ADMIN_CHAT_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_CHAT_IDS', '').split(',') if x.strip()]
DB_PATH = os.environ.get('DB_PATH', '/data/puffbear_orders.sqlite3' if os.path.isdir('/data') else 'puffbear_orders.sqlite3')
WELCOME_LOGO_URL = os.environ.get('WELCOME_LOGO_URL', '').strip()
PAIR_CODE_TTL = 600
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('puffbear')

INITIAL_FLAVORS = [
    'Strawberry raspberry ice', 'Lemon mojito', 'Prime Strawberry',
    'Mixed berries', 'Crazy blueberry', 'Forest berries',
    'Aloe blackcurrant', 'Peach lemonade', 'Kiwi passion fruit guawa',
    'Dragon fruit ice'
]
VALID_PROMOS = {'ALUS10', 'JAYSONEK', 'KULT', 'KULA2026', 'SWAGMACIEK', 'BUWER21', 'STACHU10', 'DRESIK10'}

DELIVERY_POINTS = {
    'Park Lisiniec': 'https://maps.app.goo.gl/gMayzwa1qzo2wxfPA?g_st=ipc',
    'Północ': 'https://maps.app.goo.gl/vs7pBNwC3j1FSrKp9?g_st=ipc',
    'Plac Biegańskiego': 'https://maps.app.goo.gl/rUSnkVN2howF4EKbA?g_st=ipc',
    'Estakada': 'https://maps.app.goo.gl/nZueoeHstbcCntTe6?g_st=ipc',
    'Trzech Wieszczów': 'https://maps.app.goo.gl/fYUfPC5646K594Jo8?g_st=ipc',
}
DELIVERY_FEES = {'Paczkomat InPost': 17, 'Dowóz pod pinezkę': 10}


def normalize_option(value):
    text = unicodedata.normalize('NFKD', str(value or '').strip().casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', text)


PAYMENT_ALIASES = {
    'blik': 'BLIK', 'przelewblikzgory': 'BLIK', 'przelewblik': 'BLIK',
    'gotowka': 'Gotówka', 'gotowkaprzyodbiorze': 'Gotówka',
}
DELIVERY_ALIASES = {
    'paczkomatinpost': 'Paczkomat InPost', 'inpost': 'Paczkomat InPost',
    'paczkomat': 'Paczkomat InPost', 'dowozpodpinezke': 'Dowóz pod pinezkę',
    'dowoz': 'Dowóz pod pinezkę',
}
POINT_ALIASES = {normalize_option(name): name for name in DELIVERY_POINTS}
POINT_ALIASES['centrum'] = 'Plac Biegańskiego'


def db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=30000')
    return connection


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS pair_codes (code TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, expires_at INTEGER NOT NULL)')
        c.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL)")
        c.execute('CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0), active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS stock_movements (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS promoters (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, code TEXT NOT NULL UNIQUE COLLATE NOCASE, discount_percent INTEGER NOT NULL CHECK(discount_percent BETWEEN 0 AND 90), active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY COLLATE NOCASE, discount_percent INTEGER NOT NULL CHECK(discount_percent BETWEEN 0 AND 90), active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS commissions (order_id INTEGER PRIMARY KEY, promoter_id INTEGER NOT NULL, base_grosze INTEGER NOT NULL, commission_grosze INTEGER NOT NULL, created_at INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS promoter_payouts (id INTEGER PRIMARY KEY AUTOINCREMENT, promoter_id INTEGER NOT NULL, amount_grosze INTEGER NOT NULL CHECK(amount_grosze > 0), created_at INTEGER NOT NULL, admin_id INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS accounting_periods (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL, closed_at INTEGER, closed_by INTEGER)')
        columns = {r['name'] for r in c.execute('PRAGMA table_info(orders)')}
        if 'period_id' not in columns:
            c.execute('ALTER TABLE orders ADD COLUMN period_id INTEGER NOT NULL DEFAULT 1')
        if 'source' not in columns:
            c.execute("ALTER TABLE orders ADD COLUMN source TEXT NOT NULL DEFAULT 'web'")
        c.execute('INSERT OR IGNORE INTO accounting_periods (id, started_at) VALUES (1, ?)', (int(time.time()),))
        # Backfill existing confirmed orders to the original accounting period.
        c.execute('UPDATE orders SET period_id=1 WHERE period_id IS NULL OR period_id=0')
        for code in VALID_PROMOS:
            c.execute('INSERT OR IGNORE INTO promo_codes (code, discount_percent, active, created_at) VALUES (?, 10, 1, ?)', (code, int(time.time())))
        for name in INITIAL_FLAVORS:
            c.execute('INSERT OR IGNORE INTO products (name, stock, active, created_at) VALUES (?, 10, 1, ?)', (name, int(time.time())))


def products():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT id, name, stock, active FROM products ORDER BY id')]


def new_pair_code(chat_id):
    code = str(secrets.randbelow(900000) + 100000)
    with db() as c:
        c.execute('DELETE FROM pair_codes WHERE expires_at < ?', (int(time.time()),))
        c.execute('INSERT OR REPLACE INTO pair_codes VALUES (?, ?, ?)', (code, chat_id, int(time.time()) + PAIR_CODE_TTL))
    return code


def consume_pair_code(code):
    with db() as c:
        row = c.execute('SELECT chat_id, expires_at FROM pair_codes WHERE code = ?', (code.strip(),)).fetchone()
        if not row or row['expires_at'] < int(time.time()):
            return None
        c.execute('DELETE FROM pair_codes WHERE code = ?', (code.strip(),))
        return int(row['chat_id'])


def verify_webapp_init_data(init_data):
    if not init_data:
        return None
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received = values.pop('hash', None)
        auth_date = int(values.get('auth_date', '0'))
        if not received or abs(int(time.time()) - auth_date) > 86400:
            return None
        check = '\n'.join(f'{k}={v}' for k, v in sorted(values.items()))
        secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received):
            return None
        user = json.loads(values['user'])
        return {'chat_id': int(user['id']), 'user': user}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def unit_price(qty):
    for threshold, price in [(50, 29), (20, 33), (10, 40), (5, 45), (3, 50)]:
        if qty >= threshold:
            return price
    return 55


def promo_lookup(code):
    with db() as c:
        prom = c.execute('SELECT id, code, discount_percent FROM promoters WHERE code = ? COLLATE NOCASE AND active = 1', (code,)).fetchone()
        if prom:
            return dict(prom), int(prom['discount_percent'])
        row = c.execute('SELECT discount_percent FROM promo_codes WHERE code = ? COLLATE NOCASE AND active = 1', (code,)).fetchone()
        return None, int(row['discount_percent']) if row else 0


def money_grosze(value):
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def commission_grosze(base_grosze):
    return (base_grosze + 5) // 10


def current_period_id(c):
    row = c.execute('SELECT id FROM accounting_periods WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1').fetchone()
    if not row:
        raise RuntimeError('Brak aktywnego okresu rozliczeniowego.')
    return int(row['id'])


def period_label(period):
    return time.strftime('%d.%m.%Y %H:%M', time.localtime(period['started_at']))


def confirmed_promoter_stats():
    with db() as c:
        period_id = current_period_id(c)
        return [dict(r) for r in c.execute('''SELECT p.id, p.name, p.code, p.discount_percent, p.active,
            COUNT(CASE WHEN o.period_id=? AND o.status='confirmed' THEN co.order_id END) AS orders_count,
            COALESCE(SUM(CASE WHEN o.period_id=? AND o.status='confirmed' THEN co.base_grosze ELSE 0 END),0) AS sales_grosze,
            COALESCE(SUM(CASE WHEN o.period_id=? AND o.status='confirmed' THEN co.commission_grosze ELSE 0 END),0) AS period_commission_grosze,
            COALESCE(SUM(CASE WHEN o.status='confirmed' THEN co.commission_grosze ELSE 0 END),0) AS lifetime_commission_grosze,
            COALESCE((SELECT SUM(py.amount_grosze) FROM promoter_payouts py WHERE py.promoter_id=p.id),0) AS paid_grosze
            FROM promoters p LEFT JOIN commissions co ON co.promoter_id=p.id
            LEFT JOIN orders o ON o.id=co.order_id
            GROUP BY p.id ORDER BY p.id''', (period_id, period_id, period_id))]


def promoters_text():
    rows = confirmed_promoter_stats()
    if not rows:
        return '👥 Brak promotorów. Wybierz „Dodaj promotora”.'
    lines = ['👥 PROMOTORZY — bieżący okres (prowizja 10%)', '']
    for r in rows:
        due = r['lifetime_commission_grosze'] - r['paid_grosze']
        lines += [f'#{r["id"]} {r["name"]} — {r["code"]} ({r["discount_percent"]}% rabatu)' + ('' if r['active'] else ' [WYŁĄCZONY]'),
                  f'  Zamówienia w okresie: {r["orders_count"]} | Sprzedaż: {r["sales_grosze"]/100:.2f} zł',
                  f'  Prowizja w okresie: {r["period_commission_grosze"]/100:.2f} zł',
                  f'  Łącznie naliczono: {r["lifetime_commission_grosze"]/100:.2f} zł | Wypłacono: {r["paid_grosze"]/100:.2f} zł',
                  f'  Do wypłaty (wszystkie okresy): {due/100:.2f} zł', '']
    return '\n'.join(lines)


def codes_text():
    with db() as c:
        rows = c.execute('SELECT code, discount_percent, active FROM promo_codes ORDER BY code').fetchall()
    return '🎟️ KODY OGÓLNE\n\n' + '\n'.join(f'{r["code"]} — {r["discount_percent"]}% — {"aktywny" if r["active"] else "wyłączony"}' for r in rows)


def validate_and_price(raw):
    raw_items = raw.get('items')
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError('Koszyk jest pusty.')
    catalog = {p['name'].casefold(): p for p in products()}
    quantities = {}
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError('Nieprawidłowy produkt.')
        name = str(item.get('flavor', '')).strip()
        raw_qty = item.get('quantity', 0)
        if isinstance(raw_qty, bool):
            raise ValueError('Nieprawidłowa ilość.')
        try:
            qty = int(raw_qty)
        except (ValueError, TypeError, OverflowError):
            raise ValueError('Nieprawidłowa ilość.')
        if str(raw_qty).strip() != str(qty):
            raise ValueError('Ilość musi być liczbą całkowitą.')
        product = catalog.get(name.casefold())
        if not product or not product['active']:
            raise ValueError(f'Produkt niedostępny: {name}')
        if qty < 1 or qty > 100:
            raise ValueError('Nieprawidłowa liczba sztuk.')
        canonical_name = product['name']
        quantities[canonical_name] = quantities.get(canonical_name, 0) + qty
    total_qty = sum(quantities.values())
    if total_qty > 200:
        raise ValueError('Maksymalnie 200 sztuk w jednym zamówieniu.')
    for name, qty in quantities.items():
        available = catalog[name.casefold()]['stock']
        if qty > available:
            raise ValueError(f'{name}: dostępne tylko {available} szt.')

    promo = str(raw.get('promo_code', 'Brak')).strip().upper()
    promoter, discount = promo_lookup(promo)
    # The web shop sends delivery as 'Paczkomat InPost: PLC01A'
    # or 'Dowóz pod pinezkę: Park Lisiniec'. Accept this format
    # as well as separate locker_id/delivery_point fields.
    raw_delivery = str(raw.get('delivery') or '').strip()
    delivery_label, separator, delivery_detail = raw_delivery.partition(':')
    delivery = DELIVERY_ALIASES.get(normalize_option(delivery_label))
    payment = PAYMENT_ALIASES.get(normalize_option(raw.get('payment')))
    if not delivery:
        raise ValueError('Wybierz prawidłową formę dostawy.')
    if not payment:
        raise ValueError('Wybierz prawidłową formę płatności.')

    locker_id = ''
    delivery_point = ''
    map_url = ''
    if delivery == 'Paczkomat InPost':
        if payment != 'BLIK':
            raise ValueError('Paczkomat InPost: płatność tylko BLIK.')
        locker_id = str(raw.get('locker_id') or raw.get('parcel_locker') or raw.get('paczkomat') or (delivery_detail if separator else '')).strip().upper()
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9_-]{2,29}', locker_id):
            raise ValueError('Podaj numer paczkomatu InPost (np. CZE01M).')
    else:
        delivery_point = POINT_ALIASES.get(normalize_option(raw.get('delivery_point') or raw.get('point') or raw.get('location') or (delivery_detail if separator else '')))
        if not delivery_point:
            raise ValueError('Wybierz jedną z pięciu pinezek.')
        map_url = DELIVERY_POINTS[delivery_point]

    price = unit_price(total_qty)
    before_discount = total_qty * price
    discount_amount = round(before_discount * discount / 100) if discount else 0
    products_total = before_discount - discount_amount
    delivery_fee = DELIVERY_FEES[delivery]
    grand_total = products_total + delivery_fee
    return {
        'items': [{'flavor': n, 'quantity': q} for n, q in quantities.items()],
        'total_qty': total_qty, 'unit_price': price,
        'products_before_discount': before_discount,
        'discount_amount': discount_amount,
        'products_total': products_total,
        'delivery_fee': delivery_fee, 'total_price': grand_total,
        'promo_code': promo if (discount or promoter) else 'Brak',
        'discount_percent': discount,
        'promoter_id': promoter['id'] if promoter else None,
        'delivery': delivery, 'payment': payment,
        'locker_id': locker_id, 'delivery_point': delivery_point,
        'map_url': map_url,
        'cash_notice': 'Prosimy przygotować odliczoną gotówkę.' if payment == 'Gotówka' else '',
    }


def format_order(order_id, payload, user=None):
    lines = [f'🧾 Zamówienie #{order_id}', '', 'Produkty:']
    for item in payload['items']:
        lines.append(f'• {item["flavor"]} — {item["quantity"]} szt.')
    products_total = payload.get('products_total', payload['total_price'])
    delivery_fee = payload.get('delivery_fee', 0)
    lines += ['', f'Razem sztuk: {payload["total_qty"]}',
              f'Cena za sztukę: {payload["unit_price"]} zł',
              f'Produkty przed rabatem: {payload.get("products_before_discount", products_total)} zł',
              f'Rabat: -{payload.get("discount_amount", 0)} zł',
              f'Produkty po rabacie: {products_total} zł',
              f'Koszt dostawy: {delivery_fee} zł',
              f'💰 DO ZAPŁATY: {payload["total_price"]} zł',
              f'Kod promocyjny: {payload["promo_code"]}',
              f'Dostawa: {payload["delivery"]}']
    if payload.get('locker_id'):
        lines.append(f'📦 Paczkomat: {payload["locker_id"]}')
    if payload.get('delivery_point'):
        lines.append(f'📍 Pinezka: {payload["delivery_point"]}')
        lines.append(f'🗺 Mapa: {payload.get("map_url", "")}')
    lines.append(f'Płatność: {payload["payment"]}')
    if payload.get('cash_notice'):
        lines.append(f'💵 {payload["cash_notice"]}')
    if user:
        full_name = ' '.join(str(user.get(k, '')) for k in ('first_name', 'last_name')).strip()
        lines += ['', f'Klient: {full_name or "Klient"}',
                  f'Username: @{user["username"]}' if user.get('username') else 'Username: brak',
                  f'Telegram ID: {user.get("id", "brak")}']
    return '\n'.join(lines)


def confirm_keyboard(order_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ Potwierdzam', callback_data=f'confirm:{order_id}'),
        InlineKeyboardButton('❌ Anuluj', callback_data=f'cancel:{order_id}')
    ]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = new_pair_code(update.effective_chat.id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('🛍 Otwórz sklep PuffBear', web_app=WebAppInfo(url=SHOP_WEBAPP_URL))
    ]])
    welcome = ('🐻 Hejka! Witaj w PuffBear!\n\n'
               '🍓 Wybierz ulubione smaki.\n'
               '🎟️ Korzystaj z kodów promocyjnych.\n'
               '📦 Złóż zamówienie w kilka chwil!\n\n'
               '👇 Kliknij przycisk, aby otworzyć sklep.\n\n'
               f'🔑 Kod połączenia: {code}\nWażny przez 10 minut. Nie udostępniaj go innym.')
    if WELCOME_LOGO_URL:
        try:
            await update.effective_message.reply_photo(photo=WELCOME_LOGO_URL, caption=welcome, reply_markup=keyboard)
            return
        except Exception:
            log.exception('Nie udało się wysłać logo, wysyłam tekst')
    await update.effective_message.reply_text(welcome, reply_markup=keyboard)


def save_order(chat_id, payload, user):
    with db() as c:
        cursor = c.execute('INSERT INTO orders (chat_id, payload, status, created_at, period_id) VALUES (?, ?, ?, ?, ?)',
                           (chat_id, json.dumps({'order': payload, 'user': user}, ensure_ascii=False), 'pending', int(time.time()), current_period_id(c)))
        return cursor.lastrowid


async def send_confirmation(chat_id, order_id, payload, user):
    await telegram_app.bot.send_message(
        chat_id=chat_id,
        text='Sprawdź podsumowanie. Administrator otrzyma zamówienie po potwierdzeniu.\n\n' + format_order(order_id, payload, user),
        reply_markup=confirm_keyboard(order_id))


async def order_api(payload, chat_id, user=None):
    clean = validate_and_price(payload)
    order_id = save_order(chat_id, clean, user or {})
    await send_confirmation(chat_id, order_id, clean, user or {})
    return {'ok': True, 'order_id': order_id, 'total_price': clean['total_price'],
            'products_total': clean['products_total'], 'delivery_fee': clean['delivery_fee']}


async def order_from_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.web_app_data:
        return
    try:
        clean = validate_and_price(json.loads(message.web_app_data.data))
        user = update.effective_user.to_dict() if update.effective_user else {}
        order_id = save_order(update.effective_chat.id, clean, user)
        await message.reply_text('Sprawdź podsumowanie. Administrator otrzyma zamówienie po potwierdzeniu.\n\n' +
                                 format_order(order_id, clean, user), reply_markup=confirm_keyboard(order_id))
    except (ValueError, json.JSONDecodeError) as exc:
        await message.reply_text(f'Nie udało się przyjąć zamówienia: {exc}')


def finalize_order(order_id, chat_id, action):
    """Status and stock changes happen in one SQLite transaction."""
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT chat_id, payload, status FROM orders WHERE id = ?', (order_id,)).fetchone()
        if not row:
            return 'Nie znaleziono zamówienia.', None
        if int(row['chat_id']) != int(chat_id):
            return 'To nie jest Twoje zamówienie.', None
        if row['status'] not in ('pending', 'Oczekujące', 'OczekujÄ\x85ce'):
            return 'To zamówienie zostało już obsłużone.', None
        if action == 'cancel':
            c.execute('UPDATE orders SET status = ? WHERE id = ?', ('cancelled', order_id))
            c.commit()
            return None, None
        if action != 'confirm':
            return 'Nieznana akcja.', None
        saved = json.loads(row['payload'])
        payload = saved['order']
        amounts = {}
        for item in payload['items']:
            name = item['flavor']
            amounts[name] = amounts.get(name, 0) + int(item['quantity'])
        for name, qty in amounts.items():
            product = c.execute('SELECT id, stock, active FROM products WHERE name = ? COLLATE NOCASE', (name,)).fetchone()
            if not product or not product['active'] or product['stock'] < qty:
                available = product['stock'] if product else 0
                return f'Brak wystarczającej ilości: {name} (dostępne {available} szt.).', None
        for name, qty in amounts.items():
            product = c.execute('SELECT id FROM products WHERE name = ? COLLATE NOCASE', (name,)).fetchone()
            c.execute('UPDATE products SET stock = stock - ? WHERE id = ?', (qty, product['id']))
            c.execute('INSERT INTO stock_movements (product_id, delta, reason, created_at) VALUES (?, ?, ?, ?)',
                      (product['id'], -qty, f'Zamówienie #{order_id}', int(time.time())))
        promoter_id = payload.get('promoter_id')
        if promoter_id is not None:
            promoter = c.execute('SELECT id FROM promoters WHERE id = ?', (promoter_id,)).fetchone()
            if promoter:
                # Delivery is excluded from promoter commissions, including older orders.
                product_base = payload.get('products_total', payload['total_price'])
                base = money_grosze(product_base)
                c.execute('INSERT OR IGNORE INTO commissions (order_id, promoter_id, base_grosze, commission_grosze, created_at) VALUES (?, ?, ?, ?, ?)',
                          (order_id, promoter_id, base, commission_grosze(base), int(time.time())))
        c.execute('UPDATE orders SET status = ? WHERE id = ?', ('confirmed', order_id))
        c.commit()
        return None, saved
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


async def order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        action, order_id = query.data.split(':', 1)
        order_id = int(order_id)
    except (ValueError, AttributeError):
        await query.answer()
        return
    try:
        error, saved = finalize_order(order_id, query.from_user.id, action)
    except Exception:
        log.exception('Błąd zatwierdzania zamówienia')
        await query.answer('Błąd zapisu. Spróbuj ponownie.', show_alert=True)
        return
    if error:
        await query.answer(error, show_alert=True)
        return
    await query.answer()
    if action == 'cancel':
        await query.edit_message_text(f'❌ Zamówienie #{order_id} zostało anulowane.')
        return
    text = '🚨 NOWE POTWIERDZONE ZAMÓWIENIE 🚨\n\n' + format_order(order_id, saved['order'], saved.get('user', {}))
    delivered = 0
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
            delivered += 1
        except Exception:
            log.exception('Nie udało się powiadomić administratora %s', admin_id)
    await query.edit_message_text('✅ Zamówienie zostało potwierdzone.\n' +
                                  (f'Powiadomiono administratorów: {delivered}/{len(ADMIN_CHAT_IDS)}.' if delivered else
                                   'Nie udało się wysłać powiadomienia administratorom.'))


def is_admin(update):
    return bool(update.effective_user and update.effective_user.id in ADMIN_CHAT_IDS and
                update.effective_chat and update.effective_chat.type == 'private')


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📦 Magazyn', callback_data='adm:stock'), InlineKeyboardButton('➕ Dodaj smak', callback_data='adm:add')],
        [InlineKeyboardButton('🚚 Dostawa / zmiana stanu', callback_data='adm:change'), InlineKeyboardButton('👁 Ukryj / pokaż', callback_data='adm:toggle')],
        [InlineKeyboardButton('📊 Sprzedaż', callback_data='adm:sales'), InlineKeyboardButton('🎟️ Statystyki kodów', callback_data='adm:promos')],
        [InlineKeyboardButton('🗑️ Cofnij zamówienie', callback_data='adm:voidlist'), InlineKeyboardButton('➕ Sprzedaż ręczna', callback_data='adm:manual')],
        [InlineKeyboardButton('📅 Nowy okres', callback_data='adm:periodask'), InlineKeyboardButton('🗂 Archiwum', callback_data='adm:archive')],
        [InlineKeyboardButton('👥 Promotorzy', callback_data='adm:promoters'), InlineKeyboardButton('➕ Dodaj promotora', callback_data='adm:newprom')],
        [InlineKeyboardButton('🎟️ Kody ogólne', callback_data='adm:codes'), InlineKeyboardButton('➕ Dodaj kod', callback_data='adm:newcode')],
        [InlineKeyboardButton('💸 Wypłać prowizję', callback_data='adm:payout'), InlineKeyboardButton('🔒 Włącz/wyłącz kod', callback_data='adm:codetoggle')]
    ])


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text('⛔ Brak dostępu.')
        return
    context.user_data.pop('admin_action', None)
    await update.effective_message.reply_text('🐻 PuffBear — panel administratora\nWybierz dział:', reply_markup=admin_keyboard())


def stock_text():
    rows = products()
    lines = ['📦 MAGAZYN PUFFBEAR', '']
    for p in rows:
        status = 'WYPRZEDANE' if p['stock'] == 0 else f'{p["stock"]} szt.'
        if not p['active']:
            status += ' (ukryty)'
        lines.append(f'#{p["id"]} {p["name"]} — {status}')
    lines.append(f'\nŁącznie: {sum(p["stock"] for p in rows)} szt.')
    return '\n'.join(lines)


def sales_text():
    with db() as c:
        rows = c.execute("SELECT payload FROM orders WHERE status = 'confirmed' AND period_id=?", (current_period_id(c),)).fetchall()
    counts = {}
    revenue = 0
    products_revenue = 0
    delivery_revenue = 0
    for row in rows:
        try:
            payload = json.loads(row['payload'])['order']
            revenue += float(payload['total_price'])
            products_revenue += float(payload.get('products_total', payload['total_price']))
            delivery_revenue += float(payload.get('delivery_fee', 0))
            for item in payload['items']:
                name = item['flavor']
                counts[name] = counts.get(name, 0) + int(item['quantity'])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            log.warning('Pominięto nieprawidłowy rekord statystyk')
    lines = ['📊 SPRZEDAŻ (potwierdzone zamówienia)',
             f'Zamówienia: {len(rows)}', f'Przychód wg zamówień: {revenue:.2f} zł',
             f'Produkty: {products_revenue:.2f} zł | Dostawa: {delivery_revenue:.2f} zł',
             f'Sprzedane sztuki: {sum(counts.values())}', '']
    lines += [f'• {name}: {qty} szt.' for name, qty in sorted(counts.items())]
    return '\n'.join(lines)


def promos_text():
    with db() as c:
        rows = c.execute("SELECT payload FROM orders WHERE status = 'confirmed' AND period_id=?", (current_period_id(c),)).fetchall()
    stats = {}
    for row in rows:
        try:
            payload = json.loads(row['payload'])['order']
            code = payload.get('promo_code', 'Brak')
            if code == 'Brak':
                continue
            discount = int(payload.get('discount_amount', 0))
            if 'discount_amount' not in payload:
                total = int(payload['total_price'])
                percent = int(payload.get('discount_percent', 0))
                discount = round(total * percent / (100 - percent)) if 0 < percent < 100 else 0
            count, amount = stats.get(code, (0, 0))
            stats[code] = (count + 1, amount + discount)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            log.warning('Pominięto nieprawidłowy rekord kodu')
    lines = ['🎟️ KODY PROMOCYJNE (potwierdzone zamówienia)', '']
    with db() as c:
        all_codes = [r['code'] for r in c.execute('SELECT code FROM promo_codes UNION SELECT code FROM promoters')]
    for code in sorted(all_codes):
        count, amount = stats.get(code, (0, 0))
        lines.append(f'{code}: {count} użyć, rabaty ok. {amount} zł')
    return '\n'.join(lines)


def archive_text():
    with db() as c:
        periods = c.execute('SELECT * FROM accounting_periods ORDER BY id DESC LIMIT 20').fetchall()
        lines = ['🗂 ARCHIWUM OKRESÓW', '']
        for p in periods:
            rows = c.execute("SELECT payload FROM orders WHERE status='confirmed' AND period_id=?", (p['id'],)).fetchall()
            count, revenue, qty = 0, 0, 0
            for row in rows:
                try:
                    order = json.loads(row['payload'])['order']
                    revenue += float(order['total_price'])
                    qty += sum(int(item['quantity']) for item in order['items'])
                    count += 1
                except (ValueError, KeyError, TypeError):
                    log.warning('Nieprawidłowe dane w archiwum okresu %s', p['id'])
            earned = c.execute('''SELECT COALESCE(SUM(co.commission_grosze),0) AS x FROM commissions co
                 JOIN orders o ON o.id=co.order_id WHERE o.period_id=? AND o.status='confirmed''', (p['id'],)).fetchone()['x']
            ended = time.strftime('%d.%m.%Y %H:%M', time.localtime(p['closed_at'])) if p['closed_at'] else 'TRWA'
            lines += [f'Okres #{p["id"]}: {period_label(p)} → {ended}',
                      f'  Sprzedaż: {count} zam., {qty} szt., {revenue:.2f} zł',
                      f'  Naliczona prowizja: {earned/100:.2f} zł', '']
        lines.append('Niewypłacone prowizje przechodzą na kolejne okresy.')
        return '\n'.join(lines)


def close_period(admin_id, expected_id):
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        active = current_period_id(c)
        if active != expected_id:
            c.rollback()
            return 'Okres został już zamknięty. Odśwież panel /admin.'
        now = int(time.time())
        c.execute('UPDATE accounting_periods SET closed_at=?, closed_by=? WHERE id=? AND closed_at IS NULL', (now, admin_id, active))
        c.execute('INSERT INTO accounting_periods (started_at) VALUES (?)', (now,))
        c.commit()
        return None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def manual_sale(product_id, qty, amount_text, promo_code, admin_id):
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(amount_text.replace(',', '.'))
    except InvalidOperation:
        raise ValueError('Kwota musi być liczbą, np. 100 lub 100.50.')
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValueError('Kwota musi być dodatnia, z maksymalnie dwoma miejscami po przecinku.')
    amount_gr = money_grosze(amount)
    if amount_gr <= 0 or amount_gr > 100000000:
        raise ValueError('Nieprawidłowa kwota sprzedaży.')
    if qty < 1 or qty > 1000:
        raise ValueError('Ilość musi wynosić od 1 do 1000.')
    code = promo_code.strip().upper() or 'BRAK'
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        product = c.execute('SELECT id, name, stock FROM products WHERE id=?', (product_id,)).fetchone()
        if not product:
            raise ValueError('Nie znaleziono produktu. ID sprawdzisz w Magazynie.')
        if product['stock'] < qty:
            raise ValueError(f'Za mało sztuk w magazynie: dostępne {product["stock"]}.')
        promoter = None
        if code != 'BRAK':
            promoter = c.execute('SELECT id FROM promoters WHERE code=? COLLATE NOCASE AND active=1', (code,)).fetchone()
            if not promoter and not c.execute('SELECT 1 FROM promo_codes WHERE code=? COLLATE NOCASE AND active=1', (code,)).fetchone():
                raise ValueError('Kod nie istnieje lub jest nieaktywny. Wpisz BRAK, jeśli bez kodu.')
        # Kwota jest ostateczną wartością produktów po ewentualnym rabacie.
        # Nie naliczamy rabatu drugi raz przy ręcznej sprzedaży.
        total = float(amount)
        payload = {'items': [{'flavor': product['name'], 'quantity': qty}],
                   'total_qty': qty, 'unit_price': round(total / qty, 2),
                   'products_before_discount': total, 'discount_amount': 0,
                   'products_total': total, 'delivery_fee': 0, 'total_price': total,
                   'promo_code': code if code != 'BRAK' else 'Brak',
                   'discount_percent': 0, 'promoter_id': promoter['id'] if promoter else None,
                   'delivery': 'Sprzedaż ręczna', 'payment': 'Poza sklepem',
                   'locker_id': '', 'delivery_point': '', 'map_url': '', 'cash_notice': ''}
        now = int(time.time())
        cursor = c.execute('''INSERT INTO orders (chat_id, payload, status, created_at, period_id, source)
              VALUES (?, ?, 'confirmed', ?, ?, 'manual')''',
              (admin_id, json.dumps({'order': payload, 'user': {'id': admin_id, 'first_name': 'Sprzedaż ręczna'}}, ensure_ascii=False), now, current_period_id(c)))
        order_id = cursor.lastrowid
        c.execute('UPDATE products SET stock=stock-? WHERE id=?', (qty, product_id))
        c.execute('INSERT INTO stock_movements (product_id, delta, reason, created_at) VALUES (?, ?, ?, ?)',
                  (product_id, -qty, f'Sprzedaż ręczna #{order_id} admin {admin_id}', now))
        if promoter:
            c.execute('INSERT INTO commissions (order_id, promoter_id, base_grosze, commission_grosze, created_at) VALUES (?, ?, ?, ?, ?)',
                      (order_id, promoter['id'], amount_gr, commission_grosze(amount_gr), now))
        c.commit()
        return order_id, product['name'], amount_gr
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


async def period_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await query.answer('Brak dostępu.', show_alert=True)
        return
    if query.data == 'adm:periodask':
        with db() as c:
            period = current_period_id(c)
        await query.answer()
        await query.message.reply_text(
            f'⚠️ Zamknąć okres #{period} i rozpocząć nowy?\n\n'
            'Nowe statystyki sprzedaży, kodów i okresowe promotorów zaczną się od zera. '
            'Archiwum, magazyn, historia zamówień i niewypłacone prowizje zostaną zachowane.\n\n'
            'Uwaga: niepotwierdzone zamówienia przypisane do starego okresu pozostaną w nim, nawet gdy klient potwierdzi je później.',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('✅ TAK, NOWY OKRES', callback_data=f'adm:perioddo:{period}')],
                [InlineKeyboardButton('❌ Nie, anuluj', callback_data='adm:periodno')]]))
    elif query.data == 'adm:periodno':
        await query.answer()
        await query.edit_message_text('Nie zmieniono okresu rozliczeniowego.')
    else:
        try:
            expected_id = int(query.data.split(':')[-1])
            error = close_period(query.from_user.id, expected_id)
        except Exception:
            log.exception('Błąd zamykania okresu')
            await query.answer('Błąd bazy danych.', show_alert=True)
            return
        if error:
            await query.answer(error, show_alert=True)
            return
        await query.answer('Nowy okres rozpoczęty.')
        await query.edit_message_text(f'✅ Zamknięto okres #{expected_id}. Rozpoczęto nowy okres rozliczeniowy. Poprzednie dane znajdziesz w Archiwum.')


def confirmed_orders_keyboard():
    with db() as c:
        rows = c.execute("SELECT id, payload FROM orders WHERE status='confirmed' ORDER BY id DESC LIMIT 20").fetchall()
    buttons = []
    for row in rows:
        try:
            order = json.loads(row['payload'])['order']
            amount = order.get('total_price', '?')
        except (ValueError, KeyError, TypeError):
            amount = '?'
        buttons.append([InlineKeyboardButton(f'#{row["id"]} — {amount} zł', callback_data=f'adm:voidask:{row["id"]}')])
    return InlineKeyboardMarkup(buttons) if buttons else None


def void_confirmed_order(order_id, admin_id):
    """Unieważnij sprzedaż, przywróć stan i usuń naliczoną prowizję atomowo."""
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT payload, status FROM orders WHERE id=?', (order_id,)).fetchone()
        if not row or row['status'] != 'confirmed':
            c.rollback()
            return 'Zamówienie nie istnieje lub nie jest już potwierdzone.'
        saved = json.loads(row['payload'])
        order = saved['order']
        commission = c.execute('SELECT promoter_id FROM commissions WHERE order_id=?', (order_id,)).fetchone()
        if commission:
            payout = c.execute('SELECT 1 FROM promoter_payouts WHERE promoter_id=? LIMIT 1', (commission['promoter_id'],)).fetchone()
            if payout:
                c.rollback()
                return 'Nie można cofnąć automatycznie: promotor ma już wypłaty. Najpierw rozlicz prowizję ręcznie.'
        for item in order['items']:
            qty = int(item['quantity'])
            if qty <= 0:
                raise ValueError('Nieprawidłowa ilość w zamówieniu.')
            product = c.execute('SELECT id FROM products WHERE name=? COLLATE NOCASE', (item['flavor'],)).fetchone()
            if not product:
                c.rollback()
                return f'Brak produktu w magazynie: {item["flavor"]}. Nie dokonano zmian.'
            c.execute('UPDATE products SET stock=stock+? WHERE id=?', (qty, product['id']))
            c.execute('INSERT INTO stock_movements (product_id, delta, reason, created_at) VALUES (?, ?, ?, ?)',
                      (product['id'], qty, f'Cofnięcie zamówienia #{order_id} przez admina {admin_id}', int(time.time())))
        c.execute('DELETE FROM commissions WHERE order_id=?', (order_id,))
        c.execute("UPDATE orders SET status='voided' WHERE id=? AND status='confirmed'", (order_id,))
        c.commit()
        return None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


async def void_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await query.answer('Brak dostępu.', show_alert=True)
        return
    parts = query.data.split(':')
    if len(parts) != 3 or not parts[2].isdigit():
        await query.answer('Nieprawidłowe zamówienie.', show_alert=True)
        return
    action, order_id = parts[1], int(parts[2])
    if action == 'ask':
        with db() as c:
            row = c.execute("SELECT payload FROM orders WHERE id=? AND status='confirmed'", (order_id,)).fetchone()
        if not row:
            await query.answer('Nie ma już takiego potwierdzonego zamówienia.', show_alert=True)
            return
        try:
            payload = json.loads(row['payload'])['order']
            text = f'⚠️ Cofnąć zamówienie #{order_id}?\n\nKwota: {payload["total_price"]} zł\nSztuk: {payload["total_qty"]}\n\nSprzedaż zniknie ze statystyk, a sztuki wrócą do magazynu. Operacja wymaga potwierdzenia.'
        except (KeyError, ValueError, TypeError):
            await query.answer('Nieprawidłowe dane zamówienia.', show_alert=True)
            return
        await query.answer()
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('🗑️ TAK, COFNIJ', callback_data=f'adm:voiddo:{order_id}')],
            [InlineKeyboardButton('Nie, wróć', callback_data='adm:voidlist')]
        ]))
    elif action == 'do':
        try:
            error = void_confirmed_order(order_id, query.from_user.id)
        except Exception:
            log.exception('Błąd cofania zamówienia %s', order_id)
            await query.answer('Błąd bazy danych.', show_alert=True)
            return
        if error:
            await query.answer(error, show_alert=True)
            return
        await query.answer('Cofnięto zamówienie.')
        await query.edit_message_text(f'✅ Zamówienie #{order_id} cofnięte. Statystyki i magazyn skorygowane. Historia zachowana.')


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await query.answer('Brak dostępu.', show_alert=True)
        return
    action = query.data.removeprefix('adm:')
    await query.answer()
    context.user_data.pop('admin_action', None)
    if action == 'stock':
        await query.message.reply_text(stock_text())
    elif action == 'sales':
        await query.message.reply_text(sales_text())
    elif action == 'promos':
        await query.message.reply_text(promos_text())
    elif action == 'manual':
        context.user_data['admin_action'] = 'manual'
        await query.message.reply_text('➕ SPRZEDAŻ RĘCZNA\n\nWyślij: ID produktu | liczba sztuk | kwota końcowa zł | kod\nPrzykład: 3 | 2 | 100 | BRAK\nAlbo: 3 | 2 | 90 | KUBA10\n\nKwota to ostateczna cena produktów, po rabacie (bez dostawy). Kod promotora naliczy prowizję 10%. ID produktu znajdziesz w Magazynie.\n/anuluj — przerwij.')
    elif action == 'archive':
        await query.message.reply_text(archive_text())
    elif action == 'voidlist':
        keyboard = confirmed_orders_keyboard()
        await query.message.reply_text('🗑️ Wybierz potwierdzone zamówienie do cofnięcia (ostatnie 20):' if keyboard else 'Brak potwierdzonych zamówień.', reply_markup=keyboard)
    elif action == 'promoters':
        await query.message.reply_text(promoters_text())
    elif action == 'codes':
        await query.message.reply_text(codes_text())
    elif action == 'newprom':
        context.user_data['admin_action'] = 'newprom'
        await query.message.reply_text('👥 Nowy promotor. Wyślij:\nImię | KOD | Rabat %\nNp. Kuba | KUBA10 | 10\n\n/anuluj — przerwij')
    elif action == 'newcode':
        context.user_data['admin_action'] = 'newcode'
        await query.message.reply_text('🎟️ Nowy kod ogólny. Wyślij:\nKOD | Rabat %\nNp. JESIEN15 | 15\n\n/anuluj — przerwij')
    elif action == 'payout':
        context.user_data['admin_action'] = 'payout'
        await query.message.reply_text('💸 Zapisz wypłatę. Wyślij:\nID promotora | kwota zł\nNp. 1 | 25.50\n\nID znajdziesz w Promotorach. /anuluj — przerwij')
    elif action == 'codetoggle':
        context.user_data['admin_action'] = 'codetoggle'
        await query.message.reply_text('🔒 Włącz lub wyłącz kod (ogólny albo promotora). Wyślij:\nKOD | wylacz\nlub\nKOD | wlacz\n\n/anuluj — przerwij')
    elif action == 'add':
        context.user_data['admin_action'] = 'add'
        await query.message.reply_text('➕ Podaj nowy smak i ilość w formacie:\nNazwa smaku | 10\n\nWyślij /anuluj, aby przerwać.')
    elif action == 'change':
        context.user_data['admin_action'] = 'change'
        await query.message.reply_text('🚚 Podaj ID produktu i nowy stan (nie ilość dostawy):\nID | nowy stan\nPrzykład: 3 | 25\n\nID znajdziesz w Magazynie. /anuluj przerywa.')
    elif action == 'toggle':
        context.user_data['admin_action'] = 'toggle'
        await query.message.reply_text('👁 Podaj ID produktu i działanie:\n3 | ukryj\nlub\n3 | pokaz\n\n/anuluj przerywa.')


async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.pop('admin_action', None)
    await update.effective_message.reply_text('Anulowano.', reply_markup=admin_keyboard())


async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    action = context.user_data.get('admin_action')
    if not action:
        return
    parts = [x.strip() for x in update.effective_message.text.split('|')]
    if len(parts) != (4 if action == 'manual' else 3 if action == 'newprom' else 2):
        await update.effective_message.reply_text('Nieprawidłowy format. Użyj separatora | lub /anuluj.')
        return
    try:
        if action == 'manual':
            product_id, qty = int(parts[0]), int(parts[1])
            order_id, name, amount_gr = manual_sale(product_id, qty, parts[2], parts[3], update.effective_user.id)
            result = f'✅ Dodano sprzedaż ręczną #{order_id}\n{name} — {qty} szt.\nKwota: {amount_gr/100:.2f} zł\nMagazyn i statystyki zaktualizowane.'
        elif action == 'newprom':
            name, code, percent_text = parts
            code = code.upper()
            percent = int(percent_text)
            if not name or len(name) > 80 or not re.fullmatch(r'[A-Z0-9_-]{3,32}', code) or not 0 <= percent <= 90:
                raise ValueError('Podaj imię, kod (3-32 znaków A-Z/0-9) i rabat 0-90%.')
            with db() as c:
                if c.execute('SELECT 1 FROM promo_codes WHERE code = ? COLLATE NOCASE', (code,)).fetchone():
                    raise ValueError('Ten kod już istnieje jako kod ogólny.')
                c.execute('INSERT INTO promoters (name, code, discount_percent, created_at) VALUES (?, ?, ?, ?)', (name, code, percent, int(time.time())))
            result = f'✅ Dodano promotora {name}, kod {code}, rabat {percent}%, prowizja 10%.'
        elif action == 'newcode':
            code, percent_text = parts
            code = code.upper()
            percent = int(percent_text)
            if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code) or not 0 <= percent <= 90:
                raise ValueError('Kod: 3-32 znaków A-Z/0-9, rabat 0-90%.')
            with db() as c:
                if c.execute('SELECT 1 FROM promoters WHERE code = ? COLLATE NOCASE', (code,)).fetchone():
                    raise ValueError('Ten kod należy już do promotora.')
                c.execute('INSERT INTO promo_codes (code, discount_percent, created_at) VALUES (?, ?, ?)', (code, percent, int(time.time())))
            result = f'✅ Dodano kod {code}, rabat {percent}%.'
        elif action == 'payout':
            promoter_id = int(parts[0])
            amount = money_grosze(parts[1].replace(',', '.'))
            if amount <= 0:
                raise ValueError('Kwota musi być większa od zera.')
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                earned = c.execute('SELECT COALESCE(SUM(commission_grosze),0) AS x FROM commissions WHERE promoter_id=?', (promoter_id,)).fetchone()['x']
                paid = c.execute('SELECT COALESCE(SUM(amount_grosze),0) AS x FROM promoter_payouts WHERE promoter_id=?', (promoter_id,)).fetchone()['x']
                if not c.execute('SELECT 1 FROM promoters WHERE id=?', (promoter_id,)).fetchone():
                    raise ValueError('Nie ma takiego promotora.')
                if amount > earned - paid:
                    raise ValueError(f'Do wypłaty pozostało {(earned-paid)/100:.2f} zł.')
                c.execute('INSERT INTO promoter_payouts (promoter_id, amount_grosze, created_at, admin_id) VALUES (?, ?, ?, ?)', (promoter_id, amount, int(time.time()), update.effective_user.id))
            result = f'✅ Zapisano wypłatę {amount/100:.2f} zł dla promotora #{promoter_id}.'
        elif action == 'codetoggle':
            code, mode = parts
            code = code.upper()
            mode = normalize_option(mode)
            if mode not in ('wlacz', 'wylacz'):
                raise ValueError('Wpisz wlacz albo wylacz.')
            active = 1 if mode == 'wlacz' else 0
            with db() as c:
                cur = c.execute('UPDATE promo_codes SET active=? WHERE code=? COLLATE NOCASE', (active, code))
                if not cur.rowcount:
                    cur = c.execute('UPDATE promoters SET active=? WHERE code=? COLLATE NOCASE', (active, code))
                if not cur.rowcount:
                    raise ValueError('Nie znaleziono kodu.')
            result = f'✅ Kod {code}: {"aktywny" if active else "wyłączony"}.'
        elif action == 'add':
            name, amount_text = parts
            amount = int(amount_text)
            if not name or len(name) > 100 or amount < 0 or amount > 100000:
                raise ValueError('Nazwa lub ilość jest nieprawidłowa.')
            with db() as c:
                c.execute('INSERT INTO products (name, stock, active, created_at) VALUES (?, ?, 1, ?)',
                          (name, amount, int(time.time())))
            result = f'✅ Dodano smak: {name} — {amount} szt.'
        elif action == 'change':
            product_id, amount_text = int(parts[0]), parts[1]
            amount = int(amount_text)
            if amount < 0 or amount > 100000:
                raise ValueError('Stan musi wynosić od 0 do 100000.')
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT name, stock FROM products WHERE id = ?', (product_id,)).fetchone()
                if not row:
                    raise ValueError('Nie znaleziono produktu.')
                delta = amount - row['stock']
                c.execute('UPDATE products SET stock = ? WHERE id = ?', (amount, product_id))
                c.execute('INSERT INTO stock_movements (product_id, delta, reason, created_at) VALUES (?, ?, ?, ?)',
                          (product_id, delta, f'Korekta admina {update.effective_user.id}', int(time.time())))
            result = f'✅ {row["name"]}: nowy stan {amount} szt.'
        elif action == 'toggle':
            product_id = int(parts[0])
            mode = parts[1].lower()
            if mode not in ('ukryj', 'pokaz', 'pokaż'):
                raise ValueError('Wpisz ukryj albo pokaz.')
            active = 0 if mode == 'ukryj' else 1
            with db() as c:
                cursor = c.execute('UPDATE products SET active = ? WHERE id = ?', (active, product_id))
                if not cursor.rowcount:
                    raise ValueError('Nie znaleziono produktu.')
            result = '✅ Zmieniono widoczność produktu.'
        else:
            return
    except (ValueError, sqlite3.IntegrityError) as exc:
        await update.effective_message.reply_text(f'❌ {exc}\nSpróbuj ponownie lub /anuluj.')
        return
    context.user_data.pop('admin_action', None)
    await update.effective_message.reply_text(result, reply_markup=admin_keyboard())


class OrderRequest(BaseModel):
    items: list[dict] = Field(min_length=1)
    total_qty: int | None = None
    total_price: int | None = None
    promo_code: str = 'Brak'
    delivery: str
    payment: str
    locker_id: str = ''
    delivery_point: str = ''
    init_data: str = ''
    pair_code: str = ''


telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler('start', start))
telegram_app.add_handler(CommandHandler('admin', admin))
telegram_app.add_handler(CommandHandler('anuluj', cancel_admin))
telegram_app.add_handler(CallbackQueryHandler(order_callback, pattern=r'^(confirm|cancel):'))
telegram_app.add_handler(CallbackQueryHandler(void_order_callback, pattern=r'^adm:void(?:ask|do):[0-9]+$'))
telegram_app.add_handler(CallbackQueryHandler(period_callback, pattern=r'^adm:period(?:ask|no|do:[0-9]+)$'))
telegram_app.add_handler(CallbackQueryHandler(admin_callback, pattern=r'^adm:'))
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, order_from_web))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, admin_input))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    if telegram_app.updater:
        await telegram_app.updater.start_polling(drop_pending_updates=False)
    log.info('PuffBear uruchomiony, baza: %s', DB_PATH)
    try:
        yield
    finally:
        if telegram_app.updater:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title='PuffBear Order API', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=SHOP_ORIGINS,
                   allow_credentials=False, allow_methods=['GET', 'POST', 'OPTIONS'],
                   allow_headers=['Content-Type'])


@app.get('/health')
async def health():
    return {'ok': True}


@app.get('/api/catalog')
async def catalog():
    return {'products': [{'name': p['name'], 'stock': p['stock'], 'active': bool(p['active']),
                          'sold_out': p['stock'] == 0} for p in products()]}


@app.get('/api/promos')
async def available_promos():
    with db() as c:
        general = [dict(r) for r in c.execute('SELECT code, discount_percent FROM promo_codes WHERE active=1')]
        promoter = [dict(r) for r in c.execute('SELECT code, discount_percent FROM promoters WHERE active=1')]
    return {'codes': general + promoter}


@app.post('/api/orders')
async def create_order(req: OrderRequest):
    auth = verify_webapp_init_data(req.init_data) if req.init_data else None
    if auth:
        chat_id, user = auth['chat_id'], auth['user']
    elif req.pair_code:
        chat_id = consume_pair_code(req.pair_code)
        if not chat_id:
            raise HTTPException(status_code=401, detail='Kod połączenia jest nieprawidłowy lub wygasł. Wyślij /start.')
        user = {'id': chat_id}
    else:
        raise HTTPException(status_code=401, detail='Otwórz bota Telegram i wyślij /start, aby otrzymać kod połączenia.')
    try:
        return await order_api(req.model_dump(), chat_id, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
