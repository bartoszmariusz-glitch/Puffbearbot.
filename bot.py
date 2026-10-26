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
DELIVERY_ALLOWED = {
    'Paczkomat InPost', 'Park Lisiniec (Częstochowa)',
    'Północ (Częstochowa)', 'Centrum (Częstochowa)',
    'Estakada (Częstochowa)', 'Trzech Wieszczów (Częstochowa)'
}
PAYMENT_ALLOWED = {'Przelew / BLIK z góry', 'Przelew / Blik (z góry)', 'Gotówka przy odbiorze'}


def normalize_option(value):
    """Normalize labels/HTML option values while preserving an allowlist."""
    text = unicodedata.normalize('NFKD', str(value or '').strip().casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', text)


PAYMENT_ALIASES = {
    'gotowkaprzyodbiorze': 'Gotówka przy odbiorze',
    'gotowka': 'Gotówka przy odbiorze',
    'cash': 'Gotówka przy odbiorze',
    'cashondelivery': 'Gotówka przy odbiorze',
    'cod': 'Gotówka przy odbiorze',
    'przelewblikzgory': 'Przelew / BLIK z góry',
    'przelewblik': 'Przelew / BLIK z góry',
    'blik': 'Przelew / BLIK z góry',
    'transfer': 'Przelew / BLIK z góry',
    'banktransfer': 'Przelew / BLIK z góry',
    'prepaid': 'Przelew / BLIK z góry',
    'online': 'Przelew / BLIK z góry',
}
DELIVERY_ALIASES = {normalize_option(label): label for label in DELIVERY_ALLOWED}
DELIVERY_ALIASES.update({
    'inpost': 'Paczkomat InPost',
    'paczkomat': 'Paczkomat InPost',
    'paczkomatinpost': 'Paczkomat InPost',
})


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


def confirmed_promoter_stats():
    with db() as c:
        return [dict(r) for r in c.execute('''SELECT p.id, p.name, p.code, p.discount_percent, p.active,
            COUNT(co.order_id) AS orders_count,
            COALESCE(SUM(co.base_grosze),0) AS sales_grosze,
            COALESCE(SUM(co.commission_grosze),0) AS commission_grosze,
            COALESCE((SELECT SUM(py.amount_grosze) FROM promoter_payouts py WHERE py.promoter_id=p.id),0) AS paid_grosze
            FROM promoters p LEFT JOIN commissions co ON co.promoter_id=p.id
            GROUP BY p.id ORDER BY p.id''')]


def promoters_text():
    rows = confirmed_promoter_stats()
    if not rows:
        return '👥 Brak promotorów. Wybierz „Dodaj promotora”.'
    lines = ['👥 PROMOTORZY — prowizja 10% po rabacie', '']
    for r in rows:
        due = r['commission_grosze'] - r['paid_grosze']
        lines += [f'#{r["id"]} {r["name"]} — {r["code"]} ({r["discount_percent"]}% rabatu)' + ('' if r['active'] else ' [WYŁĄCZONY]'),
                  f'  Zamówienia: {r["orders_count"]} | Sprzedaż: {r["sales_grosze"]/100:.2f} zł',
                  f'  Prowizja: {r["commission_grosze"]/100:.2f} zł | Wypłacono: {r["paid_grosze"]/100:.2f} zł',
                  f'  Do wypłaty: {due/100:.2f} zł', '']
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
        try:
            qty = int(item.get('quantity', 0))
        except (ValueError, TypeError):
            raise ValueError('Nieprawidłowa ilość.')
        product = catalog.get(name.casefold())
        if not product or not product['active']:
            raise ValueError(f'Produkt niedostępny: {name}')
        if qty < 1 or qty > 100:
            raise ValueError('Nieprawidłowa liczba sztuk.')
        quantities[product['name']] = quantities.get(product['name'], 0) + qty
    total_qty = sum(quantities.values())
    if total_qty > 200:
        raise ValueError('Maksymalnie 200 sztuk w jednym zamówieniu.')
    for name, qty in quantities.items():
        if qty > catalog[name.casefold()]['stock']:
            raise ValueError(f'{name}: dostępne tylko {catalog[name.casefold()]["stock"]} szt.')
    promo = str(raw.get('promo_code', 'Brak')).strip().upper()
    promoter, discount = promo_lookup(promo)
    raw_delivery = str(raw.get('delivery', '')).strip()
    raw_payment = str(raw.get('payment', '')).strip()
    delivery = DELIVERY_ALIASES.get(normalize_option(raw_delivery))
    payment = PAYMENT_ALIASES.get(normalize_option(raw_payment))
    if not delivery:
        log.warning('Odrzucono nieznaną formę dostawy: %r', raw_delivery)
        raise ValueError('Nieprawidłowa forma dostawy. Skontaktuj się z obsługą sklepu.')
    if not payment:
        log.warning('Odrzucono nieznaną formę płatności: %r', raw_payment)
        raise ValueError('Nieprawidłowa forma płatności. Skontaktuj się z obsługą sklepu.')
    price = unit_price(total_qty)
    total = total_qty * price
    if discount:
        total -= round(total * discount / 100)
    return {'items': [{'flavor': n, 'quantity': q} for n, q in quantities.items()],
            'total_qty': total_qty, 'unit_price': price, 'total_price': total,
            'promo_code': promo if (discount or promoter) else 'Brak', 'discount_percent': discount,
            'promoter_id': promoter['id'] if promoter else None,
            'delivery': delivery, 'payment': payment}


def format_order(order_id, payload, user=None):
    lines = [f'🧾 Zamówienie #{order_id}', '', 'Produkty:']
    for item in payload['items']:
        lines.append(f'• {item["flavor"]} — {item["quantity"]} szt.')
    lines += ['', f'Razem sztuk: {payload["total_qty"]}',
              f'Cena za sztukę: {payload["unit_price"]} zł',
              f'Kwota końcowa: {payload["total_price"]} zł',
              f'Kod promocyjny: {payload["promo_code"]}',
              f'Dostawa: {payload["delivery"]}', f'Płatność: {payload["payment"]}']
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
               'Aromaty spożywcze 30 ml — bez nikotyny.\n'
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
        cursor = c.execute('INSERT INTO orders (chat_id, payload, status, created_at) VALUES (?, ?, ?, ?)',
                           (chat_id, json.dumps({'order': payload, 'user': user}, ensure_ascii=False), 'pending', int(time.time())))
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
    return {'ok': True, 'order_id': order_id}


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
    """One transaction: status change and stock deduction cannot diverge."""
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT chat_id, payload, status FROM orders WHERE id = ?', (order_id,)).fetchone()
        if not row:
            return 'Nie znaleziono zamówienia.', None
        if int(row['chat_id']) != int(chat_id):
            return 'To nie jest Twoje zamówienie.', None
        if row['status'] not in ('pending', 'Oczekujące', 'OczekujÄce'):
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
                base = money_grosze(payload['total_price'])
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
        rows = c.execute("SELECT payload FROM orders WHERE status = 'confirmed'").fetchall()
    counts = {}
    revenue = 0
    for row in rows:
        try:
            payload = json.loads(row['payload'])['order']
            revenue += int(payload['total_price'])
            for item in payload['items']:
                name = item['flavor']
                counts[name] = counts.get(name, 0) + int(item['quantity'])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            log.warning('Pominięto nieprawidłowy rekord statystyk')
    lines = ['📊 SPRZEDAŻ (potwierdzone zamówienia)',
             f'Zamówienia: {len(rows)}', f'Przychód wg zamówień: {revenue} zł',
             f'Sprzedane sztuki: {sum(counts.values())}', '']
    lines += [f'• {name}: {qty} szt.' for name, qty in sorted(counts.items())]
    return '\n'.join(lines)


def promos_text():
    with db() as c:
        rows = c.execute("SELECT payload FROM orders WHERE status = 'confirmed'").fetchall()
    stats = {}
    for row in rows:
        try:
            payload = json.loads(row['payload'])['order']
            code = payload.get('promo_code', 'Brak')
            if code == 'Brak':
                continue
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
    if len(parts) != (3 if action == 'newprom' else 2):
        await update.effective_message.reply_text('Nieprawidłowy format. Użyj separatora | lub /anuluj.')
        return
    try:
        if action == 'newprom':
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
    init_data: str = ''
    pair_code: str = ''


telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler('start', start))
telegram_app.add_handler(CommandHandler('admin', admin))
telegram_app.add_handler(CommandHandler('anuluj', cancel_admin))
telegram_app.add_handler(CallbackQueryHandler(order_callback, pattern=r'^(confirm|cancel):'))
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
