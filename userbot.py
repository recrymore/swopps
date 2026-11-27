import asyncio
from contextlib import asynccontextmanager
import html
import json
import logging
import random
import re
import sys
import unicodedata
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any

from telethon import TelegramClient, errors
from telethon.tl import functions, types

from config import load_config, AppConfig, TelegramAccountConfig
from database import Database, normalize_seller_name, verify_seller_name, has_gift_on_profile, find_best_profile_gift
from models import LotItem, is_black_or_onyx_backdrop

# Ensure UTF-8 output
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [USERBOT] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("userbot")
logging.getLogger("telethon").setLevel(logging.WARNING)

SYSTEM_RESERVED_NAMES = {
    "unlimited", "telegram", "channel", "bot", "anonymous",
    "admin", "support", "help", "contact", "gifts", "stargift",
    "fragment", "none", "unknown", "null", "undefined"
}


def parse_attributes(
    attributes: Optional[List[Any]],
) -> Tuple[str, str, str, str]:
    """Извлекает модель, редкость модели, фон и узор из списка атрибутов starGiftUnique."""
    model_name = "—"
    model_rarity = "Обычная"
    backdrop_name = "—"
    pattern_name = "—"

    if not attributes:
        return model_name, model_rarity, backdrop_name, pattern_name

    for attr in attributes:
        if isinstance(attr, types.StarGiftAttributeModel):
            model_name = getattr(attr, "name", "—")
            rarity_obj = getattr(attr, "rarity", None)
            if rarity_obj:
                if isinstance(rarity_obj, types.StarGiftAttributeRarityUncommon):
                    model_rarity = "Uncommon"
                elif isinstance(rarity_obj, types.StarGiftAttributeRarityRare):
                    model_rarity = "Rare"
                elif isinstance(rarity_obj, types.StarGiftAttributeRarityEpic):
                    model_rarity = "Epic"
                elif isinstance(rarity_obj, types.StarGiftAttributeRarityLegendary):
                    model_rarity = "Legendary"
                elif isinstance(rarity_obj, types.StarGiftAttributeRarity):
                    pct = rarity_obj.permille / 10.0
                    model_rarity = f"{pct:g}%"
                elif hasattr(rarity_obj, "permille") and rarity_obj.permille is not None:
                    pct = rarity_obj.permille / 10.0
                    model_rarity = f"{pct:g}%"
        elif isinstance(attr, types.StarGiftAttributeBackdrop):
            backdrop_name = getattr(attr, "name", "—")
        elif isinstance(attr, types.StarGiftAttributePattern):
            pattern_name = getattr(attr, "name", "—")

    return model_name, model_rarity, backdrop_name, pattern_name


def parse_prices(
    resell_amount: Optional[List[Any]],
) -> Tuple[Optional[int], Optional[str]]:
    """Извлекает цену в Stars и цену в TON из списка resell_amount."""
    price_stars: Optional[int] = None
    price_ton: Optional[str] = None

    if not resell_amount:
        return price_stars, price_ton

    amounts = resell_amount if isinstance(resell_amount, list) else [resell_amount]
    for amt in amounts:
        if isinstance(amt, types.StarsTonAmount):
            # amount в нанотонах: 1 TON = 10^9 нанотонов
            try:
                ton_val = amt.amount / 1e9
                price_ton = f"{ton_val:.2f}"
            except Exception:
                pass
        elif isinstance(amt, types.StarsAmount):
            price_stars = amt.amount
        elif hasattr(amt, "stars"):
            price_stars = getattr(amt, "stars")
        elif hasattr(amt, "amount") and not hasattr(amt, "ton"):
            price_stars = getattr(amt, "amount")

    return price_stars, price_ton


def has_cyrillic(text: Optional[str]) -> bool:
    """Проверяет наличие символов кириллицы в строке."""
    if not text:
        return False
    return bool(re.search(r"[\u0400-\u04FF]", text))


# 1. Корни, которые однозначно указывают на трейдера/скам/ресейл/отзывы даже как подстрока:
TRADER_SUBSTRING_STEMS = (
    "гарант",     # гарант, гаранты, гарантов, гаранту, гарантом, гарант-сервис
    "garant",     # garant, garants
    "отзыв",      # отзыв, отзывы, отзывов, отзывник
    "review",     # review, reviews
    "ресейл",     # ресейл, ресейлер, ресейлеры
    "resell",     # resell, reseller, reselling
    "скуп",       # скуп, скупка, скупаю, скупщик, скупкаподарков
    "обменник",   # обменник, обменники
    "шмекер",     # шмекер
    "shmkr",      # shmkr
    "escrow",     # escrow
    "репутац",    # репутация, репутацией
    "reputation", # reputation
)

# 2. Точные слова/токены (проверяются с учетом границ слов и спецсимволов):
TRADER_TOKENS = {
    # Репутация и отзывы
    "реп", "репа", "репы", "репу", "rep", "reps", "+rep", "-rep", "vouch", "vouches",

    # Продажи / покупки / трейд
    "продажа", "продажи", "продаже", "продажу",
    "продам", "продаю", "продаем", "продается", "продаются",
    "sell", "seller", "sellers", "selling",
    "покупка", "покупки", "покупаю", "покупаем", "куплю", "купим", "buy", "buyer",
    "трейд", "трейдер", "трейдеры", "трейдинг", "trade", "trader", "trading",
    "обмен", "exchange",

    # Физы
    "физ", "физы", "физов", "физики", "физ-номер", "физномера", "физсим", "симки",

    # Ворк, дропы
    "ворк", "воркер", "воркеры", "воркаем", "work", "worker",
    "дроп", "дропы", "дроповод", "drop", "drops",

    # Каналы, прайсы, шопы, крипта, P2P
    "канал", "каналы", "канале", "channel", "channels", "тгк", "tgk",
    "прайс", "прайсы", "прайслист", "price", "prices",
    "шоп", "шопы", "шопе", "shop", "shops", "store", "stores",
    "p2p", "п2п", "crypto", "крипта", "крипт",
    "cash", "cashout", "кэш", "кэшаут", "ton",
}



def normalize_text(text: Optional[str]) -> str:
    """Нормализует текст: переводит стилизованные/математические юникод-шрифты в стандартный ASCII/кириллицу."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", str(text))
    return norm.lower()


# Регулярные выражения для поиска репутации, отзывов, цен, продаж даже в сложных связках (40rep, +rep, sell123):
TRADER_REGEXES = [
    re.compile(r'(?:\d+\s*rep|\+rep|rep\s*\+|vouch|\bреп[а-яё]*|\bотзыв[а-яё]*)', re.IGNORECASE),
    re.compile(r'(?:\b(?:sell|seller|selling|resell|reseller)\b|\bsell[a-z0-9_]+|[a-z0-9_]+sell\b)', re.IGNORECASE),
    re.compile(r'(?:\b(?:скуп|скупка|скупаю|скупщик)\b)', re.IGNORECASE),
    re.compile(r'(?:\b(?:трейд|трейдер|трейдинг|trade|trader|trading)\b)', re.IGNORECASE),
    re.compile(r'(?:\b(?:физ|физы|физов|физики|физ-номер|физномера|физсим|симки)\b)', re.IGNORECASE),
    re.compile(r'(?:\b(?:ворк|воркер|воркеры|воркаем|дроп|дропы|дроповод)\b)', re.IGNORECASE),
    re.compile(r'(?:\b(?:канал|каналы|тгк|tgk|шоп|шопы|shop|store|прайс|прайсы|прайслист|price|prices)\b)', re.IGNORECASE),
    re.compile(r'(?:\b(?:p2p|п2п|кэшаут|cashout)\b)', re.IGNORECASE),
]

USERNAME_TRADER_STEMS = (
    "sell", "buyer", "resell", "trade", "garant", "escrow", "otzyv", "skup", "mrkt", "market", "shop", "store", "crypto", "drop", "work", "vouch"
)


def is_trader_or_scam(
    first_name: str, last_name: str, about: str, username: Optional[str] = None
) -> bool:
    """Проверяет наличие признаков ресейлера, трейдера или скам-отзывов в имени, био и юзернейме."""
    # 0. Запрет на '@' в имени/фамилии (реклама каналов/ботов/картелей в никнейме), кроме разрешённой приписки @RUBUP
    fn_check = re.sub(r'(?i)@\s*rubup\b', '', first_name or '')
    ln_check = re.sub(r'(?i)@\s*rubup\b', '', last_name or '')
    if ('@' in fn_check) or ('@' in ln_check):
        return True

    # 1. Запрет на ссылки на каналы/боты/шопы в био (t.me/..., http://..., https://...)
    ab_lower = (about or '').lower()
    if 't.me/' in ab_lower or 'http://' in ab_lower or 'https://' in ab_lower:
        return True

    fn_norm = normalize_text(first_name)
    ln_norm = normalize_text(last_name)
    ab_norm = normalize_text(about)
    un_norm = normalize_text(username).lstrip('@')

    full_text = f"{fn_norm} {ln_norm} {ab_norm} {un_norm}"

    # 2. Торговые картели, снайпер-боты, биржи и шопы (RUBUP разрешён пользователем)
    CARTEL_KEYWORDS = (
        "gates", "giftsdouble", "gift_satellite", "satellite_bot",
        "starsgrain", "devil metro", "metro-rent", "snake_dice", "ksiva.net", "ксива"
    )
    for kw in CARTEL_KEYWORDS:
        if kw in full_text:
            return True

    # 3. Быстрая проверка однозначных корней подстрок
    for stem in TRADER_SUBSTRING_STEMS:
        if stem in full_text:
            return True

    # 4. Проверка юзернейма на торговые маркеры (sellmoder, giftsell, otzyv_market, etc.)
    for st in USERNAME_TRADER_STEMS:
        if st in un_norm:
            return True

    # 5. Проверка через регулярные выражения (40rep, +rep, sell...)
    for rx in TRADER_REGEXES:
        if rx.search(full_text):
            return True

    # 6. Токенизация текста с разделителями (пробелы, эмодзи, |, $, @, #, !, и др.)
    tokens = set(re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9\+\-]+', full_text))
    if tokens & TRADER_TOKENS:
        return True

    return False


DEVICE_PROFILES: List[Dict[str, str]] = [
    {
        "device_model": "Desktop",
        "system_version": "Windows 11 Home 64bit",
        "app_version": "5.6.3 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "Desktop",
        "system_version": "Windows 10 Pro 64bit",
        "app_version": "5.6.1 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "Desktop",
        "system_version": "Windows 11 Pro 64bit",
        "app_version": "5.5.5 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "Desktop",
        "system_version": "Windows 10 Enterprise",
        "app_version": "5.6.0 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
]


def get_client_profile(idx: int) -> Dict[str, str]:
    return DEVICE_PROFILES[(idx - 1) % len(DEVICE_PROFILES)]


class ReentrantAsyncLock:
    """Асинхронный реентерабельный замок для предотвращения взаимных блокировок (deadlocks) при вложенных вызовах."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._count = 0

    async def acquire(self) -> None:
        me = asyncio.current_task()
        if self._owner == me:
            self._count += 1
            return
        await self._lock.acquire()
        self._owner = me
        self._count = 1

    def release(self) -> None:
        me = asyncio.current_task()
        if self._owner != me:
            raise RuntimeError(f"Lock release attempted by non-owner: {me} != {self._owner}")
        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


class AccountWorker:
    """Представляет один клиентский аккаунт Telegram в пуле воркеров с защитой от бана."""

    def __init__(self, idx: int, session_name: str, api_id: int, api_hash: str):
        self.idx = idx
        self.session_name = session_name
        self.api_id = api_id
        self.api_hash = api_hash
        profile = get_client_profile(idx)
        self.client = TelegramClient(
            session_name,
            api_id,
            api_hash,
            device_model=profile["device_model"],
            system_version=profile["system_version"],
            app_version=profile["app_version"],
            lang_code=profile["lang_code"],
            system_lang_code=profile["system_lang_code"],
        )
        self.user: Optional[Any] = None
        self._market_throttle_lock = ReentrantAsyncLock()
        self._market_flood_wait_until = 0.0
        self._search_flood_wait_until = 0.0
        self._profile_flood_wait_until = 0.0
        self._slug_flood_wait_until = 0.0
        self._flood_wait_until = 0.0
        self._last_call_time = 0.0
        self._call_lock = ReentrantAsyncLock()
        self._full_user_lock = ReentrantAsyncLock()
        self._last_profile_call_time = 0.0
        self._last_upgrade_call_time = 0.0
        self.consecutive_floods = 0
        self.adaptive_delay = 0.25
        self.consecutive_success = 0

    @asynccontextmanager
    async def call_slot(self, min_interval: float = 0.55, category: str = "general"):
        """Гарантирует атомарность любого MTProto запроса на данном аккаунте:
        - Исключает параллельные вызовы через один TCP-сокет Telethon
        - Строго выдерживает паузу min_interval ПОСЛЕ завершения предыдущего запроса
        - Автоматически пережидает FloodWait конкретной категории запроса
        """
        async with self._call_lock:
            if not self.client.is_connected():
                try:
                    await asyncio.wait_for(self.client.connect(), timeout=5.0)
                except Exception:
                    pass
            now = asyncio.get_running_loop().time()
            if category == "market":
                fw_until = self._market_flood_wait_until
            elif category == "search":
                fw_until = self._search_flood_wait_until
            elif category == "profile":
                fw_until = self._profile_flood_wait_until
            elif category == "slug":
                fw_until = self._slug_flood_wait_until
            else:
                fw_until = self._flood_wait_until

            if fw_until > now:
                wait_sec = fw_until - now
                if category != "market" and wait_sec > 30:
                    # Вспомогательный запрос не должен блокировать сокет воркера на часы
                    raise errors.FloodWaitError(int(wait_sec))
                await asyncio.sleep(wait_sec)
                now = asyncio.get_running_loop().time()
            elapsed = now - self._last_call_time
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            try:
                yield
            finally:
                self._last_call_time = asyncio.get_running_loop().time()

    async def acquire_call_slot(self, min_interval: float = 0.55, category: str = "general") -> None:
        """Гарантирует, что между любыми MTProto запросами этого аккаунта проходит не менее min_interval секунд."""
        async with self._call_lock:
            now = asyncio.get_running_loop().time()
            fw_until = self._market_flood_wait_until if category == "market" else self._flood_wait_until
            if fw_until > now:
                await asyncio.sleep(fw_until - now)
                now = asyncio.get_running_loop().time()
            elapsed = now - self._last_call_time
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            self._last_call_time = asyncio.get_running_loop().time()


class UserbotMonitor:
    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.workers: List[AccountWorker] = []
        self.known_collection_ids: List[int] = []
        self.priority_collection_ids: List[int] = []
        self.other_collection_ids: List[int] = []
        self._priority_cursor = 0
        self._other_cursor = 0
        self._interleave_flag = False
        self.collection_titles: Dict[int, str] = {}
        self.collection_floors: Dict[int, int] = {}
        self._collection_floor_time: Dict[int, float] = {}
        self.excluded_collection_ids: set = set()
        self.last_collections_update = 0.0
        self.upgrade_model_index = 0
        self.last_upgrade_catalog_refresh = 0.0
        self._min_market_call_interval = 0.04  # 40 мс между запросами маркета для одного воркера
        self._seller_cache_ram: Dict[int, Tuple[float, Dict[str, Any]]] = {}
        self._collection_cursor = 0
        self._cursor_lock = asyncio.Lock()
        self._stats_collections_polled = 0
        self._last_heartbeat_time = 0.0
        self._last_global_search_time = 0.0
        self._last_slug_probe_time = 0.0
        self._last_upg_log_time = 0.0
        self._last_successful_poll_time = 0.0
        self.unresolved_queue: asyncio.LifoQueue = asyncio.LifoQueue(maxsize=5000)
        self._queued_slugs: set = set()
        self._unresolvable_slugs: Dict[str, float] = {}

        # Инициализируем воркеров по списку аккаунтов из конфигурации
        accounts_to_use = self.config.accounts
        if not accounts_to_use and self.config.telegram_api_id and self.config.telegram_api_hash:
            accounts_to_use = [
                TelegramAccountConfig(
                    session_name=self.config.session_name,
                    api_id=self.config.telegram_api_id,
                    api_hash=self.config.telegram_api_hash,
                )
            ]

        for i, acc in enumerate(accounts_to_use, start=1):
            self.workers.append(AccountWorker(i, acc.session_name, acc.api_id, acc.api_hash))

    def deactivate_worker(self, w: AccountWorker, reason: str = "сессия отозвана") -> None:
        """Безопасно отключает и удаляет скомпрометированного воркера из пула."""
        logger.error(
            f"❌ [Воркер #{w.idx}] Сессия была отозвана Telegram (SessionRevokedError)! "
            f"Воркер отключен ({reason}). Для повторного входа выполните: py auth_account.py {w.idx} или py login_qr.py {w.idx}"
        )
        w._market_flood_wait_until = float("inf")
        w._profile_flood_wait_until = float("inf")
        w._slug_flood_wait_until = float("inf")
        if w in self.workers:
            self.workers.remove(w)
            logger.warning(f"⚠️ Воркер #{w.idx} удалён из пула. Осталось активных воркеров: {len(self.workers)}")
        if not self.workers:
            logger.critical("❌ Все воркеры в пуле потеряли авторизацию! Экстренное завершение для переавторизации...")
            os._exit(2)

    @property
    def primary_worker(self) -> AccountWorker:
        if self.workers:
            return self.workers[0]
        raise RuntimeError("В пуле нет доступных воркеров Telegram!")

    def get_best_profile_worker(self) -> AccountWorker:
        """Выбирает воркера, у которого прошло больше всего времени с последнего запроса."""
        if not self.workers:
            return self.primary_worker
        now_ts = asyncio.get_running_loop().time()
        ready = [w for w in self.workers if w._profile_flood_wait_until <= now_ts]
        if not ready:
            return min(self.workers, key=lambda w: w._profile_flood_wait_until)
        return min(ready, key=lambda w: w._last_call_time)

    def get_best_upgrade_worker(self) -> AccountWorker:
        """Выбирает наименее загруженного воркера для фоновой проверки апгрейдов."""
        if not self.workers:
            return self.primary_worker
        now_ts = asyncio.get_running_loop().time()
        ready = [w for w in self.workers if w._slug_flood_wait_until <= now_ts]
        if not ready:
            return min(self.workers, key=lambda w: w._slug_flood_wait_until)
        return min(ready, key=lambda w: w._last_call_time)

    @property
    def client(self) -> TelegramClient:
        return self.primary_worker.client

    @property
    def _full_user_lock(self) -> asyncio.Lock:
        return self.primary_worker._full_user_lock

    @property
    def _flood_wait_until(self) -> float:
        return self.primary_worker._flood_wait_until

    @_flood_wait_until.setter
    def _flood_wait_until(self, val: float) -> None:
        self.primary_worker._flood_wait_until = val

    @property
    def _market_throttle_lock(self) -> asyncio.Lock:
        return self.primary_worker._market_throttle_lock

    @property
    def _last_market_call_time(self) -> float:
        return self.primary_worker._last_market_call_time

    @_last_market_call_time.setter
    def _last_market_call_time(self, val: float) -> None:
        self.primary_worker._last_market_call_time = val

    async def start(self) -> None:
        """Запуск Telethon клиентов из пула аккаунтов."""
        if not self.workers:
            raise ValueError(
                "TELEGRAM_API_ID и TELEGRAM_API_HASH должны быть указаны в файле .env!"
            )

        authorized_workers: List[AccountWorker] = []
        for worker in self.workers:
            logger.info(f"Подключение MTProto клиента #{worker.idx} ({worker.session_name})...")
            worker.client.flood_sleep_threshold = 0
            try:
                await asyncio.wait_for(worker.client.connect(), timeout=25.0)
                is_auth = await asyncio.wait_for(worker.client.is_user_authorized(), timeout=15.0)
                if not is_auth:
                    logger.warning(
                        f"Аккаунт #{worker.idx} ({worker.session_name}) не авторизован! "
                        f"Для входа выполните: py auth_account.py {worker.idx}"
                    )
                    try:
                        await worker.client.disconnect()
                    except Exception:
                        pass
                    continue

                me = await asyncio.wait_for(worker.client.get_me(), timeout=15.0)
                if not me:
                    raise ValueError("Не удалось получить информацию о пользователе (get_me вернул None)")
                worker.user = me
                authorized_workers.append(worker)
                logger.info(
                    f"✅ Аккаунт #{worker.idx} ({worker.session_name}) авторизован: "
                    f"{getattr(me, 'first_name', '')} (@{getattr(me, 'username', '') or 'без_юзернейма'}), ID: {me.id}"
                )
            except Exception as e:
                logger.error(
                    f"❌ Не удалось подключить аккаунт #{worker.idx} ({worker.session_name}): {e}. "
                    f"Для входа выполните: py auth_account.py {worker.idx}"
                )
                try:
                    await worker.client.disconnect()
                except Exception:
                    pass
                except Exception:
                    pass

        if not authorized_workers:
            raise RuntimeError(
                "Ни один аккаунт из пула не авторизован! "
                "Запустите: py auth_account.py для входа в Telegram."
            )

        self.workers = authorized_workers
        logger.info(f"🚀 Пул аккаунтов готов! Активно воркеров: {len(self.workers)}")

    async def update_collections(self) -> None:
        """Обновляет список доступных для перепродажи коллекций подарков."""
        now = asyncio.get_event_loop().time()
        # Обновляем список коллекций раз в 10 минут или если список пуст
        if self.known_collection_ids and (now - self.last_collections_update < 600):
            return

        # Загружаем навсегда исключенные коллекции из БД с учетом текущего max_collection_floor
        max_coll_floor = getattr(self.config, "max_collection_floor", None)
        if not self.excluded_collection_ids:
            try:
                self.excluded_collection_ids = await self.db.get_excluded_collection_ids(max_coll_floor)
            except Exception as e:
                logger.debug(f"Ошибка загрузки excluded_collections: {e}")

        logger.info("Запрос списка доступных коллекций подарков (payments.getStarGifts)...")
        try:
            gifts_res = await self.client(functions.payments.GetStarGiftsRequest(hash=0))
        except errors.FloodWaitError as e:
            logger.warning(f"FloodWait при получении коллекций: ожидание {e.seconds} сек.")
            await asyncio.sleep(e.seconds)
            return
        except Exception as e:
            logger.error(f"Ошибка получения списка коллекций: {e}")
            return

        all_resale_gifts = []
        if hasattr(gifts_res, "gifts"):
            for g in gifts_res.gifts:
                # availability_resale означает, что подарок может перепродаваться на вторичном рынке
                if getattr(g, "availability_resale", None):
                    all_resale_gifts.append(g)

        target_ids: List[int] = []
        titles_map: Dict[int, str] = {}

        config_collections = [
            str(c).lower().strip() for c in self.config.collections if str(c).strip()
        ]

        for g in all_resale_gifts:
            gift_id = g.id
            if gift_id in self.excluded_collection_ids:
                continue
            title = getattr(g, "title", f"Gift-{gift_id}") or f"Gift-{gift_id}"
            titles_map[gift_id] = title

            if not config_collections:
                # Если в конфиге список пуст — отслеживаем ВСЕ коллекции
                target_ids.append(gift_id)
            else:
                # Проверяем совпадение по ID или по названию коллекции
                if str(gift_id) in config_collections or title.lower() in config_collections:
                    target_ids.append(gift_id)
                else:
                    # Частичное совпадение по названию
                    if any(c in title.lower() for c in config_collections):
                        target_ids.append(gift_id)

        # Дополнительно добавляем числовые ID, если они были явно указаны в config.collections
        for c in self.config.collections:
            try:
                cid = int(str(c).strip())
                if cid not in self.excluded_collection_ids and cid not in target_ids:
                    target_ids.append(cid)
                    if cid not in titles_map:
                        titles_map[cid] = f"Gift-{cid}"
            except ValueError:
                pass

        CORE_HIGH_VELOCITY = {
            "chill flame", "instant ramen", "lunar snake", "vice cream", "ice cream",
            "lol pop", "fresh socks", "whip cupcake", "happy brownie", "pool float",
            "mood pack", "snake box", "pet snake", "candy cane", "clover pin",
            "b-day candle", "mirage lamp", "liberty figure", "mousse cake",
            "faith amulet", "desk calendar", "light sword", "swag bag",
            "jester hat", "party sparkler", "snoop dogg", "xmas stocking",
            "cookie heart", "hypno lollipop", "big year", "winter wreath",
            "spiced wine", "easter egg", "input key", "magic carpet",
            "potion bottle", "pumpkin", "gingerbread", "cupid bow", "sparkler",
            "heart", "magic potion", "crystal ball", "star badge"
        }

        priority_ids: List[int] = []
        other_ids: List[int] = []
        max_coll_floor = getattr(self.config, "max_collection_floor", None)

        for gid in target_ids:
            if gid in self.excluded_collection_ids:
                continue
            known_floor = self.collection_floors.get(gid)
            if max_coll_floor is not None and known_floor is not None and known_floor > max_coll_floor:
                self.excluded_collection_ids.add(gid)
                continue

            t_name = titles_map.get(gid, "").lower()
            if any(core in t_name for core in CORE_HIGH_VELOCITY):
                priority_ids.append(gid)
            else:
                other_ids.append(gid)

        self.priority_collection_ids = priority_ids
        self.other_collection_ids = other_ids
        self.known_collection_ids = priority_ids + other_ids
        self.collection_titles = titles_map
        self.last_collections_update = now
        logger.info(
            f"Готово! Найдено активных коллекций для мониторинга: {len(self.known_collection_ids)} "
            f"(приоритетных дешёвых: {len(priority_ids)}, остальных с флором <= {max_coll_floor or 'unlimited'}: {len(other_ids)}): "
            f"{[titles_map.get(gid) for gid in self.known_collection_ids[:5]]}"
            + (f" и ещё {len(self.known_collection_ids) - 5}..." if len(self.known_collection_ids) > 5 else "")
        )

    async def get_next_collection_id(self) -> Optional[int]:
        """
        Возвращает ID следующей коллекции с умным чередованием:
        чередует популярные дешёвые коллекции с высоким оборотом и остальные коллекции с флором <= 5000 ★
        в пропорции 2:1 (2 приоритетные на 1 стандартную) для сверхбыстрого перехвата.
        """
        async with self._cursor_lock:
            if not self.priority_collection_ids and not self.other_collection_ids:
                if not self.known_collection_ids:
                    return None
                cid = self.known_collection_ids[self._collection_cursor % len(self.known_collection_ids)]
                self._collection_cursor += 1
                return cid

            # Чередование 4:1 (4 приоритетные дешёвые на 1 стандартную) для максимальной скорости перехвата
            self._interleave_counter = getattr(self, "_interleave_counter", 0) + 1
            if self._interleave_counter % 5 != 0 and self.priority_collection_ids:
                cid = self.priority_collection_ids[self._priority_cursor % len(self.priority_collection_ids)]
                self._priority_cursor += 1
                return cid
            elif self.other_collection_ids:
                cid = self.other_collection_ids[self._other_cursor % len(self.other_collection_ids)]
                self._other_cursor += 1
                return cid
            elif self.priority_collection_ids:
                cid = self.priority_collection_ids[self._priority_cursor % len(self.priority_collection_ids)]
                self._priority_cursor += 1
                return cid
            return None

    async def get_seller_profile(
        self,
        seller_id: int,
        peer_user: Any,
        user_obj: Optional[Any] = None,
        worker: Optional[AccountWorker] = None,
        allow_network: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Получает расширенную информацию о продавце (users.getFullUser) с использованием кэша и пула воркеров."""
        now_epoch = asyncio.get_running_loop().time()
        w = worker if (worker and getattr(worker, "_profile_flood_wait_until", 0.0) <= now_epoch) else self.get_best_profile_worker()
        max_age_sec = self.config.seller_cache_ttl_hours * 3600

        # Сверхбыстрая проверка в ОЗУ (0 мс)
        if seller_id in self._seller_cache_ram:
            cached_time, cached_data = self._seller_cache_ram[seller_id]
            gifts_time = cached_data.get("_gifts_cached_time", cached_time)
            if (
                (now_epoch - cached_time) < max_age_sec
                and (now_epoch - gifts_time) < 300
                and "nft_gifts_count" in cached_data
                and "contact_require_premium" in cached_data
                and "profile_gifts" in cached_data
                and cached_data.get("profile_gifts") is not None
            ):
                return cached_data

        cached = await self.db.get_cached_seller(seller_id, max_age_sec)
        if (
            cached is not None
            and "nft_gifts_count" in cached
            and "contact_require_premium" in cached
            and "profile_gifts" in cached
            and cached.get("profile_gifts") is not None
        ):
            gifts_time = cached.get("_gifts_cached_time", 0)
            if (now_epoch - gifts_time) < 300:
                self._seller_cache_ram[seller_id] = (now_epoch, cached)
                return cached

        # Если сетевые запросы запрещены (быстрый прогон маркета без блокировок) — сразу выходим
        if not allow_network:
            return None

        # Формируем InputUser: либо из access_hash переданного user_obj, либо через get_input_entity
        input_user = None
        if user_obj and getattr(user_obj, "access_hash", None) is not None:
            input_user = types.InputUser(
                user_id=user_obj.id, access_hash=user_obj.access_hash
            )

        if not input_user:
            try:
                input_user = await asyncio.wait_for(
                    w.client.get_input_entity(peer_user), timeout=5.0
                )
            except Exception as e:
                logger.debug(f"Не удалось получить input_entity для {seller_id}: {e}")
                return None

        full_res = None
        async with w._full_user_lock:
            try:
                async with w.call_slot(min_interval=0.55, category="profile"):
                    full_res = await asyncio.wait_for(
                        w.client(functions.users.GetFullUserRequest(id=input_user)),
                        timeout=6.0,
                    )
            except errors.FloodWaitError as e:
                wait_time = e.seconds + 1
                logger.warning(
                    f"[Воркер #{w.idx}] FloodWait ({e.seconds} сек.) при получении профиля {seller_id}. "
                    f"Воркер временно приостановлен для профилей."
                )
                w._profile_flood_wait_until = asyncio.get_running_loop().time() + wait_time
                w.consecutive_floods += 1

                # Мгновенно пробуем переключиться на альтернативный свободный воркер из пула!
                alt_workers = [
                    ow for ow in self.workers
                    if ow.idx != w.idx and ow._profile_flood_wait_until <= asyncio.get_running_loop().time()
                ]
                if alt_workers:
                    alt_w = alt_workers[0]
                    logger.debug(f"Переключение запроса профиля {seller_id} на резервный воркер #{alt_w.idx}...")
                    try:
                        async with alt_w._full_user_lock:
                            async with alt_w.call_slot(min_interval=0.55, category="profile"):
                                full_res = await asyncio.wait_for(
                                    alt_w.client(functions.users.GetFullUserRequest(id=input_user)),
                                    timeout=6.0,
                                )
                            w = alt_w
                    except Exception as alt_err:
                        logger.error(f"[Воркер #{alt_w.idx}] Ошибка резервного получения профиля {seller_id}: {alt_err}")
                        return None
                else:
                    return None
            except errors.SessionRevokedError:
                self.deactivate_worker(w, f"отозвана при запросе профиля {seller_id}")
                return None
            except Exception as e:
                logger.debug(f"[Воркер #{w.idx}] Не удалось получить профиль продавца {seller_id}: {e}")
                return None

        if not full_res:
            return None

        full_user = full_res.full_user
        resolved_user = None
        for u in full_res.users:
            if u.id == seller_id:
                resolved_user = u
                break
        if not resolved_user:
            resolved_user = user_obj if user_obj else (full_res.users[0] if full_res.users else None)

        if resolved_user:
            if getattr(resolved_user, "deleted", False) or getattr(resolved_user, "bot", False):
                logger.debug(f"Продавец {seller_id} пропущен: удаленный аккаунт или бот")
                return None
            if getattr(resolved_user, "scam", False) or getattr(resolved_user, "fake", False):
                logger.debug(f"Продавец {seller_id} пропущен: аккаунт с меткой scam/fake")
                return None

        # Извлекаем stars_rating.level
        stars_rating = getattr(full_user, "stars_rating", None)
        level = getattr(stars_rating, "level", None) if stars_rating else None

        # Получаем количество коллекционных (NFT) подарков и список подарков на профиле продавца
        nft_gifts_count = None
        profile_gifts: List[Dict[str, Any]] = []
        total_stargifts = getattr(full_user, "stargifts_count", None)

        if total_stargifts is None or total_stargifts == 0:
            nft_gifts_count = 0
            profile_gifts = []
        else:
            try:
                access_hash = getattr(input_user, "access_hash", None)
                if access_hash is not None:
                    input_peer = types.InputPeerUser(user_id=seller_id, access_hash=access_hash)
                else:
                    input_peer = input_user

                # Безопасный запрос списка NFT-подарков профиля (до 15 шт.)
                async with w.call_slot(min_interval=0.60, category="profile"):
                    saved_res = await asyncio.wait_for(
                        w.client(
                            functions.payments.GetSavedStarGiftsRequest(
                                peer=input_peer,
                                offset="",
                                limit=15,
                                exclude_unlimited=True,
                            )
                        ),
                        timeout=6.0,
                    )
                profile_gifts = []
                nft_count = 0
                for sg in getattr(saved_res, "gifts", []) or []:
                    g_inner = getattr(sg, "gift", None)
                    if not g_inner:
                        continue
                    
                    # Фильтруем строго уникальные (NFT) подарки с валидным slug
                    is_unique = (
                        isinstance(g_inner, types.StarGiftUnique)
                        or type(g_inner).__name__ == "StarGiftUnique"
                        or bool(getattr(g_inner, "slug", None))
                    )
                    if not is_unique:
                        continue

                    g_slug = (getattr(g_inner, "slug", "") or "").strip()
                    if not g_slug:
                        continue

                    nft_count += 1
                    p_stars, p_ton = parse_prices(getattr(g_inner, "resell_amount", None))
                    m_name, m_rarity, b_name, pat_name = parse_attributes(getattr(g_inner, "attributes", []))
                    profile_gifts.append({
                        "slug": g_slug,
                        "num": getattr(g_inner, "num", None),
                        "title": (getattr(g_inner, "title", "") or "").strip(),
                        "price_stars": p_stars,
                        "price_ton": p_ton,
                        "on_sale": bool(p_stars and p_stars > 0),
                        "model": m_name,
                        "model_rarity": m_rarity,
                        "backdrop": b_name,
                        "pattern": pat_name,
                        "raw_id": getattr(g_inner, "id", None),
                        "gift_id": getattr(g_inner, "gift_id", None),
                    })
                nft_gifts_count = nft_count
            except errors.FloodWaitError as e:
                logger.warning(
                    f"[Воркер #{w.idx}] FloodWait ({e.seconds} сек.) при получении NFT-подарков продавца {seller_id}."
                )
                w._profile_flood_wait_until = asyncio.get_running_loop().time() + e.seconds + 1
                nft_gifts_count = None
                profile_gifts = []
            except Exception as e:
                logger.debug(f"Не удалось получить список NFT-подарков для {seller_id}: {e}")
                nft_gifts_count = None
                profile_gifts = []

        # Проверка платных сообщений из обоих полей Telegram MTProto:
        # 1. full_user.send_paid_messages_stars
        # 2. full_user.settings.charge_paid_message_stars
        paid_stars = getattr(full_user, "send_paid_messages_stars", None)
        if paid_stars is None or paid_stars == 0:
            peer_settings = getattr(full_user, "settings", None)
            if peer_settings:
                paid_stars = getattr(peer_settings, "charge_paid_message_stars", None)

        contact_require_premium = bool(getattr(full_user, "contact_require_premium", False))

        seller_data = {
            "seller_id": seller_id,
            "level": level,
            "about": getattr(full_user, "about", "") or "",
            "send_paid_messages_stars": paid_stars,
            "contact_require_premium": contact_require_premium,
            "first_name": getattr(resolved_user, "first_name", "") or "",
            "last_name": getattr(resolved_user, "last_name", "") or "",
            "username": getattr(resolved_user, "username", None),
            "premium": bool(getattr(resolved_user, "premium", False)),
            "nft_gifts_count": nft_gifts_count,
            "total_gifts": total_stargifts if total_stargifts is not None else 0,
            "profile_gifts": profile_gifts,
            "_gifts_cached_time": now_epoch,
        }

        self._seller_cache_ram[seller_id] = (asyncio.get_running_loop().time(), seller_data)
        await self.db.save_cached_seller(seller_id, seller_data)
        return seller_data

    async def _safe_get_seller_profile(
        self,
        sid: int,
        peer: Any,
        user_obj: Optional[Any] = None,
        worker: Optional[AccountWorker] = None,
        allow_network: bool = True,
    ) -> Optional[Dict[str, Any]]:
        try:
            return await self.get_seller_profile(
                sid, peer, user_obj=user_obj, worker=worker, allow_network=allow_network
            )
        except TypeError:
            return await self.get_seller_profile(
                sid, peer, user_obj=user_obj, worker=worker
            )

    async def get_upgrade_owner_profile(
        self, seller_id: int, peer_user: Any, user_obj: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        """Получает расширенную информацию о владельце апгрейда (уровень, NFT-подарки, юзернейм, био) с TTL кэшем."""
        return await self.get_seller_profile(
            seller_id=seller_id,
            peer_user=peer_user,
            user_obj=user_obj,
            worker=self.get_best_profile_worker(),
        )

    async def resolve_gift_owner(
        self,
        g: Any,
        users_map: Dict[int, Any],
        worker: AccountWorker,
        is_upgrade: bool = False,
        probe_deep: bool = True,
        allow_network: bool = True,
    ) -> Tuple[int, Optional[Dict[str, Any]], Optional[Any]]:
        """
        Каскадный резолвер обычных пользователей («глупеньких пользователей») в обход скрытия профилей.
        Строгое требование: имя профиля найденного пользователя (@username) ОБЯЗАНО полностью совпадать
        с серым именем владельца, отображаемым при открытии подарка. Никаких случайных совпадений.
        """
        slug = (getattr(g, "slug", "") or "").strip()
        owner_name = (getattr(g, "owner_name", "") or "").strip()

        # Если имя владельца отсутствует — невозможно подтвердить совпадение профиля
        if not owner_name:
            return 0, None, None

        # Уровень 1: Проверка постоянного реестра владения по slug (с верификацией совпадения имени)
        if slug:
            reg = await self.db.get_gift_registry(slug)
            if reg and reg.get("seller_id") and verify_seller_name(owner_name, reg):
                sid = reg["seller_id"]
                return sid, reg, types.PeerUser(user_id=sid)

        # Уровень 2: Прямой owner_id (если отдан сервером Telegram)
        owner_peer = getattr(g, "owner_id", None)
        seller_id = getattr(owner_peer, "user_id", None) if owner_peer else None
        if seller_id and owner_peer:
            seller_obj = users_map.get(seller_id)
            seller = await self._safe_get_seller_profile(
                seller_id, owner_peer, user_obj=seller_obj, worker=worker, allow_network=allow_network
            )
            if seller and verify_seller_name(owner_name, seller):
                if slug:
                    await self.db.save_gift_registry(slug, seller_id, seller)
                return seller_id, seller, owner_peer

        # Уровень 3: Проверка участников ответа Telegram (users_map / OriginalDetails)
        matched_user = None
        orig_recipient_id = None
        for attr in getattr(g, "attributes", []):
            if type(attr).__name__ == "StarGiftAttributeOriginalDetails" or hasattr(attr, "recipient_id"):
                rec_peer = getattr(attr, "recipient_id", None)
                if rec_peer:
                    orig_recipient_id = getattr(rec_peer, "user_id", None)
                break

        if orig_recipient_id and orig_recipient_id in users_map:
            candidate_u = users_map[orig_recipient_id]
            if verify_seller_name(owner_name, candidate_u):
                matched_user = candidate_u

        if not matched_user and users_map:
            for candidate_u in users_map.values():
                if verify_seller_name(owner_name, candidate_u):
                    matched_user = candidate_u
                    break

        if matched_user:
            m_uid = matched_user.id
            m_peer = types.PeerUser(user_id=m_uid)
            seller = await self._safe_get_seller_profile(
                m_uid, m_peer, user_obj=matched_user, worker=worker, allow_network=allow_network
            )
            if seller and verify_seller_name(owner_name, seller):
                if slug:
                    await self.db.save_gift_registry(slug, m_uid, seller)
                return m_uid, seller, m_peer

        # Уровень 4: Поиск в базе seller_cache (только уникальные точные совпадения)
        if owner_name:
            cached = await self.db.get_cached_seller_by_name(owner_name)
            if cached and cached.get("seller_id") and verify_seller_name(owner_name, cached):
                sid = cached["seller_id"]
                peer = types.PeerUser(user_id=sid)
                live_seller = await self._safe_get_seller_profile(
                    sid, peer, worker=worker, allow_network=allow_network
                )
                if live_seller and verify_seller_name(owner_name, live_seller):
                    # Если включена проверка наличия подарка в профиле - сверяем его наличие
                    if getattr(self.config, "require_gift_on_profile", True):
                        p_gifts = live_seller.get("profile_gifts") or []
                        require_exact = getattr(self.config, "require_exact_gift_on_profile", False)
                        target_num = getattr(g, "num", None)
                        target_title = getattr(g, "title", "")
                        if not has_gift_on_profile(
                            p_gifts,
                            slug=slug,
                            num=target_num,
                            title=target_title,
                            require_exact=require_exact,
                            nft_gifts_count=live_seller.get("nft_gifts_count"),
                        ):
                            logger.debug(
                                f"Кандидат из кэша @{live_seller.get('username')} отклонён: нет продающихся подарков на профиле"
                            )
                            return 0, None, None
                    actual_pg = find_best_profile_gift(live_seller.get("profile_gifts") or [], slug=slug, num=getattr(g, "num", None), title=getattr(g, "title", ""))
                    reg_slug = actual_pg["slug"] if (actual_pg and actual_pg.get("slug")) else slug
                    if reg_slug:
                        await self.db.save_gift_registry(reg_slug, sid, live_seller)
                    return sid, live_seller, peer

        # Если в локальном кэше нет — запускаем активные уровни обхода (если разрешено probe_deep)
        if owner_name and probe_deep and allow_network:
            async def _check_and_confirm_candidate(u_cand: Any) -> Tuple[int, Optional[Dict[str, Any]], Optional[Any]]:
                if not u_cand or not hasattr(u_cand, "id"):
                    return 0, None, None
                if not verify_seller_name(owner_name, u_cand):
                    return 0, None, None
                c_uid = u_cand.id
                c_peer = types.PeerUser(user_id=c_uid)
                c_seller = await self.get_seller_profile(c_uid, c_peer, user_obj=u_cand, worker=worker)
                if not c_seller or not verify_seller_name(owner_name, c_seller):
                    return 0, None, None
                if getattr(self.config, "require_gift_on_profile", True):
                    p_gifts = c_seller.get("profile_gifts") or []
                    require_exact = getattr(self.config, "require_exact_gift_on_profile", False)
                    target_num = getattr(g, "num", None)
                    target_title = getattr(g, "title", "")
                    if not has_gift_on_profile(
                        p_gifts,
                        slug=slug,
                        num=target_num,
                        title=target_title,
                        require_exact=require_exact,
                        nft_gifts_count=c_seller.get("nft_gifts_count"),
                    ):
                        return 0, None, None
                actual_pg = find_best_profile_gift(c_seller.get("profile_gifts") or [], slug=slug, num=getattr(g, "num", None), title=getattr(g, "title", ""))
                reg_slug = actual_pg["slug"] if (actual_pg and actual_pg.get("slug")) else slug
                if reg_slug:
                    await self.db.save_gift_registry(reg_slug, c_uid, c_seller)
                return c_uid, c_seller, c_peer

            # Уровень 5: Прямой резолв юзернейма (если серое имя похоже на @username)
            clean_uname = owner_name.lstrip("@").strip()
            if (
                clean_uname.lower() not in SYSTEM_RESERVED_NAMES
                and re.match(r"^[a-zA-Z0-9_]{4,32}$", clean_uname)
                and getattr(worker, "_search_flood_wait_until", 0.0) <= asyncio.get_running_loop().time()
            ):
                try:
                    async with worker.call_slot(min_interval=0.40, category="search"):
                        res_user = await asyncio.wait_for(
                            worker.client(functions.contacts.ResolveUsernameRequest(username=clean_uname)),
                            timeout=5.0,
                        )
                    if res_user and getattr(res_user, "users", None):
                        cand_u = res_user.users[0]
                        sid, s_data, s_peer = await _check_and_confirm_candidate(cand_u)
                        if sid:
                            logger.info(
                                f"⚡ [Обход L5] Продавец '{owner_name}' успешно де-анонимизирован через ResolveUsername: @{s_data.get('username')}"
                            )
                            return sid, s_data, s_peer
                except errors.FloodWaitError as e:
                    logger.warning(f"🚨 [Воркер #{worker.idx}] FloodWait ({e.seconds}s) на ResolveUsername '{clean_uname}'")
                    worker._search_flood_wait_until = asyncio.get_running_loop().time() + e.seconds + 1
                except Exception as e:
                    logger.debug(f"Уровень 5 (ResolveUsername '{clean_uname}') не дал результата: {e}")

            # Уровень 6: Точечный опрос карточки лота (раскрывает участников OriginalDetails и получателей на маркете)
            if not is_upgrade and slug and getattr(self.config, "enable_slug_probing", True) and getattr(worker, "_slug_flood_wait_until", 0.0) <= asyncio.get_running_loop().time():
                try:
                    async with worker.call_slot(min_interval=0.40, category="slug"):
                        card_res = await asyncio.wait_for(
                            worker.client(functions.payments.GetUniqueStarGiftRequest(slug=slug)),
                            timeout=5.0,
                        )
                    if card_res and hasattr(card_res, "users"):
                        for cand_u in card_res.users:
                            sid, s_data, s_peer = await _check_and_confirm_candidate(cand_u)
                            if sid:
                                logger.info(
                                    f"⚡ [Обход L6] Продавец '{owner_name}' успешно раскрыт через карточку лота {slug}: @{s_data.get('username')}"
                                )
                                return sid, s_data, s_peer
                except errors.FloodWaitError as e:
                    logger.warning(f"🚨 [Воркер #{worker.idx}] FloodWait ({e.seconds}s) на GetUniqueStarGift '{slug}'")
                    worker._slug_flood_wait_until = asyncio.get_running_loop().time() + e.seconds + 1
                except Exception as e:
                    logger.debug(f"Уровень 6 (GetUniqueStarGift '{slug}') не дал результата: {e}")

            # Уровень 7: Глобальный поиск контактов Telegram (contacts.search)
            # Выполняется ТОЛЬКО если включен в конфиге и нет активного FloodWait поиска
            if getattr(self.config, "enable_global_search", False) and owner_name and getattr(worker, "_search_flood_wait_until", 0.0) <= asyncio.get_running_loop().time():
                try:
                    async with worker.call_slot(min_interval=0.45, category="search"):
                        found_contacts = await asyncio.wait_for(
                            worker.client(
                                functions.contacts.SearchRequest(
                                    q=owner_name.strip(),
                                    limit=5,
                                    broadcasts=False,
                                    bots=False,
                                )
                            ),
                            timeout=5.0,
                        )
                    for cand_u in getattr(found_contacts, "users", []) or []:
                        sid, s_data, s_peer = await _check_and_confirm_candidate(cand_u)
                        if sid:
                            logger.info(
                                f"⚡ [Обход L7] Продавец '{owner_name}' успешно найден через глобальный поиск Telegram: @{s_data.get('username')}"
                            )
                            return sid, s_data, s_peer
                except errors.FloodWaitError as e:
                    logger.warning(f"🚨 [Воркер #{worker.idx}] FloodWait ({e.seconds}s) на contacts.search '{owner_name}'")
                    worker._search_flood_wait_until = asyncio.get_running_loop().time() + e.seconds + 1
                except Exception as e:
                    logger.debug(f"Уровень 7 (Global contacts.search '{owner_name}') не дал результата: {e}")
                except Exception as e:
                    logger.debug(f"Уровень 7 (Global contacts.search '{owner_name}') не дал результата: {e}")

        # Не подтверждён — пропускаем лот, чтобы не брать анонимов и фейки
        return 0, None, None

    async def bootstrap_initial_lots(self) -> None:
        """При самом первом запуске запоминает все текущие лоты без отправки уведомлений."""
        is_empty = await self.db.is_seen_lots_empty()
        if not is_empty:
            logger.info("База данных уже содержит ранее обнаруженные лоты. Пропуск первого запуска.")
            return

        logger.info("🔍 Первый запуск! Сбор существующих лотов на маркете для запоминания...")
        initial_keys: List[str] = []

        for i, gift_id in enumerate(self.known_collection_ids):
            worker = self.workers[i % len(self.workers)] if self.workers else self.primary_worker
            try:
                resale_res = await worker.client(
                    functions.payments.GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset="",
                        limit=100,
                        stars_only=True,
                    )
                )
                gifts = getattr(resale_res, "gifts", [])
                for g in gifts:
                    price_stars, _ = parse_prices(getattr(g, "resell_amount", None))
                    if price_stars is None:
                        continue
                    seller_id = getattr(getattr(g, "owner_id", None), "user_id", None) or 0
                    owner_ident = seller_id or getattr(g, "slug", "") or getattr(g, "owner_name", "") or g.id
                    lot_key = f"{g.id}:{owner_ident}:{price_stars}"
                    initial_keys.append(lot_key)
                    initial_keys.append(f"{g.id}:{owner_ident}")
                    slug = getattr(g, "slug", "")
                    if slug:
                        initial_keys.append(f"slug:{slug}")
                    # Запоминаем исходную цену в истории лотов (listing_history)
                    listing_key = f"{gift_id}:{owner_ident}"
                    await self.db.process_listing_history(
                        listing_key, price_stars, self.config.stale_minutes_threshold
                    )
                await asyncio.sleep(0.3)
            except errors.FloodWaitError as e:
                logger.warning(f"FloodWait при инициализации: ожидание {e.seconds} сек.")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Ошибка при сборе существующих лотов коллекции {gift_id}: {e}")

        await self.db.mark_lots_seen_bulk(initial_keys)
        logger.info(
            f"✅ Первый запуск завершён! Сохранено {len(initial_keys)} существующих лотов. "
            f"Уведомления будут отправляться только на НОВЫЕ лоты."
        )

    async def poll_collection(
        self, gift_id: int, worker: Optional[AccountWorker] = None
    ) -> None:
        """Опрашивает одну коллекцию на предмет новых лотов с использованием назначенного воркера."""
        w = worker or self.primary_worker
        now_ts = asyncio.get_running_loop().time()
        if w._market_flood_wait_until > now_ts:
            await asyncio.sleep(w._market_flood_wait_until - now_ts)

        # Автоматическая проверка и восстановление соединения MTProto при обрыве связи
        if not w.client.is_connected():
            logger.warning(f"[Воркер #{w.idx}] MTProto соединение потеряно. Попытка переподключения...")
            try:
                await w.client.connect()
                if not await w.client.is_user_authorized():
                    self.deactivate_worker(w, "не авторизован при переподключении")
                    return
            except errors.SessionRevokedError:
                self.deactivate_worker(w, "сессия отозвана при переподключении")
                return
            except Exception as conn_err:
                logger.error(f"[Воркер #{w.idx}] Ошибка переподключения: {conn_err}")
                await asyncio.sleep(2.0)
                return

        try:
            async with w.call_slot(min_interval=w.adaptive_delay, category="market"):
                resale_res = await asyncio.wait_for(
                    w.client(
                        functions.payments.GetResaleStarGiftsRequest(
                            gift_id=gift_id,
                            offset="",
                            limit=100,
                            stars_only=True,
                        )
                    ),
                    timeout=7.0,
                )
        except errors.FloodWaitError as e:
            wait_time = e.seconds + 1
            logger.warning(
                f"🚨 [Воркер #{w.idx}] FloodWait ({e.seconds} сек.) на коллекции {gift_id}. Пауза {wait_time} сек..."
            )
            w.adaptive_delay = min(w.adaptive_delay + 0.15, 1.5)
            w.consecutive_success = 0
            w._market_flood_wait_until = asyncio.get_running_loop().time() + wait_time
            return
        except errors.SessionRevokedError:
            self.deactivate_worker(w, "сессия отозвана на маркете")
            return
        except (ConnectionError, errors.ServerError) as conn_err:
            logger.warning(f"[Воркер #{w.idx}] Обрыв соединения ({conn_err}). Переподключение...")
            try:
                await w.client.connect()
            except Exception:
                pass
            await asyncio.sleep(1.0)
            return
        except Exception as e:
            if "disconnected" in str(e).lower():
                logger.warning(f"[Воркер #{w.idx}] Клиент отключен ({e}). Восстановление связи...")
                try:
                    await w.client.connect()
                except Exception:
                    pass
                await asyncio.sleep(1.0)
                return
            logger.error(
                f"[Воркер #{w.idx}] Ошибка запроса payments.getResaleStarGifts({gift_id}): {e}"
            )
            return

        # Успешный опрос без FloodWait: постепенная адаптация к максимальной скорости
        self._last_successful_poll_time = asyncio.get_running_loop().time()
        w.consecutive_success += 1
        if w.consecutive_success >= 10:
            base_delay = getattr(self.config, "collection_delay_seconds", 0.20)
            target_min = max(0.12, base_delay)
            if w.adaptive_delay > target_min:
                w.adaptive_delay = max(target_min, w.adaptive_delay - 0.03)
            w.consecutive_success = 0

        gifts = list(getattr(resale_res, "gifts", []) or [])
        if not gifts:
            return

        tz = self.config.get_zone_info()
        users_map = {
            u.id: u for u in getattr(resale_res, "users", []) if hasattr(u, "id")
        }

        # Фоновое автопополнение базы продавцов (seller_cache) реальными пользователями маркета
        sellers_batch = []
        for u in users_map.values():
            if getattr(u, "username", None) and not getattr(u, "bot", False) and not getattr(u, "deleted", False):
                u_data = {
                    "seller_id": u.id,
                    "first_name": getattr(u, "first_name", "") or "",
                    "last_name": getattr(u, "last_name", "") or "",
                    "username": getattr(u, "username", "") or "",
                    "phone": getattr(u, "phone", None),
                    "premium": bool(getattr(u, "premium", False)),
                    "scam": bool(getattr(u, "scam", False)),
                    "fake": bool(getattr(u, "fake", False)),
                }
                sellers_batch.append((u.id, u_data))
        if sellers_batch:
            asyncio.create_task(self.db.save_cached_sellers_bulk(sellers_batch))

        # Расчет минимального флора коллекции по первой странице (самые дешевые лоты)
        collection_prices: List[int] = []
        for g_item in gifts:
            p_stars, _ = parse_prices(getattr(g_item, "resell_amount", None))
            if p_stars is not None and p_stars > 0:
                collection_prices.append(p_stars)

        if collection_prices:
            min_floor = min(collection_prices)
            self.collection_floors[gift_id] = min_floor
            self._collection_floor_time[gift_id] = asyncio.get_running_loop().time()
        else:
            min_floor = self.collection_floors.get(gift_id)

        # Фильтр максимального флора показываемых коллекций:
        # коллекции с минимальным флором > max_collection_floor (например > 5000 ★) немедленно исключаются из ротации
        max_coll_floor = getattr(self.config, "max_collection_floor", None)
        if max_coll_floor is not None and min_floor is not None and min_floor > max_coll_floor:
            c_title = self.collection_titles.get(gift_id, f"ID-{gift_id}")
            logger.info(
                f"🚫 [Воркер #{w.idx}] Коллекция {gift_id} ('{c_title}') НАВСЕГДА исключена из ротации: "
                f"флор {min_floor} ★ > лимита {max_coll_floor} ★"
            )
            self.excluded_collection_ids.add(gift_id)
            try:
                await self.db.mark_collection_excluded(gift_id, c_title, min_floor)
            except Exception as e:
                logger.debug(f"Ошибка сохранения excluded_collection {gift_id}: {e}")
            async with self._cursor_lock:
                if gift_id in self.priority_collection_ids:
                    self.priority_collection_ids.remove(gift_id)
                if gift_id in self.other_collection_ids:
                    self.other_collection_ids.remove(gift_id)
                if gift_id in self.known_collection_ids:
                    self.known_collection_ids.remove(gift_id)
            return

        self._stats_collections_polled += 1
        now_ts_hb = asyncio.get_running_loop().time()
        if not hasattr(w, "_last_scan_log_time"):
            w._last_scan_log_time = 0.0
        if now_ts_hb - w._last_scan_log_time >= 0.5:
            c_title = self.collection_titles.get(gift_id, f"ID-{gift_id}")
            logger.info(
                f"⚡ [Воркер #{w.idx}] Сканирование маркета: '{c_title}' (флор {min_floor} ★). "
                f"Опрошено: {self._stats_collections_polled} колл., активно в ротации: {len(self.known_collection_ids)}"
            )
            w._last_scan_log_time = now_ts_hb

        # Подсчет числа разных owner_id для каждого title в текущем скане (для флага liquid)
        unique_owners_by_title: Dict[str, set] = {}
        for g_item in gifts:
            oid = getattr(getattr(g_item, "owner_id", None), "user_id", None) or getattr(g_item, "owner_name", None) or getattr(g_item, "slug", None)
            if not oid:
                continue
            gt = getattr(g_item, "title", "") or ""
            if gt not in unique_owners_by_title:
                unique_owners_by_title[gt] = set()
            unique_owners_by_title[gt].add(oid)

            p_stars, _ = parse_prices(getattr(g_item, "resell_amount", None))
            if p_stars is not None and p_stars > 0 and p_stars not in collection_prices:
                collection_prices.append(p_stars)

        if collection_prices:
            min_floor = min(collection_prices)
            self.collection_floors[gift_id] = min_floor

        seen_keys_to_save: List[str] = []

        def _fast_mark_seen(k: str, s: str = "") -> None:
            seen_keys_to_save.append(k)
            self.db._seen_cache.add(k)
            if s:
                seen_keys_to_save.append(f"slug:{s}")
                self.db._seen_cache.add(f"slug:{s}")

        for g in gifts:
            price_stars, price_ton = parse_prices(getattr(g, "resell_amount", None))
            if price_stars is None:
                continue

            owner_name = (getattr(g, "owner_name", "") or "").strip()

            # Фильтр максимальной цены (быстрый отсев в памяти)
            # Все подарки, которые попадают под границу флора (min_floor <= max_coll_floor), показываются в любой цене
            is_under_floor = bool(
                max_coll_floor is not None
                and min_floor is not None
                and min_floor <= max_coll_floor
            )
            if not is_under_floor and self.config.max_price_stars is not None and price_stars > self.config.max_price_stars:
                continue

            # Фильтр защиты от оверпрайса (если включен в конфиге):
            if (
                self.config.max_floor_overprice_ratio is not None
                and min_floor
                and price_stars > int(min_floor * self.config.max_floor_overprice_ratio)
            ):
                continue

            slug = getattr(g, "slug", "")
            if slug and await self.db.has_seen_lot(f"slug:{slug}"):
                continue

            # Мгновенный резолвер профиля продавца из ОЗУ и кэша (0 мс, без блокировки опроса маркета)
            best_w = self.get_best_profile_worker()
            seller_id, seller, owner_peer = await self.resolve_gift_owner(
                g, users_map, worker=best_w, is_upgrade=False, probe_deep=False, allow_network=False
            )
            if seller:
                await self._process_resolved_market_lot(
                    g=g,
                    gift_id=gift_id,
                    slug=slug,
                    seller_id=seller_id,
                    seller=seller,
                    owner_peer=owner_peer,
                    min_floor=min_floor,
                    unique_owners_by_title=unique_owners_by_title,
                    seen_keys_to_save=seen_keys_to_save,
                )
            else:
                # Если в ОЗУ нет — отправляем в фоновую очередь де-анонимизации!
                # НЕ сжигаем лот в seen_lots, а отдаем параллельным воркерам
                now_u = asyncio.get_running_loop().time()
                if (
                    slug
                    and owner_name
                    and slug not in self._queued_slugs
                    and (slug not in self._unresolvable_slugs or now_u > self._unresolvable_slugs[slug])
                ):
                    self._queued_slugs.add(slug)
                    try:
                        self.unresolved_queue.put_nowait(
                            (g, gift_id, slug, owner_name, min_floor, unique_owners_by_title)
                        )
                    except asyncio.QueueFull:
                        pass

        if seen_keys_to_save:
            await self.db.mark_lots_seen_bulk(seen_keys_to_save)

    async def _process_resolved_market_lot(
        self,
        g: Any,
        gift_id: int,
        slug: str,
        seller_id: int,
        seller: Dict[str, Any],
        owner_peer: Any,
        min_floor: Optional[int],
        unique_owners_by_title: Dict[str, set],
        seen_keys_to_save: Optional[List[str]] = None,
    ) -> bool:
        """Проверяет фильтры качества для де-анонимизированного лота и при успехе отправляет его в outbox."""
        owner_name = getattr(g, "owner_name", "") or ""
        if owner_name and not verify_seller_name(owner_name, seller):
            if seen_keys_to_save is not None:
                seen_keys_to_save.append(f"{g.id}:{slug}")
                self.db._seen_cache.add(f"{g.id}:{slug}")
            return False

        owner_ident = seller_id or slug or owner_name or g.id
        lot_key = f"{g.id}:{owner_ident}"
        if await self.db.has_seen_lot(lot_key):
            return False

        def _mark_lot_done():
            if seen_keys_to_save is not None:
                seen_keys_to_save.append(lot_key)
                self.db._seen_cache.add(lot_key)
                if slug:
                    seen_keys_to_save.append(f"slug:{slug}")
                    self.db._seen_cache.add(f"slug:{slug}")
            else:
                self.db._seen_cache.add(lot_key)
                if slug:
                    self.db._seen_cache.add(f"slug:{slug}")
                asyncio.create_task(
                    self.db.mark_lots_seen_bulk([lot_key, f"slug:{slug}"] if slug else [lot_key])
                )

        # Защита от повторного спама одного и того же продавца (6 часов, только для seller_id > 0)
        if seller_id:
            cooldown_sec = int(getattr(self.config, "seller_cooldown_hours", 6.0) * 3600)
            if await self.db.is_seller_on_cooldown(seller_id, cooldown_seconds=cooldown_sec):
                _mark_lot_done()
                return False

        # Фильтр наличия username: обязательно наличие @username для прямой связи
        if self.config.require_username and not seller.get("username"):
            _mark_lot_done()
            return False

        # Фильтр наличия продающихся подарков в открытом профиле продавца
        if getattr(self.config, "require_gift_on_profile", True):
            p_gifts = seller.get("profile_gifts") or []
            require_exact = getattr(self.config, "require_exact_gift_on_profile", False)
            lot_num = getattr(g, "num", None)
            if not has_gift_on_profile(
                p_gifts,
                slug=slug,
                num=lot_num,
                title=getattr(g, "title", ""),
                require_exact=require_exact,
                nft_gifts_count=seller.get("nft_gifts_count"),
            ):
                _mark_lot_done()
                return False

        # Быстрый отсев удаленных, ботов, скама и платных сообщений
        if seller.get("deleted", False) or seller.get("bot", False):
            _mark_lot_done()
            return False
        if seller.get("scam", False) or seller.get("fake", False):
            _mark_lot_done()
            return False
        if seller.get("contact_require_premium"):
            _mark_lot_done()
            return False
        paid_stars = seller.get("send_paid_messages_stars")
        if paid_stars is not None and paid_stars > 0:
            _mark_lot_done()
            return False

        # Фильтр уровня продавца (stars_rating.level <= max_level)
        level = seller.get("level")
        if level is None:
            if self.config.unknown_level_policy == "skip":
                _mark_lot_done()
                return False
        else:
            if level > self.config.max_level:
                _mark_lot_done()
                return False

        # Фильтр количества коллекционных (NFT) подарков у продавца (СТРОГО 1 <= nft_gifts_count <= max_nft_gifts)
        # Профили без NFT подарков (0 или None) категорически отсекаются
        nft_gifts_count = seller.get("nft_gifts_count")
        if nft_gifts_count is None or nft_gifts_count <= 0:
            _mark_lot_done()
            return False
        if self.config.max_nft_gifts is not None and seller_id:
            if nft_gifts_count > self.config.max_nft_gifts:
                _mark_lot_done()
                return False

        # СНГ-эвристика: кириллица в имени, фамилии, описании, юзернейме, телефоне или СНГ-стиле никнейма
        if self.config.require_cyrillic:
            fn = seller.get('first_name', '') or ''
            ln = seller.get('last_name', '') or ''
            ab = seller.get('about', '') or ''
            un = seller.get('username', '') or ''
            combined_text = f"{fn} {ln} {ab} {un} {owner_name}".strip()
            
            # 1. Кириллические символы
            is_cis = has_cyrillic(combined_text)
            # 2. Номер телефона стран СНГ (+7, +375, +380, +998, +77...)
            if not is_cis:
                phone = str(seller.get('phone') or '')
                if any(phone.startswith(pfx) for pfx in ("7", "+7", "375", "+375", "380", "+380", "998", "+998", "992", "+992", "996", "+996")):
                    is_cis = True
            # 3. Эмодзи/символы без латинских букв в имени (популярно в СНГ: 🌿, 🖤, ⚡)
            if not is_cis:
                letters_only = re.sub(r'[^a-zA-Z]', '', f"{fn}{ln}")
                if not letters_only and (fn or ln):
                    is_cis = True
            # 4. Частые русские транслит-имена в никнейме или имени
            if not is_cis:
                translit_stems = (
                    "vlad", "dima", "nikit", "sash", "mish", "artem", "maxim", "maks", "danil", "dany",
                    "egor", "kirill", "andrey", "serg", "ivan", "vanya", "anya", "nastya", "katya",
                    "masha", "dasha", "sonya", "alina", "polin", "ksyu", "ler", "vik", "yarik", "gleb",
                    "roma", "anton", "bogdan", "pavel", "pasha", "marat", "timur", "denis", "ruslan",
                    "stepan", "vadim", "daria", "soni", "dash", "aleks", "oleg", "igor", "kolya", "zhenya"
                )
                combined_lower = combined_text.lower()
                if any(stem in combined_lower for stem in translit_stems):
                    is_cis = True

            if not is_cis:
                _mark_lot_done()
                return False

        # Анти-трейдер и анти-скам: отсечение перекупов, гарантов, каналов и отзывов
        if is_trader_or_scam(
            seller.get("first_name", "") or owner_name,
            seller.get("last_name", ""),
            seller.get("about", ""),
            username=seller.get("username", "") or "",
        ):
            _mark_lot_done()
            return False

        _mark_lot_done()

        # Извлекаем атрибуты (модель, редкость, фон, узор)
        model, model_rarity, backdrop, pattern = parse_attributes(
            getattr(g, "attributes", [])
        )

        tz = self.config.get_zone_info()
        detected_at = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")

        price_stars, price_ton = parse_prices(getattr(g, "resell_amount", None))
        lot_title = getattr(g, "title", "Подарок") or "Подарок"
        lot_num = getattr(g, "num", 0) or 0
        raw_id = g.id
        final_slug = slug

        # Проверяем, попадает ли лот/коллекция под границу флора (все такие подарки показываются в любой цене)
        max_coll_floor = getattr(self.config, "max_collection_floor", None)
        is_under_floor = bool(
            max_coll_floor is not None
            and min_floor is not None
            and min_floor <= max_coll_floor
        )

        # Подмена лота на фактически продающийся подарок из открытого профиля продавца
        if getattr(self.config, "use_profile_gift_link", True):
            p_gifts = seller.get("profile_gifts") or []
            max_p = None if is_under_floor else self.config.max_price_stars
            p_gift = find_best_profile_gift(
                p_gifts,
                slug=slug,
                num=lot_num,
                title=lot_title,
                max_price_stars=max_p,
            )
            # Если подарок в профиле является точным совпадением лота с маркета, он гарантированно на продаже
            if p_gift:
                pg_slug = (p_gift.get("slug") or "").strip().lower()
                pg_num = p_gift.get("num")
                pg_title = (p_gift.get("title") or "").strip().lower()
                is_exact = bool(
                    (slug and pg_slug and slug.strip().lower() == pg_slug)
                    or (lot_num is not None and pg_num is not None and pg_num == lot_num and (not lot_title or not pg_title or lot_title.strip().lower() == pg_title))
                )
                if is_exact:
                    p_gift["on_sale"] = True
                    if not p_gift.get("price_stars"):
                        p_gift["price_stars"] = price_stars
                    if not p_gift.get("price_ton"):
                        p_gift["price_ton"] = price_ton
                elif not p_gift.get("on_sale"):
                    # Подменять на ДРУГОЙ подарок, который НЕ выставлен на продажу (None ★), запрещено!
                    p_gift = None

            if p_gift and p_gift.get("slug"):
                target_slug = p_gift["slug"]
                if target_slug != slug:
                    logger.info(
                        f"🔄 Подмена лота: заменяем ссылку с {slug} на подарок из профиля @{seller.get('username')}: "
                        f"{target_slug} ({p_gift.get('title')} #{p_gift.get('num')}, {p_gift.get('price_stars')} ★)"
                    )
                final_slug = target_slug
                lot_num = p_gift.get("num") or lot_num
                lot_title = p_gift.get("title") or lot_title
                if p_gift.get("price_stars"):
                    price_stars = p_gift["price_stars"]
                if p_gift.get("price_ton"):
                    price_ton = p_gift["price_ton"]
                if p_gift.get("model") and p_gift["model"] != "-":
                    model = p_gift["model"]
                    model_rarity = p_gift.get("model_rarity", "Обычная")
                    backdrop = p_gift.get("backdrop", "-")
                    pattern = p_gift.get("pattern", "-")
                if p_gift.get("raw_id"):
                    raw_id = p_gift["raw_id"]
                if p_gift.get("gift_id"):
                    gift_id = p_gift["gift_id"]

        # Если включено требование наличия продающегося подарка в профиле:
        # продавец ОБЯЗАН иметь продающийся подарок на витрине! Если в профиле нет лотов на продаже — отсекаем!
        if getattr(self.config, "require_gift_on_profile", True):
            if not p_gift or not p_gift.get("slug") or not p_gift.get("on_sale"):
                logger.debug(
                    f"Продавец @{seller.get('username')} отклонён: в открытом профиле нет продающихся NFT-подарков"
                )
                return False

        # Если после подмены цена превышает максимум — отсекаем только если коллекция не подпадает под границу флора
        if not is_under_floor and self.config.max_price_stars and price_stars and price_stars > self.config.max_price_stars:
            return False

        # Защита от повторного спама подменённого подарка из профиля
        if final_slug and final_slug != slug:
            sub_key = f"sub_slug:{final_slug}"
            if sub_key in self.db._seen_cache:
                return False
            self.db._seen_cache.add(sub_key)

        distinct_sellers = len(unique_owners_by_title.get(lot_title, set()))
        liquid = distinct_sellers >= self.config.supply_threshold

        listing_key = f"{gift_id}:{seller_id or owner_ident}"
        motivated, stale = await self.db.process_listing_history(
            listing_key=listing_key,
            price_stars=price_stars or 0,
            stale_minutes_threshold=self.config.stale_minutes_threshold,
        )

        lot_item = LotItem(
            lot_key=lot_key,
            gift_id=gift_id,
            raw_id=raw_id,
            title=lot_title,
            num=lot_num,
            slug=final_slug,
            price_stars=price_stars or 0,
            price_ton=price_ton or "0",
            model=model,
            model_rarity=model_rarity,
            backdrop=backdrop,
            pattern=pattern,
            seller_id=seller_id,
            username=seller.get("username"),
            first_name=seller.get("first_name"),
            last_name=seller.get("last_name"),
            level=level,
            send_paid_messages_stars=seller.get("send_paid_messages_stars"),
            premium=seller.get("premium", False),
            detected_at=detected_at,
            nft_gifts_count=nft_gifts_count,
            motivated=motivated,
            liquid=liquid,
            stale=stale,
        )

        outbox_id = await self.db.add_to_outbox(lot_item)
        if seller_id:
            await self.db.mark_seller_alerted(seller_id)
            if final_slug:
                await self.db.save_gift_registry(final_slug, seller_id, seller)
        logger.info(
            f"🎉 Находка! {lot_item.title} #{lot_item.num} за {price_stars} ★ "
            f"(продавец {seller_id or owner_name}, @{seller.get('username')}, ур. {level}, NFT: {nft_gifts_count}, "
            f"motivated={motivated}, liquid={liquid}, stale={stale}). Добавлено в outbox #{outbox_id}"
        )
        return True

    async def _deanonymization_worker(self, worker: AccountWorker) -> None:
        """Параллельный фоновый воркер де-анонимизации продавцов без замедления сканирования маркета."""
        logger.info(f"⚡ Воркер де-анонимизации #{worker.idx} ({worker.session_name}) запущен.")
        while True:
            try:
                item = await self.unresolved_queue.get()
            except asyncio.CancelledError:
                break

            g, gift_id, slug, owner_name, min_floor, unique_owners_by_title = item
            try:
                now_ts = asyncio.get_running_loop().time()
                if worker._profile_flood_wait_until > now_ts:
                    await asyncio.sleep(min(60.0, worker._profile_flood_wait_until - now_ts))

                seller_id, seller, owner_peer = await asyncio.wait_for(
                    self.resolve_gift_owner(
                        g, {}, worker=worker, is_upgrade=False, probe_deep=True
                    ),
                    timeout=15.0,
                )
                if seller_id and seller:
                    await self._process_resolved_market_lot(
                        g=g,
                        gift_id=gift_id,
                        slug=slug,
                        seller_id=seller_id,
                        seller=seller,
                        owner_peer=owner_peer,
                        min_floor=min_floor,
                        unique_owners_by_title=unique_owners_by_title,
                    )
                else:
                    self._unresolvable_slugs[slug] = asyncio.get_running_loop().time() + 300
            except asyncio.TimeoutError:
                logger.debug(f"[Воркер #{worker.idx}] Таймаут де-анонимизации {slug}")
                self._unresolvable_slugs[slug] = asyncio.get_running_loop().time() + 120
            except errors.FloodWaitError as e:
                worker._profile_flood_wait_until = asyncio.get_running_loop().time() + e.seconds + 1
                try:
                    self.unresolved_queue.put_nowait(item)
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"[Воркер #{worker.idx}] Ошибка де-анонимизации {slug}: {e}")
            finally:
                self._queued_slugs.discard(slug)
                self.unresolved_queue.task_done()

    async def refresh_upgrade_catalog(self) -> None:
        """Обновляет список моделей подарков, поддерживающих апгрейд за Stars."""
        now = asyncio.get_event_loop().time()
        if (
            self.last_upgrade_catalog_refresh
            and (now - self.last_upgrade_catalog_refresh < self.config.upgrade_catalog_refresh_seconds)
        ):
            return

        logger.info("Обновление каталога моделей для апгрейда (payments.getStarGifts)...")
        try:
            gifts_res = await self.client(functions.payments.GetStarGiftsRequest(hash=0))
        except errors.FloodWaitError as e:
            logger.warning(f"FloodWait при получении каталога апгрейдов: ожидание {e.seconds} сек.")
            await asyncio.sleep(e.seconds)
            return
        except Exception as e:
            logger.error(f"Ошибка получения каталога апгрейдов: {e}")
            return

        seen_short_names = set()
        upgradable_count = 0
        if hasattr(gifts_res, "gifts"):
            for g in gifts_res.gifts:
                upgrade_stars = getattr(g, "upgrade_stars", None)
                if upgrade_stars and upgrade_stars > 0:
                    raw_title = getattr(g, "title", "") or ""
                    short_name = re.sub(r"[\s\-\'’]+", "", raw_title)
                    if not short_name:
                        continue
                    seen_short_names.add(short_name)
                    upgradable_count += 1
                    await self.db.upsert_tracked_gift_model(
                        short_name=short_name, gift_id=g.id, active=True
                    )

        # Деактивировать модели, которых больше нет в каталоге
        active_in_db = await self.db.get_active_tracked_models()
        for m in active_in_db:
            sn = m["short_name"]
            if sn not in seen_short_names:
                await self.db.upsert_tracked_gift_model(
                    short_name=sn, gift_id=m["gift_id"], active=False
                )

        self.last_upgrade_catalog_refresh = now
        logger.info(
            f"Каталог апгрейдов обновлён. Найдено активных моделей с апгрейдом: {upgradable_count}"
        )

    async def find_max_slug_num(self, short_name: str) -> int:
        """
        Определяет текущий максимальный num для новой модели через экспоненциальные пробы
        и бинарный поиск, не отправляя уведомлений.
        """
        logger.debug(f"🔍 Инициализация новой модели апгрейдов '{short_name}' (поиск max num)...")

        # 1. Проверяем num=1
        try:
            await self.client(
                functions.payments.GetUniqueStarGiftRequest(slug=f"{short_name}-1")
            )
        except errors.FloodWaitError as e:
            logger.warning(f"FloodWait при инициализации {short_name}-1: {e.seconds} сек.")
            await asyncio.sleep(e.seconds)
            try:
                await self.client(
                    functions.payments.GetUniqueStarGiftRequest(slug=f"{short_name}-1")
                )
            except Exception:
                await self.db.set_upgrade_last_num(short_name, 0)
                return 0
        except Exception as e:
            if "STARGIFT_SLUG_INVALID" in str(e):
                logger.debug(f"Модель '{short_name}' пока не имеет апгрейдов (num=0).")
                await self.db.set_upgrade_last_num(short_name, 0)
                return 0
            else:
                logger.error(f"Ошибка при проверке {short_name}-1: {e}")
                await self.db.set_upgrade_last_num(short_name, 0)
                return 0

        low = 1
        test_num = 2
        high = None

        # 2. Экспоненциальный поиск верхней границы (num = 2, 4, 8, 16...)
        while True:
            await asyncio.sleep(0.3)
            try:
                await self.client(
                    functions.payments.GetUniqueStarGiftRequest(slug=f"{short_name}-{test_num}")
                )
                low = test_num
                test_num *= 2
            except errors.FloodWaitError as e:
                logger.warning(
                    f"FloodWait при экспоненциальном поиске {short_name}: {e.seconds} сек."
                )
                await asyncio.sleep(e.seconds)
            except Exception as e:
                if "STARGIFT_SLUG_INVALID" in str(e):
                    high = test_num
                    break
                else:
                    logger.error(f"Неожиданная ошибка при пробе {short_name}-{test_num}: {e}")
                    high = test_num
                    break

        # 3. Бинарный поиск между low и high
        while low + 1 < high:
            mid = (low + high) // 2
            await asyncio.sleep(0.3)
            try:
                await self.client(
                    functions.payments.GetUniqueStarGiftRequest(slug=f"{short_name}-{mid}")
                )
                low = mid
            except errors.FloodWaitError as e:
                logger.warning(
                    f"FloodWait при бинарном поиске {short_name}: {e.seconds} сек."
                )
                await asyncio.sleep(e.seconds)
            except Exception as e:
                if "STARGIFT_SLUG_INVALID" in str(e):
                    high = mid
                else:
                    logger.error(f"Неожиданная ошибка при пробе {short_name}-{mid}: {e}")
                    high = mid

        max_num = low
        await self.db.set_upgrade_last_num(short_name, max_num)
        logger.debug(f"✅ Модель '{short_name}' инициализирована: текущий max_num = {max_num}")
        return max_num

    async def poll_upgrades_batch(self) -> None:
        """Опрашивает одну пачку моделей из tracked_gift_models на предмет новых апгрейдов."""
        models = await self.db.get_active_tracked_models()
        if not models:
            return

        batch_size = self.config.upgrade_models_per_batch
        total_models = len(models)
        start_idx = self.upgrade_model_index % total_models
        if start_idx + batch_size <= total_models:
            batch = models[start_idx : start_idx + batch_size]
        else:
            batch = models[start_idx:] + models[: (start_idx + batch_size) % total_models]

        self.upgrade_model_index = (start_idx + batch_size) % total_models

        now_upg = asyncio.get_running_loop().time()
        if now_upg - self._last_upg_log_time >= 3.5:
            batch_names = ", ".join(m["short_name"] for m in batch[:3])
            logger.info(
                f"💎 [Апгрейды] Опрос моделей ({batch_names}...). Мониторинг {len(models)} моделей в реальном времени"
            )
            self._last_upg_log_time = now_upg

        tz = self.config.get_zone_info()
        # Ограничиваем параллельность апгрейдов до 1 запроса, чтобы не забивать сокеты воркеров маркета
        sem = asyncio.Semaphore(1)

        async def _check_one_model(m: Dict[str, Any]) -> None:
            async with sem:
                w = self.get_best_upgrade_worker()
                short_name = m["short_name"]
                gift_id = m["gift_id"]
                last_num = await self.db.get_upgrade_last_num(short_name)

                if last_num is None:
                    await self.find_max_slug_num(short_name)
                    return

                next_num = last_num + 1
                probes_count = 0

                while probes_count < self.config.upgrade_max_probes_per_cycle:
                    now_ts = asyncio.get_running_loop().time()
                    if w._flood_wait_until > now_ts:
                        # Переключаемся на другой свободный воркер, если текущий на паузе
                        alt = self.get_best_upgrade_worker()
                        if alt._flood_wait_until <= now_ts:
                            w = alt
                        else:
                            await asyncio.sleep(w._flood_wait_until - now_ts)

                    probes_count += 1
                    slug = f"{short_name}-{next_num}"

                    try:
                        async with w.call_slot(min_interval=0.35):
                            unique_res = await asyncio.wait_for(
                                w.client(
                                    functions.payments.GetUniqueStarGiftRequest(slug=slug)
                                ),
                                timeout=6.0,
                            )
                    except errors.FloodWaitError as e:
                        wait_time = e.seconds + 1
                        logger.warning(
                            f"[Воркер #{w.idx}] FloodWait ({e.seconds} сек.) при опросе апгрейда {slug}."
                        )
                        w._flood_wait_until = asyncio.get_running_loop().time() + wait_time
                        alt_ready = [
                            ow for ow in self.workers
                            if ow.idx != w.idx and ow._flood_wait_until <= asyncio.get_running_loop().time()
                        ]
                        if alt_ready:
                            w = alt_ready[0]
                            continue
                        else:
                            break
                    except errors.SessionRevokedError:
                        self.deactivate_worker(w, f"сессия отозвана при проверке апгрейда {slug}")
                        break
                    except Exception as e:
                        if "STARGIFT_SLUG_INVALID" in str(e):
                            break
                        logger.debug(f"Ошибка запроса payments.getUniqueStarGift({slug}): {e}")
                        break

                    gift_unique = getattr(unique_res, "gift", None)
                    if not gift_unique:
                        break

                    await self.db.set_upgrade_last_num(short_name, next_num)

                    if await self.db.has_seen_lot(f"upgrade:{slug}") or await self.db.has_seen_lot(f"slug:{slug}"):
                        next_num += 1
                        continue

                    # Извлекаем атрибуты уникального подарка
                    model, model_rarity, backdrop, pattern = parse_attributes(
                        getattr(gift_unique, "attributes", [])
                    )

                    users_map = {
                        u.id: u for u in getattr(unique_res, "users", []) if hasattr(u, "id")
                    }

                    # Фоновое пополнение seller_cache пользователями из ответа апгрейда
                    upg_sellers_batch = []
                    for u in users_map.values():
                        if getattr(u, "username", None) and not getattr(u, "bot", False) and not getattr(u, "deleted", False):
                            u_data = {
                                "seller_id": u.id,
                                "first_name": getattr(u, "first_name", "") or "",
                                "last_name": getattr(u, "last_name", "") or "",
                                "username": getattr(u, "username", "") or "",
                                "phone": getattr(u, "phone", None),
                                "premium": bool(getattr(u, "premium", False)),
                                "scam": bool(getattr(u, "scam", False)),
                                "fake": bool(getattr(u, "fake", False)),
                            }
                            upg_sellers_batch.append((u.id, u_data))
                    if upg_sellers_batch:
                        asyncio.create_task(self.db.save_cached_sellers_bulk(upg_sellers_batch))
                    best_w = self.get_best_profile_worker()
                    seller_id, seller, owner_peer = await self.resolve_gift_owner(
                        gift_unique, users_map, worker=best_w, is_upgrade=True
                    )

                    # Автоматически сохраняем каждый найденный апгрейд в таблицу scanned_upgrades
                    await self.db.save_scanned_upgrade(
                        slug=slug,
                        short_name=short_name,
                        num=next_num,
                        model=model,
                        model_rarity=model_rarity,
                        backdrop=backdrop,
                        pattern=pattern,
                        owner_id=seller_id or getattr(getattr(gift_unique, "owner_id", None), "user_id", None),
                        owner_name=getattr(gift_unique, "owner_name", None),
                        username=seller.get("username") if seller else None,
                        first_name=seller.get("first_name") if seller else None,
                        level=seller.get("level") if seller else None,
                        nft_gifts_count=seller.get("nft_gifts_count") if seller else None,
                        raw_data=json.dumps(seller, ensure_ascii=False) if seller else None,
                    )

                    if not seller or not seller_id:
                        logger.debug(
                            f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: не удалось определить владельца"
                        )
                        next_num += 1
                        continue

                    upg_owner_name = getattr(gift_unique, "owner_name", None) or ""
                    if upg_owner_name and not verify_seller_name(upg_owner_name, seller):
                        logger.debug(
                            f"Апгрейд {slug} ({short_name} #{next_num}) отклонён: имя профиля {seller.get('username')} "
                            f"({seller.get('first_name')} {seller.get('last_name')}) не совпадает с '{upg_owner_name}'"
                        )
                        next_num += 1
                        continue

                    # Сохраняем в постоянный реестр подарков для распознавания при перепродаже
                    await self.db.save_gift_registry(slug, seller_id, seller)

                    # 1. Фильтр наличия username: обязательно наличие @username для прямой связи
                    if self.config.require_username and not seller.get("username"):
                        logger.debug(
                            f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: у владельца {seller_id} нет username"
                        )
                        next_num += 1
                        continue

                    # 1.1. Проверка наличия продающихся подарков в открытом профиле владельца
                    if getattr(self.config, "require_gift_on_profile", True):
                        p_gifts = seller.get("profile_gifts") or []
                        require_exact = getattr(self.config, "require_exact_gift_on_profile", False)
                        if not has_gift_on_profile(p_gifts, slug=slug, num=next_num, title=short_name, require_exact=require_exact):
                            logger.debug(
                                f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: нет продающихся подарков в профиле владельца {seller_id}"
                            )
                            next_num += 1
                            continue

                    # 2. Фильтр бесплатных сообщений: сообщения ОБЯЗАТЕЛЬНО должны быть бесплатными
                    paid_stars = seller.get("send_paid_messages_stars")
                    if paid_stars is not None and paid_stars > 0:
                        logger.debug(
                            f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: у владельца {seller_id} платные сообщения ({paid_stars} ★)"
                        )
                        next_num += 1
                        continue
                    if seller.get("contact_require_premium"):
                        logger.debug(
                            f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: у владельца {seller_id} сообщения требуют Premium/контакты"
                        )
                        next_num += 1
                        continue

                    # 3. Фильтр уровня владельца (stars_rating.level <= max_level)
                    level = seller.get("level")
                    if level is None:
                        if self.config.unknown_level_policy == "skip":
                            logger.debug(
                                f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: у владельца {seller_id} нет stars_rating (политика: skip)"
                            )
                            next_num += 1
                            continue
                    else:
                        upgrade_max_lvl = getattr(self.config, "upgrade_max_level", self.config.max_level)
                        if level > upgrade_max_lvl:
                            logger.debug(
                                f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: уровень владельца {level} > {upgrade_max_lvl}"
                            )
                            next_num += 1
                            continue

                    # 4. Фильтр количества коллекционных (NFT) подарков у владельца (строго от 1 до max_gifts)
                    nft_gifts_count = seller.get("nft_gifts_count")
                    if nft_gifts_count is None or nft_gifts_count <= 0:
                        logger.debug(
                            f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: у владельца {seller_id} нет NFT-подарков ({nft_gifts_count})"
                        )
                        next_num += 1
                        continue
                    max_gifts = getattr(self.config, "upgrade_owner_max_gifts", self.config.max_nft_gifts)
                    if max_gifts is not None and nft_gifts_count > max_gifts:
                        logger.debug(
                            f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: у владельца {seller_id} {nft_gifts_count} NFT-подарков "
                            f"(максимум разрешено: {max_gifts})"
                        )
                        next_num += 1
                        continue

                    # 5. СНГ-эвристика: кириллица в имени, фамилии, описании, юзернейме, телефоне или СНГ-стиле никнейма
                    if self.config.require_cyrillic:
                        fn = seller.get('first_name', '') or ''
                        ln = seller.get('last_name', '') or ''
                        ab = seller.get('about', '') or ''
                        un = seller.get('username', '') or ''
                        upg_name = getattr(gift_unique, "owner_name", None) or ""
                        combined_text = f"{fn} {ln} {ab} {un} {upg_name}".strip()

                        is_cis = has_cyrillic(combined_text)
                        if not is_cis:
                            phone = str(seller.get('phone') or '')
                            if any(phone.startswith(pfx) for pfx in ("7", "+7", "375", "+375", "380", "+380", "998", "+998", "992", "+992", "996", "+996")):
                                is_cis = True
                        if not is_cis:
                            letters_only = re.sub(r'[^a-zA-Z]', '', f"{fn}{ln}")
                            if not letters_only and (fn or ln):
                                is_cis = True
                        if not is_cis:
                            translit_stems = (
                                "vlad", "dima", "nikit", "sash", "mish", "artem", "maxim", "maks", "danil", "dany",
                                "egor", "kirill", "andrey", "serg", "ivan", "vanya", "anya", "nastya", "katya",
                                "masha", "dasha", "sonya", "alina", "polin", "ksyu", "ler", "vik", "yarik", "gleb",
                                "roma", "anton", "bogdan", "pavel", "pasha", "marat", "timur", "denis", "ruslan",
                                "stepan", "vadim", "daria", "soni", "dash", "aleks", "oleg", "igor", "kolya", "zhenya"
                            )
                            combined_lower = combined_text.lower()
                            if any(stem in combined_lower for stem in translit_stems):
                                is_cis = True

                        if not is_cis:
                            logger.debug(
                                f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: в профиле владельца {seller_id} нет СНГ-признаков"
                            )
                            next_num += 1
                            continue

                    # 5.1 Анти-трейдер и анти-скам: отсечение перекупов, гарантов, каналов и отзывов
                    if is_trader_or_scam(
                        seller.get("first_name", ""),
                        seller.get("last_name", ""),
                        seller.get("about", ""),
                        username=seller.get("username", ""),
                    ):
                        logger.debug(
                            f"Апгрейд {slug} ({short_name} #{next_num}) пропущен: владелец {seller_id} содержит стоп-слова трейдера/отзывов в профиле"
                        )
                        next_num += 1
                        continue

                    detected_at = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")

                    lot_item = LotItem(
                        lot_key=f"upgrade:{slug}",
                        gift_id=gift_id,
                        raw_id=getattr(gift_unique, "id", 0) or 0,
                        title=getattr(gift_unique, "title", short_name) or short_name,
                        num=getattr(gift_unique, "num", next_num) or next_num,
                        slug=getattr(gift_unique, "slug", slug) or slug,
                        price_stars=0,
                        price_ton=None,
                        model=model,
                        model_rarity=model_rarity,
                        backdrop=backdrop,
                        pattern=pattern,
                        seller_id=seller_id,
                        username=seller.get("username"),
                        first_name=seller.get("first_name"),
                        last_name=seller.get("last_name"),
                        level=level,
                        send_paid_messages_stars=seller.get("send_paid_messages_stars"),
                        premium=seller.get("premium", False),
                        detected_at=detected_at,
                        nft_gifts_count=nft_gifts_count,
                        motivated=False,
                        liquid=False,
                        stale=False,
                        event_type="upgrade",
                    )
                    outbox_id = await self.db.add_to_outbox(lot_item)
                    await self.db.mark_lot_seen(lot_item.lot_key)
                    await self.db.mark_lot_seen(f"slug:{slug}")
                    if seller_id:
                        await self.db.mark_seller_alerted(seller_id)
                    logger.info(
                        f"💎 Новый апгрейд! {lot_item.title} #{lot_item.num} ({slug}) "
                        f"фон='{backdrop}', модель='{model}'. Владелец: @{seller.get('username') or seller_id} "
                        f"(ур. {level}, NFT: {nft_gifts_count}). Добавлено в outbox #{outbox_id}"
                    )

                    next_num += 1
                    await asyncio.sleep(0.1)

        await asyncio.gather(*[_check_one_model(m) for m in batch])

    async def run_market_loop(self) -> None:
        """Цикл мониторинга вторичного рынка (resale) с динамической балансировкой между воркерами."""
        total_workers = len(self.workers)
        logger.info(
            f"Цикл мониторинга маркета перепродажи запущен (активных воркеров маркета: {total_workers})."
        )

        async def _worker_loop(worker: AccountWorker) -> None:
            logger.info(
                f"🚀 Воркер маркета #{worker.idx} ({worker.session_name}) приступил к работе."
            )
            base_delay = getattr(self.config, "collection_delay_seconds", 0.55)
            worker.adaptive_delay = base_delay
            # Разносим старт воркеров, чтобы запросы были идеально распределены во времени
            await asyncio.sleep((worker.idx - 1) * (base_delay / max(1, total_workers)))

            while True:
                try:
                    now_ts = asyncio.get_running_loop().time()
                    if worker._market_flood_wait_until > now_ts:
                        logger.warning(f"⚠️ [Воркер #{worker.idx}] Пауза FloodWait маркета: {worker._market_flood_wait_until - now_ts:.1f} сек.")
                        await asyncio.sleep(worker._market_flood_wait_until - now_ts)

                    gid = await self.get_next_collection_id()
                    if gid is None:
                        logger.warning(f"⚠️ [Воркер #{worker.idx}] get_next_collection_id вернул None!")
                        await asyncio.sleep(1)
                        continue

                    # logger.info(f"➡️ [Воркер #{worker.idx}] Старт poll_collection({gid})...")
                    await self.poll_collection(gid, worker=worker)
                    # logger.info(f"⬅️ [Воркер #{worker.idx}] Финиш poll_collection({gid}).")

                    await asyncio.sleep(0.01)

                except asyncio.CancelledError:
                    logger.info(f"Воркер маркета #{worker.idx} останавливается...")
                    break
                except Exception as e:
                    logger.error(
                        f"[Воркер #{worker.idx}] Ошибка в цикле мониторинга маркета: {e}",
                        exc_info=True,
                    )
                    await asyncio.sleep(2)

        async def _collections_refresher() -> None:
            while True:
                try:
                    await asyncio.sleep(600)  # каждые 10 минут
                    await self.update_collections()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Ошибка периодического обновления коллекций: {e}")
                    await asyncio.sleep(30)

        async def _deep_market_harvester() -> None:
            """Периодически собирает продавцов с глубоких страниц маркета для опережающего пополнения базы."""
            await asyncio.sleep(20)
            logger.info("🔍 Фоновый сборщик глубоких страниц маркета (Deep Harvester) запущен.")
            while True:
                try:
                    target_cids = list(self.priority_collection_ids or self.known_collection_ids)
                    if target_cids and self.workers:
                        cids_sample = random.sample(
                            target_cids,
                            min(6, len(target_cids))
                        )
                        for cid in cids_sample:
                            offset = ""
                            for page in range(3):
                                w = self.get_best_profile_worker()
                                try:
                                    async with w.call_slot(min_interval=0.60):
                                        deep_res = await asyncio.wait_for(
                                            w.client(
                                                functions.payments.GetResaleStarGiftsRequest(
                                                    gift_id=cid,
                                                    offset=offset,
                                                    limit=100,
                                                    stars_only=True,
                                                )
                                            ),
                                            timeout=7.0,
                                        )
                                    u_batch = []
                                    for u in getattr(deep_res, "users", []) or []:
                                        if getattr(u, "username", None) and not getattr(u, "bot", False) and not getattr(u, "deleted", False):
                                            u_batch.append((u.id, {
                                                "seller_id": u.id,
                                                "first_name": getattr(u, "first_name", "") or "",
                                                "last_name": getattr(u, "last_name", "") or "",
                                                "username": getattr(u, "username", "") or "",
                                                "premium": bool(getattr(u, "premium", False)),
                                            }))
                                    if u_batch:
                                        await self.db.save_cached_sellers_bulk(u_batch)

                                    next_off = getattr(deep_res, "next_offset", "") or ""
                                    if not next_off or next_off == offset:
                                        break
                                    offset = next_off
                                    await asyncio.sleep(1.0)
                                except Exception as de:
                                    logger.debug(f"Deep harvester error ({cid}, page {page}): {de}")
                                    break
                            await asyncio.sleep(2.0)
                    await asyncio.sleep(120)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Deep harvester loop error: {e}")
                    await asyncio.sleep(30)

        async def _watchdog_loop() -> None:
            """Контролирует активность воркеров. Если опрос маркета завис более чем на 90 сек — перезапускает процесс."""
            while True:
                await asyncio.sleep(15)
                now = asyncio.get_running_loop().time()
                if self._last_successful_poll_time > 0 and (now - self._last_successful_poll_time > 90):
                    logger.error(
                        f"❌ [WATCHDOG USERBOT] Обнаружено зависание сетевых сокетов маркета "
                        f"(нет активности {int(now - self._last_successful_poll_time)} сек). "
                        f"Экстренный перезапуск процесса..."
                    )
                    os._exit(1)

        tasks = [
            asyncio.create_task(_worker_loop(w))
            for w in self.workers
        ]
        deanonymizer_tasks = [
            asyncio.create_task(self._deanonymization_worker(w))
            for w in self.workers
        ]
        refresher = asyncio.create_task(_collections_refresher())
        harvester = asyncio.create_task(_deep_market_harvester())
        watchdog = asyncio.create_task(_watchdog_loop())

        all_tasks = [refresher, harvester, watchdog, *tasks, *deanonymizer_tasks]

        try:
            await asyncio.gather(*all_tasks)
        except asyncio.CancelledError:
            for t in all_tasks:
                t.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)

    async def run_upgrade_loop(self) -> None:
        """Цикл мониторинга новых апгрейдов."""
        logger.info("Цикл мониторинга новых апгрейдов запущен (фоновый режим с приоритетом маркета).")
        while True:
            try:
                await self.refresh_upgrade_catalog()
                await self.poll_upgrades_batch()
                await asyncio.sleep(self.config.upgrade_poll_interval)
            except asyncio.CancelledError:
                logger.info("Цикл апгрейдов останавливается...")
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга апгрейдов: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def run(self) -> None:
        """Главный метод запуска: старт клиента и запуск циклов мониторинга."""
        await self.start()
        await self.db.init_db()

        # Первичное получение коллекций и сохранение существующих лотов маркета
        await self.update_collections()
        await self.bootstrap_initial_lots()

        enable_upgrades = getattr(self.config, "enable_upgrades", False)
        if enable_upgrades:
            await self.refresh_upgrade_catalog()

        total_workers = len(self.workers)
        logger.info(
            f"🚀 Все сервисы мониторинга активны! "
            f"Маркет: задержка {self.config.collection_delay_seconds} сек. ({len(self.known_collection_ids)} коллекций, все {total_workers} воркеров на маркете). "
            f"Апгрейды: {'включены' if enable_upgrades else 'отключены'} (фоновый цикл ~2.5 мин)."
        )

        tasks = [self.run_market_loop()]
        if enable_upgrades:
            tasks.append(self.run_upgrade_loop())

        await asyncio.gather(*tasks)


async def main() -> None:
    config = load_config()
    db = Database(config.database_path)
    monitor = UserbotMonitor(config, db)
    try:
        await monitor.run()
    except RuntimeError as e:
        if "не авторизован" in str(e):
            logger.error(f"❌ {e}")
            sys.exit(2)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Userbot остановлен пользователем.")
