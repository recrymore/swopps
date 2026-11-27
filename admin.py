import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from aiogram import Router, Bot, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import AppConfig
from database import Database
from models import (
    Subscriber,
    Order,
    format_datetime_msk,
    is_sub_active,
    format_hours_ru,
    get_upgrade_filter_title,
)

logger = logging.getLogger("admin")
admin_router = Router()

# Временное состояние админа (поиск пользователя, рассылка)
_admin_states: Dict[int, Dict[str, Any]] = {}


async def is_admin_authorized(user: Optional[types.User], db: Database, config: AppConfig) -> bool:
    """
    Проверяет, авторизован ли пользователь как администратор.
    1. Ищет сохраненный admin_id в БД.
    2. Если сохранен — проверяет совпадение по ID.
    3. Если не сохранен — проверяет username из ADMIN_USERNAME (без @, регистронезависимо).
       При совпадении сохраняет numeric ID в БД на будущее!
    """
    if not user:
        return False

    # 1. Проверяем сохраненный admin_id в базе
    saved_admin_id = await db.get_setting("admin_id")
    if saved_admin_id and str(user.id) == saved_admin_id:
        return True

    # 2. Проверяем username из конфигурации
    configured_username = (config.admin_username or "").strip().lstrip("@").lower()
    if configured_username and user.username and user.username.lower() == configured_username:
        # Сохраняем числовой ID администратора при первом входе
        await db.set_setting("admin_id", str(user.id))
        logger.info(f"Числовой ID администратора @{user.username} ({user.id}) сохранен в базе данных.")
        return True

    # 3. Fallback: если OWNER_CHAT_ID совпадает
    if config.owner_chat_id and user.id == config.owner_chat_id:
        return True

    return False


# ------------------ Главное меню админ-панели ------------------

def get_admin_main_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Пользователи", callback_data="admin_users:1")
    builder.button(text="💳 Платежи и заявки", callback_data="admin_orders:1")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="📢 Рассылка", callback_data="admin_broadcast_prompt")
    builder.button(text="📌 Закрепить сообщение", callback_data="admin_pin_prompt")
    builder.button(text="❌ Закрыть панель", callback_data="admin_close")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


@admin_router.message(Command("admin"))
async def handle_admin_command(message: types.Message, db: Database, config: AppConfig) -> None:
    """Команда /admin — точка входа в админ-панель."""
    if not await is_admin_authorized(message.from_user, db, config):
        # Для остальных бот не реагирует, как будто команды нет
        return

    text = (
        "👑 <b>Панель управления администратора</b>\n\n"
        "Выберите необходимый раздел для управления ботом:"
    )
    await message.answer(text, reply_markup=get_admin_main_keyboard())


@admin_router.callback_query(F.data == "admin_main")
async def handle_admin_main_callback(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return
    text = (
        "👑 <b>Панель управления администратора</b>\n\n"
        "Выберите необходимый раздел для управления ботом:"
    )
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=get_admin_main_keyboard())
        except Exception:
            await callback.message.answer(text, reply_markup=get_admin_main_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "admin_close")
async def handle_admin_close(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.answer("Панель закрыта")


# ------------------ Раздел: Пользователи ------------------

@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_users:")))
async def handle_admin_users(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        page = 1

    page_size = 10
    users, total_users = await db.admin_get_users(page=page, page_size=page_size)
    total_pages = max(1, (total_users + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    builder = InlineKeyboardBuilder()
    now = datetime.now(timezone.utc)

    for u in users:
        uid = u["chat_id"]
        uname = f"@{u['username']}" if u["username"] else f"ID: {uid}"
        # Краткий статус подписок: отображаем значки только для действительно активных подписок
        m_mark = "📦" if is_sub_active(u.get("market_until"), now) else "▫️"
        u_mark = "💎" if is_sub_active(u.get("upgrades_until"), now) else "▫️"
        builder.button(
            text=f"{uname} | {m_mark}{u_mark}",
            callback_data=f"admin_uinfo:{uid}:{page}",
        )

    # Навигация
    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton(text="◀️", callback_data=f"admin_users:{page - 1}"))
    nav_buttons.append(
        types.InlineKeyboardButton(text=f"Стр {page}/{total_pages}", callback_data=f"admin_users:{page}")
    )
    if page < total_pages:
        nav_buttons.append(types.InlineKeyboardButton(text="▶️", callback_data=f"admin_users:{page + 1}"))

    builder.adjust(1)
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        types.InlineKeyboardButton(text="🔍 Поиск по ID/юзернейму", callback_data="admin_search_prompt"),
        types.InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_main"),
    )

    text = (
        f"👥 <b>Пользователи бота</b> (всего: <b>{total_users}</b>)\n\n"
        "<i>Обозначения: 📦 — есть подписка на Маркет, 💎 — на Улучшения.</i>\n"
        "Нажмите на пользователя для просмотра профиля и управления подпиской:"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_uinfo:")))
async def handle_admin_user_info(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    parts = callback.data.split(":")
    user_id = int(parts[1])
    back_page = parts[2] if len(parts) > 2 else "1"

    details = await db.admin_get_user_details(user_id)
    if not details:
        await callback.answer("Пользователь не найден в базе данных.", show_alert=True)
        return

    sub = await db.get_subscriber(user_id)
    m_status = sub.get_status_text("market") if sub else "❌ Не активна"
    u_status = sub.get_status_text("upgrades") if sub else "❌ Не активна"

    u_name = f"@{details['username']}" if details['username'] else "отсутствует"
    full_name = f"{details.get('first_name') or ''} {details.get('last_name') or ''}".strip() or "не указано"

    text = (
        f"👤 <b>Карточка пользователя <code>{user_id}</code></b>\n\n"
        f"• Username: <b>{u_name}</b>\n"
        f"• Имя: <b>{html.escape(full_name)}</b>\n"
        f"• Регистрация: <code>{format_datetime_msk(details.get('joined_at'), with_seconds=True)}</code>\n"
        f"• Пробный период: {'Использован' if details.get('has_used_trial') else 'Не использован'}\n"
        f"• Текущий режим: <b>{details.get('mode', 'market')}</b>\n"
        f"• Фильтр апгрейдов: <b>{get_upgrade_filter_title(details.get('upgrade_filter', 'all'))}</b>\n\n"
        f"📦 <b>Подписка Маркет:</b>\n{m_status}\n\n"
        f"💎 <b>Подписка Улучшения:</b>\n{u_status}\n\n"
        f"💰 <b>Всего оплачено:</b> <code>{details.get('total_ton', 0):g} TON</code> · <code>{details.get('total_usdt', 0):.2f} USDT</code>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Выдать подписку", callback_data=f"admin_grant_type:{user_id}:{back_page}")
    builder.button(text="➖ Забрать подписку", callback_data=f"admin_revoke_menu:{user_id}:{back_page}")
    builder.button(text="🔙 К списку пользователей", callback_data=f"admin_users:{back_page}")
    builder.adjust(2, 1)

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()


# ------------------ Выдача / Продление подписки админом ------------------

@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_grant_type:")))
async def handle_admin_grant_type(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    _, uid_str, back_page = callback.data.split(":")
    user_id = int(uid_str)

    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Маркет", callback_data=f"admin_grant_dur:{user_id}:market:{back_page}")
    builder.button(text="💎 Улучшения", callback_data=f"admin_grant_dur:{user_id}:upgrades:{back_page}")
    builder.button(text="🔔 Всё вместе", callback_data=f"admin_grant_dur:{user_id}:both:{back_page}")
    builder.button(text="🔙 Отмена", callback_data=f"admin_uinfo:{user_id}:{back_page}")
    builder.adjust(1)

    text = f"➕ <b>Выдача подписки пользователю <code>{user_id}</code></b>\n\nВыберите тип подписки:"
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_grant_dur:")))
async def handle_admin_grant_duration(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    # Очищаем состояние ожидания ввода, если оно было
    _admin_states.pop(callback.from_user.id, None)

    _, uid_str, sub_type, back_page = callback.data.split(":")
    user_id = int(uid_str)

    sub_titles = {"market": "Маркет", "upgrades": "Улучшения", "both": "Всё вместе"}

    builder = InlineKeyboardBuilder()
    builder.button(text="6 часов", callback_data=f"admin_grant_do:{user_id}:{sub_type}:6h:{back_page}")
    builder.button(text="1 день", callback_data=f"admin_grant_do:{user_id}:{sub_type}:1d:{back_page}")
    builder.button(text="3 дня", callback_data=f"admin_grant_do:{user_id}:{sub_type}:3d:{back_page}")
    builder.button(text="7 дней", callback_data=f"admin_grant_do:{user_id}:{sub_type}:7d:{back_page}")
    builder.button(text="1 месяц (30 дней)", callback_data=f"admin_grant_do:{user_id}:{sub_type}:30d:{back_page}")
    builder.button(text="♾️ Навсегда", callback_data=f"admin_grant_do:{user_id}:{sub_type}:forever:{back_page}")
    builder.button(text="⏱ Ввести любое кол-во часов", callback_data=f"admin_grant_custom:{user_id}:{sub_type}:{back_page}")
    builder.button(text="🔙 Назад", callback_data=f"admin_grant_type:{user_id}:{back_page}")
    builder.adjust(3, 2, 1, 1, 1)

    text = (
        f"➕ <b>Выдача подписки</b>\n\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Тип: <b>{sub_titles.get(sub_type, sub_type)}</b>\n\n"
        "Выберите срок действия или введите любое количество часов:"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_grant_custom:")))
async def handle_admin_grant_custom(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    _, uid_str, sub_type, back_page = callback.data.split(":")
    user_id = int(uid_str)

    _admin_states[callback.from_user.id] = {
        "action": "grant_custom_hours",
        "target_user_id": user_id,
        "sub_type": sub_type,
        "back_page": back_page,
    }

    sub_titles = {"market": "Маркет", "upgrades": "Улучшения", "both": "Всё вместе"}

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Отмена", callback_data=f"admin_grant_dur:{user_id}:{sub_type}:{back_page}")

    text = (
        f"⏱ <b>Выдача любого количества часов</b>\n\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Тип: <b>{sub_titles.get(sub_type, sub_type)}</b>\n\n"
        "Отправьте в чат желаемое количество часов (целое положительное число, например <code>1</code>, <code>12</code>, <code>48</code>, <code>720</code>):"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_grant_do:")))
async def handle_admin_grant_execute(
    callback: types.CallbackQuery, db: Database, config: AppConfig, bot: Bot
) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    _, uid_str, sub_type, duration, back_page = callback.data.split(":")
    user_id = int(uid_str)

    success, msg = await db.admin_grant_subscription(user_id, sub_type, duration)
    if success:
        sub_titles = {"market": "Маркет", "upgrades": "Улучшения", "both": "Всё вместе"}
        dur_titles = {
            "6h": "6 часов",
            "1d": "1 день",
            "3d": "3 дня",
            "7d": "7 дней",
            "30d": "1 месяц (30 дней)",
            "forever": "Навсегда",
        }
        dur_display = dur_titles.get(duration)
        if not dur_display:
            if duration.endswith("h") and duration[:-1].isdigit():
                dur_display = format_hours_ru(int(duration[:-1]))
            elif duration.endswith("d") and duration[:-1].isdigit():
                dur_display = f"{duration[:-1]} дн."
            else:
                dur_display = duration

        await callback.answer(f"✅ Подписка успешно выдана!", show_alert=True)

        # Оповещаем пользователя в Telegram
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"🎁 <b>Администратор начислил вам подписку!</b>\n\n"
                    f"• Тип: <b>{sub_titles.get(sub_type, sub_type)}</b>\n"
                    f"• Срок: <b>{dur_display}</b>\n\n"
                    f"Статус подписок обновлен. Вы можете переключать режим в /mode."
                ),
            )
        except Exception:
            pass

        # Возвращаем в профиль пользователя
        await handle_admin_user_info(callback, db, config)
    else:
        await callback.answer(f"Ошибка: {msg}", show_alert=True)


# ------------------ Отзыв подписки админом ------------------

@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_revoke_menu:")))
async def handle_admin_revoke_menu(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    _, uid_str, back_page = callback.data.split(":")
    user_id = int(uid_str)

    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Снять Маркет", callback_data=f"admin_revoke_do:{user_id}:market:{back_page}")
    builder.button(text="💎 Снять Улучшения", callback_data=f"admin_revoke_do:{user_id}:upgrades:{back_page}")
    builder.button(text="❌ Снять ВСЁ", callback_data=f"admin_revoke_do:{user_id}:both:{back_page}")
    builder.button(text="🔙 Отмена", callback_data=f"admin_uinfo:{user_id}:{back_page}")
    builder.adjust(1)

    text = f"➖ <b>Отзыв подписки у пользователя <code>{user_id}</code></b>\n\nВыберите, какую подписку забрать:"
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_revoke_do:")))
async def handle_admin_revoke_execute(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    _, uid_str, sub_type, back_page = callback.data.split(":")
    user_id = int(uid_str)

    await db.admin_revoke_subscription(user_id, sub_type)
    await callback.answer("✅ Подписка успешно отозвана", show_alert=True)
    await handle_admin_user_info(callback, db, config)


# ------------------ Поиск пользователя ------------------

@admin_router.callback_query(F.data == "admin_search_prompt")
async def handle_admin_search_prompt(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    _admin_states[callback.from_user.id] = {"action": "search"}

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Отмена", callback_data="admin_users:1")

    text = (
        "🔍 <b>Поиск пользователя</b>\n\n"
        "Отправьте в чат <b>числовой ID</b> или <b>@username</b> пользователя:"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


# ------------------ Раздел: Заявки и платежи ------------------

@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_orders:")))
async def handle_admin_orders(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        page = 1

    page_size = 10
    orders, total_orders = await db.admin_get_orders(page=page, page_size=page_size)
    total_pages = max(1, (total_orders + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    builder = InlineKeyboardBuilder()

    status_icons = {
        "paid": "✅",
        "pending": "⏳",
        "expired": "⌛",
        "cancelled": "❌",
    }

    for item in orders:
        ord_obj = item["order"]
        st_icon = status_icons.get(ord_obj.status, "▫️")
        builder.button(
            text=f"#{ord_obj.id} | {st_icon} {item['human_amount']} {ord_obj.currency} | {ord_obj.sub_type}",
            callback_data=f"admin_oinfo:{ord_obj.id}:{page}",
        )

    # Навигация
    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton(text="◀️", callback_data=f"admin_orders:{page - 1}"))
    nav_buttons.append(
        types.InlineKeyboardButton(text=f"Стр {page}/{total_pages}", callback_data=f"admin_orders:{page}")
    )
    if page < total_pages:
        nav_buttons.append(types.InlineKeyboardButton(text="▶️", callback_data=f"admin_orders:{page + 1}"))

    builder.adjust(1)
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        types.InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_main"),
    )

    text = (
        f"💳 <b>История заявок и платежей</b> (всего: <b>{total_orders}</b>)\n\n"
        "<i>Статусы: ✅ — оплачено, ⏳ — ожидание, ⌛ — истекло, ❌ — отменено.</i>\n"
        "Нажмите на заявку для просмотра деталей:"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(lambda c: bool(c.data and c.data.startswith("admin_oinfo:")))
async def handle_admin_order_info(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    parts = callback.data.split(":")
    order_id = int(parts[1])
    back_page = parts[2] if len(parts) > 2 else "1"

    ord_obj = await db.get_order(order_id)
    if not ord_obj:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    status_names = {
        "paid": "✅ Оплачена",
        "pending": "⏳ Ожидает оплаты",
        "expired": "⌛ Истекла",
        "cancelled": "❌ Отменена",
    }

    text = (
        f"🧾 <b>Заявка на оплату #{ord_obj.id}</b>\n\n"
        f"• Пользователь: <code>{ord_obj.user_id}</code>\n"
        f"• Тариф: <b>{ord_obj.sub_title}</b> ({ord_obj.duration_title})\n"
        f"• Сумма: <code>{ord_obj.human_amount} {ord_obj.currency}</code>\n"
        f"• Курс TON/USDT: <code>{ord_obj.rate or '—'}</code>\n"
        f"• Комментарий: <code>{ord_obj.comment}</code>\n"
        f"• Статус: <b>{status_names.get(ord_obj.status, ord_obj.status)}</b>\n"
        f"• Создана: <code>{format_datetime_msk(ord_obj.created_at, with_seconds=True)}</code>\n"
        f"• Истекает: <code>{format_datetime_msk(ord_obj.expires_at, with_seconds=True)}</code>\n"
        f"• Оплачена: <code>{format_datetime_msk(ord_obj.paid_at, with_seconds=True) if ord_obj.paid_at else '—'}</code>\n"
        f"• TX Hash: <code>{ord_obj.tx_hash or '—'}</code>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Профиль пользователя", callback_data=f"admin_uinfo:{ord_obj.user_id}:1")
    builder.button(text="🔙 К списку заявок", callback_data=f"admin_orders:{back_page}")
    builder.adjust(1)

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()


# ------------------ Раздел: Статистика ------------------

@admin_router.callback_query(F.data == "admin_stats")
async def handle_admin_stats(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    stats = await db.admin_get_stats()

    text = (
        "📊 <b>Сводная статистика сервиса</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"📦 Активных подписок Маркет: <b>{stats['active_market']}</b>\n"
        f"💎 Активных подписок Улучшения: <b>{stats['active_upgrades']}</b>\n"
        f"🔔 Активных «Всё вместе»: <b>{stats['active_both']}</b>\n\n"
        f"🧾 Всего заявок создано: <b>{stats['total_orders']}</b>\n"
        f"✅ Успешно оплаченных: <b>{stats['paid_orders']}</b>\n\n"
        f"💰 Доход в TON: <b>{stats['revenue_ton']:g} TON</b>\n"
        f"💵 Доход в USDT: <b>{stats['revenue_usdt']:.2f} USDT</b>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="admin_stats")
    builder.button(text="🔙 Главное меню", callback_data="admin_main")
    builder.adjust(1)

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()


# ------------------ Раздел: Рассылка ------------------

@admin_router.callback_query(F.data == "admin_broadcast_prompt")
async def handle_admin_broadcast_prompt(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    _admin_states[callback.from_user.id] = {"action": "broadcast_wait_text"}

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Отмена", callback_data="admin_main")

    text = (
        "📢 <b>Рассылка сообщения всем пользователям</b>\n\n"
        "Отправьте сообщение (текст, форматирование или фото с подписью), которое вы хотите разослать.\n"
        "Перед отправкой бот покажет превью для подтверждения."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(F.data == "admin_broadcast_confirm")
async def handle_admin_broadcast_confirm(
    callback: types.CallbackQuery, db: Database, config: AppConfig, bot: Bot
) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    state = _admin_states.get(callback.from_user.id)
    if not state or state.get("action") != "broadcast_confirm":
        await callback.answer("Сессия рассылки устарела", show_alert=True)
        return

    broadcast_text = state.get("text", "")
    _admin_states.pop(callback.from_user.id, None)

    await callback.answer("🚀 Рассылка запущена в фоновом режиме!", show_alert=False)
    if callback.message:
        await callback.message.edit_text("⏳ <b>Рассылка выполняется...</b>\nПожалуйста, подождите.")

    # Фоновая отправка
    async def _run_broadcast():
        subscribers = await db.get_active_subscribers()
        success = 0
        failed = 0
        for s in subscribers:
            try:
                await bot.send_message(chat_id=s.chat_id, text=broadcast_text)
                success += 1
                await asyncio.sleep(0.04)  # ~25 сообщений в секунду
            except Exception:
                failed += 1

        try:
            await bot.send_message(
                chat_id=callback.from_user.id,
                text=(
                    f"✅ <b>Рассылка завершена!</b>\n\n"
                    f"• Успешно доставлено: <b>{success}</b>\n"
                    f"• Не удалось доставить: <b>{failed}</b>"
                ),
            )
        except Exception:
            pass

    asyncio.create_task(_run_broadcast())


@admin_router.callback_query(F.data == "admin_broadcast_cancel")
async def handle_admin_broadcast_cancel(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    if not await is_admin_authorized(callback.from_user, db, config):
        return
    _admin_states.pop(callback.from_user.id, None)
    await callback.answer("Рассылка отменена")
    await handle_admin_main_callback(callback, db, config)


# ------------------ Раздел: Закреплённое сообщение всем ------------------

@admin_router.callback_query(F.data == "admin_pin_prompt")
async def handle_admin_pin_prompt(callback: types.CallbackQuery, db: Database, config: AppConfig) -> None:
    """Точка входа для отправки закреплённого сообщения всем пользователям."""
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    _admin_states[callback.from_user.id] = {"action": "pin_broadcast_wait_text"}

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑️ Открепить все сообщения у всех", callback_data="admin_unpin_all_prompt")
    builder.button(text="🔙 Главное меню", callback_data="admin_main")
    builder.adjust(1)

    text = (
        "📌 <b>Отправка закреплённого сообщения всем пользователям</b>\n\n"
        "Отправьте текст сообщения. Бот разошлёт его <b>всем зарегистрированным пользователям</b> "
        "и <b>автоматически закрепит</b> его вверху их диалога в Telegram.\n\n"
        "<i>Поддерживаются форматирование HTML, эмодзи и ссылки. Перед отправкой будет показан предпросмотр.</i>"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(F.data == "admin_pin_confirm")
async def handle_admin_pin_confirm(
    callback: types.CallbackQuery, db: Database, config: AppConfig, bot: Bot
) -> None:
    """Подтверждение и запуск фоновой рассылки с автоматическим закреплением."""
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    state = _admin_states.get(callback.from_user.id)
    if not state or state.get("action") not in ("pin_broadcast_confirm", "broadcast_confirm"):
        await callback.answer("Сессия рассылки устарела", show_alert=True)
        return

    broadcast_text = state.get("text", "")
    _admin_states.pop(callback.from_user.id, None)

    await callback.answer("🚀 Рассылка с закреплением запущена!", show_alert=False)
    if callback.message:
        await callback.message.edit_text(
            "⏳ <b>Рассылка и закрепление сообщений выполняются...</b>\nПожалуйста, подождите."
        )

    async def _run_pin_broadcast():
        subscribers = await db.get_all_subscribers()
        total = len(subscribers)
        success = 0
        pinned = 0
        failed = 0
        for s in subscribers:
            try:
                sent = await bot.send_message(chat_id=s.chat_id, text=broadcast_text)
                success += 1
                try:
                    await bot.pin_chat_message(
                        chat_id=s.chat_id,
                        message_id=sent.message_id,
                        disable_notification=False,
                    )
                    pinned += 1
                except Exception as pin_err:
                    logger.warning(f"Не удалось закрепить сообщение у {s.chat_id}: {pin_err}")
                await asyncio.sleep(0.04)  # ~25 сообщений в секунду
            except Exception:
                failed += 1

        try:
            await bot.send_message(
                chat_id=callback.from_user.id,
                text=(
                    f"✅ <b>Рассылка с закреплением завершена!</b>\n\n"
                    f"• Всего пользователей в базе: <b>{total}</b>\n"
                    f"• Успешно доставлено: <b>{success}</b>\n"
                    f"• Закреплено в чатах: <b>{pinned}</b>\n"
                    f"• Не удалось доставить: <b>{failed}</b>"
                ),
            )
        except Exception:
            pass

    asyncio.create_task(_run_pin_broadcast())


@admin_router.callback_query(F.data == "admin_unpin_all_prompt")
async def handle_admin_unpin_all_prompt(
    callback: types.CallbackQuery, db: Database, config: AppConfig
) -> None:
    """Подтверждение открепления всех сообщений."""
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="⚠️ Да, открепить у всех", callback_data="admin_unpin_all_confirm")
    builder.button(text="🔙 Отмена", callback_data="admin_pin_prompt")
    builder.adjust(1)

    text = (
        "⚠️ <b>Подтверждение открепления</b>\n\n"
        "Вы действительно хотите открепить все закреплённые сообщения у всех пользователей бота?\n\n"
        "Это очистит закрепления в чатах со всеми подписчиками."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(F.data == "admin_unpin_all_confirm")
async def handle_admin_unpin_all_confirm(
    callback: types.CallbackQuery, db: Database, config: AppConfig, bot: Bot
) -> None:
    """Запуск фонового открепления всех сообщений у пользователей."""
    if not await is_admin_authorized(callback.from_user, db, config):
        return

    await callback.answer("⏳ Запущен процесс открепления...", show_alert=False)
    if callback.message:
        await callback.message.edit_text(
            "⏳ <b>Открепление сообщений у всех пользователей...</b>\nПожалуйста, подождите."
        )

    async def _run_unpin_all():
        subscribers = await db.get_all_subscribers()
        total = len(subscribers)
        success = 0
        failed = 0
        for s in subscribers:
            try:
                await bot.unpin_all_chat_messages(chat_id=s.chat_id)
                success += 1
                await asyncio.sleep(0.04)
            except Exception:
                failed += 1

        try:
            await bot.send_message(
                chat_id=callback.from_user.id,
                text=(
                    f"✅ <b>Открепление сообщений завершено!</b>\n\n"
                    f"• Всего пользователей в базе: <b>{total}</b>\n"
                    f"• Успешно откреплено: <b>{success}</b>\n"
                    f"• Не удалось / чат недоступен: <b>{failed}</b>"
                ),
            )
        except Exception:
            pass

    asyncio.create_task(_run_unpin_all())


# ------------------ Обработка текстовых сообщений админа (поиск и рассылка) ------------------

def _is_admin_input_active(message: types.Message) -> bool:
    """Фильтр: срабатывает только если админ сейчас в процессе ввода текста (поиск/рассылка)."""
    if not message.from_user:
        return False
    # Команды (начинающиеся с /) пропускаем в штатные хэндлеры команд
    if message.text and message.text.startswith("/"):
        return False
    return message.from_user.id in _admin_states


@admin_router.message(_is_admin_input_active)
async def handle_admin_text_inputs(message: types.Message, db: Database, config: AppConfig) -> None:
    """Обработчик входящего текста для активных состояний админа (поиск или рассылка)."""
    user = message.from_user
    if not user or not await is_admin_authorized(user, db, config):
        return

    state = _admin_states.get(user.id)
    if not state:
        return

    action = state.get("action")

    # Обработка ввода любого количества часов
    if action == "grant_custom_hours":
        raw_text = (message.text or "").strip()
        target_uid = state["target_user_id"]
        sub_type = state["sub_type"]
        back_page = state.get("back_page", "1")

        if not raw_text.isdigit() or int(raw_text) <= 0:
            builder = InlineKeyboardBuilder()
            builder.button(text="🔙 Отмена", callback_data=f"admin_grant_dur:{target_uid}:{sub_type}:{back_page}")
            await message.answer(
                "❌ Неверный формат. Пожалуйста, отправьте положительное целое число часов (например: <code>1</code>, <code>12</code>, <code>48</code>):",
                reply_markup=builder.as_markup(),
            )
            return

        hours = int(raw_text)
        _admin_states.pop(user.id, None)

        duration = f"{hours}h"
        success, msg = await db.admin_grant_subscription(target_uid, sub_type, duration)
        if success:
            sub_titles = {"market": "Маркет", "upgrades": "Улучшения", "both": "Всё вместе"}
            dur_title = format_hours_ru(hours)

            await message.answer(
                f"✅ Пользователю <code>{target_uid}</code> успешно начислена подписка!\n\n"
                f"• Тип: <b>{sub_titles.get(sub_type, sub_type)}</b>\n"
                f"• Срок: <b>{dur_title}</b> ({hours} ч.)\n"
                f"• Результат: <i>{msg}</i>"
            )

            # Оповещаем пользователя в Telegram
            bot = message.bot
            if bot:
                try:
                    await bot.send_message(
                        chat_id=target_uid,
                        text=(
                            f"🎁 <b>Администратор начислил вам подписку!</b>\n\n"
                            f"• Тип: <b>{sub_titles.get(sub_type, sub_type)}</b>\n"
                            f"• Срок: <b>{dur_title}</b> ({hours} ч.)\n\n"
                            f"Статус подписок обновлен. Вы можете переключать режим в /mode."
                        ),
                    )
                except Exception:
                    pass

            # Показываем обновленную карточку пользователя
            details = await db.admin_get_user_details(target_uid)
            if details:
                sub = await db.get_subscriber(target_uid)
                m_status = sub.get_status_text("market") if sub else "❌ Не активна"
                u_status = sub.get_status_text("upgrades") if sub else "❌ Не активна"
                u_name = f"@{details['username']}" if details['username'] else "отсутствует"
                full_name = f"{details.get('first_name') or ''} {details.get('last_name') or ''}".strip() or "не указано"

                card_text = (
                    f"👤 <b>Карточка пользователя <code>{target_uid}</code></b>\n\n"
                    f"• Username: <b>{u_name}</b>\n"
                    f"• Имя: <b>{html.escape(full_name)}</b>\n"
                    f"• Регистрация: <code>{format_datetime_msk(details.get('joined_at'), with_seconds=True)}</code>\n"
                    f"• Пробный период: {'Использован' if details.get('has_used_trial') else 'Не использован'}\n"
                    f"• Текущий режим: <b>{details.get('mode', 'market')}</b>\n"
                    f"• Фильтр апгрейдов: <b>{get_upgrade_filter_title(details.get('upgrade_filter', 'all'))}</b>\n\n"
                    f"📦 <b>Подписка Маркет:</b>\n{m_status}\n\n"
                    f"💎 <b>Подписка Улучшения:</b>\n{u_status}\n\n"
                    f"💰 <b>Всего оплачено:</b> <code>{details.get('total_ton', 0):g} TON</code> · <code>{details.get('total_usdt', 0):.2f} USDT</code>"
                )
                card_builder = InlineKeyboardBuilder()
                card_builder.button(text="➕ Выдать подписку", callback_data=f"admin_grant_type:{target_uid}:{back_page}")
                card_builder.button(text="➖ Забрать подписку", callback_data=f"admin_revoke_menu:{target_uid}:{back_page}")
                card_builder.button(text="🔙 К списку пользователей", callback_data=f"admin_users:{back_page}")
                card_builder.adjust(2, 1)

                await message.answer(card_text, reply_markup=card_builder.as_markup())
        else:
            await message.answer(f"❌ Ошибка при начислении подписки: {msg}")
        return

    # Обработка поискового запроса
    if action == "search":
        _admin_states.pop(user.id, None)
        query = (message.text or "").strip()
        found = await db.admin_search_user(query)
        if not found:
            builder = InlineKeyboardBuilder()
            builder.button(text="🔍 Попробовать снова", callback_data="admin_search_prompt")
            builder.button(text="👥 К списку пользователей", callback_data="admin_users:1")
            builder.adjust(1)
            await message.answer(
                f"❌ Пользователь по запросу <code>{html.escape(query)}</code> не найден.",
                reply_markup=builder.as_markup(),
            )
            return

        # Показываем найденного пользователя
        uid = found["chat_id"]
        sub = await db.get_subscriber(uid)
        m_status = sub.get_status_text("market") if sub else "❌ Не активна"
        u_status = sub.get_status_text("upgrades") if sub else "❌ Не активна"
        u_name = f"@{found['username']}" if found['username'] else "отсутствует"
        full_name = f"{found.get('first_name') or ''} {found.get('last_name') or ''}".strip() or "не указано"

        card_text = (
            f"👤 <b>Карточка пользователя <code>{uid}</code></b>\n\n"
            f"• Username: <b>{u_name}</b>\n"
            f"• Имя: <b>{html.escape(full_name)}</b>\n"
            f"• Регистрация: <code>{format_datetime_msk(found.get('joined_at'), with_seconds=True)}</code>\n"
            f"• Текущий режим: <b>{found.get('mode', 'market')}</b>\n"
            f"• Фильтр апгрейдов: <b>{get_upgrade_filter_title(found.get('upgrade_filter', 'all'))}</b>\n\n"
            f"📦 <b>Подписка Маркет:</b>\n{m_status}\n\n"
            f"💎 <b>Подписка Улучшения:</b>\n{u_status}"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Выдать подписку", callback_data=f"admin_grant_type:{uid}:1")
        builder.button(text="➖ Забрать подписку", callback_data=f"admin_revoke_menu:{uid}:1")
        builder.button(text="👥 К списку пользователей", callback_data="admin_users:1")
        builder.adjust(2, 1)

        await message.answer(card_text, reply_markup=builder.as_markup())
        return

    # Обработка текста для закреплённой рассылки
    if action == "pin_broadcast_wait_text":
        b_text = message.text or message.caption or ""
        if not b_text.strip():
            await message.answer("❌ Сообщение не может быть пустым. Отправьте текст для закреплённого сообщения:")
            return

        state["action"] = "pin_broadcast_confirm"
        state["text"] = b_text

        preview = (
            "📌 <b>Предпросмотр закреплённого сообщения:</b>\n"
            "────────────────────\n"
            f"{b_text}\n"
            "────────────────────\n\n"
            "⚠️ Сообщение будет разослано всем зарегистрированным пользователям и <b>закреплено вверху чата</b>.\n\n"
            "Подтвердите отправку:"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="📌 Подтвердить и закрепить у всех", callback_data="admin_pin_confirm")
        builder.button(text="❌ Отмена", callback_data="admin_broadcast_cancel")
        builder.adjust(1)

        await message.answer(preview, reply_markup=builder.as_markup())
        return

    # Обработка текста обычной рассылки
    if action == "broadcast_wait_text":
        b_text = message.text or message.caption or ""
        if not b_text.strip():
            await message.answer("❌ Сообщение не может быть пустым. Отправьте текст для рассылки:")
            return

        state["action"] = "broadcast_confirm"
        state["text"] = b_text

        preview = (
            "📢 <b>Предпросмотр сообщения для рассылки:</b>\n"
            "────────────────────\n"
            f"{b_text}\n"
            "────────────────────\n\n"
            "Выберите способ отправки подписчикам:"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="🚀 Обычная рассылка", callback_data="admin_broadcast_confirm")
        builder.button(text="📌 Отправить и закрепить у всех", callback_data="admin_pin_confirm")
        builder.button(text="❌ Отмена", callback_data="admin_broadcast_cancel")
        builder.adjust(1)

        await message.answer(preview, reply_markup=builder.as_markup())
        return
