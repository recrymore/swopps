
import asyncio
import os
from getpass import getpass

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

TOKEN = os.getenv("ACCESS_BOT_TOKEN")
GROUP_ID = -1006051447834

dp = Dispatcher()


def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 Купить подписку",
                    callback_data="buy"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 Мой профиль",
                    callback_data="profile"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Пригласить друга",
                    callback_data="referral"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 Наш канал",
                    url="https://t.me/ZergowNft"
                ),
                InlineKeyboardButton(
                    text="💬 Поддержка",
                    url="https://t.me/swoplight"
                )
            ]
        ]
    )


def back_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="main"
                )
            ]
        ]
    )


MENU = (
    "💎 SWOP ACCESS\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "Добро пожаловать в SWOP Tracker.\n\n"
    "📡 Мониторинг NFT-маркета\n"
    "⚡ Уведомления о новых лотах\n"
    "🔒 Закрытая группа\n\n"
    "Выберите действие:"
)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        MENU,
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "main")
async def main_menu(call: CallbackQuery):
    await call.message.edit_text(
        MENU,
        reply_markup=main_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "buy")
async def buy(call: CallbackQuery):
    await call.message.edit_text(
        "💎 ТАРИФЫ SWOP\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ 24 часа\n"
        "🔥 7 дней\n"
        "👑 Навсегда\n\n"
        "Оплата пока не активирована.",
        reply_markup=back_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    await call.message.edit_text(
        f"👤 МОЙ ПРОФИЛЬ\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 ID: {call.from_user.id}\n"
        f"💎 Подписка: не активирована\n",
        reply_markup=back_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "referral")
async def referral(call: CallbackQuery):
    await call.message.edit_text(
        "👥 РЕФЕРАЛЬНАЯ СИСТЕМА\n\n"
        "Пока недоступна.",
        reply_markup=back_keyboard()
    )
    await call.answer()


async def main():
    token = TOKEN or getpass("ACCESS BOT TOKEN: ")

    async with Bot(token=token) as bot:
        info = await bot.get_me()
        print(f"SWOP ACCESS ONLINE: @{info.username}")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
