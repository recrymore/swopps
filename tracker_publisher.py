
import html
import re
from decimal import Decimal, InvalidOperation

from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import (
    TRACKER_GROUP_ID,
    TRACKER_TOPICS,
    TRACKER_PRICE_RANGES,
)


def get_price_category(price_ton):
    try:
        price = Decimal(str(price_ton))
    except (InvalidOperation, ValueError, TypeError):
        return None

    if not price.is_finite() or price <= 0:
        return None

    for minimum, maximum, category in TRACKER_PRICE_RANGES:
        if Decimal(str(minimum)) <= price < Decimal(str(maximum)):
            return category

    return None


def safe(value):
    return html.escape(str(value or "—"))


def format_lot_card(lot, category):
    return (
        f"💎 <b>SWOP {category.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 <b>{safe(lot.title)} #{safe(lot.num)}</b>\n\n"
        f"💰 Цена: <b>{safe(lot.price_ton)} TON</b>\n\n"
        f"🧩 Модель: {safe(lot.model)}\n"
        f"🎨 Фон: {safe(lot.backdrop)}\n"
        f"🔮 Символ: {safe(lot.pattern)}\n\n"
        f"📡 Новый рыночный лот\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>SWOP Tracker</i>"
    )


async def publish_lot(lot, bots):
    if getattr(lot, "event_type", "market") != "market":
        return None

    category = get_price_category(lot.price_ton)

    if category is None:
        return None

    slug = str(lot.slug or "")

    if not re.fullmatch(r"[A-Za-z0-9]+-[0-9]+", slug):
        return None

    bot = bots[category]
    topic_id = TRACKER_TOPICS[category]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎁 Открыть NFT",
                    url=f"https://t.me/nft/{slug}",
                )
            ]
        ]
    )

    return await bot.send_message(
        chat_id=TRACKER_GROUP_ID,
        message_thread_id=topic_id,
        text=format_lot_card(lot, category),
        parse_mode="HTML",
        reply_markup=keyboard,
    )
