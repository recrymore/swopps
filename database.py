import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
import aiosqlite
from models import Subscriber, LotItem, Order, compute_extension

logger = logging.getLogger(__name__)


def normalize_seller_name(text: Optional[str]) -> str:
    """Нормализует имя пользователя Telegram: убирает математические шрифты, эмодзи, декоративные символы."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", str(text))
    cleaned = re.sub(r'[\U00010000-\U0010ffff]', '', norm)
    cleaned = re.sub(r'[#|★⚡👑💎•ツ✓✔\[\]\(\)\<\>\"\'`]', ' ', cleaned)
    cleaned = re.sub(r'[\u200b-\u200f\ufe00-\ufe0f\u206a-\u206f]', '', cleaned)
    return " ".join(cleaned.split()).lower()


def verify_seller_name(owner_name: Optional[str], seller_data: Any) -> bool:
    """
    Строго проверяет, что имя профиля найденного пользователя (@username)
    полностью совпадает с серым именем владельца, отображаемым на подарке.
    """
    if not owner_name or not seller_data:
        return False

    if isinstance(seller_data, dict):
        fn = (seller_data.get("first_name") or "").strip()
        ln = (seller_data.get("last_name") or "").strip()
        u = (seller_data.get("username") or "").strip().lower().lstrip("@")
    else:
        fn = (getattr(seller_data, "first_name", "") or "").strip()
        ln = (getattr(seller_data, "last_name", "") or "").strip()
        u = (getattr(seller_data, "username", "") or "").strip().lower().lstrip("@")

    full = f"{fn} {ln}".strip()
    on_clean = owner_name.strip()

    # 1. Прямое точное совпадение (без учёта регистра)
    on_lower = on_clean.lower()
    if on_lower == full.lower():
        return True
    if not ln and on_lower == fn.lower():
        return True
    if u and on_lower.lstrip("@") == u:
        return True

    # 2. Нормализованное совпадение (декодирование спецшрифтов, удаление эмодзи и разделителей)
    on_norm = normalize_seller_name(on_clean)
    full_norm = normalize_seller_name(full)
    fn_norm = normalize_seller_name(fn)

    if on_norm and full_norm and on_norm == full_norm:
        return True
    if not ln and on_norm and fn_norm and on_norm == fn_norm:
        return True
    if u and on_norm and on_norm == u:
        return True

    return False


def has_gift_on_profile(
    profile_gifts: Optional[List[Dict[str, Any]]],
    slug: str = "",
    num: Optional[int] = None,
    title: str = "",
    require_exact: bool = False,
    nft_gifts_count: Optional[int] = None,
) -> bool:
    """
    Проверяет наличие NFT-подарков у пользователя:
    1. Если require_exact=True -> ищет строго целевой подарок (по slug или title+num) в profile_gifts.
    2. Если require_exact=False:
       - Требуем наличие хотя бы одного продающегося NFT-подарка (on_sale=True с валидным slug) в профиле!
    """
    if not profile_gifts:
        return False

    slug_clean = (slug or "").strip().lower()
    title_clean = (title or "").strip().lower()

    if require_exact:
        for pg in profile_gifts:
            if not pg.get("slug"):
                continue
            pg_slug = (pg.get("slug") or "").strip().lower()
            pg_num = pg.get("num")
            pg_title = (pg.get("title") or "").strip().lower()

            # 1. Точное совпадение по уникальному slug
            if slug_clean and pg_slug and slug_clean == pg_slug:
                return True

            # 2. Совпадение по номеру и названию коллекции
            if num is not None and pg_num is not None and pg_num == num:
                if not title_clean or not pg_title or title_clean == pg_title:
                    return True
        return False

    # Если require_exact=False: требуем наличие хотя бы одного видимого NFT-подарка в открытом профиле
    return bool(profile_gifts and len(profile_gifts) > 0)


def find_best_profile_gift(
    profile_gifts: Optional[List[Dict[str, Any]]],
    slug: str = "",
    num: Optional[int] = None,
    title: str = "",
    max_price_stars: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Находит лучший подходящий подарок из открытого профиля пользователя:
    1. Приоритет: точное совпадение лота (по slug или num+title), которое выставлено на продажу (on_sale=True).
    2. Если точного совпадения на продаже нет — ЛЮБОЙ подарок в профиле, который выставлен на продажу (on_sale=True и валидный slug).
       Если таких несколько — выбирает с подходящей ценой <= max_price_stars (или самый дешевый на продаже).
    3. Точное совпадение целевого лота с маркета (по slug или num+title), даже если флаг on_sale в профиле не проставлен
       (так как лот прямо сейчас найден на маркете, он гарантированно на продаже!).
    4. Если подходящих подарков на продаже или целевого лота нет — возвращает None.
    """
    if not profile_gifts:
        return None

    slug_clean = (slug or "").strip().lower()
    title_clean = (title or "").strip().lower()

    # 1. Приоритет: точное совпадение лота, если он на продаже
    for pg in profile_gifts:
        if not pg.get("on_sale"):
            continue
        pg_slug = (pg.get("slug") or "").strip().lower()
        pg_num = pg.get("num")
        pg_title = (pg.get("title") or "").strip().lower()

        if slug_clean and pg_slug and slug_clean == pg_slug:
            return pg
        if num is not None and pg_num is not None and pg_num == num:
            if not title_clean or not pg_title or title_clean == pg_title:
                return pg

    # 2. Любой подарок из профиля, который выставлен на продажу (on_sale=True и имеет slug)
    selling_gifts = [pg for pg in profile_gifts if pg.get("on_sale") and pg.get("slug")]
    if selling_gifts:
        if max_price_stars is not None and max_price_stars > 0:
            within_budget = [
                pg for pg in selling_gifts 
                if (pg.get("price_stars") or 0) <= max_price_stars
            ]
            if within_budget:
                within_budget.sort(key=lambda x: x.get("price_stars") or 0)
                return within_budget[0]
        selling_gifts.sort(key=lambda x: x.get("price_stars") or 0)
        return selling_gifts[0]

    # 3. Точное совпадение лота (даже если on_sale не отмечен)
    for pg in profile_gifts:
        pg_slug = (pg.get("slug") or "").strip().lower()
        pg_num = pg.get("num")
        pg_title = (pg.get("title") or "").strip().lower()

        if slug_clean and pg_slug and slug_clean == pg_slug:
            return pg
        if num is not None and pg_num is not None and pg_num == num:
            if not title_clean or not pg_title or title_clean == pg_title:
                return pg

    # 4. Fallback: первый имеющийся NFT-подарок со slug
    for pg in profile_gifts:
        if pg.get("slug"):
            return pg

    return None


class Database:
    def __init__(self, db_path: str = "gifts_monitor.db"):
        self.db_path = db_path
        self._seen_cache: set = set()
        self._seen_cache_loaded: bool = False
        self._alerted_sellers: Dict[int, float] = {}
        self._name_to_seller_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    async def init_db(self) -> None:
        """Инициализация базы данных SQLite с поддержкой многопроцессного режима (WAL)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute("PRAGMA synchronous=NORMAL;")

            # Таблица подписчиков бота
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    active BOOLEAN DEFAULT 1,
                    min_price INTEGER DEFAULT 0,
                    max_price INTEGER DEFAULT 5000,
                    mode TEXT DEFAULT 'market',
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    market_until TIMESTAMP DEFAULT NULL,
                    upgrades_until TIMESTAMP DEFAULT NULL,
                    has_used_trial BOOLEAN DEFAULT 0,
                    market_reminder_sent BOOLEAN DEFAULT 0,
                    upgrades_reminder_sent BOOLEAN DEFAULT 0,
                    market_expired_sent BOOLEAN DEFAULT 0,
                    upgrades_expired_sent BOOLEAN DEFAULT 0,
                    username TEXT DEFAULT NULL,
                    first_name TEXT DEFAULT NULL,
                    last_name TEXT DEFAULT NULL,
                    custom_template TEXT DEFAULT NULL,
                    upgrade_filter TEXT DEFAULT 'all',
                    has_team_access BOOLEAN DEFAULT 0
                );
                """
            )
            # Миграции для subscribers
            for col_def in [
                "mode TEXT DEFAULT 'market'",
                "market_until TIMESTAMP DEFAULT NULL",
                "upgrades_until TIMESTAMP DEFAULT NULL",
                "has_used_trial BOOLEAN DEFAULT 0",
                "market_reminder_sent BOOLEAN DEFAULT 0",
                "upgrades_reminder_sent BOOLEAN DEFAULT 0",
                "market_expired_sent BOOLEAN DEFAULT 0",
                "upgrades_expired_sent BOOLEAN DEFAULT 0",
                "username TEXT DEFAULT NULL",
                "first_name TEXT DEFAULT NULL",
                "last_name TEXT DEFAULT NULL",
                "custom_template TEXT DEFAULT NULL",
                "upgrade_filter TEXT DEFAULT 'all'",
                "has_team_access BOOLEAN DEFAULT 0",
            ]:
                try:
                    await db.execute(f"ALTER TABLE subscribers ADD COLUMN {col_def};")
                except Exception:
                    pass

            # Таблица заявок на оплату
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    sub_type TEXT NOT NULL,
                    duration TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    rate REAL,
                    comment TEXT UNIQUE NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    paid_at TIMESTAMP,
                    tx_hash TEXT
                );
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_comment ON orders(comment);")

            # Таблица обработанных платежей (для защиты от повторного зачисления tx_hash)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_payments (
                    tx_hash TEXT PRIMARY KEY,
                    order_id INTEGER,
                    user_id INTEGER,
                    currency TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    comment TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Таблица несопоставленных платежей (недоплата, просроченные, без заявки)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS unmatched_payments (
                    tx_hash TEXT PRIMARY KEY,
                    currency TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    comment TEXT,
                    reason TEXT,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Таблица проигнорированных сторонних транзакций (личные переводы владельца, спам без кода gp-)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS ignored_payments (
                    tx_hash TEXT PRIMARY KEY,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Таблица настроек бота (хранение admin_id и других параметров)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            # Таблица уже просмотренных лотов для дедупликации
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_lots (
                    lot_key TEXT PRIMARY KEY,
                    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Таблица отправленных продавцов для предотвращения повторного спама
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS alerted_sellers (
                    seller_id INTEGER PRIMARY KEY,
                    alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Очередь найденных лотов для рассылки (outbox)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lot_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);"
            )

            # Таблица связок отправленных сообщений подписчикам для системы занятия лотов
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_dispatches (
                    outbox_id INTEGER,
                    chat_id INTEGER,
                    message_id INTEGER,
                    PRIMARY KEY (outbox_id, chat_id)
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_dispatches_outbox ON outbox_dispatches(outbox_id);"
            )

            # Таблица занятых лотов (Claim System)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS claimed_lots (
                    outbox_id INTEGER PRIMARY KEY,
                    lot_key TEXT NOT NULL,
                    claimed_by INTEGER NOT NULL,
                    claimed_username TEXT,
                    claimed_name TEXT,
                    claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_claimed_lots_user ON claimed_lots(claimed_by);"
            )

            # Кэш профилей продавцов для снижения нагрузки на users.getFullUser
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS seller_cache (
                    seller_id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL,
                    cached_at REAL NOT NULL
                );
                """
            )

            # Таблица истории цен лотов для приоритизации (motivated, stale)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS listing_history (
                    listing_key TEXT PRIMARY KEY,
                    first_seen_at TIMESTAMP,
                    first_seen_price INTEGER,
                    last_price INTEGER
                );
                """
            )

            # Таблица отслеживаемых моделей подарков для апгрейдов
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked_gift_models (
                    short_name TEXT PRIMARY KEY,
                    gift_id INTEGER,
                    last_seen_in_catalog TIMESTAMP,
                    active BOOLEAN DEFAULT 1
                );
                """
            )

            # Таблица последнего известного номера апгрейда для модели
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS upgrade_tracking (
                    short_name TEXT PRIMARY KEY,
                    last_known_num INTEGER
                );
                """
            )

            # Таблица реестра владельцев подарков по slug (Gift Registry)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS gift_registry (
                    slug TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_gift_registry_owner ON gift_registry(owner_id);"
            )

            # Таблица автоматически собираемых апгрейдов при каждом скане
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS scanned_upgrades (
                    slug TEXT PRIMARY KEY,
                    short_name TEXT NOT NULL,
                    num INTEGER NOT NULL,
                    model TEXT,
                    model_rarity TEXT,
                    backdrop TEXT,
                    pattern TEXT,
                    owner_id INTEGER,
                    owner_name TEXT,
                    username TEXT,
                    first_name TEXT,
                    level INTEGER,
                    nft_gifts_count INTEGER,
                    raw_data TEXT,
                    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_scanned_upgrades_model ON scanned_upgrades(short_name, num);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_scanned_upgrades_owner ON scanned_upgrades(owner_id);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_scanned_upgrades_username ON scanned_upgrades(username);"
            )

            # Таблица навсегда исключенных коллекций (флор > max_collection_floor)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS excluded_collections (
                    gift_id INTEGER PRIMARY KEY,
                    title TEXT,
                    floor_stars INTEGER,
                    excluded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            await db.commit()
            logger.info(f"База данных SQLite '{self.db_path}' инициализирована в режиме WAL.")
        await self._ensure_name_cache()

    # ------------------ Подписчики ------------------

    @staticmethod
    def _row_to_subscriber(row: tuple) -> Subscriber:
        return Subscriber(
            chat_id=row[0],
            active=bool(row[1]),
            min_price=row[2],
            max_price=row[3],
            mode=row[4] or "market",
            joined_at=str(row[5]) if row[5] else None,
            market_until=str(row[6]) if len(row) > 6 and row[6] else None,
            upgrades_until=str(row[7]) if len(row) > 7 and row[7] else None,
            has_used_trial=bool(row[8]) if len(row) > 8 and row[8] else False,
            username=str(row[9]) if len(row) > 9 and row[9] else None,
            first_name=str(row[10]) if len(row) > 10 and row[10] else None,
            last_name=str(row[11]) if len(row) > 11 and row[11] else None,
            custom_template=str(row[12]) if len(row) > 12 and row[12] else None,
            upgrade_filter=str(row[13]) if len(row) > 13 and row[13] else "all",
            has_team_access=bool(row[14]) if len(row) > 14 and row[14] else False,
        )

    _SUB_COLS = (
        "chat_id, active, min_price, max_price, mode, joined_at, "
        "market_until, upgrades_until, has_used_trial, username, first_name, last_name, custom_template, upgrade_filter, has_team_access"
    )

    async def upsert_subscriber(
        self,
        chat_id: int,
        default_min: int = 0,
        default_max: int = 5000,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> Subscriber:
        """Создает подписчика или активирует существующего с обновлением данных профиля."""
        username = str(username) if isinstance(username, str) else None
        first_name = str(first_name) if isinstance(first_name, str) else None
        last_name = str(last_name) if isinstance(last_name, str) else None

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                f"SELECT {self._SUB_COLS} FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                if username or first_name or last_name:
                    await db.execute(
                        """
                        UPDATE subscribers
                        SET active = 1,
                            username = COALESCE(?, username),
                            first_name = COALESCE(?, first_name),
                            last_name = COALESCE(?, last_name)
                        WHERE chat_id = ?
                        """,
                        (username, first_name, last_name, chat_id),
                    )
                else:
                    await db.execute(
                        "UPDATE subscribers SET active = 1 WHERE chat_id = ?",
                        (chat_id,),
                    )
                await db.commit()
                async with db.execute(
                    f"SELECT {self._SUB_COLS} FROM subscribers WHERE chat_id = ?",
                    (chat_id,),
                ) as cursor:
                    updated_row = await cursor.fetchone()
                return self._row_to_subscriber(updated_row or row)
            else:
                await db.execute(
                    """
                    INSERT INTO subscribers (
                        chat_id, active, min_price, max_price, mode,
                        username, first_name, last_name
                    )
                    VALUES (?, 1, ?, ?, 'market', ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        active = 1,
                        username = COALESCE(excluded.username, subscribers.username),
                        first_name = COALESCE(excluded.first_name, subscribers.first_name),
                        last_name = COALESCE(excluded.last_name, subscribers.last_name)
                    """,
                    (chat_id, default_min, default_max, username, first_name, last_name),
                )
                await db.commit()
                async with db.execute(
                    f"SELECT {self._SUB_COLS} FROM subscribers WHERE chat_id = ?",
                    (chat_id,),
                ) as cursor:
                    new_row = await cursor.fetchone()
                return self._row_to_subscriber(new_row)

    async def deactivate_subscriber(self, chat_id: int) -> None:
        """Отключает уведомления для подписчика (active = 0)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "UPDATE subscribers SET active = 0 WHERE chat_id = ?", (chat_id,)
            )
            await db.commit()

    async def get_subscriber(self, chat_id: int) -> Optional[Subscriber]:
        """Возвращает полную информацию о подписчике."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                f"SELECT {self._SUB_COLS} FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return self._row_to_subscriber(row)
        return None

    async def update_subscriber_user_info(
        self,
        chat_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> None:
        """Обновляет метаданные профиля подписчика."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                """
                UPDATE subscribers
                SET username = COALESCE(?, username),
                    first_name = COALESCE(?, first_name),
                    last_name = COALESCE(?, last_name)
                WHERE chat_id = ?
                """,
                (username, first_name, last_name, chat_id),
            )
            await db.commit()

    async def update_subscriber_price(
        self, chat_id: int, min_price: int, max_price: int
    ) -> None:
        """Обновляет диапазон цен для подписчика и активирует его."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT chat_id FROM subscribers WHERE chat_id = ?", (chat_id,)
            ) as cursor:
                exists = await cursor.fetchone()

            if exists:
                await db.execute(
                    "UPDATE subscribers SET min_price = ?, max_price = ?, active = 1 WHERE chat_id = ?",
                    (min_price, max_price, chat_id),
                )
            else:
                await db.execute(
                    "INSERT INTO subscribers (chat_id, active, min_price, max_price, mode) VALUES (?, 1, ?, ?, 'market')",
                    (chat_id, min_price, max_price),
                )
            await db.commit()

    async def update_subscriber_mode(self, chat_id: int, mode: str) -> None:
        """Обновляет режим уведомлений подписчика ('market', 'upgrades', 'both')."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "UPDATE subscribers SET mode = ? WHERE chat_id = ?",
                (mode, chat_id),
            )
            await db.commit()

    async def update_subscriber_upgrade_filter(self, chat_id: int, upgrade_filter: str) -> None:
        """Обновляет фильтр фона улучшений подписчика ('all' или 'black_onyx')."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "UPDATE subscribers SET upgrade_filter = ? WHERE chat_id = ?",
                (upgrade_filter, chat_id),
            )
            await db.commit()

    async def update_subscriber_template(self, chat_id: int, template: Optional[str]) -> None:
        """Сохраняет или сбрасывает индивидуальный шаблон сообщения продавцу."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "UPDATE subscribers SET custom_template = ? WHERE chat_id = ?",
                (template, chat_id),
            )
            await db.commit()

    async def get_subscriber_template(self, chat_id: int) -> Optional[str]:
        """Возвращает индивидуальный шаблон сообщения продавцу для пользователя."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT custom_template FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    return str(row[0])
        return None

    async def get_active_subscribers_for_price(self, price_stars: int) -> List[int]:
        """Возвращает список chat_id активных подписчиков, чей диапазон цен включает price_stars."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                """
                SELECT chat_id FROM subscribers
                WHERE active = 1 AND min_price <= ? AND max_price >= ?
                """,
                (price_stars, price_stars),
            ) as cursor:
                rows = await cursor.fetchall()
                return [r[0] for r in rows]

    async def get_active_subscribers(self) -> List[Subscriber]:
        """Возвращает всех активных подписчиков с актуальными подписками."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                f"SELECT {self._SUB_COLS} FROM subscribers WHERE active = 1"
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_subscriber(r) for r in rows]

    async def get_all_subscribers(self) -> List[Subscriber]:
        """Возвращает всех зарегистрированных пользователей бота (включая тех, кто приостановил уведомления)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                f"SELECT {self._SUB_COLS} FROM subscribers"
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_subscriber(r) for r in rows]

    async def get_stats(self) -> Dict[str, int]:
        """Возвращает статистику для администратора бота."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT COUNT(*) FROM subscribers WHERE active = 1"
            ) as c:
                active_count = (await c.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM subscribers") as c:
                total_count = (await c.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'pending'"
            ) as c:
                pending_outbox = (await c.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM seen_lots") as c:
                seen_count = (await c.fetchone())[0]

            try:
                async with db.execute("SELECT COUNT(*) FROM scanned_upgrades") as c:
                    scanned_upgrades_count = (await c.fetchone())[0]
            except Exception:
                scanned_upgrades_count = 0

            return {
                "active_subscribers": active_count,
                "total_subscribers": total_count,
                "pending_outbox": pending_outbox,
                "seen_lots": seen_count,
                "scanned_upgrades": scanned_upgrades_count,
            }

    # ------------------ Дедупликация лотов ------------------

    async def _ensure_seen_cache(self) -> None:
        """Загружает существующие ключи seen_lots и недавних продавцов в память при первом обращении."""
        if not self._seen_cache_loaded:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=10000;")
                async with db.execute("SELECT lot_key FROM seen_lots") as cursor:
                    rows = await cursor.fetchall()
                    self._seen_cache.update(r[0] for r in rows)
                try:
                    async with db.execute(
                        "SELECT seller_id, strftime('%s', alerted_at) FROM alerted_sellers WHERE alerted_at > datetime('now', '-24 hours')"
                    ) as cursor:
                        for sid, ts in await cursor.fetchall():
                            if sid and ts:
                                try:
                                    self._alerted_sellers[int(sid)] = float(ts)
                                except Exception:
                                    pass
                except Exception:
                    pass
            self._seen_cache_loaded = True

    async def is_seen_lots_empty(self) -> bool:
        """Проверяет, пуста ли таблица seen_lots (для определения первого запуска)."""
        await self._ensure_seen_cache()
        return len(self._seen_cache) == 0

    async def has_seen_lot(self, lot_key: str) -> bool:
        """Проверяет, был ли лот уже обнаружен (мгновенно из ОЗУ, 0 мс)."""
        if not self._seen_cache_loaded:
            await self._ensure_seen_cache()
        return lot_key in self._seen_cache

    async def mark_lot_seen(self, lot_key: str) -> None:
        """Запоминает ключ лота в ОЗУ и сохраняет в базе."""
        self._seen_cache.add(lot_key)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "INSERT OR IGNORE INTO seen_lots (lot_key) VALUES (?)", (lot_key,)
            )
            await db.commit()

    async def mark_lots_seen_bulk(self, lot_keys: List[str]) -> None:
        """Пакетное запоминание ключей лотов в ОЗУ и базе."""
        if not lot_keys:
            return
        self._seen_cache.update(lot_keys)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.executemany(
                "INSERT OR IGNORE INTO seen_lots (lot_key) VALUES (?)",
                [(k,) for k in lot_keys],
            )
            await db.commit()

    async def mark_seller_alerted(self, seller_id: int) -> None:
        """Запоминает продавца, чтобы не спамить пользователю повторами одного и того же человека."""
        if not seller_id:
            return
        now_ts = time.time()
        self._alerted_sellers[seller_id] = now_ts
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "INSERT OR REPLACE INTO alerted_sellers (seller_id, alerted_at) VALUES (?, CURRENT_TIMESTAMP)",
                (seller_id,),
            )
            await db.commit()

    async def is_seller_on_cooldown(self, seller_id: int, cooldown_seconds: int = 43200) -> bool:
        """Проверяет, отправлялся ли этот продавец в ленту за последние cooldown_seconds (по умолчанию 12 часов)."""
        if not seller_id:
            return False
        if not self._seen_cache_loaded:
            await self._ensure_seen_cache()
        now_ts = time.time()
        last_time = self._alerted_sellers.get(seller_id)
        if last_time is not None:
            return (now_ts - last_time) < cooldown_seconds
        return False

    # ------------------ Очередь Outbox ------------------

    async def add_to_outbox(self, lot: LotItem) -> int:
        """Добавляет прошедший общие фильтры лот в очередь outbox."""
        payload_json = json.dumps(lot.to_dict(), ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            cursor = await db.execute(
                "INSERT INTO outbox (lot_key, payload, status) VALUES (?, ?, 'pending')",
                (lot.lot_key, payload_json),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_pending_outbox(self, limit: int = 10) -> List[Tuple[int, LotItem]]:
        """Извлекает пачку лотов со статусом 'pending'."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT id, payload FROM outbox WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                results = []
                for row_id, payload_str in rows:
                    try:
                        data = json.loads(payload_str)
                        results.append((row_id, LotItem.from_dict(data)))
                    except Exception as e:
                        logger.error(f"Ошибка десериализации payload outbox #{row_id}: {e}")
                return results

    async def reset_processing_outbox(self) -> int:
        """Восстанавливает лоты, зависшие в статусе 'processing' при перезапуске бота, возвращая их в 'pending'."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            cursor = await db.execute(
                "UPDATE outbox SET status = 'pending' WHERE status = 'processing'"
            )
            await db.commit()
            return cursor.rowcount

    async def mark_outbox_processing(self, outbox_id: int) -> None:
        """Помечает лот как обрабатываемый."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "UPDATE outbox SET status = 'processing' WHERE id = ?", (outbox_id,)
            )
            await db.commit()

    async def mark_outbox_done(self, outbox_id: int) -> None:
        """Помечает лот как успешно отправленный."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "UPDATE outbox SET status = 'done' WHERE id = ?", (outbox_id,)
            )
            await db.commit()

    # ------------------ Кэш продавцов ------------------

    async def get_cached_seller(
        self, seller_id: int, max_age_seconds: float
    ) -> Optional[Dict[str, Any]]:
        """Возвращает кэшированные данные продавца, если срок жизни TTL не истек."""
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT data, cached_at FROM seller_cache WHERE seller_id = ?",
                (seller_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    data_str, cached_at = row
                    if (now - cached_at) <= max_age_seconds:
                        try:
                            return json.loads(data_str)
                        except Exception:
                            pass
        return None

    async def _ensure_name_cache(self) -> None:
        """Предварительно загружает индекс имён продавцов из базы данных в ОЗУ для мгновенного поиска (0 мс)."""
        if getattr(self, "_name_cache_loaded", False):
            return
        if not hasattr(self, "_name_to_seller_cache"):
            self._name_to_seller_cache = {}
        try:
            fn_counts: Dict[str, set] = {}
            entries: List[Tuple[float, Dict[str, Any]]] = []

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=10000;")
                async with db.execute("SELECT data, cached_at FROM seller_cache") as cur:
                    async for row in cur:
                        try:
                            d = json.loads(row[0])
                            cached_at = row[1]
                            sid = d.get("seller_id")
                            fn = (d.get("first_name") or "").strip()
                            ln = (d.get("last_name") or "").strip()
                            fn_norm = normalize_seller_name(fn)

                            if fn_norm and not ln and sid:
                                if fn_norm not in fn_counts:
                                    fn_counts[fn_norm] = set()
                                fn_counts[fn_norm].add(sid)

                            entries.append((cached_at, d))
                        except Exception:
                            pass

            for cached_at, d in entries:
                try:
                    fn = (d.get("first_name") or "").strip()
                    ln = (d.get("last_name") or "").strip()
                    full = f"{fn} {ln}".strip()
                    u = (d.get("username") or "").strip().lower().lstrip("@")

                    fn_lower = fn.lower()
                    full_lower = full.lower()
                    fn_norm = normalize_seller_name(fn)
                    full_norm = normalize_seller_name(full)

                    # Индексируем полное имя (first_name + last_name)
                    if full_lower and (full_lower not in self._name_to_seller_cache or cached_at > self._name_to_seller_cache[full_lower][0]):
                        self._name_to_seller_cache[full_lower] = (cached_at, d)
                    if full_norm and (full_norm not in self._name_to_seller_cache or cached_at > self._name_to_seller_cache[full_norm][0]):
                        self._name_to_seller_cache[full_norm] = (cached_at, d)

                    # Если фамилии нет, индексируем first_name ТОЛЬКО если это имя уникально (не омоним вроде 'Данил', 'Александр', 'Иван')
                    if not ln and fn_norm and len(fn_counts.get(fn_norm, set())) <= 1:
                        if fn_lower and (fn_lower not in self._name_to_seller_cache or cached_at > self._name_to_seller_cache[fn_lower][0]):
                            self._name_to_seller_cache[fn_lower] = (cached_at, d)
                        if fn_norm and (fn_norm not in self._name_to_seller_cache or cached_at > self._name_to_seller_cache[fn_norm][0]):
                            self._name_to_seller_cache[fn_norm] = (cached_at, d)

                    # Индексируем username
                    if u and (u not in self._name_to_seller_cache or cached_at > self._name_to_seller_cache[u][0]):
                        self._name_to_seller_cache[u] = (cached_at, d)
                except Exception:
                    pass

            self._name_cache_loaded = True
            logger.info(f"⚡ Индекс имён продавцов загружен в ОЗУ: {len(self._name_to_seller_cache)} записей.")
        except Exception as e:
            logger.warning(f"Ошибка загрузки индекса имён в ОЗУ: {e}")

    async def get_cached_seller_by_name(
        self, name: str, max_age_seconds: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Ищет кэшированного продавца по строгому совпадению отображаемого имени (first_name, full_name, username)."""
        if not name:
            return None
        await self._ensure_name_cache()
        clean_name = name.strip()
        name_lower = clean_name.lower()
        now = time.time()

        candidates = [name_lower]
        name_norm = normalize_seller_name(clean_name)
        if name_norm and name_norm not in candidates:
            candidates.append(name_norm)

        clean_no_hash = name_lower.lstrip("#").strip()
        if clean_no_hash and clean_no_hash not in candidates:
            candidates.append(clean_no_hash)
        clean_no_at = name_lower.lstrip("@").strip()
        if clean_no_at and clean_no_at not in candidates:
            candidates.append(clean_no_at)

        # 1. Быстрый поиск в ОЗУ кэше (0 мс)
        if hasattr(self, "_name_to_seller_cache"):
            for cand in candidates:
                if cand in self._name_to_seller_cache:
                    cached_at, data = self._name_to_seller_cache[cand]
                    if max_age_seconds is None or (now - cached_at) <= max_age_seconds:
                        if verify_seller_name(name, data):
                            return data

        return None

    async def save_cached_seller(self, seller_id: int, data: Dict[str, Any]) -> None:
        """Сохраняет профиль продавца в кэш."""
        now = time.time()
        data_str = json.dumps(data, ensure_ascii=False)

        # Обновляем индекс имен в ОЗУ
        fn = (data.get("first_name") or "").strip()
        ln = (data.get("last_name") or "").strip()
        full = f"{fn} {ln}".strip()
        u = (data.get("username") or "").strip().lower().lstrip("@")
        if not hasattr(self, "_name_to_seller_cache"):
            self._name_to_seller_cache = {}
        if fn:
            self._name_to_seller_cache[fn.lower()] = (now, data)
            fn_norm = normalize_seller_name(fn)
            if fn_norm:
                self._name_to_seller_cache[fn_norm] = (now, data)
        if full:
            self._name_to_seller_cache[full.lower()] = (now, data)
            full_norm = normalize_seller_name(full)
            if full_norm:
                self._name_to_seller_cache[full_norm] = (now, data)
        if u:
            self._name_to_seller_cache[u] = (now, data)

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=30000;")
                await db.execute(
                    """
                    INSERT INTO seller_cache (seller_id, data, cached_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(seller_id) DO UPDATE SET
                        data = excluded.data,
                        cached_at = excluded.cached_at
                    """,
                    (seller_id, data_str, now),
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"Ошибка сохранения seller_cache ({seller_id}): {e}")

    async def save_cached_sellers_bulk(self, sellers: List[Tuple[int, Dict[str, Any]]]) -> None:
        """Пакетное сохранение списка профилей продавцов в кэш seller_cache в одной транзакции."""
        if not sellers:
            return
        now = time.time()
        if not hasattr(self, "_name_to_seller_cache"):
            self._name_to_seller_cache = {}

        items_to_insert = []
        for seller_id, data in sellers:
            fn = (data.get("first_name") or "").strip()
            ln = (data.get("last_name") or "").strip()
            full = f"{fn} {ln}".strip()
            u = (data.get("username") or "").strip().lower().lstrip("@")
            if fn:
                self._name_to_seller_cache[fn.lower()] = (now, data)
                fn_norm = normalize_seller_name(fn)
                if fn_norm:
                    self._name_to_seller_cache[fn_norm] = (now, data)
            if full:
                self._name_to_seller_cache[full.lower()] = (now, data)
                full_norm = normalize_seller_name(full)
                if full_norm:
                    self._name_to_seller_cache[full_norm] = (now, data)
            if u:
                self._name_to_seller_cache[u] = (now, data)
            items_to_insert.append((seller_id, json.dumps(data, ensure_ascii=False), now))

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=30000;")
                await db.executemany(
                    """
                    INSERT INTO seller_cache (seller_id, data, cached_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(seller_id) DO UPDATE SET
                        data = excluded.data,
                        cached_at = excluded.cached_at
                    """,
                    items_to_insert,
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"Ошибка сохранения seller_cache bulk ({len(sellers)} шт.): {e}")

    async def save_gift_registry(self, slug: str, owner_id: int, data: Dict[str, Any]) -> None:
        """Сохраняет связь slug -> владелец в реестр подарков для 100% точного обхода скрытия профилей."""
        if not slug or not owner_id:
            return
        if not hasattr(self, "_gift_registry_cache"):
            self._gift_registry_cache = {}
        self._gift_registry_cache[slug] = data
        data_str = json.dumps(data, ensure_ascii=False)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=10000;")
                await db.execute(
                    """
                    INSERT INTO gift_registry (slug, owner_id, data, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(slug) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        data = excluded.data,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (slug, owner_id, data_str),
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"Ошибка сохранения в gift_registry ({slug}): {e}")

    async def get_gift_registry(self, slug: str) -> Optional[Dict[str, Any]]:
        """Ищет владельца подарка по его slug в реестре подарков (ОЗУ + SQLite)."""
        if not slug:
            return None
        if hasattr(self, "_gift_registry_cache") and slug in self._gift_registry_cache:
            return self._gift_registry_cache[slug]
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=10000;")
                async with db.execute(
                    "SELECT data FROM gift_registry WHERE slug = ? LIMIT 1",
                    (slug,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        parsed = json.loads(row[0])
                        if not hasattr(self, "_gift_registry_cache"):
                            self._gift_registry_cache = {}
                        self._gift_registry_cache[slug] = parsed
                        return parsed
        except Exception as e:
            logger.debug(f"Ошибка чтения из gift_registry ({slug}): {e}")
        return None

    # ------------------ Автоматический реестр апгрейдов (scanned_upgrades) ------------------

    async def save_scanned_upgrade(
        self,
        slug: str,
        short_name: str,
        num: int,
        model: Optional[str] = None,
        model_rarity: Optional[str] = None,
        backdrop: Optional[str] = None,
        pattern: Optional[str] = None,
        owner_id: Optional[int] = None,
        owner_name: Optional[str] = None,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        level: Optional[int] = None,
        nft_gifts_count: Optional[int] = None,
        raw_data: Optional[str] = None,
    ) -> None:
        """Сохраняет каждый обнаруженный при скане апгрейд в таблицу scanned_upgrades."""
        if not slug or not short_name:
            return
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=10000;")
                await db.execute(
                    """
                    INSERT INTO scanned_upgrades (
                        slug, short_name, num, model, model_rarity, backdrop, pattern,
                        owner_id, owner_name, username, first_name, level, nft_gifts_count,
                        raw_data, scanned_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(slug) DO UPDATE SET
                        model = excluded.model,
                        model_rarity = excluded.model_rarity,
                        backdrop = excluded.backdrop,
                        pattern = excluded.pattern,
                        owner_id = COALESCE(excluded.owner_id, scanned_upgrades.owner_id),
                        owner_name = COALESCE(excluded.owner_name, scanned_upgrades.owner_name),
                        username = COALESCE(excluded.username, scanned_upgrades.username),
                        first_name = COALESCE(excluded.first_name, scanned_upgrades.first_name),
                        level = COALESCE(excluded.level, scanned_upgrades.level),
                        nft_gifts_count = COALESCE(excluded.nft_gifts_count, scanned_upgrades.nft_gifts_count),
                        raw_data = COALESCE(excluded.raw_data, scanned_upgrades.raw_data)
                    """,
                    (
                        slug, short_name, num, model, model_rarity, backdrop, pattern,
                        owner_id, owner_name, username, first_name, level, nft_gifts_count,
                        raw_data,
                    ),
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"Ошибка сохранения в scanned_upgrades ({slug}): {e}")

    async def get_scanned_upgrades_count(self) -> int:
        """Возвращает общее количество зафиксированных апгрейдов."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=10000;")
                async with db.execute("SELECT COUNT(*) FROM scanned_upgrades") as cur:
                    row = await cur.fetchone()
                    return row[0] if row else 0
        except Exception:
            return 0

    async def get_scanned_upgrade(self, slug: str) -> Optional[Dict[str, Any]]:
        """Возвращает информацию о сохраненном апгрейде по slug."""
        if not slug:
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=10000;")
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM scanned_upgrades WHERE slug = ?", (slug,)) as cur:
                    row = await cur.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.debug(f"Ошибка чтения из scanned_upgrades ({slug}): {e}")
            return None

    # ------------------ История лотов (listing_history) ------------------

    async def process_listing_history(
        self, listing_key: str, price_stars: int, stale_minutes_threshold: int
    ) -> Tuple[bool, bool]:
        """
        Обрабатывает историю цен лота.
        Возвращает (motivated, stale).
        - motivated: True, если текущая цена < first_seen_price (хоть раз снижалась)
        - stale: True, если (now - first_seen_at) >= stale_minutes_threshold и цена не менялась (last_price == first_seen_price)
        """
        now = time.time()
        stale_seconds = stale_minutes_threshold * 60

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT first_seen_at, first_seen_price, last_price FROM listing_history WHERE listing_key = ?",
                (listing_key,),
            ) as cursor:
                row = await cursor.fetchone()

            if row is None:
                # 3.a Если listing_key нет в listing_history — вставить
                await db.execute(
                    """
                    INSERT INTO listing_history (listing_key, first_seen_at, first_seen_price, last_price)
                    VALUES (?, ?, ?, ?)
                    """,
                    (listing_key, now, price_stars, price_stars),
                )
                await db.commit()
                return False, False
            else:
                first_seen_at, first_seen_price, last_price = row
                try:
                    ts = float(first_seen_at)
                except (ValueError, TypeError):
                    ts = now

                # 3.b motivated = True, если текущая price_stars < first_seen_price (хоть раз снижалась)
                motivated = price_stars < first_seen_price

                # 3.c stale = True, если (now - first_seen_at) >= stale_minutes_threshold и цена не менялась
                price_never_changed = (last_price == first_seen_price) and (price_stars == first_seen_price)
                stale = ((now - ts) >= stale_seconds) and price_never_changed

                # Обновить last_price на текущую цену
                await db.execute(
                    "UPDATE listing_history SET last_price = ? WHERE listing_key = ?",
                    (price_stars, listing_key),
                )
                await db.commit()
                return motivated, stale

    # ------------------ Модели и отслеживание апгрейдов ------------------

    async def upsert_tracked_gift_model(
        self, short_name: str, gift_id: int, active: bool = True
    ) -> None:
        """Сохраняет или обновляет модель подарка в tracked_gift_models."""
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                """
                INSERT INTO tracked_gift_models (short_name, gift_id, last_seen_in_catalog, active)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(short_name) DO UPDATE SET
                    gift_id = excluded.gift_id,
                    last_seen_in_catalog = excluded.last_seen_in_catalog,
                    active = excluded.active
                """,
                (short_name, gift_id, now, int(active)),
            )
            await db.commit()

    async def get_active_tracked_models(self) -> List[Dict[str, Any]]:
        """Возвращает список активных моделей для отслеживания апгрейдов."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT short_name, gift_id FROM tracked_gift_models WHERE active = 1 ORDER BY short_name ASC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [{"short_name": r[0], "gift_id": r[1]} for r in rows]

    async def get_upgrade_last_num(self, short_name: str) -> Optional[int]:
        """Возвращает последний известный номер num для модели из upgrade_tracking."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT last_known_num FROM upgrade_tracking WHERE short_name = ?",
                (short_name,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is not None:
                    return row[0]
        return None

    async def set_upgrade_last_num(self, short_name: str, last_known_num: int) -> None:
        """Сохраняет или обновляет последний известный номер num для модели."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                """
                INSERT INTO upgrade_tracking (short_name, last_known_num)
                VALUES (?, ?)
                ON CONFLICT(short_name) DO UPDATE SET
                    last_known_num = excluded.last_known_num
                """,
                (short_name, last_known_num),
            )
            await db.commit()
    async def mark_collection_excluded(
        self, gift_id: int, title: str = "", floor_stars: int = 0
    ) -> None:
        """Навсегда исключает коллекцию из ротации мониторинга (флор > max_collection_floor)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                """
                INSERT INTO excluded_collections (gift_id, title, floor_stars, excluded_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(gift_id) DO UPDATE SET
                    title = excluded.title,
                    floor_stars = excluded.floor_stars,
                    excluded_at = CURRENT_TIMESTAMP
                """,
                (gift_id, title, floor_stars),
            )
            await db.commit()

    async def get_excluded_collection_ids(self, max_coll_floor: Optional[int] = None) -> set:
        """Возвращает множество ID навсегда исключенных коллекций."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            if max_coll_floor is not None:
                # Очищаем коллекции, которые теперь укладываются в повышенный лимит флора
                await db.execute("DELETE FROM excluded_collections WHERE floor_stars <= ?", (max_coll_floor,))
                await db.commit()
            async with db.execute("SELECT gift_id FROM excluded_collections") as cursor:
                rows = await cursor.fetchall()
                return {r[0] for r in rows}

    # ------------------ Занятие лотов (Claim system) ------------------

    async def record_outbox_dispatch(
        self, outbox_id: int, chat_id: int, message_id: int
    ) -> None:
        """Сохраняет связку отправленного сообщения подписчику для последующего обновления кнопок."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                """
                INSERT INTO outbox_dispatches (outbox_id, chat_id, message_id)
                VALUES (?, ?, ?)
                ON CONFLICT(outbox_id, chat_id) DO UPDATE SET message_id = excluded.message_id
                """,
                (outbox_id, chat_id, message_id),
            )
            await db.commit()

    async def get_outbox_dispatches(self, outbox_id: int) -> List[Tuple[int, int]]:
        """Возвращает все (chat_id, message_id) для указанного outbox_id."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT chat_id, message_id FROM outbox_dispatches WHERE outbox_id = ?",
                (outbox_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [(r[0], r[1]) for r in rows]

    async def claim_lot(
        self, outbox_id: int, lot_key: str, user_id: int, username: Optional[str], name: str
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Атомарная попытка занять лот.
        Возвращает (True, claim_data), если лот успешно занят текущим пользователем.
        Возвращает (False, existing_claim_data), если лот уже был занят кем-то ранее.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            # 1. Проверяем, не занят ли лот кем-то
            async with db.execute(
                "SELECT claimed_by, claimed_username, claimed_name, claimed_at FROM claimed_lots WHERE outbox_id = ?",
                (outbox_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return False, {
                        "claimed_by": row[0],
                        "username": row[1] or "",
                        "name": row[2] or "",
                        "claimed_at": str(row[3]),
                    }

            # 2. Пытаемся занять
            try:
                await db.execute(
                    """
                    INSERT INTO claimed_lots (outbox_id, lot_key, claimed_by, claimed_username, claimed_name)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (outbox_id, lot_key, user_id, username, name),
                )
                await db.commit()
                return True, {
                    "claimed_by": user_id,
                    "username": username or "",
                    "name": name or "",
                }
            except Exception:
                # В случае race condition (одновременных кликов) читаем победителя
                async with db.execute(
                    "SELECT claimed_by, claimed_username, claimed_name, claimed_at FROM claimed_lots WHERE outbox_id = ?",
                    (outbox_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return False, {
                            "claimed_by": row[0],
                            "username": row[1] or "",
                            "name": row[2] or "",
                            "claimed_at": str(row[3]),
                        }
                return False, {"claimed_by": 0, "username": "unknown", "name": "unknown"}

    async def get_claim(self, outbox_id: int) -> Optional[Dict[str, Any]]:
        """Возвращает информацию о занятии лота, если он занят."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT claimed_by, claimed_username, claimed_name, claimed_at FROM claimed_lots WHERE outbox_id = ?",
                (outbox_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "claimed_by": row[0],
                        "username": row[1] or "",
                        "name": row[2] or "",
                        "claimed_at": str(row[3]),
                    }
        return None

    async def get_outbox_lot(self, outbox_id: int) -> Optional[LotItem]:
        """Возвращает LotItem по id из outbox."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT payload FROM outbox WHERE id = ?", (outbox_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return LotItem.from_json(row[0])
        return None

    async def get_user_claimed_lots(
        self, user_id: int, limit: int = 15
    ) -> List[Tuple[Dict[str, Any], LotItem]]:
        """Возвращает список лотов, занятых пользователем (свежие сначала)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                """
                SELECT c.outbox_id, c.lot_key, c.claimed_at, o.payload
                FROM claimed_lots c
                JOIN outbox o ON c.outbox_id = o.id
                WHERE c.claimed_by = ?
                ORDER BY c.outbox_id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    claim_meta = {
                        "outbox_id": r[0],
                        "lot_key": r[1],
                        "claimed_at": str(r[2]),
                    }
                    lot = LotItem.from_json(r[3])
                    results.append((claim_meta, lot))
                return results

    # ------------------ Пробный период и подписки ------------------

    async def activate_trial(self, chat_id: int, trial_hours: int) -> Tuple[bool, str]:
        """
        Активирует пробный период на trial_hours для пользователя.
        Возвращает (успех, сообщение/дата).
        """
        if trial_hours <= 0:
            return False, "Пробный период выключен"

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT has_used_trial, market_until, upgrades_until FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                return False, "Пользователь не найден"

            has_used, m_until, u_until = row
            if has_used:
                return False, "Вы уже использовали пробный период"

            trial_until = (
                datetime.now(timezone.utc) + timedelta(hours=trial_hours)
            ).strftime("%Y-%m-%d %H:%M:%S")

            await db.execute(
                """
                UPDATE subscribers
                SET has_used_trial = 1,
                    market_until = ?,
                    upgrades_until = ?,
                    mode = 'both',
                    market_reminder_sent = 0,
                    upgrades_reminder_sent = 0,
                    market_expired_sent = 0,
                    upgrades_expired_sent = 0,
                    active = 1
                WHERE chat_id = ?
                """,
                (trial_until, trial_until, chat_id),
            )
            await db.commit()
            return True, trial_until

    # ------------------ Заявки и платежи ------------------

    async def create_order(
        self,
        user_id: int,
        sub_type: str,
        duration: str,
        currency: str,
        amount: int,
        rate: Optional[float] = None,
        comment: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> int:
        """Создает заявку на оплату и возвращает order_id."""
        if not comment:
            from payments import generate_order_comment
            comment = generate_order_comment()
        if not expires_at:
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            cursor = await db.execute(
                """
                INSERT INTO orders (
                    user_id, sub_type, duration, currency, amount, rate, comment, status, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    user_id,
                    sub_type,
                    duration,
                    currency,
                    amount,
                    rate,
                    comment,
                    expires_at,
                ),
            )
            await db.commit()
            oid = cursor.lastrowid
        return await self.get_order(oid)

    async def get_order(self, order_id: Any) -> Optional[Order]:
        """Возвращает заявку по id."""
        if isinstance(order_id, Order):
            return order_id
        try:
            oid = int(order_id)
        except (ValueError, TypeError):
            return None

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                """
                SELECT id, user_id, sub_type, duration, currency, amount, rate, comment,
                       status, created_at, expires_at, paid_at, tx_hash
                FROM orders WHERE id = ?
                """,
                (oid,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return Order(
                        id=row[0],
                        user_id=row[1],
                        sub_type=row[2],
                        duration=row[3],
                        currency=row[4],
                        amount=row[5],
                        rate=row[6],
                        comment=row[7],
                        status=row[8],
                        created_at=str(row[9]),
                        expires_at=str(row[10]),
                        paid_at=str(row[11]) if row[11] else None,
                        tx_hash=str(row[12]) if row[12] else None,
                    )
        return None

    async def get_order_by_comment(self, comment: str) -> Optional[Order]:
        """Возвращает заявку по комментарию."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                """
                SELECT id, user_id, sub_type, duration, currency, amount, rate, comment,
                       status, created_at, expires_at, paid_at, tx_hash
                FROM orders WHERE comment = ?
                """,
                (comment,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return Order(
                        id=row[0],
                        user_id=row[1],
                        sub_type=row[2],
                        duration=row[3],
                        currency=row[4],
                        amount=row[5],
                        rate=row[6],
                        comment=row[7],
                        status=row[8],
                        created_at=str(row[9]),
                        expires_at=str(row[10]),
                        paid_at=str(row[11]) if row[11] else None,
                        tx_hash=str(row[12]) if row[12] else None,
                    )
        return None

    async def get_user_pending_order(self, user_id: int) -> Optional[Order]:
        """Возвращает актуальную pending заявку пользователя, если срок не истек."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                """
                SELECT id, user_id, sub_type, duration, currency, amount, rate, comment,
                       status, created_at, expires_at, paid_at, tx_hash
                FROM orders
                WHERE user_id = ? AND status = 'pending' AND expires_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, now),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return Order(
                        id=row[0],
                        user_id=row[1],
                        sub_type=row[2],
                        duration=row[3],
                        currency=row[4],
                        amount=row[5],
                        rate=row[6],
                        comment=row[7],
                        status=row[8],
                        created_at=str(row[9]),
                        expires_at=str(row[10]),
                        paid_at=str(row[11]) if row[11] else None,
                        tx_hash=str(row[12]) if row[12] else None,
                    )
        return None

    async def cancel_order(self, order_id: Any, user_id: int) -> bool:
        """Отменяет заявку пользователя."""
        order_id = getattr(order_id, "id", order_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            cursor = await db.execute(
                "UPDATE orders SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'pending'",
                (order_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def mark_order_expired(self, order_id: Any) -> None:
        """Помечает заявку как истекшую."""
        order_id = getattr(order_id, "id", order_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "UPDATE orders SET status = 'expired' WHERE id = ? AND status = 'pending'",
                (order_id,),
            )
            await db.commit()

    async def is_tx_processed(self, tx_hash: str) -> bool:
        """Проверяет, был ли tx_hash уже обработан (включая проигнорированные сторонние транзакции)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                """
                SELECT 1 FROM processed_payments WHERE tx_hash = ?
                UNION
                SELECT 1 FROM unmatched_payments WHERE tx_hash = ?
                UNION
                SELECT 1 FROM ignored_payments WHERE tx_hash = ?
                """,
                (tx_hash, tx_hash, tx_hash),
            ) as cursor:
                if await cursor.fetchone():
                    return True
        return False

    is_payment_processed = is_tx_processed

    async def mark_tx_ignored(self, tx_hash: str) -> None:
        """Помечает стороннюю транзакцию (личный перевод владельца, спам) как проигнорированную."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "INSERT OR IGNORE INTO ignored_payments (tx_hash) VALUES (?)",
                (tx_hash,),
            )
            await db.commit()

    async def record_unmatched_payment(
        self, tx_hash: str, currency: str, amount: int, comment: str, reason: str
    ) -> bool:
        """Сохраняет запись о несопоставленном или ошибочном платеже."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO unmatched_payments (tx_hash, currency, amount, comment, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tx_hash, currency, amount, comment, reason),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def process_successful_payment(
        self,
        order_id: Any,
        tx_hash: str,
        actual_amount: int,
        currency: str,
    ) -> Tuple[bool, str, Optional[Subscriber]]:
        """
        Зачисляет оплату по заявке в ОДНОЙ транзакции БД.
        Возвращает (success, message, updated_subscriber).
        """
        order_id = getattr(order_id, "id", order_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute("BEGIN IMMEDIATE;")
            try:
                # 1. Проверяем, не зачислен ли уже этот tx_hash
                async with db.execute(
                    "SELECT 1 FROM processed_payments WHERE tx_hash = ?", (tx_hash,)
                ) as cursor:
                    if await cursor.fetchone():
                        await db.execute("ROLLBACK;")
                        return False, "Платёж с таким хэшем уже был зачислен", None

                # 2. Получаем заявку
                async with db.execute(
                    """
                    SELECT id, user_id, sub_type, duration, currency, amount, comment, status, expires_at
                    FROM orders WHERE id = ?
                    """,
                    (order_id,),
                ) as cursor:
                    order_row = await cursor.fetchone()

                if not order_row:
                    await db.execute("ROLLBACK;")
                    return False, "Заявка не найдена", None

                o_id, u_id, sub_type, duration, o_curr, o_amount, o_comment, status, exp_at = order_row

                if status != "pending":
                    await db.execute("ROLLBACK;")
                    return False, f"Заявка уже имеет статус '{status}'", None

                # Проверяем, не истекла ли заявка
                now = datetime.now(timezone.utc)
                exp_dt = datetime.fromisoformat(exp_at)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if now > exp_dt:
                    await db.execute("UPDATE orders SET status = 'expired' WHERE id = ?", (order_id,))
                    await db.commit()
                    return False, "Срок действия заявки истёк", None

                # Проверяем валюту и сумму
                if currency != o_curr:
                    await db.execute("ROLLBACK;")
                    return False, f"Несовпадение валюты (ожидалась {o_curr}, получена {currency})", None

                if actual_amount < o_amount:
                    await db.execute("ROLLBACK;")
                    return False, f"Недоплата (ожидалось {o_amount}, получено {actual_amount})", None

                # 3. Записываем в processed_payments
                await db.execute(
                    """
                    INSERT INTO processed_payments (tx_hash, order_id, user_id, currency, amount, comment)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (tx_hash, order_id, u_id, currency, actual_amount, o_comment),
                )

                # 4. Обновляем статус заявки
                await db.execute(
                    """
                    UPDATE orders
                    SET status = 'paid', paid_at = CURRENT_TIMESTAMP, tx_hash = ?
                    WHERE id = ?
                    """,
                    (tx_hash, order_id),
                )

                # 5. Получаем текущие сроки подписчика
                async with db.execute(
                    """
                    SELECT market_until, upgrades_until, mode FROM subscribers WHERE chat_id = ?
                    """,
                    (u_id,),
                ) as cursor:
                    sub_row = await cursor.fetchone()

                curr_market = sub_row[0] if sub_row else None
                curr_upgrades = sub_row[1] if sub_row else None
                curr_mode = sub_row[2] if sub_row else "market"

                if sub_type == "team":
                    await db.execute(
                        """
                        UPDATE subscribers
                        SET has_team_access = 1,
                            active = 1
                        WHERE chat_id = ?
                        """,
                        (u_id,),
                    )
                else:
                    # Вычисляем новые сроки продления
                    new_market = curr_market
                    new_upgrades = curr_upgrades

                    if sub_type in ("market", "both"):
                        new_market = compute_extension(curr_market, duration, now)

                    if sub_type in ("upgrades", "both"):
                        new_upgrades = compute_extension(curr_upgrades, duration, now)

                    # Корректируем режим по умолчанию
                    new_mode = curr_mode
                    if sub_type == "both":
                        new_mode = "both"
                    elif sub_type == "market" and curr_mode not in ("market", "both"):
                        new_mode = "market"
                    elif sub_type == "upgrades" and curr_mode not in ("upgrades", "both"):
                        new_mode = "upgrades"

                    # Обновляем подписчика и сбрасываем флаги напоминаний
                    await db.execute(
                        """
                        UPDATE subscribers
                        SET market_until = ?,
                            upgrades_until = ?,
                            mode = ?,
                            market_reminder_sent = 0,
                            upgrades_reminder_sent = 0,
                            market_expired_sent = 0,
                            upgrades_expired_sent = 0,
                            active = 1
                        WHERE chat_id = ?
                        """,
                        (new_market, new_upgrades, new_mode, u_id),
                    )

                await db.commit()

                updated_sub = await self.get_subscriber(u_id)
                return True, "Оплата успешно зачислена", updated_sub

            except Exception as e:
                await db.execute("ROLLBACK;")
                logger.error(f"Ошибка при обработке платежа по заявке #{order_id}: {e}", exc_info=True)
                return False, str(e), None

    # ------------------ Напоминания и уведомления об окончании ------------------

    async def get_subscriptions_needing_reminder(self) -> List[Tuple[int, str, str]]:
        """
        Находит пользователей, чья подписка истекает менее чем через 24 часа,
        но еще не истекла, и напоминание еще не отправлялось.
        Возвращает [(chat_id, sub_type, until_str)].
        """
        results = []
        now = datetime.now(timezone.utc)
        in_24h = now + timedelta(hours=24)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        in_24h_str = in_24h.strftime("%Y-%m-%d %H:%M:%S")

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            # Маркет
            async with db.execute(
                """
                SELECT chat_id, market_until FROM subscribers
                WHERE active = 1
                  AND market_until IS NOT NULL
                  AND market_until != 'forever'
                  AND market_until > ?
                  AND market_until <= ?
                  AND (market_reminder_sent IS NULL OR market_reminder_sent = 0)
                """,
                (now_str, in_24h_str),
            ) as cursor:
                for cid, until in await cursor.fetchall():
                    results.append((cid, "market", str(until)))

            # Улучшения
            async with db.execute(
                """
                SELECT chat_id, upgrades_until FROM subscribers
                WHERE active = 1
                  AND upgrades_until IS NOT NULL
                  AND upgrades_until != 'forever'
                  AND upgrades_until > ?
                  AND upgrades_until <= ?
                  AND (upgrades_reminder_sent IS NULL OR upgrades_reminder_sent = 0)
                """,
                (now_str, in_24h_str),
            ) as cursor:
                for cid, until in await cursor.fetchall():
                    results.append((cid, "upgrades", str(until)))

        return results

    async def mark_reminder_sent(self, chat_id: int, sub_type: str) -> None:
        """Помечает напоминание как отправленное."""
        col = "market_reminder_sent" if sub_type == "market" else "upgrades_reminder_sent"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(f"UPDATE subscribers SET {col} = 1 WHERE chat_id = ?", (chat_id,))
            await db.commit()

    async def get_newly_expired_subscriptions(self) -> List[Tuple[int, str]]:
        """
        Находит пользователей, чья подписка уже истекла, но уведомление об окончании еще не отправлялось.
        Возвращает [(chat_id, sub_type)].
        """
        results = []
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            # Маркет
            async with db.execute(
                """
                SELECT chat_id FROM subscribers
                WHERE active = 1
                  AND market_until IS NOT NULL
                  AND market_until != 'forever'
                  AND market_until <= ?
                  AND (market_expired_sent IS NULL OR market_expired_sent = 0)
                """,
                (now_str,),
            ) as cursor:
                for (cid,) in await cursor.fetchall():
                    results.append((cid, "market"))

            # Улучшения
            async with db.execute(
                """
                SELECT chat_id FROM subscribers
                WHERE active = 1
                  AND upgrades_until IS NOT NULL
                  AND upgrades_until != 'forever'
                  AND upgrades_until <= ?
                  AND (upgrades_expired_sent IS NULL OR upgrades_expired_sent = 0)
                """,
                (now_str,),
            ) as cursor:
                for (cid,) in await cursor.fetchall():
                    results.append((cid, "upgrades"))

        return results

    async def mark_expired_sent(self, chat_id: int, sub_type: str) -> None:
        """Помечает уведомление об окончании как отправленное."""
        col = "market_expired_sent" if sub_type == "market" else "upgrades_expired_sent"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(f"UPDATE subscribers SET {col} = 1 WHERE chat_id = ?", (chat_id,))
            await db.commit()

    # ------------------ Настройки и админ-панель ------------------

    async def get_setting(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)) as c:
                row = await c.fetchone()
                return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            await db.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    async def admin_get_users(self, page: int = 1, page_size: int = 10) -> Tuple[List[Dict[str, Any]], int]:
        offset = max(0, (page - 1) * page_size)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute("SELECT COUNT(*) FROM subscribers") as c:
                total = (await c.fetchone())[0]

            async with db.execute(
                """
                SELECT chat_id, username, first_name, last_name, active, mode,
                       market_until, upgrades_until, joined_at
                FROM subscribers
                ORDER BY joined_at DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ) as c:
                rows = await c.fetchall()
                users = []
                for r in rows:
                    users.append({
                        "chat_id": r[0],
                        "username": r[1],
                        "first_name": r[2],
                        "last_name": r[3],
                        "active": bool(r[4]),
                        "mode": r[5] or "market",
                        "market_until": str(r[6]) if r[6] else None,
                        "upgrades_until": str(r[7]) if r[7] else None,
                        "joined_at": str(r[8]) if r[8] else "",
                    })
                return users, total

    async def admin_search_user(self, query: str) -> Optional[Dict[str, Any]]:
        clean_q = query.strip().lstrip("@")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            if clean_q.isdigit():
                async with db.execute(
                    """
                    SELECT chat_id, username, first_name, last_name, active, mode,
                           market_until, upgrades_until, joined_at
                    FROM subscribers WHERE chat_id = ?
                    """,
                    (int(clean_q),),
                ) as c:
                    r = await c.fetchone()
                    if r:
                        return {
                            "chat_id": r[0],
                            "username": r[1],
                            "first_name": r[2],
                            "last_name": r[3],
                            "active": bool(r[4]),
                            "mode": r[5] or "market",
                            "market_until": str(r[6]) if r[6] else None,
                            "upgrades_until": str(r[7]) if r[7] else None,
                            "joined_at": str(r[8]) if r[8] else "",
                        }

            async with db.execute(
                """
                SELECT chat_id, username, first_name, last_name, active, mode,
                       market_until, upgrades_until, joined_at
                FROM subscribers WHERE LOWER(username) = LOWER(?)
                """,
                (clean_q,),
            ) as c:
                r = await c.fetchone()
                if r:
                    return {
                        "chat_id": r[0],
                        "username": r[1],
                        "first_name": r[2],
                        "last_name": r[3],
                        "active": bool(r[4]),
                        "mode": r[5] or "market",
                        "market_until": str(r[6]) if r[6] else None,
                        "upgrades_until": str(r[7]) if r[7] else None,
                        "joined_at": str(r[8]) if r[8] else "",
                    }
        return None

    async def admin_get_user_details(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                """
                SELECT chat_id, username, first_name, last_name, active, mode,
                       market_until, upgrades_until, joined_at, has_used_trial, upgrade_filter
                FROM subscribers WHERE chat_id = ?
                """,
                (user_id,),
            ) as c:
                r = await c.fetchone()
                if not r:
                    return None

            async with db.execute(
                """
                SELECT currency, SUM(amount), COUNT(*)
                FROM processed_payments WHERE user_id = ?
                GROUP BY currency
                """,
                (user_id,),
            ) as c:
                pay_rows = await c.fetchall()
                total_ton = 0.0
                total_usdt = 0.0
                for curr, amt, _ in pay_rows:
                    if curr == "TON":
                        total_ton = (amt or 0) / 1_000_000_000
                    elif curr == "USDT":
                        total_usdt = (amt or 0) / 1_000_000

            return {
                "chat_id": r[0],
                "username": r[1],
                "first_name": r[2],
                "last_name": r[3],
                "active": bool(r[4]),
                "mode": r[5] or "market",
                "market_until": str(r[6]) if r[6] else None,
                "upgrades_until": str(r[7]) if r[7] else None,
                "joined_at": str(r[8]) if r[8] else "",
                "has_used_trial": bool(r[9]),
                "upgrade_filter": str(r[10]) if len(r) > 10 and r[10] else "all",
                "total_ton": total_ton,
                "total_usdt": total_usdt,
            }

    async def admin_grant_subscription(
        self, user_id: int, sub_type: str, duration: str
    ) -> Tuple[bool, str]:
        """Вручную выдает или продлевает подписку пользователю."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                "SELECT market_until, upgrades_until, mode FROM subscribers WHERE chat_id = ?",
                (user_id,),
            ) as c:
                row = await c.fetchone()

            if not row:
                await self.upsert_subscriber(user_id)
                curr_market = None
                curr_upgrades = None
                curr_mode = "market"
            else:
                curr_market, curr_upgrades, curr_mode = row

            now = datetime.now(timezone.utc)
            new_market = curr_market
            new_upgrades = curr_upgrades

            if sub_type in ("market", "both"):
                new_market = compute_extension(curr_market, duration, now)
            if sub_type in ("upgrades", "both"):
                new_upgrades = compute_extension(curr_upgrades, duration, now)

            new_mode = curr_mode
            if sub_type == "both":
                new_mode = "both"
            elif sub_type == "market" and curr_mode not in ("market", "both"):
                new_mode = "market"
            elif sub_type == "upgrades" and curr_mode not in ("upgrades", "both"):
                new_mode = "upgrades"

            await db.execute(
                """
                UPDATE subscribers
                SET market_until = ?,
                    upgrades_until = ?,
                    mode = ?,
                    market_reminder_sent = 0,
                    upgrades_reminder_sent = 0,
                    market_expired_sent = 0,
                    upgrades_expired_sent = 0,
                    active = 1
                WHERE chat_id = ?
                """,
                (new_market, new_upgrades, new_mode, user_id),
            )
            await db.commit()
            return True, f"Маркет: {new_market or '—'}, Улучшения: {new_upgrades or '—'}"

    async def admin_revoke_subscription(self, user_id: int, sub_type: str) -> bool:
        """Снимает подписку у пользователя."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            if sub_type == "market":
                await db.execute(
                    "UPDATE subscribers SET market_until = NULL, market_reminder_sent = 0, market_expired_sent = 0 WHERE chat_id = ?",
                    (user_id,),
                )
            elif sub_type == "upgrades":
                await db.execute(
                    "UPDATE subscribers SET upgrades_until = NULL, upgrades_reminder_sent = 0, upgrades_expired_sent = 0 WHERE chat_id = ?",
                    (user_id,),
                )
            elif sub_type == "both":
                await db.execute(
                    """
                    UPDATE subscribers
                    SET market_until = NULL,
                        upgrades_until = NULL,
                        market_reminder_sent = 0,
                        upgrades_reminder_sent = 0,
                        market_expired_sent = 0,
                        upgrades_expired_sent = 0
                    WHERE chat_id = ?
                    """,
                    (user_id,),
                )
            await db.commit()
            return True

    async def admin_get_orders(self, page: int = 1, page_size: int = 10) -> Tuple[List[Dict[str, Any]], int]:
        offset = max(0, (page - 1) * page_size)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute("SELECT COUNT(*) FROM orders") as c:
                total = (await c.fetchone())[0]

            async with db.execute(
                """
                SELECT id, user_id, sub_type, duration, currency, amount, rate, comment,
                       status, created_at, expires_at, paid_at, tx_hash
                FROM orders
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ) as c:
                rows = await c.fetchall()
                orders = []
                for r in rows:
                    o = Order(
                        id=r[0],
                        user_id=r[1],
                        sub_type=r[2],
                        duration=r[3],
                        currency=r[4],
                        amount=r[5],
                        rate=r[6],
                        comment=r[7],
                        status=r[8],
                        created_at=str(r[9]),
                        expires_at=str(r[10]),
                        paid_at=str(r[11]) if r[11] else None,
                        tx_hash=str(r[12]) if r[12] else None,
                    )
                    orders.append({
                        "order": o,
                        "human_amount": o.human_amount,
                        "sub_title": o.sub_title,
                        "duration_title": o.duration_title,
                    })
                return orders, total

    async def admin_get_stats(self) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            async with db.execute("SELECT COUNT(*) FROM subscribers") as c:
                total_users = (await c.fetchone())[0]

            async with db.execute(
                """
                SELECT COUNT(*) FROM subscribers
                WHERE (market_until = 'forever' OR market_until > ?)
                """,
                (now_str,),
            ) as c:
                active_market = (await c.fetchone())[0]

            async with db.execute(
                """
                SELECT COUNT(*) FROM subscribers
                WHERE (upgrades_until = 'forever' OR upgrades_until > ?)
                """,
                (now_str,),
            ) as c:
                active_upgrades = (await c.fetchone())[0]

            async with db.execute(
                """
                SELECT COUNT(*) FROM subscribers
                WHERE (market_until = 'forever' OR market_until > ?)
                  AND (upgrades_until = 'forever' OR upgrades_until > ?)
                """,
                (now_str, now_str),
            ) as c:
                active_both = (await c.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM orders") as c:
                total_orders = (await c.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM orders WHERE status = 'paid'") as c:
                paid_orders = (await c.fetchone())[0]

            async with db.execute(
                "SELECT SUM(amount) FROM processed_payments WHERE currency = 'TON'"
            ) as c:
                ton_row = await c.fetchone()
                ton_sum = ((ton_row[0] or 0) if ton_row else 0) / 1_000_000_000

            async with db.execute(
                "SELECT SUM(amount) FROM processed_payments WHERE currency = 'USDT'"
            ) as c:
                usdt_row = await c.fetchone()
                usdt_sum = ((usdt_row[0] or 0) if usdt_row else 0) / 1_000_000

            async with db.execute(
                """
                SELECT COUNT(*) FROM subscribers
                WHERE (market_until = 'forever' OR market_until > ?)
                   OR (upgrades_until = 'forever' OR upgrades_until > ?)
                """,
                (now_str, now_str),
            ) as c:
                active_subscribers = (await c.fetchone())[0]

            return {
                "total_users": total_users,
                "active_subscribers": active_subscribers,
                "active_market": active_market,
                "active_upgrades": active_upgrades,
                "active_both": active_both,
                "total_orders": total_orders,
                "paid_orders": paid_orders,
                "revenue_ton": ton_sum,
                "revenue_usdt": usdt_sum,
            }

    async def get_team_orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Возвращает список заявок на вступление в закрытую тиму с данными пользователей."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout=10000;")
            async with db.execute(
                """
                SELECT o.id, o.user_id, o.sub_type, o.duration, o.currency, o.amount, o.rate, o.comment,
                       o.status, o.created_at, o.expires_at, o.paid_at, o.tx_hash,
                       s.username, s.first_name, s.last_name
                FROM orders o
                LEFT JOIN subscribers s ON o.user_id = s.chat_id
                WHERE o.sub_type = 'team'
                ORDER BY o.id DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    o = Order(
                        id=r[0],
                        user_id=r[1],
                        sub_type=r[2],
                        duration=r[3],
                        currency=r[4],
                        amount=r[5],
                        rate=r[6],
                        comment=r[7],
                        status=r[8],
                        created_at=str(r[9]),
                        expires_at=str(r[10]),
                        paid_at=str(r[11]) if r[11] else None,
                        tx_hash=str(r[12]) if r[12] else None,
                    )
                    results.append({
                        "order": o,
                        "username": r[13],
                        "first_name": r[14],
                        "last_name": r[15],
                    })
                return results

