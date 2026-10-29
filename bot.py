import os
import asyncio
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
from fastapi.responses import FileResponse
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ['BOT_TOKEN']
SHOP_WEBAPP_URL = os.environ.get('SHOP_WEBAPP_URL', 'https://strona-do-bot-liquidy.pages.dev').strip()
SHOP_ORIGINS = [x.strip().rstrip('/') for x in os.environ.get('SHOP_ORIGINS', SHOP_WEBAPP_URL).split(',') if x.strip()]
ADMIN_CHAT_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_CHAT_IDS', '').split(',') if x.strip()]
DB_PATH = os.environ.get('DB_PATH', '/data/puffbear_orders.sqlite3' if os.path.isdir('/data') else 'puffbear_orders.sqlite3')
WELCOME_LOGO_URL = os.environ.get('WELCOME_LOGO_URL', '').strip()
IMAGE_DIR = Path(os.environ.get('PRODUCT_IMAGE_DIR', '/data/product_images' if os.path.isdir('/data') else 'product_images')).resolve()
PUBLIC_API_BASE = os.environ.get('PUBLIC_API_BASE', 'https://courageous-clarity-botdotestow.up.railway.app').strip().rstrip('/')
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
        c.execute("""CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            sender TEXT NOT NULL CHECK(sender IN ('customer', 'admin')),
            admin_id INTEGER,
            body TEXT NOT NULL,
            order_id INTEGER,
            created_at INTEGER NOT NULL
        )""")
        c.execute('CREATE INDEX IF NOT EXISTS idx_support_customer ON support_messages(customer_id, created_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_support_created ON support_messages(created_at)')
        c.execute("""CREATE TABLE IF NOT EXISTS support_claims (
            customer_id INTEGER PRIMARY KEY,
            admin_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )""")
        c.execute('''CREATE TABLE IF NOT EXISTS delivery_plans (order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, preferred_date TEXT, preferred_slots TEXT, agreed_slot TEXT, proposed_slot TEXT, state TEXT NOT NULL DEFAULT 'awaiting_admin', updated_at INTEGER NOT NULL)''')
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
        c.execute('CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, active INTEGER NOT NULL DEFAULT 1)')
        c.execute('CREATE TABLE IF NOT EXISTS category_tiers (category_id INTEGER NOT NULL, min_qty INTEGER NOT NULL, price_grosze INTEGER NOT NULL CHECK(price_grosze>=0), PRIMARY KEY(category_id,min_qty))')
        pcols = {r['name'] for r in c.execute('PRAGMA table_info(products)')}
        for column, definition in [('category_id', 'INTEGER NOT NULL DEFAULT 1'), ('description', "TEXT NOT NULL DEFAULT ''"), ('image_name', "TEXT NOT NULL DEFAULT ''")]:
            if column not in pcols:
                c.execute(f'ALTER TABLE products ADD COLUMN {column} {definition}')
        c.execute("INSERT OR IGNORE INTO categories(id,name,active) VALUES (1,'Liquidy',1)")
        for min_qty, zl in [(1,55),(3,50),(5,45),(10,40),(20,33),(50,29)]:
            c.execute('INSERT OR IGNORE INTO category_tiers(category_id,min_qty,price_grosze) VALUES (1,?,?)', (min_qty,zl*100))
        # Migracja dotychczasowych kodow ogolnych do promotorow.
        # Bez zmiany istniejacych prowizji, zamowien i wyplat.
        for code in sorted(VALID_PROMOS):
            existing = c.execute(
                'SELECT discount_percent, active, created_at FROM promo_codes WHERE code=? COLLATE NOCASE',
                (code,)
            ).fetchone()
            already_promoter = c.execute(
                'SELECT id FROM promoters WHERE code=? COLLATE NOCASE', (code,)
            ).fetchone()
            if not already_promoter:
                c.execute(
                    'INSERT INTO promoters (name, code, discount_percent, active, created_at) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (code, code, int(existing['discount_percent']) if existing else 10,
                     int(existing['active']) if existing else 1,
                     int(existing['created_at']) if existing else int(time.time()))
                )
            c.execute('DELETE FROM promo_codes WHERE code=? COLLATE NOCASE', (code,))
        for name in INITIAL_FLAVORS:
            c.execute('INSERT OR IGNORE INTO products (name, stock, active, created_at) VALUES (?, 10, 1, ?)', (name, int(time.time())))


def products():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT id, name, stock, active, category_id, description, image_name FROM products ORDER BY id')]


def category_catalog():
    with db() as c:
        cats = [dict(r) for r in c.execute('SELECT id,name,active FROM categories ORDER BY id')]
        tiers = [dict(r) for r in c.execute('SELECT category_id,min_qty,price_grosze FROM category_tiers ORDER BY category_id,min_qty')]
    for cat in cats:
        cat['tiers'] = [{'min_qty': t['min_qty'], 'price_grosze': t['price_grosze']} for t in tiers if t['category_id']==cat['id']]
    return cats


def parse_tiers(value):
    # Format: 1-2 = 100; 3-4 = 90; 5+ = 80
    entries=[]
    for segment in re.split(r'[;\n]+',value.strip()):
        segment=segment.strip()
        if not segment: continue
        match=re.fullmatch(r'(\d+)\s*(?:-\s*(\d+)|\+)?\s*=\s*(\d+(?:[.,]\d{1,2})?)',segment)
        if not match: raise ValueError('Format progów: 1-2 = 100; 3-4 = 90; 5+ = 80')
        lo=int(match.group(1)); hi=int(match.group(2)) if match.group(2) else None
        if lo<1 or lo>10000 or (hi is not None and hi<lo): raise ValueError('Nieprawidłowy zakres ilości.')
        gr=money_grosze(match.group(3).replace(',','.'))
        if gr<0 or gr>100000000: raise ValueError('Nieprawidłowa cena.')
        entries.append((lo,hi,gr))
    if not entries or entries[0][0]!=1: raise ValueError('Pierwszy próg musi zaczynać się od 1.')
    for idx,(lo,hi,gr) in enumerate(entries):
        if idx and (entries[idx-1][1] is None or lo!=entries[idx-1][1]+1): raise ValueError('Progi muszą być ciągłe i bez nakładania.')
        if idx<len(entries)-1 and hi is None: raise ValueError('Tylko ostatni próg może mieć +.')
        if idx==len(entries)-1 and hi is not None: raise ValueError('Ostatni próg musi kończyć się znakiem +.')
    return [(lo,gr) for lo,hi,gr in entries]


def set_tiers(category_id, text):
    tiers=parse_tiers(text)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM categories WHERE id=?',(category_id,)).fetchone(): raise ValueError('Nie ma takiej kategorii.')
        c.execute('DELETE FROM category_tiers WHERE category_id=?',(category_id,))
        c.executemany('INSERT INTO category_tiers(category_id,min_qty,price_grosze) VALUES (?,?,?)',[(category_id,lo,gr) for lo,gr in tiers])


def categories_text():
    cats=category_catalog()
    lines=['📁 KATEGORIE I PROGI','']
    for cat in cats:
        lines.append(f'#{cat["id"]} {cat["name"]}' + ('' if cat['active'] else ' [UKRYTA]'))
        for i,t in enumerate(cat['tiers']):
            upper=str(cat['tiers'][i+1]['min_qty']-1) if i+1<len(cat['tiers']) else '+'
            lines.append(f'  {t["min_qty"]}-{upper} szt.: {t["price_grosze"]/100:.2f} zł/szt.')
        lines.append('')
    return '\n'.join(lines)



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


def money_from_grosze(value):
    from decimal import Decimal
    return float(Decimal(value)/100)


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

    recipient = {'recipient_first_name': '', 'recipient_last_name': '',
                 'recipient_email': '', 'recipient_phone': ''}
    if delivery == 'Paczkomat InPost':
        recipient = {key: str(raw.get(key) or '').strip() for key in recipient}
        if not all(recipient.values()):
            raise ValueError('Podaj imię, nazwisko, e-mail i telefon odbiorcy InPost.')
        if any(len(recipient[k]) > limit for k, limit in (
                ('recipient_first_name', 80), ('recipient_last_name', 80),
                ('recipient_email', 254), ('recipient_phone', 32))):
            raise ValueError('Dane odbiorcy są zbyt długie.')
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient['recipient_email']):
            raise ValueError('Podaj prawidłowy adres e-mail.')
        if not re.fullmatch(r'\+?[0-9 ()-]{7,32}', recipient['recipient_phone']):
            raise ValueError('Podaj prawidłowy numer telefonu.')

    preferred_date = ''
    preferred_slots = []
    if delivery == 'Dowóz pod pinezkę':
        preferred_date = str(raw.get('delivery_date') or '').strip()
        slots = raw.get('delivery_slots') or []
        if not isinstance(slots, list):
            raise ValueError('Wybierz godziny dostawy.')
        allowed_slots = {'10:00–12:00', '12:00–14:00', '14:00–16:00', '16:00–18:00', '18:00–20:00'}
        preferred_slots = list(dict.fromkeys(str(x) for x in slots))
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', preferred_date):
            raise ValueError('Wybierz datę dostawy.')
        from datetime import date, timedelta
        try:
            selected_day = date.fromisoformat(preferred_date)
        except ValueError:
            raise ValueError('Nieprawidłowa data dostawy.')
        if selected_day < date.today() or selected_day > date.today() + timedelta(days=30):
            raise ValueError('Wybierz dzień w ciągu najbliższych 30 dni.')
        if not preferred_slots or len(preferred_slots) > 5 or any(x not in allowed_slots for x in preferred_slots):
            raise ValueError('Zaznacz co najmniej jeden przedział godzinowy.')

    # Liczba sztuk i progi są rozliczane osobno w każdej kategorii.
    with db() as c:
        cat_quantities = {}
        for name,qty in quantities.items():
            cid = int(catalog[name.casefold()]['category_id'])
            cat_quantities[cid] = cat_quantities.get(cid,0)+qty
        cat_prices={}
        for cid,qty in cat_quantities.items():
            tiers=c.execute('SELECT min_qty,price_grosze FROM category_tiers WHERE category_id=? ORDER BY min_qty',(cid,)).fetchall()
            if not tiers: raise ValueError('Brak cennika dla kategorii.')
            cat_prices[cid]=next((int(t['price_grosze']) for t in reversed(tiers) if qty>=t['min_qty']),None)
            if cat_prices[cid] is None: raise ValueError('Nieprawidłowe progi cenowe.')
    item_prices={name:cat_prices[int(catalog[name.casefold()]['category_id'])] for name in quantities}
    before_discount_grosze=sum(qty*item_prices[name] for name,qty in quantities.items())
    before_discount=money_from_grosze(before_discount_grosze)
    price=unit_price(total_qty)  # zgodność ze starszymi wiadomościami

    discount_grosze=(before_discount_grosze*discount+50)//100
    discount_amount=money_from_grosze(discount_grosze)
    products_total=money_from_grosze(before_discount_grosze-discount_grosze)
    delivery_fee = DELIVERY_FEES[delivery]
    grand_total = products_total + delivery_fee
    return {
        'items': [{'flavor': n, 'quantity': q, 'unit_price_grosze': item_prices[n]} for n, q in quantities.items()],
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
        'map_url': map_url, 'delivery_date': preferred_date, 'delivery_slots': preferred_slots,
        **recipient,
        'cash_notice': 'Prosimy przygotować odliczoną gotówkę.' if payment == 'Gotówka' else '',
    }


def format_order(order_id, payload, user=None):
    lines = [f'🧾 Zamówienie #{order_id}', '', 'Produkty:']
    for item in payload['items']:
        lines.append(f'• {item["flavor"]} — {item["quantity"]} szt.' + (f' × {item["unit_price_grosze"]/100:.2f} zł' if 'unit_price_grosze' in item else ''))
    products_total = payload.get('products_total', payload['total_price'])
    delivery_fee = payload.get('delivery_fee', 0)
    lines += ['', f'Razem sztuk: {payload["total_qty"]}',
              ('Ceny za sztukę zależą od kategorii i ilości' if any('unit_price_grosze' in i for i in payload['items']) else f'Cena za sztukę: {payload["unit_price"]} zł'),
              f'Produkty przed rabatem: {payload.get("products_before_discount", products_total)} zł',
              f'Rabat: -{payload.get("discount_amount", 0)} zł',
              f'Produkty po rabacie: {products_total} zł',
              f'Koszt dostawy: {delivery_fee} zł',
              f'💰 DO ZAPŁATY: {payload["total_price"]} zł',
              f'Kod promocyjny: {payload["promo_code"]}',
              f'Dostawa: {payload["delivery"]}']
    if payload.get('locker_id'):
        lines.append(f'📦 Paczkomat: {payload["locker_id"]}')
        if payload.get('recipient_first_name'):
            lines.extend([
                '', '👤 DANE ODBIORCY INPOST',
                f'Imię: {payload.get("recipient_first_name", "")}',
                f'Nazwisko: {payload.get("recipient_last_name", "")}',
                f'E-mail: {payload.get("recipient_email", "")}',
                f'Telefon: {payload.get("recipient_phone", "")}',
            ])
    if payload.get('delivery_point'):
        lines.append(f'📍 Pinezka: {payload["delivery_point"]}')
        lines.append(f'🗺 Mapa: {payload.get("map_url", "")}')
    if payload.get('delivery_date'):
        lines.append(f'📅 Preferowany dzień: {payload["delivery_date"]}')
        lines.append('🕒 Pasujące godziny: ' + ', '.join(payload.get('delivery_slots', [])))
        lines.append('ℹ️ Termin wymaga potwierdzenia przez obsługę.')
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
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('🛍 Otwórz sklep PuffBear', web_app=WebAppInfo(url=SHOP_WEBAPP_URL))],
        [InlineKeyboardButton('💬 Kontakt z obsługą', callback_data='support:open')]
    ])
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
    payload = saved['order']
    if payload.get('delivery') == 'Dowóz pod pinezkę':
        with db() as c:
            c.execute('INSERT OR IGNORE INTO delivery_plans(order_id,customer_id,preferred_date,preferred_slots,state,updated_at) VALUES (?,?,?,?,?,?)',
                      (order_id, query.from_user.id, payload.get('delivery_date',''), json.dumps(payload.get('delivery_slots',[]),ensure_ascii=False), 'awaiting_admin', int(time.time())))
    text = '🐻 PUFFBEAR | NOWE ZAMÓWIENIE\n\n' + format_order(order_id, payload, saved.get('user', {}))
    delivered = 0
    for admin_id in ADMIN_CHAT_IDS:
        try:
            kb = delivery_admin_keyboard(order_id) if payload.get('delivery') == 'Dowóz pod pinezkę' else order_status_keyboard(order_id)
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=kb)
            if payload.get('map_url'):
                await context.bot.send_message(chat_id=admin_id, text='📍 Wybrana pinezka: ' + payload['delivery_point'], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🗺 Otwórz pinezkę w mapach',url=payload['map_url'])]]))
            delivered += 1
        except Exception:
            log.exception('Nie udało się powiadomić administratora %s', admin_id)
    await query.edit_message_text('✅ Zamówienie przyjęte! 🐻\n\nDziękujemy za zakupy. Status i ustalenia dotyczące dostawy otrzymasz tutaj, w tym czacie.\n\n' +
                                  (f'Powiadomiono administratorów: {delivered}/{len(ADMIN_CHAT_IDS)}.' if delivered else
                                   'Nie udało się wysłać powiadomienia administratorom.'))


def is_admin(update):
    return bool(update.effective_user and update.effective_user.id in ADMIN_CHAT_IDS and
                update.effective_chat and update.effective_chat.type == 'private')


def admin_keyboard():
    """Czytelne menu główne; wszystkie dotychczasowe funkcje w podmenu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📦 Produkty i magazyn', callback_data='ui:products')],
        [InlineKeyboardButton('📁 Kategorie i ceny', callback_data='ui:categories')],
        [InlineKeyboardButton('🛒 Zamówienia i sprzedaż', callback_data='ui:orders')],
        [InlineKeyboardButton('💬 Obsługa klientów', callback_data='ui:support')],
        [InlineKeyboardButton('👥 Promotorzy i kody', callback_data='ui:marketing')],
        [InlineKeyboardButton('📊 Raporty i okresy', callback_data='ui:reports')],
    ])


def ui_keyboard(section):
    pages={
      'products': [('➕ Dodaj produkt 📷','ui:begin:product_new'),('✏️ Edytuj produkt','ui:pick:product_edit'),('🖼 Zmień zdjęcie','ui:pick:product_photo_id'),('📥 Ustaw stan magazynu','ui:pick:change'),('👁 Ukryj / pokaż','ui:pick:toggle'),('📦 Pokaż magazyn','adm:stock')],
      'categories':[('➕ Nowa kategoria','ui:begin:category_new'),('💰 Zmień ceny przyciskami','price:categories'),('📁 Pokaż kategorie','ui:show:categories')],
      'support':[('💬 Otwórz rozmowy','ui:show:support')],
      'orders':[('💬 Otwórz zamówienia i napisz do klienta','orders:list'),('🗑 Cofnij zamówienie','adm:voidlist'),('➕ Sprzedaż ręczna','ui:pick:manual'),('📦 Magazyn','adm:stock')],
      'marketing':[('👥 Lista promotorów','adm:promoters'),('➕ Dodaj promotora','ui:begin:newprom'),('🎟 Kody ogólne','adm:codes'),('➕ Dodaj kod','ui:begin:newcode'),('💸 Wypłać prowizję','ui:pick:payout'),('🔒 Włącz / wyłącz kod','ui:pick:codetoggle')],
      'reports':[('📊 Sprzedaż','adm:sales'),('🎟 Statystyki kodów','adm:promos'),('🗂 Archiwum','adm:archive'),('📅 Rozpocznij nowy okres','adm:periodask')],
    }
    buttons=[[InlineKeyboardButton(label,callback_data=callback)] for label,callback in pages.get(section,[])]
    buttons.append([InlineKeyboardButton('⬅️ Menu główne',callback_data='ui:home')])
    return InlineKeyboardMarkup(buttons)


def ui_choices(kind):
    with db() as c:
        if kind=='category':
            rows=c.execute('SELECT id,name FROM categories ORDER BY id').fetchall()
        elif kind=='product':
            rows=c.execute('SELECT id,name FROM products ORDER BY id DESC LIMIT 80').fetchall()
        elif kind=='promoter':
            rows=c.execute('SELECT id,name FROM promoters ORDER BY id DESC LIMIT 80').fetchall()
        elif kind=='code':
            rows=c.execute('SELECT code AS id,code AS name FROM promo_codes UNION ALL SELECT code AS id,code AS name FROM promoters ORDER BY name LIMIT 80').fetchall()
        else: rows=[]
    return [dict(r) for r in rows]


UI_FIELDS={
 'product_new':[('name','✏️ Wpisz nazwę nowego produktu:'),('category','📁 Wybierz kategorię produktu:'),('stock','📦 Ile sztuk jest w magazynie? Podaj liczbę:'),('description','📝 Wpisz opis produktu (lub wyślij - aby pominąć):')],
 'product_edit':[('product','✏️ Wybierz produkt do edycji:'),('name','✏️ Podaj nową nazwę produktu:'),('category','📁 Wybierz kategorię:'),('description','📝 Podaj nowy opis (lub - aby pozostawić pusty):')],
 'category_new':[('name','📁 Jak nazwać nową kategorię?')],
 'category_tiers':[('category','📁 Wybierz kategorię, której ceny zmieniasz:'),('tiers','💰 Wyślij progi cenowe, np. 1-2 = 100; 3-4 = 90; 5+ = 80')],
 'product_photo_id':[('product','🖼 Wybierz produkt, któremu zmieniasz zdjęcie:')],
 'change':[('product','📦 Wybierz produkt:'),('stock','📥 Podaj NOWY łączny stan magazynu (liczba):')],
 'toggle':[('product','👁 Wybierz produkt:'),('visibility','Czy chcesz ukryć czy pokazać produkt?')],
 'manual':[('product','🛒 Wybierz produkt sprzedany ręcznie:'),('quantity','📦 Podaj sprzedaną liczbę sztuk:'),('amount','💰 Podaj końcową kwotę produktów w zł (bez dostawy):'),('code','🎟 Wpisz kod promotora lub BRAK:')],
 'newprom':[('name','👤 Podaj imię lub nazwę promotora:'),('code','🎟 Podaj jego kod promocyjny:'),('percent','💯 Podaj rabat procentowy, np. 10:')],
 'newcode':[('code','🎟 Podaj nowy kod ogólny:'),('percent','💯 Podaj rabat procentowy, np. 10:')],
 'payout':[('promoter','👤 Wybierz promotora:'),('amount','💸 Podaj kwotę wypłaty w zł:')],
 'codetoggle':[('code_select','🎟 Wybierz kod:'),('code_mode','🔒 Włącz czy wyłącz kod?')],
}


def ui_picker(field):
    kind={'category':'category','product':'product','promoter':'promoter','code_select':'code'}.get(field)
    if not kind: return None
    rows=ui_choices(kind)
    buttons=[[InlineKeyboardButton(f"{r['name'][:35]} (#{r['id']})" if kind!='code' else r['name'][:45],callback_data=f"ui:choice:{str(r['id'])}")] for r in rows]
    buttons.append([InlineKeyboardButton('❌ Anuluj',callback_data='ui:stop')])
    return InlineKeyboardMarkup(buttons)


async def ui_prompt(message,context):
    state=context.user_data.get('ui_wizard')
    if not state:return
    fields=UI_FIELDS[state['action']]
    if state['step']>=len(fields):return
    field,question=fields[state['step']]
    if field in ('visibility','code_mode'):
        options=[('👁 Pokaż','pokaz'),('🚫 Ukryj','ukryj')] if field=='visibility' else [('🔓 Włącz','wlacz'),('🔒 Wyłącz','wylacz')]
        kb=InlineKeyboardMarkup([[InlineKeyboardButton(label,callback_data=f'ui:choice:{value}')] for label,value in options]+[[InlineKeyboardButton('❌ Anuluj',callback_data='ui:stop')]])
    else:kb=ui_picker(field)
    await message.reply_text(f"Krok {state['step']+1}/{len(fields)} — {question}\n\n/anuluj — przerwij",reply_markup=kb)


class _WizardMessage:
    def __init__(self,original,text):self.original=original;self.text=text
    async def reply_text(self,*args,**kwargs):return await self.original.reply_text(*args,**kwargs)


class _WizardUpdate:
    def __init__(self,original,text):self.effective_user=original.effective_user;self.effective_chat=original.effective_chat;self.effective_message=_WizardMessage(original.effective_message,text)


async def ui_finish(update,context):
    state=context.user_data.pop('ui_wizard',None)
    if not state:return
    action=state['action'];v=state['values']
    if action=='category_new':raw=v['name']
    elif action=='product_new':raw=' | '.join([v['name'],v['category'],v['stock'],v['description']])
    elif action=='product_edit':raw=' | '.join([v['product'],v['name'],v['category'],v['description']])
    elif action=='category_tiers':raw=v['category']+' | '+v['tiers']
    elif action=='product_photo_id':raw=v['product']
    elif action=='change':raw=v['product']+' | '+v['stock']
    elif action=='toggle':raw=v['product']+' | '+v['visibility']
    elif action=='manual':raw=' | '.join([v['product'],v['quantity'],v['amount'],v['code']])
    elif action=='newprom':raw=' | '.join([v['name'],v['code'],v['percent']])
    elif action=='newcode':raw=' | '.join([v['code'],v['percent']])
    elif action=='payout':raw=v['promoter']+' | '+v['amount']
    elif action=='codetoggle':raw=v['code_select']+' | '+v['code_mode']
    else:return
    context.user_data['admin_action']=action
    # Korzystamy z tej samej sprawdzonej walidacji i transakcji co w starym panelu.
    await admin_input(_WizardUpdate(update,raw),context)
    # Gdy walidacja zwróci błąd, umożliwiamy powrót do ostatniego kroku.
    if context.user_data.get('admin_action')==action:
        context.user_data.pop('admin_action',None)
        state['step']=len(UI_FIELDS[action])-1
        state['values'].pop(UI_FIELDS[action][-1][0],None)
        context.user_data['ui_wizard']=state
        await ui_prompt(update.effective_message,context)


def price_categories_keyboard():
    rows = ui_choices('category')
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f'📁 {r["name"]}', callback_data=f'price:cat:{r["id"]}')] for r in rows]
        + [[InlineKeyboardButton('⬅️ Menu kategorii', callback_data='ui:categories')]])


def price_tiers_keyboard(category_id):
    with db() as c:
        category = c.execute('SELECT name FROM categories WHERE id=?', (category_id,)).fetchone()
        tiers = c.execute('SELECT min_qty,price_grosze FROM category_tiers WHERE category_id=? ORDER BY min_qty', (category_id,)).fetchall()
    if not category:
        raise ValueError('Kategoria nie istnieje.')
    buttons = []
    for i, t in enumerate(tiers):
        hi = str(tiers[i+1]['min_qty'] - 1) if i+1 < len(tiers) else '+'
        label = f'💰 {t["min_qty"]}–{hi} szt.  |  {t["price_grosze"]/100:.2f} zł'
        buttons.append([InlineKeyboardButton(label, callback_data=f'price:edit:{category_id}:{t["min_qty"]}')])
    buttons.append([InlineKeyboardButton('➕ Dodaj nowy próg', callback_data=f'price:add:{category_id}')])
    buttons.append([InlineKeyboardButton('⬅️ Wybierz kategorię', callback_data='price:categories')])
    return category['name'], InlineKeyboardMarkup(buttons)


async def price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer('Brak dostępu.', show_alert=True)
        return
    parts = q.data.split(':')
    await q.answer()
    if parts[1] == 'categories':
        context.user_data.pop('price_edit', None)
        await q.message.reply_text('💰 Wybierz kategorię:', reply_markup=price_categories_keyboard())
        return
    try:
        cid = int(parts[2])
        if parts[1] == 'cat':
            name, keyboard = price_tiers_keyboard(cid)
            await q.message.reply_text(f'💰 {name}\nWybierz próg do zmiany:', reply_markup=keyboard)
        elif parts[1] == 'edit':
            minimum = int(parts[3])
            with db() as c:
                row = c.execute('SELECT price_grosze FROM category_tiers WHERE category_id=? AND min_qty=?', (cid,minimum)).fetchone()
            if not row:
                await q.message.reply_text('Nie znaleziono progu. Otwórz cennik ponownie.')
                return
            context.user_data['price_edit'] = {'category_id': cid, 'min_qty': minimum, 'mode': 'edit'}
            await q.message.reply_text(f'✏️ Próg od {minimum} szt.\nAktualna cena: {row["price_grosze"]/100:.2f} zł\n\nWpisz tylko NOWĄ CENĘ w zł, np. 49.90.\n/anuluj — przerwij.')
        elif parts[1] == 'add':
            context.user_data['price_edit'] = {'category_id': cid, 'mode': 'add_min'}
            await q.message.reply_text('➕ Podaj liczbę sztuk, od której ma obowiązywać nowa cena, np. 15.\n/anuluj — przerwij.')
    except (ValueError, IndexError):
        await q.message.reply_text('Nieprawidłowy wybór. Otwórz cennik ponownie.')


async def price_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('price_edit')
    if not state or not is_admin(update):
        return
    raw = (update.effective_message.text or '').strip().replace(',', '.')
    if state['mode'] == 'add_min':
        if not raw.isdigit() or not 1 <= int(raw) <= 10000:
            await update.effective_message.reply_text('Podaj liczbę sztuk od 1 do 10000.')
            return
        state['min_qty'] = int(raw)
        state['mode'] = 'add_price'
        await update.effective_message.reply_text('💰 Podaj cenę za sztukę w zł, np. 39.90:')
        return
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount <= 0 or amount > 1000000 or amount.as_tuple().exponent < -2:
            raise ValueError()
        grosze = money_grosze(amount)
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text('Podaj poprawną cenę, np. 49.90.')
        return
    cid, minimum = state['category_id'], state['min_qty']
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM categories WHERE id=?', (cid,)).fetchone():
            raise ValueError('Kategoria nie istnieje.')
        if state['mode'] == 'edit':
            c.execute('UPDATE category_tiers SET price_grosze=? WHERE category_id=? AND min_qty=?', (grosze,cid,minimum))
        else:
            c.execute('INSERT INTO category_tiers(category_id,min_qty,price_grosze) VALUES (?,?,?) ON CONFLICT(category_id,min_qty) DO UPDATE SET price_grosze=excluded.price_grosze', (cid,minimum,grosze))
    context.user_data.pop('price_edit', None)
    name, keyboard = price_tiers_keyboard(cid)
    await update.effective_message.reply_text(f'✅ Zapisano: od {minimum} szt. — {grosze/100:.2f} zł/szt.\n\n💰 {name}', reply_markup=keyboard)


async def ui_callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    if not is_admin(update):await query.answer('Brak dostępu.',show_alert=True);return
    await query.answer()
    data=query.data.split(':',2)
    command=data[1]
    if command=='home':
        context.user_data.pop('ui_wizard',None)
        await query.message.reply_text('🐻 Panel administratora — wybierz dział:',reply_markup=admin_keyboard());return
    if command == 'support':
        await query.message.reply_text('💬 Obsługa klientów\n\nNowe wiadomości trafiają tutaj automatycznie. Kliknij „Odpowiedz” pod wiadomością klienta.');return
    if command in ('products','categories','orders','marketing','reports'):
        context.user_data.pop('ui_wizard',None)
        titles={'products':'📦 Produkty i magazyn','categories':'📁 Kategorie i ceny','orders':'🛒 Zamówienia i sprzedaż','marketing':'👥 Promotorzy i kody','reports':'📊 Raporty i okresy'}
        await query.message.reply_text(titles[command]+'\nWybierz, co chcesz zrobić:',reply_markup=ui_keyboard(command));return
    if command=='show':
        if data[2]=='support':
            await query.message.reply_text('💬 Rozmowy przychodzą automatycznie. Odpowiedz przyciskiem pod zgłoszeniem.');return
        await query.message.reply_text(categories_text(),reply_markup=ui_keyboard('categories'));return
    if command=='stop':
        context.user_data.pop('ui_wizard',None)
        await query.message.reply_text('Anulowano.',reply_markup=admin_keyboard());return
    if command=='begin':
        action=data[2]
        if action not in UI_FIELDS:return
        context.user_data.pop('admin_action',None)
        context.user_data['ui_wizard']={'action':action,'step':0,'values':{}}
        await ui_prompt(query.message,context);return
    if command=='pick':
        action=data[2]
        if action not in UI_FIELDS:return
        context.user_data.pop('admin_action',None)
        context.user_data['ui_wizard']={'action':action,'step':0,'values':{}}
        await ui_prompt(query.message,context);return
    if command=='choice':
        state=context.user_data.get('ui_wizard')
        if not state:return
        field=UI_FIELDS[state['action']][state['step']][0]
        if field not in ('category','product','promoter','code_select','visibility','code_mode'):return
        state['values'][field]=data[2]
        state['step']+=1
        if state['step']>=len(UI_FIELDS[state['action']]):await ui_finish(update,context)
        else:await ui_prompt(query.message,context)


async def ui_text(update:Update,context:ContextTypes.DEFAULT_TYPE):
    state=context.user_data.get('ui_wizard')
    if not state:return
    field=UI_FIELDS[state['action']][state['step']][0]
    if field in ('category','product','promoter','code_select','visibility','code_mode'):
        await update.effective_message.reply_text('Wybierz jedną z opcji przyciskami powyżej lub /anuluj.');return
    value=update.effective_message.text.strip()
    if not value:
        await update.effective_message.reply_text('Wpisz wartość lub /anuluj.');return
    if field in ('stock','quantity') and (not value.isdigit() or int(value)>100000 or (field=='quantity' and int(value)<1)):
        await update.effective_message.reply_text('Podaj poprawną liczbę całkowitą.');return
    if field=='description' and value=='-':value=' '
    if field=='tiers':
        try:parse_tiers(value)
        except ValueError as exc:await update.effective_message.reply_text(f'❌ {exc}');return
    if '|' in value:
        await update.effective_message.reply_text('Nie używaj znaku |. Wpisz tylko odpowiedź na pytanie.');return
    state['values'][field]=value
    state['step']+=1
    if state['step']>=len(UI_FIELDS[state['action']]):await ui_finish(update,context)
    else:await ui_prompt(update.effective_message,context)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text('⛔ Brak dostępu.')
        return
    context.user_data.pop('admin_action', None)
    context.user_data.pop('ui_wizard', None)
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
    action, order_id = parts[1].removeprefix('void'), int(parts[2])
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
    elif action == 'categories':
        await query.message.reply_text(categories_text()+'\nNowa kategoria: /admin → Dodaj kategorię')
        context.user_data['admin_action']='category_new'
        await query.message.reply_text('📁 Aby dodać kategorię, wyślij jej nazwę (np. Akcesoria). /anuluj — przerwij.')
    elif action == 'tiers':
        context.user_data['admin_action']='category_tiers'
        await query.message.reply_text(categories_text()+'\nWyślij ID kategorii | progi\nNp. 2 | 1-2 = 100; 3-4 = 90; 5+ = 80\n/anuluj — przerwij.')
    elif action == 'newproduct':
        context.user_data['admin_action']='product_new'
        await query.message.reply_text('➕ Nowy produkt: wyślij\nNazwa | ID kategorii | stan | opis\nNp. Nowy produkt | 2 | 10 | Opis produktu\nNastępnie poproszę o zdjęcie. ID znajdziesz w Kategorie. /anuluj — przerwij.')
    elif action == 'editproduct':
        context.user_data['admin_action']='product_edit'
        await query.message.reply_text('✏️ Wyślij: ID produktu | nowa nazwa | ID kategorii | opis\nNp. 11 | Nowa nazwa | 2 | Opis\nID produktu znajdziesz w Magazynie. /anuluj — przerwij.')
    elif action == 'photo':
        context.user_data['admin_action']='product_photo_id'
        await query.message.reply_text('🖼 Wyślij ID produktu, którego zdjęcie chcesz zmienić. /anuluj — przerwij.')
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


async def admin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or context.user_data.get('admin_action')!='product_photo_wait': return
    pid=context.user_data.get('photo_product_id')
    if not pid: return
    photo=update.effective_message.photo[-1]
    if photo.file_size and photo.file_size>8*1024*1024:
        await update.effective_message.reply_text('❌ Zdjęcie jest zbyt duże (maks. 8 MB).');return
    IMAGE_DIR.mkdir(parents=True,exist_ok=True)
    filename=f'product_{pid}_{secrets.token_hex(8)}.jpg'
    path=IMAGE_DIR/filename
    try:
        tgfile=await context.bot.get_file(photo.file_id)
        await tgfile.download_to_drive(custom_path=str(path))
        if path.stat().st_size>8*1024*1024: raise ValueError('Plik przekracza 8 MB.')
        with db() as c:
            old=c.execute('SELECT image_name FROM products WHERE id=?',(pid,)).fetchone()
            if not old: raise ValueError('Produkt nie istnieje.')
            c.execute('UPDATE products SET image_name=? WHERE id=?',(filename,pid))
        # Starego zdjęcia nie usuwamy automatycznie, aby nie uszkodzić otwartych sesji.
    except Exception:
        path.unlink(missing_ok=True)
        log.exception('Błąd zapisu zdjęcia produktu')
        await update.effective_message.reply_text('❌ Nie udało się zapisać zdjęcia. Sprawdź dysk Railway i spróbuj ponownie.');return
    context.user_data.pop('admin_action',None)
    context.user_data.pop('photo_product_id',None)
    await update.effective_message.reply_text(f'✅ Zdjęcie produktu #{pid} zapisane. Produkt jest dostępny w katalogu.',reply_markup=admin_keyboard())


async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.pop('admin_action', None)
    context.user_data.pop('ui_wizard', None)
    context.user_data.pop('photo_product_id', None)
    context.user_data.pop('delivery_propose', None)
    context.user_data.pop('delivery_draft', None)
    context.user_data.pop('price_edit', None)
    customer_id = context.user_data.pop('support_reply_to', None)
    if customer_id:
        with db() as c:
            c.execute('DELETE FROM support_claims WHERE customer_id=? AND admin_id=?',
                      (customer_id, update.effective_user.id))
    await update.effective_message.reply_text('Anulowano.', reply_markup=admin_keyboard())


async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('ui_wizard') and not context.user_data.get('admin_action'):
        await ui_text(update, context)
        return
    if not is_admin(update):
        return
    action = context.user_data.get('admin_action')
    if not action:
        return
    raw_text=update.effective_message.text.strip()
    if action == 'category_new':
        try:
            if not raw_text or len(raw_text)>80: raise ValueError('Nazwa musi mieć 1–80 znaków.')
            with db() as c:
                c.execute('INSERT INTO categories(name) VALUES (?)',(raw_text,))
            result=f'✅ Dodano kategorię {raw_text}. Ustaw jej progi przez /admin → Progi cenowe.'
        except (ValueError,sqlite3.IntegrityError) as exc:
            await update.effective_message.reply_text(f'❌ {exc}');return
        context.user_data.pop('admin_action',None)
        await update.effective_message.reply_text(result,reply_markup=admin_keyboard());return
    if action == 'product_photo_id':
        try:
            pid=int(raw_text)
            with db() as c:
                if not c.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone(): raise ValueError('Nie ma produktu o takim ID.')
        except ValueError as exc:
            await update.effective_message.reply_text(f'❌ {exc}');return
        context.user_data['photo_product_id']=pid
        context.user_data['admin_action']='product_photo_wait'
        await update.effective_message.reply_text('📷 Teraz wyślij zdjęcie produktu jako zdjęcie Telegram (nie plik). /anuluj — przerwij.');return
    parts = [x.strip() for x in raw_text.split('|')]
    if action in ('product_new','product_edit'):
        expected=4
    elif action=='category_tiers':
        expected=2
    else:
        expected=4 if action == 'manual' else 3 if action == 'newprom' else 2
    if len(parts) != expected:
        await update.effective_message.reply_text('Nieprawidłowy format. Użyj separatora | lub /anuluj.')
        return
    try:
        if action == 'category_tiers':
            category_id=int(parts[0]);set_tiers(category_id,parts[1]);result=f'✅ Zapisano progi dla kategorii #{category_id}.'
        elif action == 'product_new':
            name,category_id,stock,description=parts[0],int(parts[1]),int(parts[2]),parts[3]
            if not name or len(name)>100 or len(description)>500 or not 0<=stock<=100000: raise ValueError('Sprawdź nazwę, opis i stan.')
            with db() as c:
                if not c.execute('SELECT 1 FROM categories WHERE id=?',(category_id,)).fetchone(): raise ValueError('Nie ma takiej kategorii.')
                cur=c.execute('INSERT INTO products(name,stock,active,created_at,category_id,description) VALUES (?,?,1,?,?,?)',(name,stock,int(time.time()),category_id,description))
                pid=cur.lastrowid
            context.user_data['photo_product_id']=pid
            context.user_data['admin_action']='product_photo_wait'
            await update.effective_message.reply_text(f'✅ Produkt #{pid} zapisany. Teraz wyślij jego zdjęcie jako zdjęcie Telegram. /anuluj kończy bez zdjęcia.')
            return
        elif action == 'product_edit':
            pid,name,category_id,description=int(parts[0]),parts[1],int(parts[2]),parts[3]
            if not name or len(name)>100 or len(description)>500: raise ValueError('Nieprawidłowa nazwa lub opis.')
            with db() as c:
                if not c.execute('SELECT 1 FROM categories WHERE id=?',(category_id,)).fetchone(): raise ValueError('Nie ma takiej kategorii.')
                cur=c.execute('UPDATE products SET name=?,category_id=?,description=? WHERE id=?',(name,category_id,description,pid))
                if not cur.rowcount: raise ValueError('Nie ma takiego produktu.')
            result=f'✅ Zaktualizowano produkt #{pid}.'
        elif action == 'manual':
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
    delivery_date: str = ''
    delivery_slots: list[str] = Field(default_factory=list)
    recipient_first_name: str = ''
    recipient_last_name: str = ''
    recipient_email: str = ''
    recipient_phone: str = ''


SUPPORT_RETENTION_SECONDS = 30 * 86400
SUPPORT_CLAIM_SECONDS = 10 * 60


def support_cleanup():
    now = int(time.time())
    with db() as c:
        c.execute('DELETE FROM support_messages WHERE created_at < ?', (now - SUPPORT_RETENTION_SECONDS,))
        c.execute('DELETE FROM support_claims WHERE expires_at < ?', (now,))


def support_latest_order(customer_id):
    with db() as c:
        row = c.execute("SELECT id FROM orders WHERE chat_id=? AND status='confirmed' ORDER BY id DESC LIMIT 1", (customer_id,)).fetchone()
    return int(row['id']) if row else None


def support_save(customer_id, sender, body, admin_id=None):
    support_cleanup()
    order_id = support_latest_order(customer_id)
    with db() as c:
        c.execute('INSERT INTO support_messages(customer_id,sender,admin_id,body,order_id,created_at) VALUES(?,?,?,?,?,?)',
                  (customer_id, sender, admin_id, body, order_id, int(time.time())))
    return order_id


def support_history(customer_id):
    support_cleanup()
    with db() as c:
        rows = c.execute('SELECT sender,body,created_at FROM support_messages WHERE customer_id=? ORDER BY id DESC LIMIT 12', (customer_id,)).fetchall()
    if not rows:
        return 'Brak wiadomości z ostatnich 30 dni.'
    lines = []
    for row in reversed(rows):
        stamp = time.strftime('%d.%m %H:%M', time.localtime(row['created_at']))
        who = 'Klient' if row['sender'] == 'customer' else 'Obsługa'
        lines.append(f'{stamp} {who}: {row["body"][:300]}')
    return '\n'.join(lines)[-3500:]


def support_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('💬 Mam pytanie', callback_data='support:topic:question')],
        [InlineKeyboardButton('📦 Moje zamówienie', callback_data='support:topic:order')],
        [InlineKeyboardButton('❓ Inna sprawa', callback_data='support:topic:other')],
    ])


def order_status_keyboard(order_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('💬 Napisz do klienta', callback_data=f'delivery:message:{order_id}')],
        [InlineKeyboardButton('📜 Historia rozmowy', callback_data=f'delivery:history:{order_id}')],
        [InlineKeyboardButton('📦 W przygotowaniu', callback_data=f'delivery:status:{order_id}:preparing')],
        [InlineKeyboardButton('🚚 W drodze', callback_data=f'delivery:status:{order_id}:onway')],
        [InlineKeyboardButton('✅ Zrealizowane', callback_data=f'delivery:status:{order_id}:done')],
    ])


def delivery_admin_keyboard(order_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Potwierdź preferowany termin', callback_data=f'delivery:accept:{order_id}')],
        [InlineKeyboardButton('🕒 Zaproponuj inną godzinę', callback_data=f'delivery:propose:{order_id}')],
        [InlineKeyboardButton('💬 Napisz do klienta', callback_data=f'delivery:message:{order_id}')],
        [InlineKeyboardButton('📜 Historia rozmowy', callback_data=f'delivery:history:{order_id}')],
        [InlineKeyboardButton('📦 Zmień status', callback_data=f'delivery:menu:{order_id}')],
    ])


def get_delivery_order(order_id):
    with db() as c:
        row = c.execute('SELECT id,chat_id,payload,status FROM orders WHERE id=? AND source=?',(order_id,'web')).fetchone()
    if not row or row['status'] not in ('confirmed',):
        return None
    return dict(row)


async def recent_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text('⛔ Brak dostępu.')
        return
    with db() as c:
        rows = c.execute("SELECT id, payload, status FROM orders WHERE source='web' AND status='confirmed' ORDER BY id DESC LIMIT 25").fetchall()
    if not rows:
        await update.effective_message.reply_text('Brak potwierdzonych zamówień.')
        return
    buttons = []
    for row in rows:
        try:
            data = json.loads(row['payload'])['order']
            place = data.get('delivery_point') or data.get('locker_id') or 'Zamówienie'
        except (ValueError, KeyError, TypeError):
            place = 'Zamówienie'
        buttons.append([InlineKeyboardButton(f'📦 #{row["id"]} — {place[:35]}', callback_data=f'orders:open:{row["id"]}')])
    await update.effective_message.reply_text('🐻 Ostatnie 25 zamówień. Wybierz klienta, aby napisać także po ustaleniu dostawy:', reply_markup=InlineKeyboardMarkup(buttons))


async def orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer('Brak dostępu.', show_alert=True)
        return
    if q.data == 'orders:list':
        await q.answer()
        await recent_orders(update, context)
        return
    try:
        oid = int(q.data.split(':')[-1])
    except ValueError:
        await q.answer('Nieprawidłowy numer.', show_alert=True)
        return
    row = get_delivery_order(oid)
    if not row:
        await q.answer('Zamówienie niedostępne.', show_alert=True)
        return
    saved = json.loads(row['payload'])
    await q.answer()
    await q.message.reply_text(format_order(oid, saved['order'], saved.get('user', {})), reply_markup=delivery_admin_keyboard(oid))


async def delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    parts=(q.data or '').split(':')
    if len(parts)<3 or not parts[2].isdigit():
        await q.answer('Nieprawidłowe zamówienie.',show_alert=True);return
    action, oid=parts[1],int(parts[2])
    row=get_delivery_order(oid)
    if not row:
        await q.answer('Zamówienie nie jest dostępne.',show_alert=True);return
    cid=int(row['chat_id'])
    if action=='clientaccept':
        if q.from_user.id!=cid:
            await q.answer('To nie Twoje zamówienie.',show_alert=True);return
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            plan=c.execute('SELECT proposed_slot,state FROM delivery_plans WHERE order_id=?',(oid,)).fetchone()
            if not plan or plan['state']!='proposed':
                await q.answer('Propozycja jest już nieaktualna.',show_alert=True);return
            slot=plan['proposed_slot']
            c.execute('UPDATE delivery_plans SET agreed_slot=?,proposed_slot=NULL,state=?,updated_at=? WHERE order_id=?',(slot,'agreed',int(time.time()),oid))
        await q.answer('Termin zaakceptowany!')
        await q.edit_message_text(f'✅ Potwierdzono dostawę zamówienia #{oid}: {slot}.')
        for aid in ADMIN_CHAT_IDS:
            try:await context.bot.send_message(aid,f'✅ Klient zaakceptował termin dostawy #{oid}: {slot}.')
            except Exception:log.exception('Powiadomienie admina')
        return
    if action=='clientreject':
        if q.from_user.id!=cid:
            await q.answer('To nie Twoje zamówienie.',show_alert=True);return
        with db() as c:
            cur=c.execute('UPDATE delivery_plans SET state=?,proposed_slot=NULL,updated_at=? WHERE order_id=? AND state=?',('awaiting_admin',int(time.time()),oid,'proposed'))
        if not cur.rowcount:
            await q.answer('Propozycja jest już nieaktualna.',show_alert=True);return
        await q.answer()
        await q.edit_message_text(f'💬 Rozumiem. Obsługa zaproponuje inny termin dla zamówienia #{oid}. Możesz też napisać przez /kontakt.')
        for aid in ADMIN_CHAT_IDS:
            try:await context.bot.send_message(aid,f'🕒 Klient nie zaakceptował propozycji dla #{oid}.',reply_markup=delivery_admin_keyboard(oid))
            except Exception:log.exception('Powiadomienie admina')
        return
    if not is_admin(update):
        await q.answer('Brak dostępu.',show_alert=True);return
    if action=='history':
        await q.answer()
        await q.message.reply_text(f'📜 Historia rozmowy — zamówienie #{oid}\n\n{support_history(cid)}', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('💬 Napisz do klienta', callback_data=f'delivery:message:{oid}')]]))
        return
    if action=='menu':
        await q.answer();await q.message.reply_text(f'📦 Status zamówienia #{oid}',reply_markup=order_status_keyboard(oid));return
    if action=='message':
        context.user_data['support_reply_to']=cid
        now=int(time.time())
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            claim=c.execute('SELECT admin_id FROM support_claims WHERE customer_id=? AND expires_at>?',(cid,now)).fetchone()
            if claim and int(claim['admin_id'])!=q.from_user.id:
                await q.answer('Inny administrator odpowiada.',show_alert=True);return
            c.execute('INSERT OR REPLACE INTO support_claims(customer_id,admin_id,expires_at) VALUES (?,?,?)',(cid,q.from_user.id,now+SUPPORT_CLAIM_SECONDS))
        await q.answer();await q.message.reply_text(f'✍️ Napisz odpowiedź dla klienta zamówienia #{oid}. /anuluj — przerwij.');return
    if action=='propose':
        context.user_data.pop('delivery_propose', None)
        context.user_data['delivery_draft']={'order_id':oid}
        await q.answer()
        await q.message.reply_text('📅 Wybierz dzień dostawy:',reply_markup=delivery_day_keyboard(oid))
        return
    if action=='day' and len(parts)==4 and parts[3].isdigit():
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        offset=int(parts[3])
        if not 0<=offset<=30:
            await q.answer('Nieprawidłowy dzień.',show_alert=True);return
        day=(datetime.now(ZoneInfo('Europe/Warsaw')).date()+timedelta(days=offset)).isoformat()
        context.user_data['delivery_draft']={'order_id':oid,'date':day}
        await q.answer()
        await q.message.reply_text(f'🕒 {day} — wybierz godzinę albo wpisz własną:',reply_markup=delivery_slot_keyboard(oid))
        return
    if action=='slot' and len(parts)==4 and parts[3].isdigit():
        draft=context.user_data.get('delivery_draft',{})
        slots=DELIVERY_QUICK_SLOTS
        idx=int(parts[3])
        if draft.get('order_id')!=oid or not draft.get('date') or idx>=len(slots):
            await q.answer('Wybierz najpierw dzień.',show_alert=True);return
        draft['time']=slots[idx]
        await q.answer()
        await q.message.reply_text(f'📦 Zamówienie #{oid}\n📅 {draft["date"]}\n🕒 {draft["time"]}\n\nWysłać tę propozycję klientowi?',reply_markup=delivery_send_keyboard(oid))
        return
    if action=='custom':
        draft=context.user_data.get('delivery_draft',{})
        if draft.get('order_id')!=oid or not draft.get('date'):
            await q.answer('Najpierw wybierz dzień.',show_alert=True);return
        context.user_data['delivery_propose']=oid
        await q.answer()
        await q.message.reply_text('✏️ Wpisz tylko godzinę, np. 18:45, albo przedział, np. 18:45–19:15.\n\n/anuluj — przerwij.')
        return
    if action=='send':
        draft=context.user_data.get('delivery_draft',{})
        if draft.get('order_id')!=oid or not draft.get('date') or not draft.get('time'):
            await q.answer('Wybierz termin ponownie.',show_alert=True);return
        await q.answer()
        success=await delivery_send_proposal(context,oid,f'{draft["date"]} {draft["time"]}')
        if success:
            context.user_data.pop('delivery_draft',None)
            context.user_data.pop('delivery_propose',None)
            await q.message.reply_text(f'✅ Propozycja wysłana klientowi.\n📅 {draft["date"]}\n🕒 {draft["time"]}',reply_markup=delivery_admin_keyboard(oid))
        else:
            await q.message.reply_text('❌ Nie udało się wysłać. Spróbuj ponownie.')
        return
    if action=='back':
        await q.answer()
        await q.message.reply_text('🕒 Wybierz godzinę:',reply_markup=delivery_slot_keyboard(oid))
        return
    if action=='accept':
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            plan=c.execute('SELECT preferred_date,preferred_slots,state FROM delivery_plans WHERE order_id=?',(oid,)).fetchone()
            if not plan:
                await q.answer('Brak danych terminu.',show_alert=True);return
            if plan['state']=='agreed':
                await q.answer('Termin już potwierdzony.',show_alert=True);return
            slots=json.loads(plan['preferred_slots'] or '[]')
            if len(slots)!=1:
                await q.answer()
                await q.message.reply_text('Wybierz jeden z przedziałów klienta:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(slot,callback_data=f'delivery:choose:{oid}:{i}')] for i,slot in enumerate(slots)]));return
            agreed=f'{plan["preferred_date"]} {slots[0]}'
            c.execute('UPDATE delivery_plans SET agreed_slot=?,state=?,proposed_slot=NULL,updated_at=? WHERE order_id=?',(agreed,'agreed',int(time.time()),oid))
        await q.answer('Termin potwierdzony.')
        await context.bot.send_message(cid,f'🐻 Dostawa zamówienia #{oid} potwierdzona!\n📅 {agreed}\n📍 {json.loads(row["payload"])["order"].get("delivery_point","")}')
        await q.message.reply_text(f'✅ Potwierdzono termin: {agreed}');return
    if action=='choose' and len(parts)==4 and parts[3].isdigit():
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            plan=c.execute('SELECT preferred_date,preferred_slots,state FROM delivery_plans WHERE order_id=?',(oid,)).fetchone()
            if not plan or plan['state']=='agreed':
                await q.answer('Termin już ustalony.',show_alert=True);return
            slots=json.loads(plan['preferred_slots'] or '[]');i=int(parts[3])
            if i>=len(slots):
                await q.answer('Nieprawidłowa godzina.',show_alert=True);return
            agreed=f'{plan["preferred_date"]} {slots[i]}'
            c.execute('UPDATE delivery_plans SET agreed_slot=?,state=?,proposed_slot=NULL,updated_at=? WHERE order_id=?',(agreed,'agreed',int(time.time()),oid))
        await q.answer('Potwierdzono!')
        await context.bot.send_message(cid,f'🐻 Potwierdziliśmy termin dostawy #{oid}: {agreed}. 💙')
        await q.message.reply_text(f'✅ Termin dostawy #{oid}: {agreed}');return
    if action=='status' and len(parts)==4:
        labels={'preparing':'📦 W przygotowaniu','onway':'🚚 W drodze','done':'✅ Zrealizowane'}
        status=parts[3]
        if status not in labels:
            await q.answer();return
        # Status realizacji jest oddzielny od statusu księgowego confirmed.
        with db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS order_progress(order_id INTEGER PRIMARY KEY, progress TEXT NOT NULL, updated_at INTEGER NOT NULL)')
            c.execute('INSERT OR REPLACE INTO order_progress VALUES (?,?,?)',(oid,status,int(time.time())))
        await q.answer('Zapisano status.')
        await context.bot.send_message(cid,f'🐻 Aktualizacja zamówienia #{oid}\n{labels[status]}')
        await q.message.reply_text(f'{labels[status]} — zamówienie #{oid}');return
    await q.answer()


DELIVERY_QUICK_SLOTS=['10:00–12:00','12:00–14:00','14:00–16:00','16:00–18:00','17:00–19:00','18:00–20:00']


def delivery_day_keyboard(oid):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    today=datetime.now(ZoneInfo('Europe/Warsaw')).date()
    buttons=[]
    for offset in range(7):
        day=today+timedelta(days=offset)
        label=('Dzisiaj' if offset==0 else 'Jutro' if offset==1 else day.strftime('%d.%m.%Y'))
        buttons.append([InlineKeyboardButton(f'📅 {label} ({day.strftime("%d.%m")})',callback_data=f'delivery:day:{oid}:{offset}')])
    return InlineKeyboardMarkup(buttons)


def delivery_slot_keyboard(oid):
    buttons=[[InlineKeyboardButton(slot,callback_data=f'delivery:slot:{oid}:{idx}')] for idx,slot in enumerate(DELIVERY_QUICK_SLOTS)]
    buttons.append([InlineKeyboardButton('✏️ Wpisz własną godzinę',callback_data=f'delivery:custom:{oid}')])
    buttons.append([InlineKeyboardButton('⬅️ Zmień dzień',callback_data=f'delivery:propose:{oid}')])
    return InlineKeyboardMarkup(buttons)


def delivery_send_keyboard(oid):
    return InlineKeyboardMarkup([[InlineKeyboardButton('✅ Wyślij propozycję klientowi',callback_data=f'delivery:send:{oid}')],
                                 [InlineKeyboardButton('⬅️ Zmień godzinę',callback_data=f'delivery:back:{oid}')]])


async def delivery_send_proposal(context,oid,slot):
    row=get_delivery_order(oid)
    if not row:return False
    kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Pasuje mi',callback_data=f'delivery:clientaccept:{oid}')],
                             [InlineKeyboardButton('🔄 Poproszę o inny termin',callback_data=f'delivery:clientreject:{oid}')]])
    try:
        await context.bot.send_message(int(row['chat_id']),
            f'🐻 PuffBear | Ustalenie dostawy\n\nZamówienie #{oid}\n📅 Proponujemy: {slot}\n\nCzy ten termin Ci odpowiada?',reply_markup=kb)
    except Exception:
        log.exception('Nie udało się wysłać propozycji terminu')
        return False
    with db() as c:
        c.execute('UPDATE delivery_plans SET proposed_slot=?,state=?,updated_at=? WHERE order_id=?',
                  (slot,'proposed',int(time.time()),oid))
    return True


async def delivery_propose_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    oid=context.user_data.get('delivery_propose')
    if not oid or not is_admin(update):return
    draft=context.user_data.get('delivery_draft',{})
    if draft.get('order_id')!=oid or not draft.get('date'):
        context.user_data.pop('delivery_propose',None)
        await update.effective_message.reply_text('Wybierz dzień ponownie.',reply_markup=delivery_day_keyboard(oid));return
    raw=(update.effective_message.text or '').strip().replace(' ','').replace('-', '–')
    match=re.fullmatch(r'([01]\d|2[0-3]):([0-5]\d)(?:–([01]\d|2[0-3]):([0-5]\d))?',raw)
    if not match:
        await update.effective_message.reply_text('❌ Wpisz np. 18:45 lub 18:45–19:15. /anuluj — przerwij.');return
    from datetime import datetime
    from zoneinfo import ZoneInfo
    start_minutes=int(match.group(1))*60+int(match.group(2))
    if match.group(3) is not None:
        end_minutes=int(match.group(3))*60+int(match.group(4))
        if end_minutes<=start_minutes:
            await update.effective_message.reply_text('❌ Koniec przedziału musi być później niż początek.');return
    local_now=datetime.now(ZoneInfo('Europe/Warsaw'))
    if draft['date']==local_now.date().isoformat() and start_minutes<=local_now.hour*60+local_now.minute:
        await update.effective_message.reply_text('❌ Ta godzina już minęła. Wpisz późniejszą.');return
    draft['time']=raw
    context.user_data.pop('delivery_propose',None)
    await update.effective_message.reply_text(
        f'📦 Zamówienie #{oid}\n📅 {draft["date"]}\n🕒 {raw}\n\nWysłać tę propozycję klientowi?',
        reply_markup=delivery_send_keyboard(oid))


async def support_topic(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if is_admin(update):await q.answer();return
    topic=q.data.split(':')[-1]
    names={'question':'💬 Pytanie','order':'📦 Zamówienie','other':'❓ Inna sprawa'}
    context.user_data['support_active']=True
    context.user_data['support_topic']=names.get(topic,'💬 Pytanie')
    await q.answer()
    if topic=='order':
        oid=support_latest_order(q.from_user.id)
        intro=f'📦 Ostatnie zamówienie: #{oid}\n' if oid else '📦 Jeśli masz numer zamówienia, podaj go.\n'
    else:intro=''
    await q.message.reply_text('🐻 '+context.user_data['support_topic']+'\n\n'+intro+'Napisz swoją wiadomość poniżej. Odpowiedź otrzymasz tutaj, w tym czacie. 💙')


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return
    if is_admin(update):
        await update.effective_message.reply_text('Panel administratora: /admin. Aby przerwać odpowiedź: /anuluj.')
        return
    context.user_data['support_active'] = False
    await update.effective_message.reply_text(
        '💬 Kontakt z obsługą PuffBear\n\nNapisz wiadomość w tym czacie. '
        'Obsługa odpowie tutaj przez bota. Wiadomości przechowujemy przez 30 dni. '
        'Wybierz temat poniżej, aby rozpocząć rozmowę.', reply_markup=support_menu())


async def support_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return
    context.user_data.pop('support_active', None)
    await update.effective_message.reply_text('✅ Zakończono tryb kontaktu. Możesz wrócić przez /kontakt.')


async def support_customer_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or is_admin(update):
        return
    if not context.user_data.get('support_active'):
        await update.effective_message.reply_text('Aby napisać do obsługi PuffBear, wybierz /kontakt.')
        return
    body = (update.effective_message.text or '').strip()
    if not body or len(body) > 3500:
        await update.effective_message.reply_text('Wiadomość musi mieć od 1 do 3500 znaków.')
        return
    customer_id = update.effective_user.id
    order_id = support_save(customer_id, 'customer', body)
    order_text = f'📦 Ostatnie zamówienie: #{order_id}' if order_id else '🛍️ Kontakt przed zakupem'
    alert = f'💬 PUFFBEAR | NOWA WIADOMOŚĆ\nTemat: {context.user_data.get("support_topic", "Pytanie")}\nKlient: {update.effective_user.full_name}\nKlient ID: {customer_id}\n{order_text}\n\n{body}'
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton('✍️ Odpowiedz', callback_data=f'support:reply:{customer_id}'),
        InlineKeyboardButton('📜 Historia', callback_data=f'support:history:{customer_id}')
    ]])
    sent = 0
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=alert, reply_markup=kb)
            sent += 1
        except Exception:
            log.exception('Błąd powiadomienia admina %s', admin_id)
    await update.effective_message.reply_text(
        '✅ Wiadomość dotarła do obsługi. Odpowiemy tutaj, gdy tylko będzie to możliwe. 💙' if sent else
        '⚠️ Wiadomość zapisana, ale nie udało się powiadomić obsługi. Spróbuj później.')


async def support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await query.answer('Brak dostępu.', show_alert=True)
        return
    parts = (query.data or '').split(':')
    if len(parts) != 3 or not parts[2].isdigit():
        await query.answer('Nieprawidłowy identyfikator.', show_alert=True)
        return
    action, customer_id = parts[1], int(parts[2])
    if action == 'history':
        await query.answer()
        await query.message.reply_text(f'📜 Historia klienta {customer_id}:\n\n{support_history(customer_id)}')
        return
    if action != 'reply':
        await query.answer()
        return
    now = int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM support_claims WHERE expires_at < ?', (now,))
        claim = c.execute('SELECT admin_id FROM support_claims WHERE customer_id=?', (customer_id,)).fetchone()
        if claim and int(claim['admin_id']) != query.from_user.id:
            allowed = False
        else:
            c.execute('INSERT OR REPLACE INTO support_claims(customer_id,admin_id,expires_at) VALUES(?,?,?)',
                      (customer_id, query.from_user.id, now + SUPPORT_CLAIM_SECONDS))
            allowed = True
    if not allowed:
        await query.answer('Inny administrator odpowiada. Spróbuj za chwilę.', show_alert=True)
        return
    context.user_data['support_reply_to'] = customer_id
    context.user_data.pop('ui_wizard', None)
    context.user_data.pop('admin_action', None)
    await query.answer()
    await query.message.reply_text(
        f'✍️ Odpowiedź dla klienta {customer_id}\n\n'
        'Wyślij TERAZ jedną wiadomość tekstową. /anuluj — rezygnacja.\n'
        'Rezerwacja rozmowy trwa 10 minut.')


async def support_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    customer_id = context.user_data.get('support_reply_to')
    if not customer_id:
        return
    body = (update.effective_message.text or '').strip()
    if not body or len(body) > 3500:
        await update.effective_message.reply_text('Odpowiedź musi mieć od 1 do 3500 znaków.')
        return
    now = int(time.time())
    with db() as c:
        claim = c.execute('SELECT admin_id,expires_at FROM support_claims WHERE customer_id=?', (customer_id,)).fetchone()
    if not claim or int(claim['admin_id']) != update.effective_user.id or int(claim['expires_at']) < now:
        context.user_data.pop('support_reply_to', None)
        await update.effective_message.reply_text('Rezerwacja wygasła. Kliknij ponownie „Odpowiedz”.')
        return
    try:
        await context.bot.send_message(chat_id=customer_id, text='🐻 PuffBear — obsługa:\n\n' + body)
    except Exception:
        log.exception('Nie udało się wysłać odpowiedzi klientowi %s', customer_id)
        await update.effective_message.reply_text('❌ Nie udało się wysłać. Spróbuj ponownie lub /anuluj.')
        return
    support_save(customer_id, 'admin', body, update.effective_user.id)
    with db() as c:
        c.execute('DELETE FROM support_claims WHERE customer_id=? AND admin_id=?', (customer_id, update.effective_user.id))
    context.user_data.pop('support_reply_to', None)
    await update.effective_message.reply_text('✅ Odpowiedź wysłana jako PuffBear. Twoje konto nie zostało ujawnione.')
    for admin_id in ADMIN_CHAT_IDS:
        if admin_id == update.effective_user.id:
            continue
        try:
            await context.bot.send_message(chat_id=admin_id, text=f'✅ Obsłużono wiadomość klienta {customer_id}.')
        except Exception:
            log.exception('Nie udało się wysłać informacji drugiemu administratorowi')


async def support_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        if context.user_data.get('price_edit'):
            await price_text(update, context)
        elif context.user_data.get('delivery_propose'):
            await delivery_propose_text(update,context)
        elif context.user_data.get('support_reply_to'):
            await support_admin_text(update, context)
        else:
            await admin_input(update, context)
    else:
        await support_customer_text(update, context)


async def support_cleanup_loop():
    while True:
        try:
            support_cleanup()
        except Exception:
            log.exception('Błąd czyszczenia historii czatu')
        await asyncio.sleep(3600)


async def support_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if is_admin(update):
        await query.message.reply_text('Administrator: użyj /admin.')
        return
    context.user_data['support_active'] = False
    await query.message.reply_text('🐻 PuffBear | Centrum pomocy\n\nWybierz temat rozmowy. Następnie napisz wiadomość, a obsługa odpowie tutaj. 💙',reply_markup=support_menu())


telegram_app = Application.builder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler('start', start))
telegram_app.add_handler(CommandHandler('kontakt', support_command))
telegram_app.add_handler(CommandHandler('koniec', support_end))
telegram_app.add_handler(CallbackQueryHandler(support_open, pattern=r'^support:open$'))
telegram_app.add_handler(CallbackQueryHandler(support_topic, pattern=r'^support:topic:(?:question|order|other)$'))
telegram_app.add_handler(CallbackQueryHandler(delivery_callback, pattern=r'^delivery:'))
telegram_app.add_handler(CallbackQueryHandler(support_callback, pattern=r'^support:(?:reply|history):[0-9]+$'))
telegram_app.add_handler(CommandHandler('admin', admin))
telegram_app.add_handler(CommandHandler('zamowienia', recent_orders))
telegram_app.add_handler(CallbackQueryHandler(orders_callback, pattern=r'^orders:(?:list|open:[0-9]+)$'))
telegram_app.add_handler(CommandHandler('anuluj', cancel_admin))
telegram_app.add_handler(CallbackQueryHandler(order_callback, pattern=r'^(confirm|cancel):'))
telegram_app.add_handler(CallbackQueryHandler(void_order_callback, pattern=r'^adm:void(?:ask|do):[0-9]+$'))
telegram_app.add_handler(CallbackQueryHandler(period_callback, pattern=r'^adm:period(?:ask|no|do:[0-9]+)$'))
telegram_app.add_handler(CallbackQueryHandler(price_callback, pattern=r'^price:'))
telegram_app.add_handler(CallbackQueryHandler(ui_callback, pattern=r'^ui:'))
telegram_app.add_handler(CallbackQueryHandler(admin_callback, pattern=r'^adm:'))
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, order_from_web))
telegram_app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, admin_photo))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, support_router))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    support_cleanup()
    cleanup_task = asyncio.create_task(support_cleanup_loop())
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    await telegram_app.initialize()
    # Zastąp przestarzałe menu komend prostą listą. Nie usuwa funkcji /admin.
    commands = [
        BotCommand('start', 'Uruchom sklep PuffBear'),
        BotCommand('kontakt', 'Napisz do obsługi PuffBear'),
        BotCommand('koniec', 'Zakończ rozmowę z obsługą'),
        BotCommand('admin', 'Panel administratora'),
        BotCommand('zamowienia', 'Lista zamówień i kontakt z klientami'),
        BotCommand('anuluj', 'Anuluj bieżącą operację'),
    ]
    try:
        for scope in (BotCommandScopeDefault(), BotCommandScopeAllPrivateChats(),
                      BotCommandScopeAllGroupChats(), BotCommandScopeAllChatAdministrators()):
            await telegram_app.bot.set_my_commands(commands, scope=scope)
        log.info('Odświeżono menu komend Telegram.')
    except Exception:
        log.exception('Nie udało się odświeżyć menu komend Telegram.')
    await telegram_app.start()
    if telegram_app.updater:
        await telegram_app.updater.start_polling(drop_pending_updates=False)
    log.info('PuffBear uruchomiony, baza: %s', DB_PATH)
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
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
                          'sold_out': p['stock'] == 0, 'category_id': p['category_id'],
                          'description': p['description'],
                          'image_url': f'{PUBLIC_API_BASE}/api/product-images/{p["image_name"]}' if p['image_name'] else ''}
                         for p in products()], 'categories': category_catalog()}


@app.get('/api/product-images/{filename}')
async def product_image(filename: str):
    if not re.fullmatch(r'product_[0-9]+_[0-9a-f]{16}\.jpg',filename):
        raise HTTPException(status_code=404,detail='Nie znaleziono zdjęcia.')
    path=IMAGE_DIR/filename
    if not path.is_file(): raise HTTPException(status_code=404,detail='Nie znaleziono zdjęcia.')
    return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'public, max-age=3600'})


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
