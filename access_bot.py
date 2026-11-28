"""SWOP NFT Tracker — demo/testnet TON checkout, no real access grants."""
import asyncio
import base64
import binascii
import html
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
from uuid import uuid4

ADMIN_ID = 8927864266
CHANNEL = '@swopkod'
SUPPORT = 'https://t.me/swophelp_bot'
DB_PATH = os.getenv('ACCESS_DB_PATH', 'swop_access_test.db')
WALLET = os.getenv('TON_TESTNET_WALLET', '').strip()
API_KEY = os.getenv('TONCENTER_TESTNET_API_KEY', '').strip()
API_URL = 'https://testnet.toncenter.com/api/v3/transactions'
TARIFFS = {
    'day': ('24 часа', 3, 86400),
    'week': ('7 дней', 14, 604800),
    'forever': ('Навсегда + офферы', 20, None),
}


def raw_address(address, test_only=False):
    if ':' in address and not test_only:
        wc, account = address.split(':', 1)
        if len(account) != 64 or len(bytes.fromhex(account)) != 32:
            raise ValueError('Invalid address')
        return f'{int(wc)}:{account.lower()}'
    data = base64.b64decode(address + '=' * (-len(address) % 4),
                            altchars=b'-_', validate=True)
    if len(data) != 36 or binascii.crc_hqx(data[:34], 0).to_bytes(2, 'big') != data[34:]:
        raise ValueError('Invalid address checksum')
    if data[0] & 0x7f not in (0x11, 0x51):
        raise ValueError('Invalid address tag')
    if test_only and not data[0] & 0x80:
        raise ValueError('Only testnet-only wallet addresses are accepted')
    return f'{int.from_bytes(data[1:2], "big", signed=True)}:{data[2:34].hex()}'


def payment_link(wallet, amount_nano, memo):
    raw_address(wallet, test_only=True)
    if not isinstance(amount_nano, int) or amount_nano <= 0:
        raise ValueError('Invalid amount')
    return 'https://app.tonkeeper.com/transfer/' + quote(wallet, safe='') + '?' + urlencode(
        {'amount': amount_nano, 'text': memo}, quote_via=quote)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            tariff TEXT NOT NULL, wallet TEXT NOT NULL,
            amount INTEGER NOT NULL, memo TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', tx_hash TEXT UNIQUE,
            created INTEGER NOT NULL, paid_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY, expires INTEGER, lifetime INTEGER NOT NULL
        );
        ''')


def create_order(uid, tariff):
    _, price, _ = TARIFFS[tariff]
    oid = uuid4().hex
    with db() as c:
        c.execute('''INSERT INTO orders(id,user_id,tariff,wallet,amount,memo,created)
                     VALUES (?,?,?,?,?,?,?)''',
                  (oid, uid, tariff, WALLET, price * 10**9, 'SWOP-' + oid.upper(), int(time.time())))
    return get_order(oid)


def get_order(oid):
    with db() as c:
        row = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        return dict(row) if row else None


def get_subscription(uid):
    with db() as c:
        row = c.execute('SELECT * FROM subscriptions WHERE user_id=?', (uid,)).fetchone()
        return dict(row) if row else None


def activate(oid, uid, tx_hash):
    if not tx_hash:
        return False
    now = int(time.time())
    try:
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            order = c.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (oid, uid)).fetchone()
            if not order or order['status'] != 'pending':
                return False
            c.execute('UPDATE orders SET status=?,tx_hash=?,paid_at=? WHERE id=?', ('paid', tx_hash, now, oid))
            old = c.execute('SELECT * FROM subscriptions WHERE user_id=?', (uid,)).fetchone()
            duration = TARIFFS[order['tariff']][2]
            lifetime = duration is None or bool(old and old['lifetime'])
            expires = None if lifetime else max(now, (old['expires'] or 0) if old else 0) + duration
            c.execute('''INSERT INTO subscriptions VALUES(?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET expires=excluded.expires,lifetime=excluded.lifetime''',
                      (uid, expires, int(lifetime)))
        return True
    except sqlite3.IntegrityError:
        return False


def match_transaction(tx, order):
    try:
        msg = tx.get('in_msg') or {}
        desc = tx.get('description') or {}
        decoded = (msg.get('message_content') or {}).get('decoded') or {}
        if tx.get('emulated') or not tx.get('mc_block_seqno') or desc.get('aborted') is not False:
            return False
        if msg.get('bounced') is not False or not msg.get('source'):
            return False
        if any((desc.get(k) or {}).get('success') is False for k in ('compute_ph', 'action')):
            return False
        if not isinstance(decoded, dict):
            return False
        return (raw_address(msg['destination']) == raw_address(order['wallet'], test_only=True)
                and int(msg['value']) == order['amount']
                and int(tx['now']) >= order['created']
                and (decoded.get('type') or decoded.get('@type')) == 'text_comment'
                and decoded.get('comment', decoded.get('text')) == order['memo']
                and bool(tx.get('hash')))
    except (KeyError, ValueError, TypeError, binascii.Error):
        return False


async def find_payment(order):
    import aiohttp
    raw = raw_address(order['wallet'], test_only=True)
    headers = {'X-API-Key': API_KEY} if API_KEY else {}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as s:
        end = int(time.time())
        for offset in range(0, 500, 100):
            params = {'account': raw, 'start_utime': order['created'], 'end_utime': end,
                      'sort': 'desc', 'limit': 100, 'offset': offset}
            async with s.get(API_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
            for tx in data['transactions']:
                if match_transaction(tx, order):
                    return tx['hash']
            if len(data['transactions']) < 100:
                return None
    raise RuntimeError('Search incomplete')


async def main():
    import aiohttp
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import CommandStart, Command
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from aiogram.exceptions import TelegramAPIError

    token = os.environ['ACCESS_BOT_TOKEN']
    if WALLET:
        raw_address(WALLET, test_only=True)
    init_db()
    dp = Dispatcher()
    lock = asyncio.Lock()
    dp.message.filter(F.chat.type == 'private')
    dp.callback_query.filter(F.message.chat.type == 'private')

    def btn(text, callback=None, url=None):
        return InlineKeyboardButton(text=text, callback_data=callback, url=url)

    def kb(*rows):
        return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])

    def menu():
        return kb([btn('💎 Купить подписку', 'buy')], [btn('👤 Ваш профиль', 'profile')],
                  [btn('📢 Наш канал', url='https://t.me/swopkod'), btn('💬 Поддержка', url=SUPPORT)],
                  [btn('ℹ️ Информация', 'info')])

    async def subscribed(bot, uid):
        try:
            member = await bot.get_chat_member(CHANNEL, uid)
            return member.status in ('member', 'administrator', 'creator') or bool(getattr(member, 'is_member', False))
        except TelegramAPIError:
            return False

    async def show_menu(message, bot, uid):
        if not await subscribed(bot, uid):
            await message.answer('Подпишись на @swopkod. Бот должен быть администратором канала для проверки.',
                                 reply_markup=kb([btn('Подписаться', url='https://t.me/swopkod')], [btn('Проверить подписку', 'main')]))
            return
        await message.answer('💎 SWOP NFT Tracker\n\nТестовый режим. Реальные деньги не принимаются.\nВыберите действие:', reply_markup=menu())

    @dp.message(CommandStart())
    async def start(message, bot):
        await show_menu(message, bot, message.from_user.id)

    @dp.callback_query(F.data == 'main')
    async def back(call, bot):
        await call.answer()
        await show_menu(call.message, bot, call.from_user.id)

    @dp.callback_query(F.data == 'info')
    async def info(call):
        await call.answer()
        await call.message.answer('SWOP NFT Tracker — тестовый интерфейс подписок для публичного мониторинга NFT. Офферы и доступ в реальную группу не подключены.', reply_markup=kb([btn('Назад', 'main')]))

    @dp.callback_query(F.data == 'profile')
    async def profile(call):
        row = get_subscription(call.from_user.id)
        status = '❌ Не активна'
        if row and row['lifetime']:
            status = '✅ Навсегда + офферы (тест)'
        elif row and row['expires'] and row['expires'] > time.time():
            status = '✅ До ' + datetime.fromtimestamp(row['expires'], timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
        await call.answer()
        await call.message.answer(f'👤 Ваш профиль\n\n🏷 ID: {call.from_user.id}\n💎 Подписка: {status}\n\nТолько тестовая база.', reply_markup=kb([btn('Назад', 'main')]))

    @dp.callback_query(F.data == 'buy')
    async def buy(call, bot):
        if not await subscribed(bot, call.from_user.id):
            await call.answer('Сначала подпишись на @swopkod', show_alert=True)
            return
        await call.answer()
        rows = [[btn(f'{label} — {price} TON', 'buy:' + key)] for key, (label, price, _) in TARIFFS.items()]
        await call.message.answer('💎 Тестовые тарифы', reply_markup=kb(*rows, [btn('Назад', 'main')]))

    @dp.callback_query(F.data.startswith('buy:'))
    async def invoice(call, bot):
        tariff = call.data.split(':', 1)[1]
        if tariff not in TARIFFS or not await subscribed(bot, call.from_user.id):
            await call.answer('Тариф или подписка на канал недоступны', show_alert=True)
            return
        order = create_order(call.from_user.id, tariff)
        label, price, _ = TARIFFS[tariff]
        rows = []
        if order['wallet']:
            rows.append([btn('Оплатить в Tonkeeper (testnet)', url=payment_link(order['wallet'], order['amount'], order['memo']))])
        rows.append([btn('Проверить оплату', 'check:' + order['id'])])
        if not order['wallet'] and call.from_user.id == ADMIN_ID:
            rows.append([btn('Симулировать оплату (админ)', 'demo:' + order['id'])])
        rows.append([btn('Назад', 'main')])
        await call.answer()
        await call.message.answer(f'💎 ТЕСТОВЫЙ СЧЁТ\n\n{label}\nК оплате: {price} тестовых TON\n\nАдрес: {order["wallet"] or "Демо — переводы не принимаются"}\n\nMEMO: {order["memo"]}\n\nНе отправляйте реальные средства.', reply_markup=kb(*rows))

    @dp.callback_query(F.data.startswith('check:'))
    async def check(call):
        order = get_order(call.data.split(':', 1)[1])
        if not order or order['user_id'] != call.from_user.id:
            await call.answer('Заказ не найден', show_alert=True)
            return
        if order['status'] == 'paid':
            await call.answer('Тестовая оплата уже подтверждена', show_alert=True)
            return
        if not order['wallet']:
            await call.answer('Демо: используйте симуляцию администратора', show_alert=True)
            return
        if lock.locked():
            await call.answer('Проверка уже идёт')
            return
        await call.answer('Проверяем testnet…')
        async with lock:
            try:
                tx = await find_payment(order)
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError, KeyError):
                await call.message.answer('Проверка временно недоступна. Попробуйте позже.')
                return
        if not tx:
            await call.message.answer('Тестовая оплата ещё не поступила.')
        elif activate(order['id'], order['user_id'], tx):
            await call.message.answer('✅ Тестовая оплата подтверждена. Подписка обновлена в демо-базе.', reply_markup=menu())
        else:
            await call.message.answer('Проверьте статус заказа в профиле.')

    @dp.callback_query(F.data.startswith('demo:'))
    async def demo(call):
        order = get_order(call.data.split(':', 1)[1])
        if call.from_user.id != ADMIN_ID or not order or order['user_id'] != ADMIN_ID or order['wallet']:
            await call.answer('Недоступно', show_alert=True)
            return
        ok = activate(order['id'], ADMIN_ID, 'demo:' + order['id'])
        await call.answer('Тестовая подписка активирована' if ok else 'Уже обработано', show_alert=True)

    @dp.message(Command('admin'))
    async def admin(message):
        if message.from_user.id != ADMIN_ID:
            return
        with db() as c:
            count = c.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0]
        await message.answer(f'SWOP ADMIN\nТестовых оплат: {count}\nРежим: {"testnet" if WALLET else "demo"}')

    async with Bot(token=token) as bot:
        print('SWOP ACCESS ONLINE — TEST MODE', flush=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
