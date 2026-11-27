import html
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, Dict

MSK_TZ = timezone(timedelta(hours=3))


def format_datetime_msk(val: Any, with_seconds: bool = False) -> str:
    """Форматирует дату/время в московское время (МСК, UTC+3)."""
    if not val:
        return "—"
    if val == "forever":
        return "навсегда"
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val)
        except Exception:
            return val
    elif isinstance(val, datetime):
        dt = val
    else:
        return str(val)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    msk_dt = dt.astimezone(MSK_TZ)
    fmt = "%d.%m.%Y %H:%M:%S МСК" if with_seconds else "%d.%m.%Y %H:%M МСК"
    return msk_dt.strftime(fmt)


def format_time_remaining(total_seconds: int) -> str:
    """Форматирует оставшееся время в человекочитаемый вид (д, ч, мин)."""
    if total_seconds <= 0:
        return "срок истек"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    if days > 0:
        if hours > 0:
            return f"{days} д. {hours} ч."
        return f"{days} д."
    if hours > 0:
        if minutes > 0:
            return f"{hours} ч. {minutes} мин."
        return f"{hours} ч."
    if minutes > 0:
        return f"{minutes} мин."
    return "< 1 мин."


def format_hours_ru(hours: int) -> str:
    """Склонение часов на русском языке (1 час, 2 часа, 5 часов)."""
    abs_h = abs(hours)
    if abs_h % 10 == 1 and abs_h % 100 != 11:
        return f"{hours} час"
    elif 2 <= abs_h % 10 <= 4 and not (12 <= abs_h % 100 <= 14):
        return f"{hours} часа"
    else:
        return f"{hours} часов"


def is_sub_active(until_val: Optional[str], now: Optional[datetime] = None) -> bool:
    """Проверяет, активна ли подписка в данный момент времени."""
    if not until_val:
        return False
    if until_val == "forever":
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        exp = datetime.fromisoformat(until_val)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp >= now
    except Exception:
        return False


@dataclass
class LotItem:
    lot_key: str
    gift_id: int
    raw_id: int
    title: str
    num: int
    slug: str
    price_stars: int
    price_ton: Optional[str] = None
    model: str = "—"
    model_rarity: str = "—"
    backdrop: str = "—"
    pattern: str = "—"
    seller_id: int = 0
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    level: Optional[int] = 1
    send_paid_messages_stars: Optional[int] = None
    premium: bool = False
    detected_at: str = ""
    nft_gifts_count: Optional[int] = None
    motivated: bool = False
    liquid: bool = False
    stale: bool = False
    event_type: str = "market"  # "market" or "upgrade"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LotItem":
        valid_keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in valid_keys})

    @classmethod
    def from_json(cls, json_str: str) -> "LotItem":
        import json
        return cls.from_dict(json.loads(json_str))

    def format_notification_html(self) -> str:
        """Форматирует HTML-текст уведомления строго по требуемому шаблону."""
        safe_title = html.escape(self.title)
        safe_slug = html.escape(self.slug)
        safe_model = html.escape(self.model)
        safe_rarity = html.escape(self.model_rarity)
        safe_backdrop = html.escape(self.backdrop)
        safe_pattern = html.escape(self.pattern)

        title_link = f'<a href="https://t.me/nft/{safe_slug}">{safe_title} #{self.num}</a>'

        safe_first_name = html.escape(self.first_name) if self.first_name else ""
        if self.username:
            clean_user = html.escape(self.username.lstrip("@"))
            seller_link = f'<a href="https://t.me/{clean_user}">@{clean_user}</a>'
            seller_line = f"👤 {seller_link} · <code>{self.seller_id}</code>\n" if self.seller_id else f"👤 {seller_link}\n"
        elif self.seller_id:
            seller_link = f'<a href="tg://user?id={self.seller_id}">профиль</a>'
            seller_line = f"👤 {seller_link} · <code>{self.seller_id}</code>\n"
        else:
            owner_display = safe_first_name or "Аноним"
            seller_line = f"👤 <b>{owner_display}</b> <i>(скрыт Telegram)</i>\n"

        level_str = str(self.level) if self.level is not None else "неизвестен"
        messages_str = (
            "бесплатно"
            if not self.send_paid_messages_stars
            else f"{self.send_paid_messages_stars} ★"
        )
        premium_status = "с Premium" if self.premium else "без Premium"

        nft_line = (
            f"🎁 NFT у продавца: {self.nft_gifts_count}\n"
            if self.nft_gifts_count is not None
            else ""
        )

        # Шаблон для события "upgrade" (без цены)
        if self.event_type == "upgrade":
            return (
                f"💎 <b>Новый апгрейд</b>\n\n"
                f"🎁 {title_link}\n\n"
                f"🧩 Модель: {safe_model} ({safe_rarity})\n"
                f"🎨 Фон: {safe_backdrop} · Узор: {safe_pattern}\n\n"
                f"{seller_line}"
                f"📊 Уровень: {level_str}\n"
                f"{nft_line}"
                f"💬 Сообщения: {messages_str}\n"
                f"⭐ {premium_status}\n\n"
                f"🕓 {self.detected_at}"
            )

        # Шаблон для события "market" (с ценой)
        if self.price_ton:
            price_line = f"💰 <b>{self.price_stars} ★</b> · {self.price_ton} TON"
        else:
            price_line = f"💰 <b>{self.price_stars} ★</b>"

        nft_line = (
            f"🎁 NFT у продавца: {self.nft_gifts_count}\n"
            if self.nft_gifts_count is not None
            else ""
        )

        return (
            f"🎉 <b>Новый листинг</b>\n\n"
            f"🎁 {title_link}\n"
            f"{price_line}\n\n"
            f"🧩 Модель: {safe_model} ({safe_rarity})\n"
            f"🎨 Фон: {safe_backdrop} · Узор: {safe_pattern}\n\n"
            f"{seller_line}"
            f"📊 Уровень: {level_str}\n"
            f"{nft_line}"
            f"💬 Сообщения: {messages_str}\n"
            f"⭐ {premium_status}\n\n"
            f"🕓 {self.detected_at}"
        )

    def format_claimed_notification_html(self, claimed_by_mention: str) -> str:
        """Форматирует HTML-текст уведомления для общей ленты после того, как лот заняли.
        Юзернейм продавца, ссылка на контакт и ссылка на NFT-подарок полностью скрываются,
        чтобы никто больше не мог перейти или написать продавцу."""
        safe_title = html.escape(self.title)
        safe_model = html.escape(self.model)
        safe_rarity = html.escape(self.model_rarity)
        safe_backdrop = html.escape(self.backdrop)
        safe_pattern = html.escape(self.pattern)
        safe_mention = html.escape(claimed_by_mention)

        # Без ссылки на маркет и БЕЗ порядкового номера (чтобы никто не мог вычислить slug и лот):
        title_text = f"<b>{safe_title}</b>"

        level_str = str(self.level) if self.level is not None else "неизвестен"
        messages_str = (
            "бесплатно"
            if not self.send_paid_messages_stars
            else f"{self.send_paid_messages_stars} ★"
        )
        premium_status = "с Premium" if self.premium else "без Premium"

        nft_line = (
            f"🎁 NFT у продавца: {self.nft_gifts_count}\n"
            if self.nft_gifts_count is not None
            else ""
        )

        if self.event_type == "upgrade":
            return (
                f"🔒 <b>Апгрейд занят ({safe_mention})</b>\n\n"
                f"🎁 {title_text}\n\n"
                f"🧩 Модель: {safe_model} ({safe_rarity})\n"
                f"🎨 Фон: {safe_backdrop} · Узор: {safe_pattern}\n\n"
                f"👤 <i>[Контакт продавца скрыт — занято {safe_mention}]</i>\n"
                f"📊 Уровень: {level_str}\n"
                f"{nft_line}"
                f"💬 Сообщения: {messages_str}\n"
                f"⭐ {premium_status}\n\n"
                f"🕓 {self.detected_at}"
            )

        if self.price_ton:
            price_line = f"💰 <b>{self.price_stars} ★</b> · {self.price_ton} TON"
        else:
            price_line = f"💰 <b>{self.price_stars} ★</b>"

        return (
            f"🔒 <b>Лот занят ({safe_mention})</b>\n\n"
            f"🎁 {title_text}\n"
            f"{price_line}\n\n"
            f"🧩 Модель: {safe_model} ({safe_rarity})\n"
            f"🎨 Фон: {safe_backdrop} · Узор: {safe_pattern}\n\n"
            f"👤 <i>[Контакт продавца скрыт — занято {safe_mention}]</i>\n"
            f"📊 Уровень: {level_str}\n"
            f"{nft_line}"
            f"💬 Сообщения: {messages_str}\n"
            f"⭐ {premium_status}\n\n"
            f"🕓 {self.detected_at}"
        )

    def get_offer_url(self, discount: int, template: str) -> Optional[str]:
        """Возвращает URL для кнопки «✉️ Написать продавцу» маркета или None, если нет username."""
        if not self.username:
            return None

        offer_price = max(1, self.price_stars - discount)
        try:
            offer_text = template.format(
                title=self.title,
                num=self.num,
                discount=discount,
                offer=offer_price,
            )
        except Exception:
            offer_text = template or (
                "привет нфт подарок за рубли/$/звезды/тоны напрямую продашь? "
                "просто 3 недели кд ждать и комку платить не хочу (ботов не хожу, гарантов по ситуации)"
            )

        encoded = urllib.parse.quote(offer_text)
        clean_username = self.username.lstrip("@")
        return f"https://t.me/{clean_username}?text={encoded}"

    def get_upgrade_offer_url(self, template: str) -> Optional[str]:
        """Возвращает URL для кнопки «✉️ Написать продавцу» нового апгрейда или None, если нет username."""
        if not self.username:
            return None

        try:
            offer_text = template.format(
                title=self.title,
                num=self.num,
            )
        except Exception:
            offer_text = template or (
                "привет нфт подарок за рубли/$/звезды/тоны напрямую продашь? "
                "просто 3 недели кд ждать и комку платить не хочу (ботов не хожу, гарантов по ситуации)"
            )

        encoded = urllib.parse.quote(offer_text)
        clean_username = self.username.lstrip("@")
        return f"https://t.me/{clean_username}?text={encoded}"


@dataclass
class Subscriber:
    chat_id: int
    active: bool = True
    min_price: int = 0
    max_price: int = 5000
    mode: str = "market"  # "market" | "upgrades" | "both"
    joined_at: Optional[str] = None
    market_until: Optional[str] = None
    upgrades_until: Optional[str] = None
    has_used_trial: bool = False
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    custom_template: Optional[str] = None
    upgrade_filter: str = "all"  # "all" | "black_onyx"
    has_team_access: bool = False

    def allows_upgrade_backdrop(self, backdrop: Optional[str]) -> bool:
        if self.upgrade_filter == "black_onyx":
            return is_black_or_onyx_backdrop(backdrop)
        return True

    def has_market_access(self, now: Optional[datetime] = None) -> bool:
        return is_sub_active(self.market_until, now)

    def has_upgrades_access(self, now: Optional[datetime] = None) -> bool:
        return is_sub_active(self.upgrades_until, now)

    def has_any_access(self, now: Optional[datetime] = None) -> bool:
        return self.has_market_access(now) or self.has_upgrades_access(now)

    def is_forever(self, sub_type: str) -> bool:
        if sub_type == "market":
            return self.market_until == "forever"
        elif sub_type == "upgrades":
            return self.upgrades_until == "forever"
        elif sub_type == "both":
            return self.market_until == "forever" and self.upgrades_until == "forever"
        return False

    def get_status_text(self, sub_type: str, now: Optional[datetime] = None) -> str:
        until_val = self.market_until if sub_type == "market" else self.upgrades_until
        if not until_val:
            return "❌ Не активна"
        if until_val == "forever":
            return "♾️ Навсегда"
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(until_val)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            date_str = format_datetime_msk(exp)
            if exp < now:
                return f"❌ Истекла ({date_str})"
            rem_sec = int((exp - now).total_seconds())
            rem_str = format_time_remaining(rem_sec)
            return f"✅ До {date_str} (осталось {rem_str})"
        except Exception:
            return f"❓ {until_val}"


@dataclass
class Order:
    id: int
    user_id: int
    sub_type: str  # 'market', 'upgrades', 'both'
    duration: str  # '6h', '1d', '3d', '7d', '30d', 'forever'
    currency: str  # 'TON', 'USDT'
    amount: int  # minimal integer units (nanoTON or microUSDT)
    rate: Optional[float]
    comment: str  # e.g. gp-a1b2c3
    status: str  # 'pending', 'paid', 'expired', 'cancelled'
    created_at: str
    expires_at: str
    paid_at: Optional[str] = None
    tx_hash: Optional[str] = None

    @property
    def human_amount(self) -> str:
        if self.currency == "TON":
            val = self.amount / 1_000_000_000
            return f"{val:g}"
        else:
            val = self.amount / 1_000_000
            return f"{val:.2f}"

    @property
    def sub_title(self) -> str:
        mapping = {
            "market": "Маркет",
            "upgrades": "Улучшения",
            "both": "Всё вместе (Маркет + Улучшения)",
            "team": "Закрытая тима & Обучение",
        }
        return mapping.get(self.sub_type, self.sub_type)

    @property
    def duration_title(self) -> str:
        mapping = {
            "6h": "6 часов",
            "1d": "1 день",
            "3d": "3 дня",
            "7d": "7 дней",
            "30d": "1 месяц (30 дней)",
            "1m": "1 месяц (30 дней)",
            "forever": "Навсегда",
            "access": "Бессрочный доступ",
        }
        if self.duration in mapping:
            return mapping[self.duration]
        if self.duration.endswith("h") and self.duration[:-1].isdigit():
            return format_hours_ru(int(self.duration[:-1]))
        if self.duration.endswith("d") and self.duration[:-1].isdigit():
            return f"{self.duration[:-1]} дн."
        if self.duration.isdigit():
            return format_hours_ru(int(self.duration))
        return self.duration


def compute_extension(
    current_until: Optional[str],
    duration: str,
    now: Optional[datetime] = None,
) -> str:
    """
    Покупка или выдача при действующей подписке продлевает её:
    новая дата = max(сейчас, текущая дата окончания) + срок.
    Если подписка 'forever' уже есть или покупается 'forever' — возвращает 'forever'.
    Поддерживает стандартные тарифы (6h, 1d, 3d, 7d, 30d/1m), а также произвольное число часов (например '12h' или 12) или дней ('5d').
    """
    if current_until == "forever" or duration == "forever":
        return "forever"

    if now is None:
        now = datetime.now(timezone.utc)

    base = now
    if current_until:
        try:
            curr_dt = datetime.fromisoformat(current_until)
            if curr_dt.tzinfo is None:
                curr_dt = curr_dt.replace(tzinfo=timezone.utc)
            if curr_dt > base:
                base = curr_dt
        except Exception:
            pass

    delta_map = {
        "6h": timedelta(hours=6),
        "1d": timedelta(days=1),
        "3d": timedelta(days=3),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "1m": timedelta(days=30),
    }
    if duration in delta_map:
        delta = delta_map[duration]
    elif duration.endswith("h") and duration[:-1].isdigit():
        delta = timedelta(hours=int(duration[:-1]))
    elif duration.endswith("d") and duration[:-1].isdigit():
        delta = timedelta(days=int(duration[:-1]))
    elif duration.isdigit():
        delta = timedelta(hours=int(duration))
    else:
        delta = timedelta(days=30)

    new_dt = base + delta
    return new_dt.strftime("%Y-%m-%d %H:%M:%S")


def is_black_or_onyx_backdrop(backdrop: Optional[str]) -> bool:
    """
    Проверяет, является ли фон подарка Black или Оникс Black.
    Улучшения должны приходить со всех подарков только на Black фоне и на Оникс Black.
    """
    if not backdrop:
        return False
    b = backdrop.strip().lower()
    return (
        b in ("black", "onyx black", "оникс black", "onyx", "оникс")
        or "black" in b
        or "оникс" in b
        or "onyx" in b
    )


def get_upgrade_filter_title(upgrade_filter: str) -> str:
    """Возвращает человекочитаемое название фильтра фона улучшений."""
    if upgrade_filter == "black_onyx":
        return "🖤 Только Black и Оникс"
    return "🌌 Все фоны (все вместе)"


