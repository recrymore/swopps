import asyncio
import html
import logging
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from aiogram import Bot, Dispatcher, Router, types, F, BaseMiddleware
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramAPIError,
    TelegramNetworkError,
)
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import load_config, AppConfig
from database import Database
from models import (
    LotItem,
    Subscriber,
    Order,
    format_datetime_msk,
    format_time_remaining,
    get_upgrade_filter_title,
)
from payments import CurrencyConverter, PaymentVerifier, generate_order_comment, TransferItem
from admin import admin_router

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [BOT] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)

from aiogram.client.session.aiohttp import AiohttpSession

router = Router()
converter = CurrencyConverter()


class ResilientAiohttpSession(AiohttpSession):
    """Кастомная сессия aiohttp с защитой от залипания DNS и автоматическим сбросом коннектора."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if hasattr(self, "_connector_init") and isinstance(self._connector_init, dict):
            self._connector_init["ttl_dns_cache"] = 10

    async def make_request(
        self,
        bot: Bot,
        method: Any,
        timeout: Optional[int] = None,
    ) -> Any:
        try:
            return await super().make_request(bot, method, timeout=timeout)
        except Exception:
            self._should_reset_connector = True
            raise


class ConsoleLoggingMiddleware(BaseMiddleware):
    """Логирует каждое входящее сообщение и нажатие inline-кнопок в консоль."""

    async def __call__(
        self, handler, event: types.TelegramObject, data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, types.Message):
            u = event.from_user
            user_str = (
                f"@{u.username}"
                if (u and u.username)
                else (u.full_name if u else str(event.chat.id))
            )
            logger.info(
                f"📩 [ВХОДЯЩЕЕ] от {user_str} (ID: {event.chat.id}): {event.text or '<служебное/медиа>'}"
            )
        elif isinstance(event, types.CallbackQuery):
            u = event.from_user
            user_str = (
                f"@{u.username}"
                if (u and u.username)
                else (u.full_name if u else str(u.id if u else 'none'))
            )
            logger.info(f"🔘 [КНОПКА] от {user_str}: {event.data}")

        try:
            return await handler(event, data)
        except TelegramBadRequest as e:
            if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
                logger.debug(f"Игнорирован устаревший callback_query: {e}")
                return None
            raise


# ------------------ Функции генерации главного меню и клавиатур ------------------

def get_main_menu_text(sub: Subscriber, trial_msg: str = "") -> str:
    status_icon = "🟢 Включены" if sub.active else "🔴 Приостановлены"
    return (
        "👋 <b>Добро пожаловать в сервис мониторинга подарков Telegram!</b>\n\n"
        "Я отслеживаю появление новых коллекционных подарков (Collectible Gifts) "
        "на маркете Stars и уникальных улучшений (NFT Black и Оникс Black).\n"
        f"{trial_msg}\n"
        "👤 <b>Ваш статус:</b>\n"
        f"• Уведомления: <b>{status_icon}</b>\n"
        f"• Режим: <b>{get_mode_title(sub.mode)}</b>\n"
        f"• Фильтр апгрейдов: <b>{get_upgrade_filter_title(getattr(sub, 'upgrade_filter', 'all'))}</b>\n"
        f"• Диапазон цен: <b>{sub.min_price} – {sub.max_price} ★</b>\n"
        f"• Маркет: {sub.get_status_text('market')}\n"
        f"• Улучшения: {sub.get_status_text('upgrades')}\n\n"
        "<i>Используйте интерактивные кнопки меню ниже:</i>"
    )


class CustomTemplateState(StatesGroup):
    waiting_for_template = State()


def get_main_menu_keyboard(sub: Subscriber) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Тарифы и подписка", callback_data="menu:tariffs")
    builder.button(text="👤 Мой профиль", callback_data="menu:profile")
    builder.button(text="⚙️ Режим уведомлений", callback_data="menu:mode")
    builder.button(text="💰 Диапазон цен", callback_data="menu:price")
    builder.button(text="📋 Занятые лоты", callback_data="menu:my")
    builder.button(text="📝 Шаблон сообщения", callback_data="menu:template")

    toggle_text = "⏸️ Приостановить" if sub.active else "▶️ Возобновить"
    builder.button(text=toggle_text, callback_data="menu:toggle_active")
    builder.button(text="ℹ️ Справка", callback_data="menu:help")

    builder.adjust(2, 2, 2, 2)
    return builder.as_markup()


def get_template_text(sub: Subscriber, default_tpl: str) -> str:
    cur_tpl = sub.custom_template or default_tpl
    status_badge = "🟢 Индивидуальный" if sub.custom_template else "⚪ Стандартный (по умолчанию)"
    return (
        "📝 <b>Настройка шаблона сообщения продавцу</b>\n\n"
        f"Текущий статус: <b>{status_badge}</b>\n\n"
        "<b>Ваш текущий текст сообщения:</b>\n"
        f"<code>{html.escape(cur_tpl)}</code>\n\n"
        "Этот текст открывается в диалоге при нажатии кнопки <b>«✉️ Написать продавцу»</b> после того, как вы заняли лот.\n\n"
        "<b>Доступные переменные (подставляются автоматически):</b>\n"
        "• <code>{title}</code> — название подарка (например, <i>Instant Ramen</i>)\n"
        "• <code>{num}</code> — номер подарка (например, <i>#42</i>)\n"
        "• <code>{offer}</code> — предложенная цена со скидкой в Stars\n"
        "• <code>{discount}</code> — скидка в Stars\n\n"
        "<i>Нажмите кнопку ниже, чтобы ввести новый текст или сбросить на стандартный:</i>"
    )


def get_template_keyboard(has_custom: bool) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить шаблон", callback_data="template:set")
    if has_custom:
        builder.button(text="🔄 Сбросить на стандартный", callback_data="template:reset")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def get_price_keyboard(sub: Subscriber) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="0 – 500 ★", callback_data="price_set:0:500")
    builder.button(text="0 – 1 000 ★", callback_data="price_set:0:1000")
    builder.button(text="0 – 2 500 ★", callback_data="price_set:0:2500")
    builder.button(text="0 – 5 000 ★", callback_data="price_set:0:5000")
    builder.button(text="Все лоты (0 – 100k ★)", callback_data="price_set:0:100000")

    builder.button(text="-100 мин", callback_data="price_adj:min:-100")
    builder.button(text="+100 мин", callback_data="price_adj:min:100")
    builder.button(text="-200 макс", callback_data="price_adj:max:-200")
    builder.button(text="+200 макс", callback_data="price_adj:max:200")

    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(2, 2, 1, 2, 2, 1)
    return builder.as_markup()


def get_price_text(sub: Subscriber) -> str:
    return (
        "⚙️ <b>Ваш текущий диапазон цен:</b>\n"
        f"От <b>{sub.min_price}</b> до <b>{sub.max_price} ★</b>\n\n"
        "• Выберите готовый пресет или используйте кнопки точной подстройки.\n"
        "• Либо просто отправьте сообщение в чат с двумя числами (например: <code>100 800</code>)."
    )


# ------------------ Обработчики команд ------------------

@router.message(CommandStart())
async def handle_start(
    message: types.Message, db: Database, config: Optional[AppConfig] = None
) -> None:
    """Команда /start — регистрация подписчика и открытие главного меню."""
    chat_id = message.chat.id
    user = message.from_user
    username = str(user.username) if (user and isinstance(user.username, str)) else None
    first_name = str(user.first_name) if (user and isinstance(user.first_name, str)) else None
    last_name = str(user.last_name) if (user and isinstance(user.last_name, str)) else None

    sub = await db.upsert_subscriber(
        chat_id=chat_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
    )

    # Проверка пробного периода
    trial_msg = ""
    trial_hours = getattr(config, "trial_hours", 0) if config else 0
    if trial_hours > 0 and not sub.has_used_trial:
        success, until_date = await db.activate_trial(chat_id, trial_hours)
        if success:
            trial_msg = (
                f"\n\n🎁 <b>Вам начислен бесплатный пробный период на {trial_hours} ч.!</b>\n"
                f"Доступ открыт до: <b>{format_datetime_msk(until_date)}</b>."
            )
            sub = await db.get_subscriber(chat_id) or sub

    greeting_text = get_main_menu_text(sub, trial_msg)
    await message.answer(greeting_text, reply_markup=get_main_menu_keyboard(sub))

    reviews_chat = (getattr(config, "reviews_chat_link", None) if config else None) or "https://t.me/+your_reviews_chat"
    profits_chat_text = (
        "Появился чат для ваших профитов с бота, и отзывах о нем, огромная просьба, "
        f"скрины ваших профитов заливать сюда: {reviews_chat}"
    )
    chat_builder = InlineKeyboardBuilder()
    chat_builder.button(text="💬 Чат профитов и отзывов", url=reviews_chat)
    await message.answer(
        profits_chat_text,
        reply_markup=chat_builder.as_markup(),
        disable_web_page_preview=False,
    )
    logger.info(f"Пользователь {chat_id} запустил бота (/start).")


@router.message(Command("stop"))
async def handle_stop(message: types.Message, db: Database) -> None:
    """Команда /stop — отключение рассылки для пользователя."""
    chat_id = message.chat.id
    await db.deactivate_subscriber(chat_id)
    sub = await db.get_subscriber(chat_id) or Subscriber(chat_id=chat_id, active=False)

    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Возобновить уведомления", callback_data="menu:toggle_active")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)

    await message.answer(
        "🔕 <b>Уведомления отключены.</b>\n\n"
        "Вы больше не будете получать оповещения о новых лотах.\n"
        "Чтобы включить их снова, нажмите кнопку ниже:",
        reply_markup=builder.as_markup(),
    )
    logger.info(f"Пользователь {chat_id} отключил уведомления (/stop).")


@router.message(Command("help"))
async def handle_help(message: types.Message) -> None:
    """Команда /help — справка по командам и настройкам."""
    help_text = (
        "📖 <b>Справка по сервису мониторинга подарков:</b>\n\n"
        "• 📦 <b>Мониторинг Маркета</b> — бот отслеживает появление выгодных листингов подарков за Telegram Stars.\n"
        "• 💎 <b>Мониторинг Улучшений</b> — отслеживание только что улучшенных подарков со всех коллекций, "
        "строго на фонах <b>Black</b> и <b>Оникс Black</b>.\n"
        "• 🔒 <b>Система бронирования лотов</b> — при поступлении лота нажмите <b>[🔒 Занять лот]</b>. "
        "Вы получите прямую ссылку для торга с продавцом, а у остальных подписчиков лот будет помечен как занятый вами.\n"
        "• 💳 <b>Оплата подписки</b> — доступна в криптовалюте TON и USDT сети TON с автоматической проверкой платежей.\n\n"
        "<i>Всё управление ботом осуществляется исключительно с помощью интерактивных кнопок.</i>"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Тарифы и подписка", callback_data="menu:tariffs")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)
    await message.answer(help_text, reply_markup=builder.as_markup())


def get_mode_keyboard(
    current_mode: str, sub: Optional[Subscriber] = None
) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    has_m = sub.has_market_access() if sub else True
    has_u = sub.has_upgrades_access() if sub else True

    m_lock = "" if has_m else " 🔒"
    u_lock = "" if has_u else " 🔒"
    b_lock = "" if (has_m and has_u) else " 🔒"

    builder.button(
        text="📦 Маркет" + (" ✅" if current_mode == "market" else m_lock),
        callback_data="set_mode:market",
    )
    builder.button(
        text="💎 Апгрейды" + (" ✅" if current_mode == "upgrades" else u_lock),
        callback_data="set_mode:upgrades",
    )
    builder.button(
        text="🔔 Оба" + (" ✅" if current_mode == "both" else b_lock),
        callback_data="set_mode:both",
    )

    # Кнопка переключения фильтра фона улучшений (только черный/оникс или все фоны)
    upg_filter = getattr(sub, "upgrade_filter", "all") if sub else "all"
    if upg_filter == "black_onyx":
        filter_btn_text = "🎨 Фон апгрейдов: 🖤 Black / Оникс"
    else:
        filter_btn_text = "🎨 Фон апгрейдов: 🌌 Все вместе"

    if not has_u:
        filter_btn_text += " 🔒"

    builder.button(
        text=filter_btn_text,
        callback_data="toggle_upgrade_filter",
    )

    builder.button(text="💳 Тарифы и подписка", callback_data="menu:tariffs")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(3, 1, 1, 1)
    return builder.as_markup()


def get_mode_title(mode: str) -> str:
    mapping = {
        "market": "📦 Маркет",
        "upgrades": "💎 Новые апгрейды",
        "both": "🔔 Оба режима",
    }
    return mapping.get(mode, mode)


@router.message(Command("mode"))
async def handle_mode(message: types.Message, db: Database) -> None:
    """Команда /mode: выбор режима уведомлений."""
    sub = await db.get_subscriber(message.chat.id)
    if not sub:
        sub = await db.upsert_subscriber(message.chat.id)

    # Если подписки нет совсем, бот показывает тарифы (Требование 1)
    if not sub.has_any_access():
        text = (
            "🔒 <b>Доступ ограничен!</b>\n\n"
            "Для выбора режима и получения алертов требуется активная подписка.\n"
            "Ознакомьтесь с тарифами ниже:"
        )
        await message.answer(text, reply_markup=get_tariffs_select_type_keyboard())
        return

    current_mode = sub.mode or "market"
    upg_filter = getattr(sub, "upgrade_filter", "all")
    text = (
        f"⚙️ <b>Режим уведомлений</b>\n\n"
        f"• Текущий режим: <b>{get_mode_title(current_mode)}</b>\n"
        f"• Фильтр апгрейдов: <b>{get_upgrade_filter_title(upg_filter)}</b>\n\n"
        f"📦 Маркет: {sub.get_status_text('market')}\n"
        f"💎 Улучшения: {sub.get_status_text('upgrades')}\n\n"
        "Выберите режим уведомлений или нажмите на кнопку фильтра фонов апгрейдов:"
    )
    await message.answer(text, reply_markup=get_mode_keyboard(current_mode, sub))


@router.callback_query(lambda c: bool(c.data and c.data.startswith("set_mode:")))
async def handle_mode_callback(callback: types.CallbackQuery, db: Database) -> None:
    """Обработчик inline-кнопок переключения режима /mode."""
    new_mode = callback.data.split(":")[1]
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)

    # Проверка наличия активной подписки
    if not sub.has_any_access():
        await callback.answer("🔒 У вас нет активной подписки! Ознакомьтесь с тарифами.", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "🔒 <b>Доступ ограничен.</b> Выберите тариф:",
                reply_markup=get_tariffs_select_type_keyboard(),
            )
        return

    # Проверка оплачен ли выбранный режим
    if new_mode == "market" and not sub.has_market_access():
        await callback.answer("⚠️ Требуется активная подписка на «Маркет»!", show_alert=True)
        return
    if new_mode == "upgrades" and not sub.has_upgrades_access():
        await callback.answer("⚠️ Требуется активная подписка на «Улучшения»!", show_alert=True)
        return
    if new_mode == "both" and not (sub.has_market_access() and sub.has_upgrades_access()):
        await callback.answer("⚠️ Требуются обе подписки («Всё вместе»)!", show_alert=True)
        return

    sub.mode = new_mode
    await db.update_subscriber_mode(chat_id, new_mode)
    await callback.answer(f"Режим изменен: {get_mode_title(new_mode)}")
    upg_filter = getattr(sub, "upgrade_filter", "all")
    text = (
        f"⚙️ <b>Режим уведомлений</b>\n\n"
        f"• Текущий режим: <b>{get_mode_title(new_mode)}</b>\n"
        f"• Фильтр апгрейдов: <b>{get_upgrade_filter_title(upg_filter)}</b>\n\n"
        f"📦 Маркет: {sub.get_status_text('market')}\n"
        f"💎 Улучшения: {sub.get_status_text('upgrades')}\n\n"
        "Выберите режим уведомлений или нажмите на кнопку фильтра фонов апгрейдов:"
    )
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=get_mode_keyboard(new_mode, sub))
        except Exception:
            pass


@router.callback_query(F.data == "toggle_upgrade_filter")
async def handle_toggle_upgrade_filter(callback: types.CallbackQuery, db: Database) -> None:
    """Переключает фильтр фона улучшений между Black/Оникс и всеми фонами."""
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)

    if not sub.has_upgrades_access():
        await callback.answer(
            "⚠️ Фильтр улучшений доступен при активной подписке на «Улучшения» или «Всё вместе»!",
            show_alert=True,
        )
        return

    current_filter = getattr(sub, "upgrade_filter", "all")
    new_filter = "black_onyx" if current_filter == "all" else "all"

    await db.update_subscriber_upgrade_filter(chat_id, new_filter)
    sub.upgrade_filter = new_filter

    if new_filter == "black_onyx":
        msg = "🖤 Включен фильтр: только Black и Оникс Black"
    else:
        msg = "🌌 Включен фильтр: все фоны улучшений (все вместе)"

    await callback.answer(msg)

    current_mode = sub.mode or "market"
    text = (
        f"⚙️ <b>Режим уведомлений</b>\n\n"
        f"• Текущий режим: <b>{get_mode_title(current_mode)}</b>\n"
        f"• Фильтр апгрейдов: <b>{get_upgrade_filter_title(new_filter)}</b>\n\n"
        f"📦 Маркет: {sub.get_status_text('market')}\n"
        f"💎 Улучшения: {sub.get_status_text('upgrades')}\n\n"
        "Выберите режим уведомлений или нажмите на кнопку фильтра фонов апгрейдов:"
    )
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=get_mode_keyboard(current_mode, sub))
        except Exception:
            pass



# ------------------ Обработчики тарифов и оплаты подписки ------------------

def get_tariffs_select_type_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Маркет", callback_data="tariffs_type:market")
    builder.button(text="💎 Улучшения", callback_data="tariffs_type:upgrades")
    builder.button(text="🔔 Всё вместе (оба режима)", callback_data="tariffs_type:both")
    builder.button(text="👥 Закрытая тима & Обучение", callback_data="tariffs_type:team")
    builder.button(text="👤 Мой профиль / Статус", callback_data="menu:profile")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


@router.message(Command("tariffs"))
@router.message(Command("sub"))
async def handle_tariffs_command(message: types.Message, db: Database, config: AppConfig) -> None:
    """Команда /tariffs: меню выбора тарифа."""
    sub = await db.get_subscriber(message.chat.id)
    if not sub:
        user = message.from_user
        sub = await db.upsert_subscriber(
            message.chat.id,
            username=str(user.username) if (user and isinstance(user.username, str)) else None,
            first_name=str(user.first_name) if (user and isinstance(user.first_name, str)) else None,
            last_name=str(user.last_name) if (user and isinstance(user.last_name, str)) else None,
        )

    text = (
        "💎 <b>Тарифы подписки и услуги сервиса</b>\n\n"
        "Выберите интересующий вас раздел:\n\n"
        "• 📦 <b>Маркет</b> — моментальные алерты о новых листингах на вторичном маркете Telegram Stars.\n"
        "• 💎 <b>Улучшения</b> — алерты о подарках, только что улучшенных владельцами до уникальных NFT.\n"
        "• 🔔 <b>Всё вместе</b> — полный доступ к обоим режимам мониторинга с максимальной выгодой!\n"
        "• 👥 <b>Закрытая тима & Обучение</b> — приватное комьюнити, обучение с нуля, помощь и фейк-гарант.\n\n"
        "Оплата принимается в криптовалюте <b>TON</b> и <b>USDT</b> (сеть TON)."
    )
    await message.answer(text, reply_markup=get_tariffs_select_type_keyboard())


@router.message(Command("profile"))
async def handle_profile_command(message: types.Message, db: Database, config: AppConfig) -> None:
    """Команда /profile: текущий статус подписок пользователя."""
    sub = await db.get_subscriber(message.chat.id)
    if not sub:
        sub = await db.upsert_subscriber(message.chat.id)

    team_status = "✅ Доступ открыт" if getattr(sub, "has_team_access", False) else "❌ Не приобретено"
    team_line = f"👥 <b>Закрытая тима:</b> {team_status}\n"
    if getattr(sub, "has_team_access", False):
        team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
        team_line += f"👉 <i>Ссылка на тиму:</i> {team_link}\n"

    text = (
        f"👤 <b>Ваш профиль (ID: <code>{message.chat.id}</code>)</b>\n\n"
        f"📦 <b>Подписка Маркет:</b> {sub.get_status_text('market')}\n"
        f"💎 <b>Подписка Улучшения:</b> {sub.get_status_text('upgrades')}\n"
        f"{team_line}\n"
        f"🔔 <b>Текущий режим:</b> <b>{get_mode_title(sub.mode)}</b>\n"
        f"🎨 <b>Фон апгрейдов:</b> <b>{get_upgrade_filter_title(getattr(sub, 'upgrade_filter', 'all'))}</b>\n"
        f"💰 <b>Диапазон цен:</b> <b>{sub.min_price} – {sub.max_price} ★</b>"
    )
    builder = InlineKeyboardBuilder()
    if getattr(sub, "has_team_access", False):
        team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
        builder.button(text="👥 Чат закрытой тимы", url=team_link)
    builder.button(text="💳 Выбрать тариф / Услуги", callback_data="tariffs_back_types")
    builder.button(text="⚙️ Сменить режим", callback_data="tariffs_open_mode")
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup())


@router.message(Command("trial"))
async def handle_trial_command(message: types.Message, db: Database, config: AppConfig) -> None:
    """Команда /trial: активация бесплатного пробного периода."""
    chat_id = message.chat.id
    trial_hours = getattr(config, "trial_hours", 0)
    if trial_hours <= 0:
        await message.answer("ℹ️ Пробный период в данный момент выключен в настройках сервиса.")
        return

    success, msg = await db.activate_trial(chat_id, trial_hours)
    if success:
        await message.answer(
            f"🎉 <b>Пробный период успешно активирован на {trial_hours} ч.!</b>\n\n"
            f"Вам открыт доступ ко всем режимам до: <b>{format_datetime_msk(msg)}</b>.\n"
            "Настройте режим в /mode и диапазон цен в /price."
        )
    else:
        await message.answer(f"⚠️ {msg}.")


@router.callback_query(F.data == "tariffs_back_types")
async def handle_tariffs_back(callback: types.CallbackQuery) -> None:
    text = (
        "💎 <b>Тарифы подписки и услуги сервиса</b>\n\n"
        "Выберите интересующий вас раздел:\n\n"
        "• 📦 <b>Маркет</b> — новые листинги перепродажи на маркете Stars.\n"
        "• 💎 <b>Улучшения</b> — подарки, только что улучшенные до уникальных.\n"
        "• 🔔 <b>Всё вместе</b> — оба режима (выгодно).\n"
        "• 👥 <b>Закрытая тима & Обучение</b> — приватное комьюнити, обучение с нуля, помощь и фейк-гарант.\n\n"
        "Оплата принимается в <b>TON</b> и <b>USDT</b>."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=get_tariffs_select_type_keyboard())
    await callback.answer()


@router.callback_query(F.data == "tariffs_profile")
async def handle_tariffs_profile_callback(
    callback: types.CallbackQuery, db: Database, config: AppConfig
) -> None:
    sub = await db.get_subscriber(callback.from_user.id)
    if not sub:
        sub = await db.upsert_subscriber(callback.from_user.id)
    team_status = "✅ Доступ открыт" if getattr(sub, "has_team_access", False) else "❌ Не приобретено"
    team_line = f"👥 <b>Закрытая тима:</b> {team_status}\n"
    if getattr(sub, "has_team_access", False):
        team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
        team_line += f"👉 <i>Ссылка на тиму:</i> {team_link}\n"

    text = (
        f"👤 <b>Ваш профиль (ID: <code>{callback.from_user.id}</code>)</b>\n\n"
        f"📦 <b>Подписка Маркет:</b> {sub.get_status_text('market')}\n"
        f"💎 <b>Подписка Улучшения:</b> {sub.get_status_text('upgrades')}\n"
        f"{team_line}\n"
        f"🔔 <b>Текущий режим:</b> <b>{get_mode_title(sub.mode)}</b>\n"
        f"🎨 <b>Фон апгрейдов:</b> <b>{get_upgrade_filter_title(getattr(sub, 'upgrade_filter', 'all'))}</b>\n"
        f"💰 <b>Диапазон цен:</b> <b>{sub.min_price} – {sub.max_price} ★</b>"
    )
    builder = InlineKeyboardBuilder()
    if getattr(sub, "has_team_access", False):
        team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
        builder.button(text="👥 Чат закрытой тимы", url=team_link)
    builder.button(text="💳 Выбрать тариф / Услуги", callback_data="tariffs_back_types")
    builder.button(text="⚙️ Сменить режим", callback_data="tariffs_open_mode")
    builder.adjust(1)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "tariffs_open_mode")
async def handle_tariffs_open_mode(callback: types.CallbackQuery, db: Database) -> None:
    sub = await db.get_subscriber(callback.from_user.id)
    current_mode = sub.mode if sub else "market"
    upg_filter = getattr(sub, "upgrade_filter", "all") if sub else "all"
    text = (
        f"⚙️ <b>Режим уведомлений</b>\n\n"
        f"• Текущий режим: <b>{get_mode_title(current_mode)}</b>\n"
        f"• Фильтр апгрейдов: <b>{get_upgrade_filter_title(upg_filter)}</b>\n\n"
        "Выберите режим уведомлений или нажмите на кнопку фильтра фонов апгрейдов:"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=get_mode_keyboard(current_mode, sub))
    await callback.answer()


@router.callback_query(lambda c: bool(c.data and c.data.startswith("tariffs_type:")))
async def handle_tariffs_type_chosen(
    callback: types.CallbackQuery, db: Database, config: AppConfig
) -> None:
    sub_type = callback.data.split(":")[1]
    sub = await db.get_subscriber(callback.from_user.id)

    if sub_type == "team":
        team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
        has_access = getattr(sub, "has_team_access", False) if sub else False
        if has_access:
            builder = InlineKeyboardBuilder()
            builder.button(text="🚀 Вступить в закрытую тиму", url=team_link)
            builder.button(text="🔙 Назад к тарифам", callback_data="tariffs_back_types")
            builder.button(text="◀️ Главное меню", callback_data="menu:main")
            builder.adjust(1)
            text = (
                "👥 <b>Закрытая тима & Обучение</b>\n\n"
                "✅ <b>У вас уже оплачен доступ в закрытую команду!</b>\n\n"
                f"👉 Ваша постоянная ссылка для входа:\n{team_link}"
            )
            if callback.message:
                await callback.message.edit_text(text, reply_markup=builder.as_markup())
            await callback.answer()
            return

        price_ton = config.tariffs.get("team", {}).get("access", 0.5)
        rate = await converter.get_ton_usdt_rate()
        usdt_price_str = ""
        builder = InlineKeyboardBuilder()
        builder.button(
            text=f"💎 Оплатить в TON ({price_ton:g} TON)",
            callback_data="order_create:team:access:TON",
        )
        if rate is not None and rate > 0:
            usdt_float, _ = converter.calculate_usdt_amount(price_ton, rate)
            usdt_price_str = f" (≈ {usdt_float:.2f} USDT)"
            builder.button(
                text=f"💵 Оплатить в USDT ({usdt_float:.2f} USDT)",
                callback_data="order_create:team:access:USDT",
            )
        builder.button(text="🔙 Назад к выбору тарифа", callback_data="tariffs_back_types")
        builder.button(text="◀️ Главное меню", callback_data="menu:main")
        builder.adjust(1)

        text = (
            "👥 <b>Закрытая тима, Обучение & Фейк-гарант</b>\n\n"
            "🔥 <b>Что входит в закрытый доступ:</b>\n"
            "• 🤝 <b>Приём в закрытую тиму</b> — приватный чат и сильное комьюнити профессионалов в нише Telegram Gifts.\n"
            "• 📚 <b>Обучение с нуля до результата</b> — проверенные связки, алгоритмы поиска прибыльных лотов, секреты быстрой перепродажи.\n"
            "• 🛡️ <b>Фейк-гарант сервис</b> — инструмент для безопасного выкупа подарков напрямую у продавцов.\n"
            "• 💬 <b>Личная поддержка & кураторство</b> — оперативная помощь, ответы на любые вопросы и помощь в сделках.\n\n"
            f"💰 <b>Стоимость (бессрочный доступ):</b> <b>{price_ton:g} TON</b>{usdt_price_str}\n"
            "<i>(Персональная ссылка на вступление выдаётся автоматически сразу после оплаты)</i>\n\n"
            "Выберите валюту для оплаты:"
        )
        if callback.message:
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await callback.answer()
        return

    sub_tariffs = config.tariffs.get(sub_type, {})
    sub_titles = {
        "market": "📦 Маркет",
        "upgrades": "💎 Улучшения",
        "both": "🔔 Всё вместе (оба режима)",
    }

    already_forever = sub.is_forever(sub_type) if sub else False

    builder = InlineKeyboardBuilder()
    duration_labels = [
        ("6h", "6 часов"),
        ("1d", "1 день"),
        ("3d", "3 дня"),
        ("7d", "7 дней"),
        ("30d", "1 месяц (30 дней)"),
        ("forever", "Навсегда"),
    ]

    for dur_key, dur_label in duration_labels:
        price_ton = sub_tariffs.get(dur_key, 0.0)
        if dur_key == "forever" and already_forever:
            builder.button(
                text=f"♾️ {dur_label} (уже активно)",
                callback_data="tariffs_already_forever",
            )
        else:
            builder.button(
                text=f"{dur_label} — {price_ton:g} TON",
                callback_data=f"tariffs_dur:{sub_type}:{dur_key}",
            )

    builder.button(text="🔙 Назад к выбору тарифа", callback_data="tariffs_back_types")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)

    text = (
        f"Тариф: <b>{sub_titles.get(sub_type, sub_type)}</b>\n\n"
        "Выберите срок действия подписки:\n"
        "<i>(При действующей подписке покупка продлевает её срок)</i>"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "tariffs_already_forever")
async def handle_already_forever(callback: types.CallbackQuery) -> None:
    await callback.answer("У вас уже действует бессрочная подписка («Навсегда»)!", show_alert=True)


@router.callback_query(lambda c: bool(c.data and c.data.startswith("tariffs_dur:")))
async def handle_tariffs_duration_chosen(
    callback: types.CallbackQuery, db: Database, config: AppConfig
) -> None:
    _, sub_type, duration = callback.data.split(":")
    price_ton = config.tariffs.get(sub_type, {}).get(duration, 0.0)

    # Получаем курс TON/USDT
    rate = await converter.get_ton_usdt_rate()

    sub_titles = {
        "market": "📦 Маркет",
        "upgrades": "💎 Улучшения",
        "both": "🔔 Всё вместе",
    }
    dur_titles = {
        "6h": "6 часов",
        "1d": "1 день",
        "3d": "3 дня",
        "7d": "7 дней",
        "30d": "1 месяц (30 дней)",
        "forever": "Навсегда",
    }

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"💎 Оплатить в TON ({price_ton:g} TON)",
        callback_data=f"order_create:{sub_type}:{duration}:TON",
    )

    usdt_note = ""
    usdt_price_str = ""
    if rate is not None and rate > 0:
        usdt_float, _ = converter.calculate_usdt_amount(price_ton, rate)
        usdt_price_str = f" (≈ {usdt_float:.2f} USDT)"
        builder.button(
            text=f"💵 Оплатить в USDT ({usdt_float:.2f} USDT)",
            callback_data=f"order_create:{sub_type}:{duration}:USDT",
        )
    else:
        usdt_note = "\n\n⚠️ <i>Оплата в USDT временно недоступна (курс не получен). Доступна оплата в TON.</i>"

    builder.button(text="🔙 Назад", callback_data=f"tariffs_type:{sub_type}")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)

    text = (
        f"🧾 <b>Оформление подписки</b>\n\n"
        f"• Режим: <b>{sub_titles.get(sub_type, sub_type)}</b>\n"
        f"• Срок: <b>{dur_titles.get(duration, duration)}</b>\n"
        f"• Стоимость: <b>{price_ton:g} TON</b>{usdt_price_str}"
        f"{usdt_note}\n\n"
        "Выберите криптовалюту для оплаты:"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(lambda c: bool(c.data and c.data.startswith("order_create:")))
async def handle_order_create(
    callback: types.CallbackQuery, db: Database, config: AppConfig
) -> None:
    _, sub_type, duration, currency = callback.data.split(":")
    user_id = callback.from_user.id

    if not config.owner_address:
        await callback.answer(
            "⚠️ Кошелек для приема платежей не настроен администратором.",
            show_alert=True,
        )
        return

    price_ton = config.tariffs.get(sub_type, {}).get(duration, 0.0)
    rate = await converter.get_ton_usdt_rate()

    if currency == "USDT":
        if rate is None or rate <= 0:
            await callback.answer(
                "Курс USDT временно недоступен. Выберите оплату в TON.",
                show_alert=True,
            )
            return
        usdt_float, minimal_amount = converter.calculate_usdt_amount(price_ton, rate)
    else:
        minimal_amount = converter.calculate_ton_amount(price_ton)

    comment = generate_order_comment()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=30)
    ).strftime("%Y-%m-%d %H:%M:%S")

    order_id = await db.create_order(
        user_id=user_id,
        sub_type=sub_type,
        duration=duration,
        currency=currency,
        amount=minimal_amount,
        rate=rate,
        comment=comment,
        expires_at=expires_at,
    )

    order = await db.get_order(order_id)
    if not order:
        await callback.answer("Ошибка при создании заявки", show_alert=True)
        return

    text = (
        f"🧾 <b>Заявка на оплату #{order.id}</b>\n\n"
        f"📋 <b>Тариф:</b> {order.sub_title} ({order.duration_title})\n"
        f"💰 <b>К оплате:</b> <code>{order.human_amount} {order.currency}</code>\n\n"
        f"📬 <b>Адрес кошелька (сеть TON):</b>\n<code>{config.owner_address}</code>\n\n"
        f"💬 <b>ОБЯЗАТЕЛЬНЫЙ комментарий к переводу:</b>\n<code>{order.comment}</code>\n\n"
        "⚠️ <b>ВНИМАНИЕ!</b>\n"
        f"1. Обязательно укажите комментарий <code>{order.comment}</code> при переводе! Без него платеж не зачислится автоматически.\n"
        "2. Отправляйте точную сумму в сети TON (нажмите на число/адрес для копирования).\n"
        "3. Заявка действительна <b>30 минут</b>."
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Я оплатил (проверить)", callback_data=f"order_check:{order.id}")
    builder.button(text="❌ Отменить заявку", callback_data=f"order_cancel:{order.id}")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)

    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer("Заявка создана! Оплатите в течение 30 минут.")


async def notify_owner(
    bot: Optional[Bot], db: Database, config: AppConfig, text: str
) -> None:
    """Отправляет служебное уведомление владельцу или администратору бота."""
    if bot is None:
        return
    target_id = config.owner_chat_id
    if not target_id:
        admin_id_str = await db.get_setting("admin_id")
        if admin_id_str:
            try:
                target_id = int(admin_id_str)
            except ValueError:
                pass

    if target_id:
        try:
            await bot.send_message(
                chat_id=target_id, text=text, disable_web_page_preview=True
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление владельцу ({target_id}): {e}")
    else:
        logger.info(f"Владелец бота не задан, служебное сообщение: {text}")


async def verify_and_process_payments(
    bot: Optional[Bot],
    db: Database,
    config: AppConfig,
    verifier: Optional[PaymentVerifier] = None,
) -> int:
    """
    Проверяет блокчейн на входящие платежи и сопоставляет их с заявками.
    Возвращает количество успешно обработанных заявок.
    """
    if not config.owner_address:
        logger.debug("OWNER_ADDRESS не настроен, проверка платежей пропущена.")
        return 0

    if verifier is None:
        verifier = PaymentVerifier(
            owner_address=config.owner_address,
            ton_network=config.ton_network,
            tonapi_key=config.tonapi_key,
            usdt_master=config.usdt_master_address,
        )

    processed_count = 0
    try:
        transfers = await verifier.fetch_incoming_transfers()
    except Exception as e:
        logger.error(f"Ошибка получения входящих переводов из блокчейна: {e}")
        return 0

    for transfer in transfers:
        # Проверяем, обрабатывали ли мы этот хэш ранее
        if await db.is_payment_processed(transfer.tx_hash):
            continue

        # 1. Спам с нулевой суммой игнорируем без уведомлений
        if transfer.amount <= 0:
            await db.mark_tx_ignored(transfer.tx_hash)
            continue

        raw_comment = (transfer.comment or "").strip()

        # 2. Проверяем, относится ли перевод к оплате подписок в боте.
        # Комментарии заявок бота строго имеют формат gp-xxxxxx.
        # Любые личные переводы владельца кошелька (выводы с бирж, DEX, P2P, переводы от друзей или спам)
        # не являются платежами за подписку и игнорируются молча без уведомлений.
        if not (raw_comment and raw_comment.lower().startswith("gp-")):
            await db.mark_tx_ignored(transfer.tx_hash)
            continue

        # Форматируем отображение суммы
        if transfer.currency == "TON":
            amt_display = (
                f"{transfer.amount / 1_000_000_000:.4f}".rstrip("0").rstrip(".") + " TON"
            )
        else:
            amt_display = f"{transfer.amount / 1_000_000:.2f} USDT"

        # Ищем заявку по комментарию
        order = await db.get_order_by_comment(raw_comment)

        # 3. Комментарий имеет формат gp-xxxxxx, но заявка не найдена в базе (просрочена или опечатка)
        if not order:
            await db.record_unmatched_payment(
                tx_hash=transfer.tx_hash,
                currency=transfer.currency,
                amount=transfer.amount,
                comment=raw_comment,
                reason="Заявка с таким комментарием не найдена в базе",
            )
            notify_text = (
                "⚠️ <b>Входящий платёж с кодом подписки без действующей заявки!</b>\n\n"
                f"• Сумма: <b>{amt_display}</b>\n"
                f"• Валюта: <b>{transfer.currency}</b>\n"
                f"• Комментарий: <code>{raw_comment}</code>\n"
                f"• Отправитель: <code>{transfer.sender or 'неизвестно'}</code>\n"
                f"• TX Hash: <code>{transfer.tx_hash}</code>\n\n"
                "Платёж сохранён в базе (unmatched_payments), автоматически не зачислен."
            )
            await notify_owner(bot, db, config, notify_text)
            continue

        # 2. Если заявка найдена, но уже имеет статус 'paid'
        if order.status == "paid":
            await db.record_unmatched_payment(
                tx_hash=transfer.tx_hash,
                currency=transfer.currency,
                amount=transfer.amount,
                comment=raw_comment,
                reason=f"Заявка #{order.id} уже была оплачена",
            )
            notify_text = (
                f"⚠️ <b>Повторный платёж по уже оплаченной заявке #{order.id}!</b>\n\n"
                f"• Пользователь: <code>{order.user_id}</code>\n"
                f"• Сумма: <b>{amt_display}</b>\n"
                f"• TX Hash: <code>{transfer.tx_hash}</code>\n"
                f"• Комментарий: <code>{raw_comment}</code>"
            )
            await notify_owner(bot, db, config, notify_text)
            continue

        # 3. Проверка на истечение срока заявки
        now_utc = datetime.now(timezone.utc)
        order_exp = datetime.fromisoformat(order.expires_at)
        if order_exp.tzinfo is None:
            order_exp = order_exp.replace(tzinfo=timezone.utc)

        if order.status == "expired" or now_utc > order_exp:
            await db.mark_order_expired(order.id)
            await db.record_unmatched_payment(
                tx_hash=transfer.tx_hash,
                currency=transfer.currency,
                amount=transfer.amount,
                comment=raw_comment,
                reason=f"Заявка #{order.id} истекла к моменту платежа",
            )
            # Уведомляем владельца
            notify_owner_text = (
                f"⚠️ <b>Платёж по истекшей заявке #{order.id}!</b>\n\n"
                f"• Пользователь: <code>{order.user_id}</code>\n"
                f"• Сумма: <b>{amt_display}</b>\n"
                f"• Валюта: <b>{transfer.currency}</b>\n"
                f"• Комментарий: <code>{raw_comment}</code>\n"
                f"• Истекла: <code>{order.expires_at}</code>\n"
                f"• TX Hash: <code>{transfer.tx_hash}</code>\n\n"
                "Автоматически не засчитано. Требуется ручная проверка администратором."
            )
            await notify_owner(bot, db, config, notify_owner_text)

            # Уведомляем пользователя
            if bot is not None:
                user_msg = (
                    f"⚠️ <b>Оплата по заявке #{order.id} поступила после истечения срока!</b>\n\n"
                    f"Срок действия заявки истёк (<code>{format_datetime_msk(order.expires_at, with_seconds=True)}</code>).\n"
                    f"Сумма: <b>{amt_display}</b>\n"
                    f"TX Hash: <code>{transfer.tx_hash}</code>\n\n"
                    "Пожалуйста, свяжитесь с поддержкой/администратором для ручного зачисления подписки."
                )
                try:
                    await bot.send_message(chat_id=order.user_id, text=user_msg)
                except Exception as e:
                    logger.warning(
                        f"Не удалось отправить сообщение пользователю {order.user_id}: {e}"
                    )
            continue

        # 4. Проверка валюты
        if transfer.currency != order.currency:
            await db.record_unmatched_payment(
                tx_hash=transfer.tx_hash,
                currency=transfer.currency,
                amount=transfer.amount,
                comment=raw_comment,
                reason=f"Несовпадение валюты: ожидалась {order.currency}, получена {transfer.currency}",
            )
            await notify_owner(
                bot,
                db,
                config,
                f"⚠️ Несовпадение валюты по заявке #{order.id}: ожидалась {order.currency}, получена {transfer.currency} (TX: {transfer.tx_hash})",
            )
            continue

        # 5. Проверка недоплаты
        if transfer.amount < order.amount:
            # Ожидаемая сумма
            if order.currency == "TON":
                exp_display = (
                    f"{order.amount / 1_000_000_000:.4f}".rstrip("0").rstrip(".") + " TON"
                )
            else:
                exp_display = f"{order.amount / 1_000_000:.2f} USDT"

            await db.record_unmatched_payment(
                tx_hash=transfer.tx_hash,
                currency=transfer.currency,
                amount=transfer.amount,
                comment=raw_comment,
                reason=f"Недоплата: получено {transfer.amount}, требовалось {order.amount}",
            )

            # Сообщаем пользователю и владельцу
            if bot is not None:
                user_err = (
                    f"⚠️ <b>Недоплата по заявке #{order.id}!</b>\n\n"
                    f"• Получено: <b>{amt_display}</b>\n"
                    f"• Требовалось: <b>{exp_display}</b>\n"
                    f"• TX Hash: <code>{transfer.tx_hash}</code>\n\n"
                    "Платёж не засчитан. Пожалуйста, обратитесь к администратору."
                )
                try:
                    await bot.send_message(chat_id=order.user_id, text=user_err)
                except Exception as e:
                    logger.warning(
                        f"Не удалось отправить сообщение о недоплате пользователю {order.user_id}: {e}"
                    )

            owner_err = (
                f"⚠️ <b>Недоплата по заявке #{order.id}!</b>\n\n"
                f"• Пользователь: <code>{order.user_id}</code>\n"
                f"• Получено: <b>{amt_display}</b>\n"
                f"• Требовалось: <b>{exp_display}</b>\n"
                f"• TX Hash: <code>{transfer.tx_hash}</code>\n"
                f"• Комментарий: <code>{raw_comment}</code>"
            )
            await notify_owner(bot, db, config, owner_err)
            continue

        # 6. Все проверки пройдены! Проводим атомарное зачисление в БД
        success, msg, updated_sub = await db.process_successful_payment(
            order_id=order.id,
            tx_hash=transfer.tx_hash,
            actual_amount=transfer.amount,
            currency=transfer.currency,
        )

        if not success:
            logger.error(f"Не удалось зачислить платёж #{order.id}: {msg}")
            continue

        processed_count += 1

        # Отправляем пользователю сообщение об активации и дате окончания
        if order.sub_type == "team":
            team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
            user_success_msg = (
                f"🎉 <b>Оплата успешно получена! Доступ в закрытую тиму открыт!</b>\n\n"
                f"📋 Услуга: <b>{order.sub_title}</b> ({order.duration_title})\n"
                f"🔗 Хэш транзакции: <code>{transfer.tx_hash}</code>\n\n"
                f"👉 <b>Ваша ссылка для вступления:</b>\n{team_link}\n\n"
                "<i>Ссылка также всегда доступна в вашем /profile</i>\n"
                "Добро пожаловать в команду!"
            )
            team_kb = InlineKeyboardBuilder()
            team_kb.button(text="🚀 Вступить в закрытую тиму", url=team_link)
            team_kb.button(text="👤 Мой профиль", callback_data="menu:profile")
            team_kb.button(text="◀️ Главное меню", callback_data="menu:main")
            team_kb.adjust(1)
            if bot is not None:
                try:
                    await bot.send_message(
                        chat_id=order.user_id,
                        text=user_success_msg,
                        reply_markup=team_kb.as_markup(),
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    logger.warning(
                        f"Не удалось отправить уведомление об оплате пользователю {order.user_id}: {e}"
                    )
        else:
            if updated_sub:
                if order.sub_type == "both":
                    until_lines = (
                        f"• Маркет: <b>{updated_sub.market_until if updated_sub.market_until != 'forever' else 'навсегда'}</b>\n"
                        f"• Улучшения: <b>{updated_sub.upgrades_until if updated_sub.upgrades_until != 'forever' else 'навсегда'}</b>"
                    )
                elif order.sub_type == "market":
                    until_lines = f"• Срок действия: <b>{updated_sub.market_until if updated_sub.market_until != 'forever' else 'навсегда'}</b>"
                else:
                    until_lines = f"• Срок действия: <b>{updated_sub.upgrades_until if updated_sub.upgrades_until != 'forever' else 'навсегда'}</b>"
            else:
                until_lines = f"• Срок: <b>{order.duration_title}</b>"

            user_success_msg = (
                f"🎉 <b>Оплата успешно получена и подписка активирована!</b>\n\n"
                f"📋 Тариф: <b>{order.sub_title}</b> ({order.duration_title})\n"
                f"{until_lines}\n\n"
                f"🔗 Хэш транзакции: <code>{transfer.tx_hash}</code>\n\n"
                "Спасибо за покупку! Настроить получение алертов можно командами /mode и /price. "
                "Статус подписки доступен в /profile."
            )
            if bot is not None:
                try:
                    await bot.send_message(chat_id=order.user_id, text=user_success_msg)
                except Exception as e:
                    logger.warning(
                        f"Не удалось отправить уведомление об оплате пользователю {order.user_id}: {e}"
                    )

        # Уведомляем владельца
        owner_success_msg = (
            f"💰 <b>Успешная оплата подписки!</b>\n\n"
            f"• Заявка: <b>#{order.id}</b>\n"
            f"• Пользователь: <code>{order.user_id}</code>\n"
            f"• Тариф: <b>{order.sub_title}</b> ({order.duration_title})\n"
            f"• Сумма: <b>{amt_display}</b>\n"
            f"• TX Hash: <code>{transfer.tx_hash}</code>"
        )
        await notify_owner(bot, db, config, owner_success_msg)

    return processed_count


@router.callback_query(lambda c: bool(c.data and c.data.startswith("order_check:")))
async def handle_order_check(
    callback: types.CallbackQuery, db: Database, config: AppConfig, bot: Bot
) -> None:
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    if order.status == "paid":
        await callback.answer("✅ Заявка уже успешно оплачена!", show_alert=True)
        if callback.message:
            if order.sub_type == "team":
                team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
                b = InlineKeyboardBuilder()
                b.button(text="🚀 Вступить в закрытую тиму", url=team_link)
                b.button(text="◀️ Главное меню", callback_data="menu:main")
                b.adjust(1)
                await callback.message.edit_text(
                    f"✅ <b>Заявка #{order.id} уже успешно оплачена!</b>\n\n"
                    f"Услуга: <b>{order.sub_title}</b>\n\n"
                    f"👉 <b>Ваша ссылка для вступления:</b>\n{team_link}",
                    reply_markup=b.as_markup(),
                )
            else:
                await callback.message.edit_text(
                    f"✅ <b>Заявка #{order.id} успешно оплачена!</b>\n\n"
                    f"Подписка активна. Статус можно проверить в /profile.",
                )
        return

    if order.status in ("expired", "cancelled"):
        await callback.answer(f"Заявка #{order.id} имеет статус: {order.status}", show_alert=True)
        return

    verifier = PaymentVerifier(
        owner_address=config.owner_address or "",
        ton_network=config.ton_network,
        tonapi_key=config.tonapi_key,
        usdt_master=config.usdt_master_address,
    )
    await verify_and_process_payments(bot, db, config, verifier)

    order_after = await db.get_order(order_id)
    if order_after and order_after.status == "paid":
        await callback.answer("🎉 Оплата успешно получена!", show_alert=True)
        if callback.message:
            builder = InlineKeyboardBuilder()
            if order_after.sub_type == "team":
                team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
                builder.button(text="🚀 Вступить в закрытую тиму", url=team_link)
                builder.button(text="👤 Мой профиль", callback_data="menu:profile")
                builder.button(text="◀️ Главное меню", callback_data="menu:main")
                builder.adjust(1)
                await callback.message.edit_text(
                    f"✅ <b>Заявка #{order.id} успешно оплачена!</b>\n\n"
                    f"Услуга: <b>{order_after.sub_title}</b> ({order_after.duration_title})\n"
                    f"Хэш транзакции: <code>{order_after.tx_hash}</code>\n\n"
                    f"👉 <b>Ваша ссылка для вступления:</b>\n{team_link}\n\n"
                    "Добро пожаловать в команду!",
                    reply_markup=builder.as_markup(),
                )
            else:
                builder.button(text="◀️ Главное меню", callback_data="menu:main")
                builder.adjust(1)
                await callback.message.edit_text(
                    f"✅ <b>Заявка #{order.id} успешно оплачена!</b>\n\n"
                    f"Тариф: <b>{order_after.sub_title}</b> ({order_after.duration_title})\n"
                    f"Хэш транзакции: <code>{order_after.tx_hash}</code>\n\n"
                    "Подписка активирована. Спасибо!",
                    reply_markup=builder.as_markup(),
                )
        return
    else:
        await callback.answer(
            "⏳ Платёж пока не обнаружен в блокчейне. "
            "Обычно подтверждение в сети TON занимает 10-30 секунд. "
            "Попробуйте нажать кнопку ещё раз через полминуты.",
            show_alert=True,
        )


@router.callback_query(lambda c: bool(c.data and c.data.startswith("order_cancel:")))
async def handle_order_cancel(callback: types.CallbackQuery, db: Database) -> None:
    order_id = int(callback.data.split(":")[1])
    success = await db.cancel_order(order_id, callback.from_user.id)
    if success:
        await callback.answer("Заявка отменена", show_alert=False)
        if callback.message:
            builder = InlineKeyboardBuilder()
            builder.button(text="💳 Тарифы", callback_data="tariffs_back_types")
            builder.button(text="◀️ Главное меню", callback_data="menu:main")
            builder.adjust(2)
            await callback.message.edit_text(
                f"❌ <b>Заявка #{order_id} была отменена.</b>\n\n"
                "Вы можете создать новую заявку в любое время через меню тарифов.",
                reply_markup=builder.as_markup(),
            )
    else:
        await callback.answer("Не удалось отменить заявку", show_alert=True)



@router.message(Command("price"))
async def handle_price(
    message: types.Message, command: CommandObject, db: Database
) -> None:
    """
    Команда /price:
    - Без аргументов: показать текущий диапазон цен.
    - С аргументами (<min> <max>): обновить диапазон цен.
    """
    chat_id = message.chat.id
    args = command.args

    # Если вызвана без аргументов — показываем текущие значения
    if not args or not args.strip():
        sub = await db.get_subscriber(chat_id)
        if not sub:
            sub = await db.upsert_subscriber(chat_id)

        await message.answer(
            get_price_text(sub),
            reply_markup=get_price_keyboard(sub),
        )
        return

    # Валидация аргументов
    parts = args.strip().split()
    if len(parts) != 2:
        await message.answer(
            "❌ <b>Неверный формат команды!</b>\n\n"
            "Необходимо указать ровно два числа через пробел: минимальную и максимальную цену.\n\n"
            "Пример правильного использования:\n"
            "<code>/price 100 800</code>\n"
            "<i>(где 100 — минимум, 800 — максимум в Stars)</i>"
        )
        return

    min_str, max_str = parts[0], parts[1]

    try:
        min_price = int(min_str)
        max_price = int(max_str)
    except ValueError:
        await message.answer(
            "❌ <b>Оба значения должны быть целыми числами!</b>\n\n"
            "Пример правильного использования:\n"
            "<code>/price 100 800</code>"
        )
        return

    if min_price < 0 or max_price <= min_price:
        await message.answer(
            "❌ <b>Ошибка валидации цен!</b>\n\n"
            "Требования к значениям:\n"
            "• Минимальная цена должна быть <b>>= 0</b>\n"
            "• Максимальная цена должна быть <b>строго больше минимальной</b> (макс > мин)\n\n"
            "Пример правильного использования:\n"
            "<code>/price 100 800</code>"
        )
        return

    # Сохраняем новые настройки
    await db.update_subscriber_price(chat_id, min_price, max_price)

    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)

    await message.answer(
        f"✅ <b>Диапазон цен успешно обновлён!</b>\n\n"
        f"Теперь вы будете получать уведомления только о лотах стоимостью от <b>{min_price}</b> "
        f"до <b>{max_price} ★</b>.",
        reply_markup=builder.as_markup(),
    )
    logger.info(
        f"Пользователь {chat_id} установил диапазон цен: [{min_price}, {max_price}]"
    )


@router.message(Command("stats"))
async def handle_stats(
    message: types.Message, db: Database, config: AppConfig
) -> None:
    """Команда /stats — статистика подписчиков (только для OWNER_CHAT_ID)."""
    user_id = message.from_user.id if message.from_user else None

    # Доступ только для владельца бота
    if not config.owner_chat_id or user_id != config.owner_chat_id:
        logger.warning(
            f"Попытка вызова /stats неавторизованным пользователем: {user_id}"
        )
        return

    stats = await db.get_stats()

    stats_text = (
        "📊 <b>Статистика сервиса мониторинга:</b>\n\n"
        f"👥 Активных подписчиков: <b>{stats['active_subscribers']}</b>\n"
        f"👥 Всего подписчиков в базе: <b>{stats['total_subscribers']}</b>\n"
        f"📦 Лотов в очереди outbox (pending): <b>{stats['pending_outbox']}</b>\n"
        f"🏷️ Всего обнаружено уникальных лотов: <b>{stats['seen_lots']}</b>\n"
        f"💎 Автоматически зафиксировано апгрейдов: <b>{stats.get('scanned_upgrades', 0)}</b>"
    )

    await message.answer(stats_text)


@router.message(Command("my"))
async def handle_my_claimed(
    message: types.Message, db: Database, config: AppConfig
) -> None:
    """Команда /my: показывает список лотов, занятых пользователем сегодня."""
    user = message.from_user
    if not user:
        return

    items = await db.get_user_claimed_lots(user.id, limit=10)
    if not items:
        await message.answer(
            "📭 <b>У вас пока нет занятых лотов.</b>\n\n"
            "Когда в ленте появится новый лот, нажмите кнопку <b>[🔒 Занять лот]</b>, "
            "чтобы забронировать его за собой и сразу получить ссылку на диалог с продавцом."
        )
        return

    await message.answer(
        f"📋 <b>Ваши последние занятые лоты ({len(items)}):</b>\n"
        f"<i>Лоты, закреплённые за вами:</i>"
    )

    for meta, lot in items:
        event_type = getattr(lot, "event_type", "market")
        if event_type == "upgrade":
            offer_url = lot.get_upgrade_offer_url(config.upgrade_message_template)
        else:
            offer_url = lot.get_offer_url(
                discount=config.offer_discount,
                template=config.message_template,
            )

        card_text = (
            f"🎁 <b>{lot.title} #{lot.num}</b>\n"
            f"💰 Цена: <b>{lot.price_stars} ★</b>\n"
            f"👤 Продавец: @{lot.username or 'без_юзернейма'}\n"
            f"🕒 Занято: <code>{format_datetime_msk(meta.get('claimed_at', ''), with_seconds=True)}</code>"
        )

        builder = InlineKeyboardBuilder()
        if offer_url:
            builder.button(text="✉️ Написать продавцу", url=offer_url)
        if lot.slug:
            builder.button(text="🔗 Маркет", url=f"https://t.me/nft/{lot.slug}")
        builder.adjust(1)

        await message.answer(card_text, reply_markup=builder.as_markup())


@router.callback_query(lambda c: bool(c.data and c.data.startswith("claim:")))
async def handle_claim_lot(
    callback: types.CallbackQuery, db: Database, config: AppConfig, bot: Bot
) -> None:
    """Обработчик нажатия на кнопку [🔒 Занять лот]."""
    user = callback.from_user
    if not user or not callback.data:
        return

    try:
        outbox_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка данных лота", show_alert=True)
        return

    lot = await db.get_outbox_lot(outbox_id)
    if not lot:
        await callback.answer("❌ Лот не найден в базе данных.", show_alert=True)
        return

    user_name = user.full_name or user.first_name or f"User {user.id}"
    user_mention = f"@{user.username}" if user.username else user_name

    success, claim_info = await db.claim_lot(
        outbox_id=outbox_id,
        lot_key=lot.lot_key,
        user_id=user.id,
        username=user.username,
        name=user_name,
    )

    if success:
        try:
            await callback.answer("🎉 Вы успешно заняли этот лот!", show_alert=False)
        except TelegramBadRequest as e:
            logger.debug(f"Игнорирован устаревший callback (claim success): {e}")

        event_type = getattr(lot, "event_type", "market")
        custom_tpl = await db.get_subscriber_template(user.id)
        effective_tpl = custom_tpl or (
            config.upgrade_message_template if event_type == "upgrade" else config.message_template
        )
        if event_type == "upgrade":
            offer_url = lot.get_upgrade_offer_url(effective_tpl)
        else:
            offer_url = lot.get_offer_url(
                discount=config.offer_discount,
                template=effective_tpl,
            )

        winner_builder = InlineKeyboardBuilder()
        if offer_url:
            winner_builder.button(text="✉️ Написать продавцу", url=offer_url)
        winner_builder.button(text="✅ Вы заняли этот лот", callback_data="claimed_self")
        if lot.slug:
            winner_builder.button(text="🔗 Открыть на маркете", url=f"https://t.me/nft/{lot.slug}")
        winner_builder.adjust(1)

        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=winner_builder.as_markup())
        except Exception as e:
            logger.debug(f"Не удалось обновить клавиатуру победителя: {e}")

        # В фоновом режиме обновляем сообщение для всех остальных подписчиков
        async def update_other_dispatches():
            try:
                dispatches = await db.get_outbox_dispatches(outbox_id)
                other_builder = InlineKeyboardBuilder()
                other_builder.button(
                    text=f"❌ Занято: {user_mention}", callback_data="claimed_taken"
                )
                other_builder.adjust(1)
                other_markup = other_builder.as_markup()
                hidden_text = lot.format_claimed_notification_html(user_mention)

                for cid, mid in dispatches:
                    if cid == user.id:
                        continue
                    try:
                        # Моментально скрываем юзернейм продавца, ссылку на контакт и ссылку на маркет:
                        if hasattr(bot, "edit_message_text"):
                            await bot.edit_message_text(
                                text=hidden_text,
                                chat_id=cid,
                                message_id=mid,
                                reply_markup=other_markup,
                                parse_mode="HTML",
                            )
                        else:
                            await bot.edit_message_reply_markup(
                                chat_id=cid, message_id=mid, reply_markup=other_markup
                            )
                    except Exception:
                        try:
                            await bot.edit_message_reply_markup(
                                chat_id=cid, message_id=mid, reply_markup=other_markup
                            )
                        except Exception:
                            pass
            except Exception as err:
                logger.debug(f"Ошибка фонового обновления сообщений подписчиков: {err}")

        asyncio.create_task(update_other_dispatches())

    else:
        claimed_username = claim_info.get("username")
        mention = f"@{claimed_username}" if claimed_username else (claim_info.get("name") or "другим пользователем")

        try:
            await callback.answer(f"⚠️ Этот лот уже занял {mention}!", show_alert=True)
        except TelegramBadRequest as e:
            logger.debug(f"Игнорирован устаревший callback (claim taken): {e}")

        taken_builder = InlineKeyboardBuilder()
        taken_builder.button(text=f"❌ Занято: {mention}", callback_data="claimed_taken")
        taken_builder.adjust(1)
        taken_markup = taken_builder.as_markup()
        hidden_text = lot.format_claimed_notification_html(mention)

        try:
            if callback.message:
                if hasattr(callback.message, "edit_text"):
                    await callback.message.edit_text(
                        text=hidden_text,
                        reply_markup=taken_markup,
                        parse_mode="HTML",
                    )
                else:
                    await callback.message.edit_reply_markup(reply_markup=taken_markup)
        except Exception:
            try:
                if callback.message:
                    await callback.message.edit_reply_markup(reply_markup=taken_markup)
            except Exception:
                pass


@router.callback_query(lambda c: c.data in ("claimed_self", "claimed_taken"))
async def handle_claimed_status(callback: types.CallbackQuery) -> None:
    """Информационные заглушки для кнопок статуса занятия."""
    if callback.data == "claimed_self":
        await callback.answer("✅ Этот лот закреплен за вами!", show_alert=False)
    else:
        await callback.answer("❌ Этот лот уже занят другим подписчиком.", show_alert=False)


# ------------------ Обработчики интерактивного главного меню ------------------

@router.callback_query(F.data == "menu:main")
async def handle_menu_main_callback(callback: types.CallbackQuery, db: Database) -> None:
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)
    text = get_main_menu_text(sub)
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=get_main_menu_keyboard(sub))
        except Exception:
            await callback.message.answer(text, reply_markup=get_main_menu_keyboard(sub))
    await callback.answer()


@router.callback_query(F.data == "menu:tariffs")
async def handle_menu_tariffs_callback(callback: types.CallbackQuery) -> None:
    text = (
        "💎 <b>Тарифы подписки и услуги сервиса</b>\n\n"
        "Выберите интересующий вас раздел:\n\n"
        "• 📦 <b>Маркет</b> — моментальные алерты о новых листингах на вторичном маркете Telegram Stars.\n"
        "• 💎 <b>Улучшения</b> — алерты о подарках, только что улучшенных владельцами до уникальных NFT.\n"
        "• 🔔 <b>Всё вместе</b> — полный доступ к обоим режимам мониторинга с максимальной выгодой!\n"
        "• 👥 <b>Закрытая тима & Обучение</b> — приватное комьюнити, обучение с нуля, помощь и фейк-гарант.\n\n"
        "Оплата принимается в криптовалюте <b>TON</b> и <b>USDT</b> (сеть TON)."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=get_tariffs_select_type_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def handle_menu_profile_callback(
    callback: types.CallbackQuery, db: Database, config: AppConfig
) -> None:
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)
    status_str = "🟢 Включена" if sub.active else "🔴 На паузе"
    tpl_status = "🟢 Индивидуальный" if sub.custom_template else "⚪ Стандартный"
    team_status = "✅ Доступ открыт" if getattr(sub, "has_team_access", False) else "❌ Не приобретено"
    team_line = f"👥 <b>Закрытая тима:</b> {team_status}\n"
    if getattr(sub, "has_team_access", False):
        team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
        team_line += f"👉 <i>Ссылка на тиму:</i> {team_link}\n"

    text = (
        f"👤 <b>Ваш профиль (ID: <code>{chat_id}</code>)</b>\n\n"
        f"📦 <b>Подписка Маркет:</b> {sub.get_status_text('market')}\n"
        f"💎 <b>Подписка Улучшения:</b> {sub.get_status_text('upgrades')}\n"
        f"{team_line}\n"
        f"🔔 <b>Текущий режим:</b> <b>{get_mode_title(sub.mode)}</b>\n"
        f"🎨 <b>Фон апгрейдов:</b> <b>{get_upgrade_filter_title(getattr(sub, 'upgrade_filter', 'all'))}</b>\n"
        f"💰 <b>Диапазон цен:</b> <b>{sub.min_price} – {sub.max_price} ★</b>\n"
        f"📝 <b>Шаблон продавцу:</b> <b>{tpl_status}</b>\n"
        f"📡 <b>Рассылка:</b> <b>{status_str}</b>"
    )
    builder = InlineKeyboardBuilder()
    if getattr(sub, "has_team_access", False):
        team_link = getattr(config, "team_invite_link", "https://t.me/+your_team_invite_link")
        builder.button(text="👥 Чат закрытой тимы", url=team_link)
    builder.button(text="💳 Выбрать тариф / Услуги", callback_data="menu:tariffs")
    builder.button(text="⚙️ Сменить режим", callback_data="menu:mode")
    builder.button(text="💰 Настроить цены", callback_data="menu:price")
    builder.button(text="📝 Шаблон сообщения", callback_data="menu:template")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "menu:template")
async def handle_menu_template_callback(
    callback: types.CallbackQuery, db: Database, config: AppConfig, state: FSMContext
) -> None:
    await state.clear()
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)
    text = get_template_text(sub, config.message_template)
    kb = get_template_keyboard(bool(sub.custom_template))
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "template:set")
async def handle_template_set_callback(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    await state.set_state(CustomTemplateState.waiting_for_template)
    cancel_b = InlineKeyboardBuilder()
    cancel_b.button(text="❌ Отмена", callback_data="template:cancel")
    text = (
        "✏️ <b>Введите ваш новый шаблон сообщения продавцу:</b>\n\n"
        "Отправьте сообщением текст, который будет автоматически открываться в диалоге с продавцом при нажатии кнопки <b>«✉️ Написать продавцу»</b>.\n\n"
        "<b>Доступные переменные (подставляются автоматически):</b>\n"
        "• <code>{title}</code> — название подарка (например, <i>Chill Flame</i>)\n"
        "• <code>{num}</code> — номер подарка (например, <i>#42</i>)\n"
        "• <code>{offer}</code> — предложенная цена со скидкой\n"
        "• <code>{discount}</code> — размер скидки в Stars\n\n"
        "<i>Пример:</i>\n"
        "<code>Привет! За сколько отдашь {title} #{num}? Заберу прямо сейчас за {offer} звёзд или переводом на карту.</code>\n\n"
        "<i>Или нажмите кнопку «Отмена» ниже:</i>"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=cancel_b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "template:cancel")
async def handle_template_cancel_callback(
    callback: types.CallbackQuery, db: Database, config: AppConfig, state: FSMContext
) -> None:
    await state.clear()
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)
    text = get_template_text(sub, config.message_template)
    kb = get_template_keyboard(bool(sub.custom_template))
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("Ввод шаблона отменён", show_alert=False)


@router.callback_query(F.data == "template:reset")
async def handle_template_reset_callback(
    callback: types.CallbackQuery, db: Database, config: AppConfig, state: FSMContext
) -> None:
    await state.clear()
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await db.update_subscriber_template(chat_id, None)
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)
    text = get_template_text(sub, config.message_template)
    kb = get_template_keyboard(False)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("🔄 Шаблон сброшен на стандартный!", show_alert=False)


@router.callback_query(F.data == "menu:mode")
async def handle_menu_mode_callback(callback: types.CallbackQuery, db: Database) -> None:
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)

    if not sub.has_any_access():
        await callback.answer("🔒 У вас нет активной подписки! Ознакомьтесь с тарифами.", show_alert=True)
        if callback.message:
            await callback.message.edit_text(
                "🔒 <b>Доступ ограничен.</b> Выберите тариф:",
                reply_markup=get_tariffs_select_type_keyboard(),
            )
        return

    current_mode = sub.mode or "market"
    upg_filter = getattr(sub, "upgrade_filter", "all")
    text = (
        f"⚙️ <b>Режим уведомлений</b>\n\n"
        f"• Текущий режим: <b>{get_mode_title(current_mode)}</b>\n"
        f"• Фильтр апгрейдов: <b>{get_upgrade_filter_title(upg_filter)}</b>\n\n"
        f"📦 Маркет: {sub.get_status_text('market')}\n"
        f"💎 Улучшения: {sub.get_status_text('upgrades')}\n\n"
        "Выберите режим уведомлений или нажмите на кнопку фильтра фонов апгрейдов:"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=get_mode_keyboard(current_mode, sub))
    await callback.answer()


@router.callback_query(F.data == "menu:price")
async def handle_menu_price_callback(callback: types.CallbackQuery, db: Database) -> None:
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)
    text = get_price_text(sub)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=get_price_keyboard(sub))
    await callback.answer()


@router.callback_query(lambda c: bool(c.data and c.data.startswith("price_set:")))
async def handle_price_set_callback(callback: types.CallbackQuery, db: Database) -> None:
    _, min_str, max_str = callback.data.split(":")
    min_p, max_p = int(min_str), int(max_str)
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await db.update_subscriber_price(chat_id, min_p, max_p)
    sub = await db.get_subscriber(chat_id)
    await callback.answer(f"✅ Установлен диапазон: {min_p} – {max_p} ★")
    if callback.message and sub:
        try:
            await callback.message.edit_text(get_price_text(sub), reply_markup=get_price_keyboard(sub))
        except Exception:
            pass


@router.callback_query(lambda c: bool(c.data and c.data.startswith("price_adj:")))
async def handle_price_adj_callback(callback: types.CallbackQuery, db: Database) -> None:
    _, field, delta_str = callback.data.split(":")
    delta = int(delta_str)
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)

    new_min = sub.min_price
    new_max = sub.max_price
    if field == "min":
        new_min = max(0, new_min + delta)
        if new_min >= new_max:
            new_max = new_min + 100
    else:
        new_max = max(new_min + 50, new_max + delta)

    await db.update_subscriber_price(chat_id, new_min, new_max)
    sub.min_price = new_min
    sub.max_price = new_max
    await callback.answer(f"✅ Обновлено: {new_min} – {new_max} ★")
    if callback.message:
        try:
            await callback.message.edit_text(get_price_text(sub), reply_markup=get_price_keyboard(sub))
        except Exception:
            pass


@router.callback_query(F.data == "menu:toggle_active")
async def handle_menu_toggle_active_callback(callback: types.CallbackQuery, db: Database) -> None:
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        sub = await db.upsert_subscriber(chat_id)

    new_active = not sub.active
    if new_active:
        await db.upsert_subscriber(chat_id)
        sub.active = True
        msg = "Уведомления включены! ✅"
    else:
        await db.deactivate_subscriber(chat_id)
        sub.active = False
        msg = "Уведомления приостановлены. ⏸️"

    await callback.answer(msg)
    if callback.message:
        try:
            await callback.message.edit_text(
                get_main_menu_text(sub),
                reply_markup=get_main_menu_keyboard(sub),
            )
        except Exception:
            pass


@router.callback_query(F.data == "menu:my")
async def handle_menu_my_callback(
    callback: types.CallbackQuery, db: Database, config: AppConfig
) -> None:
    chat_id = callback.from_user.id
    items = await db.get_user_claimed_lots(chat_id, limit=10)
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)

    if not items:
        text = (
            "📭 <b>У вас пока нет занятых лотов.</b>\n\n"
            "Когда в ленте появится новый лот, нажмите кнопку <b>[🔒 Занять лот]</b>, "
            "чтобы забронировать его за собой и сразу получить ссылку на диалог с продавцом."
        )
        if callback.message:
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await callback.answer()
        return

    text = f"📋 <b>Ваши последние занятые лоты ({len(items)}):</b>\n\n"
    for idx, (meta, lot) in enumerate(items, 1):
        event_type = getattr(lot, "event_type", "market")
        ev_title = "💎 Улучшение" if event_type == "upgrade" else "📦 Маркет"
        text += (
            f"<b>{idx}. {lot.title} #{lot.num}</b> ({ev_title})\n"
            f"• Цена: <b>{lot.price_stars} ★</b>\n"
            f"• Продавец: @{lot.username or 'без_юзернейма'}\n"
            f"• Занято: <code>{format_datetime_msk(meta.get('claimed_at', ''), with_seconds=True)}</code>\n"
        )
        if lot.slug:
            text += f"• Ссылка: https://t.me/nft/{lot.slug}\n"
        text += "\n"

    if callback.message:
        await callback.message.edit_text(
            text, reply_markup=builder.as_markup(), disable_web_page_preview=True
        )
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def handle_menu_help_callback(callback: types.CallbackQuery) -> None:
    text = (
        "📖 <b>Справка по сервису мониторинга подарков:</b>\n\n"
        "• 📦 <b>Мониторинг Маркета</b> — бот отслеживает появление выгодных листингов подарков за Telegram Stars.\n"
        "• 💎 <b>Мониторинг Улучшений</b> — отслеживание только что улучшенных подарков со всех коллекций, "
        "строго на фонах <b>Black</b> и <b>Оникс Black</b>.\n"
        "• 🔒 <b>Система бронирования лотов</b> — при получении алерта нажмите кнопку <b>[🔒 Занять лот]</b>. "
        "Лот будет закреплён за вами, а вам откроется прямая ссылка на диалог с продавцом для торга.\n"
        "• 💳 <b>Оплата подписки</b> — доступна в криптовалюте TON и USDT сети TON с автоматической проверкой платежей.\n\n"
        "<i>Всё управление ботом осуществляется исключительно с помощью интерактивных кнопок.</i>"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Тарифы и подписка", callback_data="menu:tariffs")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@router.message(CustomTemplateState.waiting_for_template, F.text)
async def handle_custom_template_input(
    message: types.Message, db: Database, state: FSMContext
) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        if text.startswith("/admin"):
            return  # Передаём управление админ-роутеру
        await state.clear()
        cancel_b = InlineKeyboardBuilder()
        cancel_b.button(text="◀️ Главное меню", callback_data="menu:main")
        await message.answer("⚠️ Ввод шаблона отменён из-за ввода команды.", reply_markup=cancel_b.as_markup())
        return

    if len(text) > 1000:
        cancel_b = InlineKeyboardBuilder()
        cancel_b.button(text="❌ Отмена", callback_data="template:cancel")
        await message.answer(
            "⚠️ <b>Текст слишком длинный!</b>\n\nМаксимальная длина шаблона — 1000 символов. Сократите текст и отправьте снова:",
            reply_markup=cancel_b.as_markup(),
            parse_mode="HTML",
        )
        return

    # Проверяем корректность фигурных скобок и поддерживаемых переменных
    try:
        text.format(title="Chill Flame", num=123, offer=550, discount=50)
    except KeyError as ke:
        cancel_b = InlineKeyboardBuilder()
        cancel_b.button(text="❌ Отмена", callback_data="template:cancel")
        await message.answer(
            f"⚠️ <b>Неизвестная переменная в шаблоне:</b> <code>{{{ke.args[0]}}}</code>\n\n"
            "Разрешены только следующие переменные:\n"
            "• <code>{title}</code> — название подарка\n"
            "• <code>{num}</code> — номер подарка\n"
            "• <code>{offer}</code> — предложенная цена в Stars\n"
            "• <code>{discount}</code> — скидка в Stars\n\n"
            "Отправьте исправленный текст или нажмите «Отмена»:",
            reply_markup=cancel_b.as_markup(),
            parse_mode="HTML",
        )
        return
    except ValueError as ve:
        cancel_b = InlineKeyboardBuilder()
        cancel_b.button(text="❌ Отмена", callback_data="template:cancel")
        await message.answer(
            f"⚠️ <b>Синтаксическая ошибка в фигурных скобках:</b> <i>{html.escape(str(ve))}</i>\n\n"
            "Отправьте исправленный текст или нажмите «Отмена»:",
            reply_markup=cancel_b.as_markup(),
            parse_mode="HTML",
        )
        return

    chat_id = message.chat.id
    await db.update_subscriber_template(chat_id, text)
    await state.clear()

    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Посмотреть шаблон", callback_data="menu:template")
    builder.button(text="◀️ Главное меню", callback_data="menu:main")
    builder.adjust(1)

    await message.answer(
        "✅ <b>Индивидуальный шаблон сообщения успешно сохранён!</b>\n\n"
        f"<b>Ваш текст:</b>\n<code>{html.escape(text)}</code>\n\n"
        "Теперь при нажатии кнопки <b>«✉️ Написать продавцу»</b> у занятых вами лотов будет автоматически открываться этот текст с подставленными данными лота.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


# ------------------ Команда /inf для закрытой тимы (Администратор) ------------------

def format_team_orders_view(items: List[Dict[str, Any]]) -> List[str]:
    """Форматирует сводку и список заявок на закрытую тиму с разбивкой по сообщениям (до 3800 симв.)."""
    total = len(items)
    if total == 0:
        return [
            "👥 <b>Заявки на вступление в Закрытую Тиму (/inf)</b>\n\n"
            "ℹ️ На данный момент заявок на вступление в тиму не создано."
        ]

    paid_items = [x for x in items if x["order"].status == "paid"]
    pending_items = [x for x in items if x["order"].status == "pending"]
    expired_items = [x for x in items if x["order"].status in ("expired", "cancelled")]

    ton_sum = sum(
        x["order"].amount / 1_000_000_000
        for x in paid_items
        if x["order"].currency == "TON"
    )
    usdt_sum = sum(
        x["order"].amount / 1_000_000
        for x in paid_items
        if x["order"].currency == "USDT"
    )

    header = (
        "👥 <b>Заявки на вступление в Закрытую Тиму (/inf)</b>\n\n"
        "📊 <b>Общая статистика:</b>\n"
        f"• Всего заявок: <b>{total}</b>\n"
        f"• ✅ Оплачено: <b>{len(paid_items)}</b> ({ton_sum:g} TON, {usdt_sum:.2f} USDT)\n"
        f"• ⏳ В ожидании: <b>{len(pending_items)}</b>\n"
        f"• ⌛ Истекло/Отменено: <b>{len(expired_items)}</b>\n\n"
        "──────────────\n\n"
    )

    status_titles = {
        "paid": "✅ Оплачено",
        "pending": "⏳ В ожидании оплаты",
        "expired": "⌛ Истекла",
        "cancelled": "❌ Отменена",
    }

    chunks = []
    current_chunk = header

    for item in items:
        order: Order = item["order"]
        username = item.get("username")
        first_name = item.get("first_name") or ""
        last_name = item.get("last_name") or ""
        full_name = f"{first_name} {last_name}".strip()

        if username:
            user_info = f"@{username} (ID: <code>{order.user_id}</code>)"
        elif full_name:
            user_info = f"{html.escape(full_name)} (ID: <code>{order.user_id}</code>)"
        else:
            user_info = f"ID: <code>{order.user_id}</code>"

        status_str = status_titles.get(order.status, order.status)
        created_str = format_datetime_msk(order.created_at, with_seconds=True)

        lines = [
            f"🧾 <b>Заявка #{order.id}</b> — {status_str}",
            f"• Клиент: {user_info}",
            f"• Сумма: <b>{order.human_amount} {order.currency}</b> (код: <code>{order.comment}</code>)",
            f"• Создана: <i>{created_str}</i>",
        ]

        if order.status == "paid":
            paid_str = format_datetime_msk(order.paid_at, with_seconds=True) if order.paid_at else "—"
            tx = order.tx_hash or "не указан"
            short_tx = f"{tx[:10]}...{tx[-8:]}" if len(tx) > 22 else tx
            lines.append(f"• Оплата: <i>{paid_str}</i> | TX: <code>{short_tx}</code>")
        elif order.status == "pending":
            exp_str = format_datetime_msk(order.expires_at, with_seconds=True)
            lines.append(f"• Истекает: <i>{exp_str}</i>")

        block = "\n".join(lines) + "\n\n"

        if len(current_chunk) + len(block) > 3800:
            chunks.append(current_chunk)
            current_chunk = block
        else:
            current_chunk += block

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


@router.message(Command("inf"))
@router.message(F.text.lower().in_({"inf", "/inf"}))
async def handle_inf_command(message: types.Message, db: Database, config: Optional[AppConfig] = None) -> None:
    """Команда /inf: просмотр заявок на вступление в Закрытую Тиму (доступ для администратора)."""
    user = message.from_user
    username = (user.username or "").strip().lower().lstrip("@") if user else ""
    user_id = user.id if user else 0
    admin_u = (config.admin_username or "").strip().lower().lstrip("@") if config else ""
    is_owner = bool(config and config.owner_chat_id and user_id == config.owner_chat_id)
    is_admin = bool(admin_u and username == admin_u)
    if not (is_owner or is_admin):
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    orders = await db.get_team_orders()
    chunks = format_team_orders_view(orders)

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить список", callback_data="inf:refresh")
    builder.adjust(1)

    for idx, chunk in enumerate(chunks):
        kb = builder.as_markup() if idx == 0 else None
        await message.answer(chunk, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "inf:refresh")
async def handle_inf_refresh(callback: types.CallbackQuery, db: Database, config: Optional[AppConfig] = None) -> None:
    user = callback.from_user
    username = (user.username or "").strip().lower().lstrip("@") if user else ""
    user_id = user.id if user else 0
    admin_u = (config.admin_username or "").strip().lower().lstrip("@") if config else ""
    is_owner = bool(config and config.owner_chat_id and user_id == config.owner_chat_id)
    is_admin = bool(admin_u and username == admin_u)
    if not (is_owner or is_admin):
        await callback.answer("⛔ У вас нет доступа к этой функции.", show_alert=True)
        return

    orders = await db.get_team_orders()
    chunks = format_team_orders_view(orders)

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить список", callback_data="inf:refresh")
    builder.adjust(1)

    if callback.message:
        try:
            await callback.message.edit_text(
                chunks[0], reply_markup=builder.as_markup(), disable_web_page_preview=True
            )
        except Exception:
            pass
        for chunk in chunks[1:]:
            await callback.message.answer(chunk, disable_web_page_preview=True)

    await callback.answer("Список заявок обновлён!")


@router.message(F.text)
async def handle_text_message(
    message: types.Message, db: Database, config: Optional[AppConfig] = None
) -> None:
    """Обработчик любого входящего текста от пользователя."""
    text = (message.text or "").strip()
    chat_id = message.chat.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        user = message.from_user
        sub = await db.upsert_subscriber(
            chat_id,
            username=str(user.username) if (user and isinstance(user.username, str)) else None,
            first_name=str(user.first_name) if (user and isinstance(user.first_name, str)) else None,
            last_name=str(user.last_name) if (user and isinstance(user.last_name, str)) else None,
        )

    # Проверяем, ввёл ли пользователь диапазон цен (например "100 800" или "/price 100 800")
    clean_text = text.replace("/price", "").strip()
    parts = clean_text.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        min_p, max_p = int(parts[0]), int(parts[1])
        if min_p >= 0 and max_p > min_p:
            await db.update_subscriber_price(chat_id, min_p, max_p)
            sub.min_price = min_p
            sub.max_price = max_p
            builder = InlineKeyboardBuilder()
            builder.button(text="◀️ Главное меню", callback_data="menu:main")
            builder.adjust(1)
            await message.answer(
                f"✅ <b>Диапазон цен успешно обновлён!</b>\n\n"
                f"Теперь вы будете получать уведомления о лотах от <b>{min_p}</b> до <b>{max_p} ★</b>.",
                reply_markup=builder.as_markup(),
            )
            return

    # В любых других случаях показываем главное меню с inline-кнопками
    menu_text = get_main_menu_text(sub)
    await message.answer(menu_text, reply_markup=get_main_menu_keyboard(sub))


# ------------------ Фоновый процесс рассылки из Outbox ------------------

_bot_api_semaphore = asyncio.Semaphore(25)  # Глобальный лимит Bot API: до 25 сообщений/сек
_chat_cooldowns: Dict[int, float] = {}
_chat_last_send: Dict[int, float] = {}
_chat_locks: Dict[int, asyncio.Lock] = {}


def _get_chat_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


async def send_notification_to_user(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Optional[types.InlineKeyboardMarkup],
    db: Database,
) -> Optional[types.Message]:
    """Отправка сообщения одному пользователю с защитой от 429 Flood Control и без блокировки общего потока."""
    now_ts = asyncio.get_running_loop().time()

    # 1. Проверяем, не находится ли чат на временном КД из-за предыдущего 429
    cooldown_until = _chat_cooldowns.get(chat_id, 0.0)
    if cooldown_until > now_ts:
        logger.debug(
            f"Чат {chat_id} временно на КД после 429 еще {cooldown_until - now_ts:.1f} сек. Пропуск отправки."
        )
        return None

    lock = _get_chat_lock(chat_id)
    async with lock:
        now_ts = asyncio.get_running_loop().time()
        cooldown_until = _chat_cooldowns.get(chat_id, 0.0)
        if cooldown_until > now_ts:
            return None

        # Микро-интервал между сообщениями в один и тот же чат (250 мс), чтобы исключить 429
        last_time = _chat_last_send.get(chat_id, 0.0)
        elapsed = now_ts - last_time
        if elapsed < 0.25:
            await asyncio.sleep(0.25 - elapsed)
        _chat_last_send[chat_id] = asyncio.get_running_loop().time()

        for attempt in range(3):
            try:
                async with _bot_api_semaphore:
                    msg = await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=reply_markup,
                        disable_web_page_preview=False,
                    )
                    return msg
            except TelegramRetryAfter as e:
                wait_time = e.retry_after
                logger.warning(
                    f"Bot API Flood control (429)! Для чата {chat_id} Telegram требует ожидание {wait_time} сек. "
                    f"Ожидание и повторная отправка..."
                )
                _chat_cooldowns[chat_id] = asyncio.get_running_loop().time() + wait_time
                if wait_time <= 10 and attempt < 2:
                    await asyncio.sleep(wait_time + 0.2)
                    continue
                return None
            except TelegramForbiddenError:
                # Пользователь заблокировал бота или аккаунт удален — деактивируем подписку
                logger.debug(f"Пользователь {chat_id} заблокировал бота или деактивирован. Отключение подписки.")
                await db.deactivate_subscriber(chat_id)
                return None
            except TelegramBadRequest as e:
                err_text = str(e).lower()
                if "chat not found" in err_text or "user not found" in err_text:
                    logger.debug(f"Чат {chat_id} не найден. Отключение подписки.")
                    await db.deactivate_subscriber(chat_id)
                    return None
                logger.warning(f"Ошибка запроса TelegramBadRequest при отправке {chat_id}: {e}")
                return None
            except (TelegramNetworkError, Exception) as e:
                if attempt == 0 and ("disconnected" in str(e).lower() or isinstance(e, TelegramNetworkError)):
                    await asyncio.sleep(0.4)
                    continue
                logger.error(f"Ошибка при отправке сообщения в {chat_id}: {e}")
                return None
        return None


def lot_priority_key(lot: LotItem) -> int:
    """
    Приоритет отправки внутри пачки:
    0: motivated == True (сначала)
    1: liquid == True    (затем)
    2: stale == True     (затем)
    3: остальные         (в конце)
    """
    if getattr(lot, "motivated", False):
        return 0
    if getattr(lot, "liquid", False):
        return 1
    if getattr(lot, "stale", False):
        return 2
    return 3


async def outbox_broadcaster_worker(
    bot: Bot, db: Database, config: AppConfig
) -> None:
    """Фоновый воркер: моментально читает новые лоты из outbox и распределяет их подписчикам без искусственных задержек."""
    logger.info("Воркер рассылки outbox запущен (без искусственного ограничения скорости лотов в секунду).")

    round_robin_index = 0

    while True:
        try:
            pending_items = await db.get_pending_outbox(limit=20)
            if not pending_items:
                await asyncio.sleep(0.20)  # Сверхбыстрый опрос без паразитной нагрузки на SQLite
                continue

            active_subscribers = await db.get_active_subscribers()
            if not active_subscribers:
                for outbox_id, _ in pending_items:
                    await db.mark_outbox_done(outbox_id)
                continue

            # Сортировка пачки по приоритету (motivated -> liquid -> stale -> other)
            if len(pending_items) > 1:
                pending_items.sort(key=lambda item: lot_priority_key(item[1]))

            for outbox_id, lot in pending_items:
                await db.mark_outbox_processing(outbox_id)
                event_type = getattr(lot, "event_type", "market")
                # Строгий фильтр: профили без NFT-подарков (0 или None) или больше max_nft_gifts категорически не рассылаются в логах
                max_allowed = getattr(config, "max_nft_gifts", 5) or 5
                if lot.nft_gifts_count is None or lot.nft_gifts_count <= 0 or lot.nft_gifts_count > max_allowed:
                    await db.mark_outbox_done(outbox_id)
                    continue

                # Определяем подходящих подписчиков
                eligible = []
                for sub in active_subscribers:
                    if event_type == "upgrade":
                        if sub.mode in ("upgrades", "both") and sub.has_upgrades_access():
                            if sub.allows_upgrade_backdrop(getattr(lot, "backdrop", None)):
                                eligible.append(sub)
                    else:
                        if (
                            sub.mode in ("market", "both")
                            and sub.has_market_access()
                            and sub.min_price <= lot.price_stars <= sub.max_price
                        ):
                            eligible.append(sub)

                if not eligible:
                    await db.mark_outbox_done(outbox_id)
                    continue

                # Распределение логов: делятся между юзерами или отправляются всем
                if getattr(config, "distribute_leads_round_robin", False):
                    target_subs = [eligible[round_robin_index % len(eligible)]]
                    round_robin_index += 1
                else:
                    target_subs = eligible

                # Формируем карточку и первоначальную клавиатуру с кнопкой занятия лота
                message_text = lot.format_notification_html()

                builder = InlineKeyboardBuilder()
                builder.button(text="🔒 Занять лот", callback_data=f"claim:{outbox_id}")
                if lot.slug:
                    market_url = f"https://t.me/nft/{lot.slug}"
                    builder.button(text="🔗 Открыть на маркете", url=market_url)
                builder.adjust(1)
                reply_markup = builder.as_markup()

                # Мгновенная параллельная доставка всем подписчикам с гарантией изоляции сбоев
                async def _send_sub(sub: Subscriber) -> None:
                    try:
                        sent_msg = await send_notification_to_user(
                            bot=bot,
                            chat_id=sub.chat_id,
                            text=message_text,
                            reply_markup=reply_markup,
                            db=db,
                        )
                        if sent_msg and hasattr(sent_msg, "message_id"):
                            await db.record_outbox_dispatch(
                                outbox_id=outbox_id,
                                chat_id=sub.chat_id,
                                message_id=sent_msg.message_id,
                            )
                    except Exception as sub_err:
                        logger.error(f"Ошибка при доставке сообщения подписчику {sub.chat_id}: {sub_err}")

                await asyncio.gather(*[_send_sub(s) for s in target_subs], return_exceptions=True)

                logger.info(
                    f"✅ [БОТ] Обработка outbox #{outbox_id} ({lot.title} #{lot.num}, {event_type}) отправлена подписчикам"
                )
                await db.mark_outbox_done(outbox_id)

                # Искусственное ограничение скорости снято: лоты уходят моментально,
                # а безопасность Bot API гарантируется _bot_api_semaphore и _chat_cooldowns.

        except asyncio.CancelledError:
            logger.info("Воркер рассылки получил сигнал отмены.")
            raise
        except Exception as e:
            logger.error(f"Ошибка в цикле рассылки outbox: {e}", exc_info=True)
            await asyncio.sleep(1)


# ------------------ Фоновые воркеры платежей и подписок ------------------

async def payment_checker_worker(bot: Bot, db: Database, config: AppConfig) -> None:
    """Фоновый воркер периодической проверки платежей в блокчейне (каждые 25 секунд)."""
    logger.info("Воркер проверки платежей запущен.")
    while True:
        try:
            await verify_and_process_payments(bot, db, config)
        except asyncio.CancelledError:
            logger.info("Воркер проверки платежей останавливается...")
            break
        except Exception as e:
            logger.error(f"Ошибка в цикле проверки платежей: {e}", exc_info=True)

        try:
            await asyncio.sleep(25)
        except asyncio.CancelledError:
            break


async def subscription_reminder_worker(bot: Bot, db: Database) -> None:
    """Фоновый воркер напоминаний за 24 часа и уведомлений об окончании подписки."""
    logger.info("Воркер напоминаний о подписке запущен.")
    while True:
        try:
            # 1. Напоминания за 1 день до окончания
            reminders = await db.get_subscriptions_needing_reminder()
            for chat_id, sub_type, until_str in reminders:
                sub_name = "«Маркет»" if sub_type == "market" else "«Улучшения»"
                text = (
                    f"⏰ <b>Внимание! Срок подписки истекает</b>\n\n"
                    f"Ваша подписка на режим <b>{sub_name}</b> истекает менее чем через 24 часа "
                    f"(<code>{format_datetime_msk(until_str)}</code>).\n\n"
                    "Чтобы уведомления о лотах не прекращались, вы можете продлить её в меню /tariffs."
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="💳 Продлить подписку", callback_data="tariffs_back_types")
                try:
                    await bot.send_message(
                        chat_id=chat_id, text=text, reply_markup=builder.as_markup()
                    )
                    await db.mark_reminder_sent(chat_id, sub_type)
                except (TelegramForbiddenError, TelegramBadRequest) as err:
                    err_msg = str(err).lower()
                    if (
                        isinstance(err, TelegramForbiddenError)
                        or "chat not found" in err_msg
                        or "user not found" in err_msg
                        or "user is deactivated" in err_msg
                        or "bot was blocked" in err_msg
                    ):
                        logger.debug(
                            f"Пользователь {chat_id} недоступен ({err}). Напоминание помечено отправленным."
                        )
                        await db.mark_reminder_sent(chat_id, sub_type)
                        await db.deactivate_subscriber(chat_id)
                    else:
                        logger.warning(f"Не удалось отправить напоминание {chat_id}: {err}")
                except Exception as err:
                    logger.warning(f"Не удалось отправить напоминание {chat_id}: {err}")

            # 2. Уведомления об окончании подписки
            expired_list = await db.get_newly_expired_subscriptions()
            for chat_id, sub_type in expired_list:
                sub_name = "«Маркет»" if sub_type == "market" else "«Улучшения»"
                text = (
                    f"⌛ <b>Подписка завершилась</b>\n\n"
                    f"Срок действия вашей подписки на режим <b>{sub_name}</b> истёк.\n"
                    "Уведомления о новых лотах для этого режима приостановлены.\n\n"
                    "Вы можете возобновить мониторинг в любой момент через /tariffs."
                )
                builder = InlineKeyboardBuilder()
                builder.button(text="💳 Возобновить подписку", callback_data="tariffs_back_types")
                try:
                    await bot.send_message(
                        chat_id=chat_id, text=text, reply_markup=builder.as_markup()
                    )
                    await db.mark_expired_sent(chat_id, sub_type)
                except (TelegramForbiddenError, TelegramBadRequest) as err:
                    err_msg = str(err).lower()
                    if (
                        isinstance(err, TelegramForbiddenError)
                        or "chat not found" in err_msg
                        or "user not found" in err_msg
                        or "user is deactivated" in err_msg
                        or "bot was blocked" in err_msg
                    ):
                        logger.debug(
                            f"Пользователь {chat_id} недоступен ({err}). Уведомление об окончании помечено отправленным."
                        )
                        await db.mark_expired_sent(chat_id, sub_type)
                        await db.deactivate_subscriber(chat_id)
                    else:
                        logger.warning(
                            f"Не удалось отправить уведомление об окончании {chat_id}: {err}"
                        )
                except Exception as err:
                    logger.warning(
                        f"Не удалось отправить уведомление об окончании {chat_id}: {err}"
                    )

        except asyncio.CancelledError:
            logger.info("Воркер напоминаний о подписке останавливается...")
            break
        except Exception as e:
            logger.error(f"Ошибка в цикле напоминаний о подписке: {e}", exc_info=True)

        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break


# ------------------ Главная функция ------------------

async def main() -> None:
    config = load_config()

    if not config.bot_token:
        logger.error("BOT_TOKEN не указан в файле .env! Пожалуйста, укажите токен от @BotFather.")
        sys.exit(1)

    db = Database(config.database_path)
    await db.init_db()
    recovered_count = await db.reset_processing_outbox()
    if recovered_count > 0:
        logger.info(f"🔄 Восстановлено {recovered_count} лотов из незавершенной рассылки (processing -> pending).")

    bot = Bot(
        token=config.bot_token,
        session=ResilientAiohttpSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Внедряем зависимости в хендлеры
    dp["db"] = db
    dp["config"] = config

    # Логирование всех входящих сообщений и кнопок в консоль
    logging_mw = ConsoleLoggingMiddleware()
    dp.message.outer_middleware(logging_mw)
    dp.callback_query.outer_middleware(logging_mw)

    # Подключаем роутеры: сначала админ-панель, затем общий
    dp.include_router(admin_router)
    dp.include_router(router)

    # Получаем информацию о боте с повторами при временных сбоях сети
    bot_info = None
    for attempt in range(1, 11):
        try:
            bot_info = await bot.get_me()
            logger.info(f"Бот запущен: @{bot_info.username} (ID: {bot_info.id})")
            break
        except (TelegramNetworkError, Exception) as e:
            logger.warning(
                f"Попытка {attempt}/10: не удалось подключиться к api.telegram.org ({e}). "
                f"Повтор через 4 сек..."
            )
            await asyncio.sleep(4)

    if not bot_info:
        logger.error("Не удалось подключиться к серверам Telegram Bot API после 10 попыток.")
        await bot.session.close()
        return

    # Регистрация команд в меню Telegram: ТОЛЬКО /admin
    try:
        await bot.set_my_commands([
            types.BotCommand(command="admin", description="Панель администратора"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось установить команды в меню бота: {e}")

    # Запускаем фоновые процессы с супервайзером (автоматический перезапуск при любых сбоях)
    stop_event = asyncio.Event()

    async def broadcaster_supervisor() -> None:
        while not stop_event.is_set():
            try:
                await outbox_broadcaster_worker(bot, db, config)
            except asyncio.CancelledError:
                if stop_event.is_set():
                    break
            except Exception as e:
                logger.critical(f"🔥 outbox_broadcaster_worker упал: {e}! Перезапуск через 2 сек...", exc_info=True)
                await asyncio.sleep(2)

    broadcaster_task = asyncio.create_task(broadcaster_supervisor())
    payment_task = asyncio.create_task(
        payment_checker_worker(bot, db, config)
    )
    reminder_task = asyncio.create_task(
        subscription_reminder_worker(bot, db)
    )

    try:
        logger.info("Начало приема обновлений через Bot API polling...")
        while not stop_event.is_set():
            try:
                await dp.start_polling(
                    bot,
                    allowed_updates=["message", "callback_query"],
                    drop_pending_updates=True,
                )
                if stop_event.is_set():
                    break
                logger.warning("Polling неожиданно завершился без ошибки. Перезапуск через 2 сек...")
                await asyncio.sleep(2)
            except TelegramNetworkError as e:
                logger.warning(f"Временный сбой сети Telegram ({e}). Переподключение через 3 сек...")
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Неожиданная ошибка polling: {e}. Переподключение через 3 сек...", exc_info=True)
                await asyncio.sleep(3)
    finally:
        stop_event.set()
        broadcaster_task.cancel()
        payment_task.cancel()
        reminder_task.cancel()
        await bot.session.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot остановлен пользователем.")
