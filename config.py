import os
import sys
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from zoneinfo import ZoneInfo
import yaml
from dotenv import load_dotenv

# Ensure stdout and stderr use UTF-8 on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()


@dataclass
class TelegramAccountConfig:
    session_name: str
    api_id: int
    api_hash: str


@dataclass
class AppConfig:
    # Secrets from .env
    telegram_api_id: Optional[int] = None
    telegram_api_hash: Optional[str] = None
    accounts: List[TelegramAccountConfig] = field(default_factory=list)
    bot_token: Optional[str] = None
    owner_chat_id: Optional[int] = None
    owner_address: Optional[str] = None
    ton_network: str = "mainnet"  # "mainnet" or "testnet"
    tonapi_key: Optional[str] = None
    admin_username: Optional[str] = None
    usdt_master_address: str = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
    team_invite_link: str = "https://t.me/+your_team_invite_link"
    reviews_chat_link: Optional[str] = None
    trial_hours: int = 0
    tariffs: dict = field(
        default_factory=lambda: {
            "market": {
                "6h": 0.2,
                "1d": 0.5,
                "3d": 1.0,
                "7d": 1.9,
                "30d": 4.0,
                "forever": 5.0,
            },
            "upgrades": {
                "6h": 0.2,
                "1d": 0.5,
                "3d": 1.0,
                "7d": 1.9,
                "30d": 4.0,
                "forever": 5.0,
            },
            "both": {
                "6h": 0.3,
                "1d": 0.7,
                "3d": 1.5,
                "7d": 3.0,
                "30d": 6.0,
                "forever": 7.5,
            },
            "team": {
                "access": 0.5,
            },
        }
    )

    # Settings from config.yaml
    poll_interval_seconds: int = 4
    collections: List[str | int] = field(default_factory=list)
    max_level: int = 2
    supply_threshold: int = 3
    stale_minutes_threshold: int = 30
    max_nft_gifts: Optional[int] = 5
    unknown_level_policy: str = "skip"  # "skip" or "allow"
    require_cyrillic: bool = True
    require_username: bool = True
    require_gift_on_profile: bool = True
    require_exact_gift_on_profile: bool = False
    use_profile_gift_link: bool = True
    enable_global_search: bool = False
    enable_slug_probing: bool = True
    global_search_rate_limit: float = 0.5
    max_price_stars: Optional[int] = None
    max_floor_overprice_ratio: Optional[float] = None
    max_collection_floor: Optional[int] = 17000
    timezone: str = "Europe/Moscow"
    offer_discount: int = 50
    message_template: str = (
        "привет нфт подарок за рубли/$/звезды/тоны напрямую продашь? "
        "просто 3 недели кд ждать и комку платить не хочу (ботов не хожу, гарантов по ситуации)"
    )
    seller_cache_ttl_hours: float = 6.0
    seller_cooldown_hours: float = 6.0
    rate_limit_messages_per_second: int = 4
    database_path: str = "gifts_monitor.db"
    session_name: str = "userbot"
    distribute_leads_round_robin: bool = False
    user_rate_limit_seconds: float = 0.0
    market_concurrency: int = 3
    collection_delay_seconds: float = 0.65
    enable_upgrades: bool = False

    # Settings for upgrade monitoring
    upgrade_catalog_refresh_seconds: int = 3600
    upgrade_poll_interval: int = 2
    upgrade_models_per_batch: int = 25
    upgrade_max_probes_per_cycle: int = 20
    upgrade_max_level: int = 2
    upgrade_owner_max_gifts: int = 5
    upgrade_unknown_gifts_policy: str = "skip"  # "skip" or "allow"
    upgrade_owner_cache_ttl: float = 6.0
    upgrade_message_template: str = (
        "привет нфт подарок за рубли/$/звезды/тоны напрямую продашь? "
        "просто 3 недели кд ждать и комку платить не хочу (ботов не хожу, гарантов по ситуации)"
    )

    def get_zone_info(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception as e:
            logger.warning(
                f"Не удалось загрузить таймзону '{self.timezone}' ({e}), используется Europe/Moscow"
            )
            try:
                return ZoneInfo("Europe/Moscow")
            except Exception:
                return ZoneInfo("UTC")


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """Loads configuration from environment variables (.env) and YAML config."""
    config = AppConfig()

    # Load from .env (Поддержка мульти-аккаунтного пула)
    api_id_env = os.getenv("TELEGRAM_API_ID")
    api_hash_env = os.getenv("TELEGRAM_API_HASH")
    if api_id_env and api_id_env.strip() and api_hash_env and api_hash_env.strip():
        try:
            aid = int(api_id_env.strip())
            ahash = api_hash_env.strip()
            config.telegram_api_id = aid
            config.telegram_api_hash = ahash
            sess_name = os.getenv("TELEGRAM_SESSION_1", "userbot")
            config.accounts.append(
                TelegramAccountConfig(
                    session_name=sess_name,
                    api_id=aid,
                    api_hash=ahash,
                )
            )
        except ValueError:
            logger.error("TELEGRAM_API_ID в .env должен быть целым числом")

    # Дополнительные аккаунты (TELEGRAM_API_ID_2, TELEGRAM_API_ID_3 и т.д.)
    for idx in range(2, 11):
        aid_env = os.getenv(f"TELEGRAM_API_ID_{idx}")
        ahash_env = os.getenv(f"TELEGRAM_API_HASH_{idx}")
        if aid_env and aid_env.strip() and ahash_env and ahash_env.strip():
            try:
                aid = int(aid_env.strip())
                ahash = ahash_env.strip()
                sess_name = os.getenv(f"TELEGRAM_SESSION_{idx}", f"userbot_{idx}")
                config.accounts.append(
                    TelegramAccountConfig(
                        session_name=sess_name,
                        api_id=aid,
                        api_hash=ahash,
                    )
                )
            except ValueError:
                logger.error(f"TELEGRAM_API_ID_{idx} в .env должен быть целым числом")

    bot_token_env = os.getenv("BOT_TOKEN")
    if bot_token_env and bot_token_env.strip():
        config.bot_token = bot_token_env.strip()

    owner_id_env = os.getenv("OWNER_CHAT_ID")
    if owner_id_env and owner_id_env.strip():
        try:
            config.owner_chat_id = int(owner_id_env.strip())
        except ValueError:
            logger.error("OWNER_CHAT_ID в .env должен быть целым числом")

    owner_addr_env = os.getenv("OWNER_ADDRESS")
    if owner_addr_env and owner_addr_env.strip():
        config.owner_address = owner_addr_env.strip()

    network_env = os.getenv("TON_NETWORK") or os.getenv("NETWORK")
    if network_env and network_env.strip():
        config.ton_network = network_env.strip().lower()

    tonapi_key_env = os.getenv("TONAPI_KEY")
    if tonapi_key_env and tonapi_key_env.strip():
        config.tonapi_key = tonapi_key_env.strip()

    admin_username_env = os.getenv("ADMIN_USERNAME")
    if admin_username_env and admin_username_env.strip():
        config.admin_username = admin_username_env.strip().lstrip("@")

    usdt_master_env = os.getenv("USDT_MASTER_ADDRESS")
    if usdt_master_env and usdt_master_env.strip():
        config.usdt_master_address = usdt_master_env.strip()

    # Load from config.yaml
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            if "poll_interval_seconds" in data:
                config.poll_interval_seconds = int(data["poll_interval_seconds"])
            if "collections" in data and isinstance(data["collections"], list):
                config.collections = data["collections"]
            if "max_level" in data:
                config.max_level = int(data["max_level"])
            if "supply_threshold" in data:
                config.supply_threshold = int(data["supply_threshold"])
            if "stale_minutes_threshold" in data:
                config.stale_minutes_threshold = int(data["stale_minutes_threshold"])
            if "max_nft_gifts" in data:
                config.max_nft_gifts = (
                    int(data["max_nft_gifts"])
                    if data["max_nft_gifts"] is not None
                    else None
                )
            if "unknown_level_policy" in data:
                config.unknown_level_policy = str(data["unknown_level_policy"]).strip().lower()
            if "require_cyrillic" in data:
                config.require_cyrillic = bool(data["require_cyrillic"])
            if "require_username" in data:
                config.require_username = bool(data["require_username"])
            if "require_gift_on_profile" in data:
                config.require_gift_on_profile = bool(data["require_gift_on_profile"])
            if "require_exact_gift_on_profile" in data:
                config.require_exact_gift_on_profile = bool(data["require_exact_gift_on_profile"])
            if "enable_global_search" in data:
                config.enable_global_search = bool(data["enable_global_search"])
            if "enable_slug_probing" in data:
                config.enable_slug_probing = bool(data["enable_slug_probing"])
            if "global_search_rate_limit" in data:
                config.global_search_rate_limit = float(data["global_search_rate_limit"])
            if "max_price_stars" in data:
                config.max_price_stars = (
                    int(data["max_price_stars"])
                    if data["max_price_stars"] is not None
                    else None
                )
            if "max_floor_overprice_ratio" in data:
                config.max_floor_overprice_ratio = (
                    float(data["max_floor_overprice_ratio"])
                    if data["max_floor_overprice_ratio"] is not None
                    else None
                )
            if "max_collection_floor" in data:
                config.max_collection_floor = (
                    int(data["max_collection_floor"])
                    if data["max_collection_floor"] is not None
                    else None
                )
            if "timezone" in data:
                config.timezone = str(data["timezone"]).strip()
            if "offer_discount" in data:
                config.offer_discount = int(data["offer_discount"])
            if "message_template" in data:
                config.message_template = str(data["message_template"]).strip()
            if "seller_cache_ttl_hours" in data:
                config.seller_cache_ttl_hours = float(data["seller_cache_ttl_hours"])
            if "seller_cooldown_hours" in data:
                config.seller_cooldown_hours = float(data["seller_cooldown_hours"])
            if "rate_limit_messages_per_second" in data:
                config.rate_limit_messages_per_second = int(data["rate_limit_messages_per_second"])
            if "database_path" in data:
                config.database_path = str(data["database_path"]).strip()
            if "distribute_leads_round_robin" in data:
                config.distribute_leads_round_robin = bool(data["distribute_leads_round_robin"])
            if "user_rate_limit_seconds" in data:
                config.user_rate_limit_seconds = float(data["user_rate_limit_seconds"])
            if "market_concurrency" in data:
                config.market_concurrency = int(data["market_concurrency"])
            if "collection_delay_seconds" in data:
                config.collection_delay_seconds = float(data["collection_delay_seconds"])
            if "enable_upgrades" in data:
                config.enable_upgrades = bool(data["enable_upgrades"])
            if "upgrade_catalog_refresh_seconds" in data:
                config.upgrade_catalog_refresh_seconds = int(data["upgrade_catalog_refresh_seconds"])
            if "upgrade_poll_interval" in data:
                config.upgrade_poll_interval = int(data["upgrade_poll_interval"])
            if "upgrade_models_per_batch" in data:
                config.upgrade_models_per_batch = int(data["upgrade_models_per_batch"])
            if "upgrade_max_probes_per_cycle" in data:
                config.upgrade_max_probes_per_cycle = int(data["upgrade_max_probes_per_cycle"])
            if "upgrade_max_level" in data:
                config.upgrade_max_level = int(data["upgrade_max_level"])
            if "upgrade_owner_max_gifts" in data:
                config.upgrade_owner_max_gifts = int(data["upgrade_owner_max_gifts"])
            if "upgrade_unknown_gifts_policy" in data:
                config.upgrade_unknown_gifts_policy = str(data["upgrade_unknown_gifts_policy"]).strip().lower()
            if "upgrade_owner_cache_ttl" in data:
                config.upgrade_owner_cache_ttl = float(data["upgrade_owner_cache_ttl"])
            if "upgrade_message_template" in data:
                config.upgrade_message_template = str(data["upgrade_message_template"]).strip()
            if "trial_hours" in data:
                config.trial_hours = int(data["trial_hours"])
            if "team_invite_link" in data:
                config.team_invite_link = str(data["team_invite_link"]).strip()
            if "reviews_chat_link" in data and data["reviews_chat_link"]:
                config.reviews_chat_link = str(data["reviews_chat_link"]).strip()
            if "tariffs" in data and isinstance(data["tariffs"], dict):
                for sub_t, durations in data["tariffs"].items():
                    if isinstance(durations, dict):
                        if sub_t not in config.tariffs:
                            config.tariffs[sub_t] = {}
                        for dur, price in durations.items():
                            config.tariffs[sub_t][dur] = float(price)
        except Exception as e:
            logger.error(f"Ошибка чтения {config_path}: {e}")
    else:
        logger.warning(f"Файл {config_path} не найден, используются настройки по умолчанию")

    return config


# SWOP TRACKER — PRIVATE GROUP

TRACKER_GROUP_ID = -1006051447834

TRACKER_TOPICS = {
    "news": 1,
    "low": 3,
    "medium": 2,
    "premium": 8,
    "elite": 9,
    "exclusive": 10,
}

# Price ranges in TON

TRACKER_PRICE_RANGES = [
    (0, 10, "low"),
    (10, 20, "medium"),
    (20, 50, "premium"),
    (50, 100, "elite"),
    (100, float("inf"), "exclusive"),
]

FREE_ACCESS = True
RESERVATION_MINUTES = 15
