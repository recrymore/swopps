"""SWOP NFT Tracker: standalone Aiogram 3 access bot for public market analytics.
Install: pip install 'aiogram>=3.15,<4' aiohttp
"""
import asyncio
import logging
import os
import sqlite3
import time
import secrets
import aiohttp
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# --- ВАШИ ДАННЫЕ ---
ADMIN_ID = 8927864266
CHANNEL = '@swopkod'
CHANNEL_URL = 'https://t.me/swopkod'
SUPPORT = 'https://t.me/swophelp_bot'
TON_WALLET = 'UQDCYU_uQ8vA_hGTp2UuQ7KRCfvcbqTkoN08XHjOGcTocG0y'
# -------------------

# Обновили название тарифа "Навсегда"
PLANS = {'day': ('24 часа', 86400), 'week': ('7 дней', 604800), 'forever': ('Навсегда + офферы', None)}
log = logging.getLogger('swop-access')

class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
            CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, ref INTEGER, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS entitlements(id TEXT PRIMARY KEY, uid INTEGER NOT NULL, plan TEXT NOT NULL, created INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, uid INTEGER NOT NULL, plan TEXT NOT NULL, amount REAL NOT NULL, created INTEGER NOT NULL, paid INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS invites(url TEXT PRIMARY KEY, uid INTEGER NOT NULL, expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS managed(uid INTEGER PRIMARY KEY, removed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, plan TEXT NOT NULL, remaining INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS promo_uses(code TEXT NOT NULL, uid INTEGER NOT NULL, PRIMARY KEY(code,uid));
            CREATE INDEX IF NOT EXISTS ent_uid ON entitlements(uid,created);
            ''')

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c: yield c
        finally: c.close()

    def register(self, uid, ref=None):
        with self.db() as c:
            if ref == uid or not ref or not c.execute('SELECT 1 FROM users WHERE uid=?',(ref,)).fetchone(): ref = None
            c.execute('INSERT OR IGNORE INTO users VALUES(?,?,?)',(uid,ref,int(time.time())))

    def _profile(self, c, uid):
        expires = 0
        lifetime = False
        for r in c.execute('SELECT plan,created FROM entitlements WHERE uid=? AND revoked=0 ORDER BY created,id',(uid,)):
            duration = PLANS[r['plan']][1]
            if duration is None: lifetime = True
            else: expires = max(expires,r['created']) + duration
        refs = c.execute('SELECT COUNT(*) FROM users WHERE ref=?',(uid,)).fetchone()[0]
        return dict(active=lifetime or expires>time.time(), lifetime=lifetime, expires=expires, refs=refs)

    def profile(self, uid):
        with self.db() as c: return self._profile(c,uid)

    def _grant(self,c,uid,plan,eid,now):
        c.execute('INSERT INTO entitlements(id,uid,plan,created) VALUES(?,?,?,?)',(eid,uid,plan,now))
        c.execute('INSERT INTO managed(uid,removed) VALUES(?,0) ON CONFLICT(uid) DO UPDATE SET removed=0',(uid,))

    def grant(self,uid,plan):
        if uid<=0 or plan not in PLANS: raise ValueError('invalid user/plan')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            self._grant(c,uid,plan,'admin:'+secrets.token_hex(16),int(time.time()))

    def revoke(self,uid):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE entitlements SET revoked=1 WHERE uid=?',(uid,))
            c.execute('INSERT INTO managed VALUES(?,0) ON CONFLICT(uid) DO UPDATE SET removed=0',(uid,))

    def order(self,uid,plan,amount):
        # ID заказа для MEMO (формат: SWOP-XXXXXX)
        oid = 'SWOP-' + secrets.token_hex(4).upper()
        with self.db() as c: c.execute('INSERT INTO orders(id,uid,plan,amount,created) VALUES(?,?,?,?,?)',(oid,uid,plan,amount,int(time.time())))
        return oid

    def get_order(self, oid):
        with self.db() as c:
            r = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
            return dict(r) if r else None

    def mark_order_paid(self, oid, uid, plan):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            r = c.execute('SELECT paid FROM orders WHERE id=?', (oid,)).fetchone()
            if r and not r['paid']:
                c.execute('UPDATE orders SET paid=1 WHERE id=?', (oid,))
                self._grant(c, uid, plan, 'pay:'+oid, int(time.time()))
                return True
        return False

    def get_pending_orders(self):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM orders WHERE paid=0 AND created > ?", (int(time.time()) - 7200,))]

    def make_promo(self,code,plan,uses):
        if not code.isascii() or not code.isalnum() or not 3<=len(code)<=32 or plan not in PLANS or not 1<=uses<=10000: raise ValueError('invalid promo')
        with self.db() as c: c.execute('INSERT INTO promos VALUES(?,?,?)',(code.upper(),plan,uses))

    def redeem(self,uid,code):
        code=code.upper()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT * FROM promos WHERE code=? AND remaining>0',(code,)).fetchone()
            if not r or c.execute('SELECT 1 FROM promo_uses WHERE code=? AND uid=?',(code,uid)).fetchone(): return False
            c.execute('INSERT INTO promo_uses VALUES(?,?)',(code,uid))
            c.execute('UPDATE promos SET remaining=remaining-1 WHERE code=?',(code,))
            self._grant(c,uid,r['plan'],'promo:'+code+':'+str(uid),int(time.time()))
            return True

    def invite(self,url,uid,expires):
        with self.db() as c: c.execute('INSERT INTO invites VALUES(?,?,?)',(url,uid,expires))

    def invite_owner(self,url):
        with self.db() as c:
            r=c.execute('SELECT uid FROM invites WHERE url=? AND expires>?',(url,int(time.time()))).fetchone()
            return r[0] if r else None

    def managed(self):
        with self.db() as c: return [(r['uid'],self._profile(c,r['uid'])['active']) for r in c.execute('SELECT uid FROM managed WHERE removed=0')]

    def removed(self,uid):
        with self.db() as c: c.execute('UPDATE managed SET removed=1 WHERE uid=?',(uid,))

    def stats(self):
        with self.db() as c:
            users=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            payments=c.execute("SELECT COUNT(*),COALESCE(SUM(amount),0) FROM orders WHERE paid=1").fetchone()
            active=sum(self._profile(c,r['uid'])['active'] for r in c.execute('SELECT DISTINCT uid FROM entitlements'))
            return users,active,payments[0],payments[1]

def date(ts): return datetime.fromtimestamp(ts,timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
def is_member(m): return m.status in ('creator','administrator','member') or (m.status=='restricted' and bool(getattr(m,'is_member',False)))

# Проверка транзакций в блокчейне TON
async def check_ton_transactions(bot, db, pending_orders):
    if not pending_orders: return
    url = f"https://toncenter.com/api/v2/getTransactions?address={TON_WALLET}&limit=50"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200: return
                data = await resp.json()
                txs = data.get('result', [])
                
                for tx in txs:
                    in_msg = tx.get('in_msg', {})
                    if not in_msg: continue
                    
                    value_nano = int(in_msg.get('value', 0))
                    memo = in_msg.get('message', '').strip()
                    
                    if not memo or value_nano == 0: continue
                    
                    for order in pending_orders:
                        order_id = order['id']
                        amount_ton = order['amount']
                        amount_nano_expected = int(amount_ton * 1_000_000_000)
                        
                        if memo == order_id and abs(value_nano - amount_nano_expected) <= 10000000:
                            success = db.mark_order_paid(order_id, order['uid'], order['plan'])
                            if success:
                                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Получить приглашение", callback_data="invite")]])
                                await bot.send_message(order['uid'], "✅ Оплата в TON подтверждена! Подписка активирована.", reply_markup=kb)
                                log.info(f"Order {order_id} paid successfully.")
    except Exception as e:
        log.error(f"TON API Error: {e}")

async def main():
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command, CommandStart
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
    from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

    token = os.environ['ACCESS_BOT_TOKEN']
    path = Path(os.environ['ACCESS_DB_PATH']).expanduser()
    if not path.is_absolute(): raise ValueError('ACCESS_DB_PATH must be absolute')
    group = int(os.environ['ACCESS_GROUP_ID'])
    
    # Цены в TON, как на скриншоте
    prices_ton = {
        'day': 3.0,
        'week': 14.0,
        'forever': 20.0
    }
    
    os.umask(0o077)
    db = Store(path)
    dp = Dispatcher()
    dp.message.filter(F.chat.type == 'private')
    dp.callback_query.filter(F.message.chat.type == 'private')
    membership_lock = asyncio.Lock()

    def button(text, data=None, url=None, style=None):
        args = dict(text=text)
        args.update(dict(url=url) if url else dict(callback_data=data))
        if style: args['style'] = style
        return InlineKeyboardButton(**args)
    def kb(*rows): return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])
    def back(): return [button('Назад', 'main', style='danger')]
    def help_row(): return [button('Поддержка', url=SUPPORT)]
    
    def menu(uid):
        rows = [[button('Купить подписку', 'buy', style='success')], [button('Ваш профиль', 'profile')], [button('Информация', 'info')], [button('Наш канал', url=CHANNEL_URL), button('Поддержка', url=SUPPORT)]]
        if uid == ADMIN_ID: rows.append([button('Админ-панель', 'admin')])
        return kb(*rows)
        
    def profile_text(uid):
        p = db.profile(uid)
        status = '✅ Навсегда' if p['lifetime'] else ('✅ До ' + date(p['expires']) if p['active'] else ('❌ Истекла (' + date(p['expires']) + ')' if p['expires'] else '❌ Не активна'))
        return f'👤 Ваш профиль\n\n🏷 ID: {uid}\n💎 Подписка: {status}\n👥 Приглашено: {p["refs"]}\n'
        
    async def show(message, text, markup, edit=False):
        if edit:
            try: return await message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as e:
                if 'message is not modified' in str(e).lower(): return
        return await message.answer(text, reply_markup=markup)
        
    async def subscribed(bot, uid):
        try: return is_member(await bot.get_chat_member(CHANNEL, uid))
        except TelegramAPIError: return False
        
    async def gate(bot, uid, message, edit=False):
        if await subscribed(bot, uid): return True
        await show(message, '🔐 Подпишитесь на @swopkod и нажмите «Проверить подписку».', kb([button('Подписаться', url=CHANNEL_URL, style='success')], [button('Проверить подписку', 'main')], help_row()), edit)
        return False
        
    async def home(message, uid, edit=False):
        p = db.profile(uid)
        await show(message, '💎 SWOP NFT Tracker\n\nМониторинг публичных лотов Telegram-маркета.\nУведомления в закрытой группе.\n\nПодписка: ' + ('✅ Активна' if p['active'] else '❌ Не активна'), menu(uid), edit)
        
    async def safe_send(bot, uid, text, markup=None):
        try: await bot.send_message(uid, text, reply_markup=markup)
        except TelegramAPIError: pass
        
    async def admin_screen(message, edit=False):
        u, a, p, s = db.stats()
        await show(message, f'⚙️ SWOP ADMIN\n\nПользователей: {u}\nАктивных: {a}\nПлатежей (TON): {p}\nПолучено: {s:.2f} TON\n\n/grant ID day|week|forever\n/revoke ID\n/user ID\n/promoadd CODE day|week|forever USES\n/refund_order ORDER_ID', kb(back()), edit)
        
    async def invite(message, bot, uid, edit=False):
        async with membership_lock:
            if not db.profile(uid)['active']:
                return await show(message, 'Подписка не активна.', kb([button('Купить подписку', 'buy')], back()), edit)
            try:
                m = await bot.get_chat_member(group, uid)
                if is_member(m): return await show(message, '✅ Вы уже в группе.', kb(back()), edit)
                if m.status == 'kicked': await bot.unban_chat_member(group, uid, only_if_banned=True)
                expires = int(time.time()) + 600
                link = await bot.create_chat_invite_link(group, expire_date=expires, creates_join_request=True, name=f'SWOP {uid}')
                db.invite(link.invite_link, uid, expires)
                await show(message, '✅ Приглашение действует 10 минут. Отправьте заявку со своего аккаунта.', kb([button('Вступить', url=link.invite_link, style='success')], back()), edit)
            except TelegramAPIError:
                await show(message, 'Не удалось создать приглашение. Обратитесь в поддержку.', kb(help_row(), back()), edit)

    @dp.message(CommandStart())
    async def start(message, bot):
        uid = message.from_user.id
        parts = (message.text or '').split(); ref = None
        if len(parts) == 2 and parts[1].startswith('r_') and parts[1][2:].isdigit(): ref = int(parts[1][2:])
        db.register(uid, ref)
        if await gate(bot, uid, message): await home(message, uid)

    @dp.message(Command('terms'))
    async def terms(message):
        await message.answer('Условия: доступ к публичному мониторингу рынка. Разовая оплата TON, без автопродления. Поддержка: @swophelp_bot.')
    @dp.message(Command('paysupport', 'help'))
    async def support(message):
        await message.answer('По вопросам оплаты: @swophelp_bot', reply_markup=kb(help_row()))

    @dp.message(Command('admin', 'grant', 'revoke', 'user', 'promoadd', 'refund_order'))
    async def admin(message, bot):
        if message.from_user.id != ADMIN_ID: return await message.answer('Нет доступа.')
        parts = message.text.split(); cmd = parts[0].split('@')[0]
        try:
            async with membership_lock:
                if cmd == '/admin': return await admin_screen(message)
                if cmd == '/grant' and len(parts) == 3:
                    uid = int(parts[1]); db.grant(uid, parts[2]); await message.answer('Доступ выдан.'); await safe_send(bot, uid, 'Администратор выдал доступ.', kb([button('Получить приглашение', 'invite')]))
                elif cmd == '/revoke' and len(parts) == 2:
                    uid = int(parts[1]); db.revoke(uid); await message.answer('Доступ отозван.')
                elif cmd == '/user' and len(parts) == 2: await message.answer(profile_text(int(parts[1])))
                elif cmd == '/promoadd' and len(parts) == 4:
                    db.make_promo(parts[1], parts[2], int(parts[3])); await message.answer('Промокод создан.')
                elif cmd == '/refund_order' and len(parts) == 2:
                    oid = parts[1]
                    with db.db() as c:
                        r = c.execute('SELECT uid FROM orders WHERE id=? AND paid=1', (oid,)).fetchone()
                        if not r: return await message.answer('Заказ не найден или не оплачен.')
                        uid = r['uid']
                        db.revoke(uid)
                    await message.answer(f'Доступ для {uid} отозван. Верните TON вручную.')
                else: await admin_screen(message)
        except (ValueError, sqlite3.IntegrityError): await message.answer('Ошибка параметров.')

    @dp.message(Command('promo'))
    async def promo(message, bot):
        uid = message.from_user.id; db.register(uid)
        if not await gate(bot, uid, message): return
        parts = message.text.split()
        if len(parts) != 2: return await message.answer('Введите /promo КОД')
        async with membership_lock: ok = db.redeem(uid, parts[1])
        await message.answer('✅ Промокод активирован.' if ok else 'Промокод недействителен.', reply_markup=menu(uid))

    @dp.callback_query()
    async def callback(call, bot):
        uid = call.from_user.id; message = call.message; action = call.data or ''
        if not message or message.chat.id != uid: return await call.answer()
        await call.answer(); db.register(uid)
        
        if action == 'admin':
            if uid == ADMIN_ID: await admin_screen(message, True)
            return
        if not await gate(bot, uid, message, True): return
        
        if action == 'main': await home(message, uid, True)
        elif action == 'profile': await show(message, profile_text(uid), kb([button('Ввести промокод', 'promo', style='success')], [button('Реферальная ссылка', 'ref')], [button('Получить приглашение', 'invite')], back()), True)
        
        elif action == 'buy':
            # Точная копия меню с картинки
            text = '💎 **SWOP NFT TRACKER**\nВыберите срок подписки:'
            rows = [
                [button('⚡ 24 часа — 3 TON', 'buy:day', style='success')],
                [button('🔥 7 дней — 14 TON', 'buy:week', style='success')],
                [button('👑 Навсегда + офферы — 20 TON', 'buy:forever', style='success')],
                [button('Назад', 'main', style='danger')]
            ]
            await show(message, text, kb(*rows), True)
            
        elif action.startswith('buy:'):
            plan = action[4:]
            if plan not in PLANS: return
            if db.profile(uid)['lifetime']: return await show(message, 'У вас уже бессрочный доступ.', kb(back()), True)
            
            amount_ton = prices_ton[plan]
            oid = db.order(uid, plan, amount_ton)
            
            # Формируем Deep Link для Tonkeeper (без # в самом URL, чтобы не сломать ссылку)
            amount_nano = int(amount_ton * 1_000_000_000)
            url = f"https://app.tonkeeper.com/transfer/{TON_WALLET}?amount={amount_nano}&text={urllib.parse.quote(oid)}"
            
            # Текст точь-в-точь как на скриншоте
            text = (
                f"💎 **TON (сеть TON)**\n"
                f"#{oid}\n\n"
                f"⭐ К оплате: **{amount_ton} TON**\n\n"
                f"🔗 Адрес:\n`{TON_WALLET}`\n\n"
                f"ℹ️ Комментарий (MEMO):\n`{oid}`\n\n"
                f"❗ **ВНИМАНИЕ!**\n"
                f"Не изменяйте комментарий к переводу."
            )
            
            markup = kb(
                [button('Оплатить в Tonkeeper ↗', url=url, style='success')],
                [button('Проверить оплату', f'check_pay:{oid}')],
                [button('Назад', 'main', style='danger')]
            )
            await show(message, text, markup, True)

        elif action.startswith('check_pay:'):
            oid = action.split(':')[1]
            order = db.get_order(oid)
            if not order or order['paid']:
                return await show(message, 'Заказ уже оплачен.', kb(back()), True)
            
            await check_ton_transactions(bot, db, [order])
            order = db.get_order(oid)
            if order['paid']:
                await show(message, '✅ Оплата получена! Подписка активирована.', kb([button('Получить приглашение', 'invite', style='success')], back()), True)
            else:
                await show(message, '⏳ Платеж еще не поступил в сеть. Подождите 10-30 секунд и проверьте снова.', kb([button('Проверить снова', f'check_pay:{oid}')], back()), True)

        elif action == 'invite': await invite(message, bot, uid, True)
        elif action == 'info': await show(message, '🛡 Информация\n\nSWOP NFT Tracker — мониторинг публичных лотов.', kb(help_row(), back()), True)
        elif action == 'promo': await show(message, 'Отправьте /promo КОД', kb(back()), True)
        elif action == 'ref':
            me = await bot.get_me(); await show(message, f'👥 Ваша ссылка:\nhttps://t.me/{me.username}?start=r_{uid}', kb(back()), True)

    @dp.chat_join_request()
    async def join(q, bot):
        if q.chat.id != group or not q.invite_link: return
        me = await bot.get_me()
        if q.invite_link.creator.id != me.id: return
        uid = q.from_user.id; url = q.invite_link.invite_link
        async with membership_lock:
            allowed = db.invite_owner(url) == uid and db.profile(uid)['active'] and await subscribed(bot, uid)
            if allowed:
                await bot.approve_chat_join_request(group, uid)
                try: await bot.revoke_chat_invite_link(group, url)
                except TelegramAPIError: pass
            else: await bot.decline_chat_join_request(group, uid)

    async def sweep(bot):
        while True:
            try:
                # Очистка истекших подписок
                for uid, active in db.managed():
                    if active: continue
                    async with membership_lock:
                        if db.profile(uid)['active']: continue
                        try:
                            m = await bot.get_chat_member(group, uid)
                            if m.status in ('creator', 'administrator'): continue
                            if is_member(m): await bot.ban_chat_member(group, uid)
                            db.removed(uid)
                            await safe_send(bot, uid, 'Подписка истекла.', kb([button('Продлить', 'buy')]))
                        except TelegramAPIError: pass
                
                # Проверка транзакций
                pending = db.get_pending_orders()
                await check_ton_transactions(bot, db, pending)

            except Exception: log.exception('Sweep failed')
            await asyncio.sleep(30)

    async with Bot(token) as bot:
        me = await bot.get_me()
        for chat in (CHANNEL, group):
            member = await bot.get_chat_member(chat, me.id)
            if member.status not in ('administrator', 'creator'): raise RuntimeError(f'Bot must be admin in {chat}')
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands([BotCommand(command='start', description='Главное меню'), BotCommand(command='terms', description='Условия'), BotCommand(command='paysupport', description='Поддержка')])
        print(f'SWOP ACCESS ONLINE: @{me.username}', flush=True)
        
        task = asyncio.create_task(sweep(bot))
        try: await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_as_tasks=False, close_bot_session=False)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())