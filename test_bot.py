import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram.filters import CommandObject
from bot import (
    handle_start,
    handle_stop,
    handle_price,
    handle_stats,
    handle_help,
    handle_my_claimed,
    handle_claim_lot,
    handle_menu_main_callback,
    handle_menu_tariffs_callback,
    handle_menu_profile_callback,
    handle_menu_price_callback,
    handle_price_set_callback,
    handle_price_adj_callback,
    handle_menu_toggle_active_callback,
    handle_text_message,
    handle_menu_template_callback,
    handle_template_set_callback,
    handle_template_reset_callback,
    handle_template_cancel_callback,
    handle_custom_template_input,
    CustomTemplateState,
)
from config import AppConfig
from database import Database
from models import (
    LotItem,
    is_black_or_onyx_backdrop,
    Subscriber,
    format_datetime_msk,
    format_time_remaining,
)
from userbot import is_trader_or_scam


class TestBotCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "test_bot_commands.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = Database(self.test_db_path)
        await self.db.init_db()
        self.config = AppConfig(owner_chat_id=1234567)

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    async def test_start_command(self):
        msg = MagicMock()
        msg.chat.id = 999111
        msg.answer = AsyncMock()

        await handle_start(msg, self.db)

        sub = await self.db.get_subscriber(999111)
        self.assertIsNotNone(sub)
        self.assertTrue(sub.active)
        self.assertEqual(sub.min_price, 0)
        self.assertEqual(msg.answer.call_count, 2)
        self.assertIn("Добро пожаловать", msg.answer.call_args_list[0][0][0])
        self.assertIn("Появился чат для ваших профитов", msg.answer.call_args_list[1][0][0])

    async def test_stop_command(self):
        msg = MagicMock()
        msg.chat.id = 999111
        msg.answer = AsyncMock()

        await handle_start(msg, self.db)
        await handle_stop(msg, self.db)

        sub = await self.db.get_subscriber(999111)
        self.assertFalse(sub.active)
        self.assertIn("Уведомления отключены", msg.answer.call_args[0][0])

    async def test_price_no_args(self):
        msg = MagicMock()
        msg.chat.id = 999111
        msg.answer = AsyncMock()
        cmd = CommandObject(prefix="/", command="price", args=None)

        await self.db.update_subscriber_price(999111, 200, 600)
        await handle_price(msg, cmd, self.db)

        msg.answer.assert_called_once()
        self.assertIn("От <b>200</b> до <b>600 ★</b>", msg.answer.call_args[0][0])

    async def test_price_valid_args(self):
        msg = MagicMock()
        msg.chat.id = 999111
        msg.answer = AsyncMock()
        cmd = CommandObject(prefix="/", command="price", args="100 800")

        await handle_price(msg, cmd, self.db)

        sub = await self.db.get_subscriber(999111)
        self.assertEqual(sub.min_price, 100)
        self.assertEqual(sub.max_price, 800)
        self.assertTrue(sub.active)
        self.assertIn("успешно обновлён", msg.answer.call_args[0][0])

    async def test_price_invalid_args_format(self):
        msg = MagicMock()
        msg.chat.id = 999111
        msg.answer = AsyncMock()

        # Только один аргумент
        cmd1 = CommandObject(prefix="/", command="price", args="100")
        await handle_price(msg, cmd1, self.db)
        self.assertIn("Неверный формат", msg.answer.call_args[0][0])

        # Нечисловые аргументы
        cmd2 = CommandObject(prefix="/", command="price", args="abc def")
        await handle_price(msg, cmd2, self.db)
        self.assertIn("целыми числами", msg.answer.call_args[0][0])

        # max <= min
        cmd3 = CommandObject(prefix="/", command="price", args="500 200")
        await handle_price(msg, cmd3, self.db)
        self.assertIn("Ошибка валидации", msg.answer.call_args[0][0])

        # min < 0
        cmd4 = CommandObject(prefix="/", command="price", args="-10 200")
        await handle_price(msg, cmd4, self.db)
        self.assertIn("Ошибка валидации цен", msg.answer.call_args[0][0])

    async def test_stats_command_owner_vs_stranger(self):
        msg_stranger = MagicMock()
        msg_stranger.from_user.id = 7777777
        msg_stranger.answer = AsyncMock()

        await handle_stats(msg_stranger, self.db, self.config)
        # Не владелец — ответ не вызывается
        msg_stranger.answer.assert_not_called()

        msg_owner = MagicMock()
        msg_owner.from_user.id = 1234567
        msg_owner.answer = AsyncMock()

        await handle_stats(msg_owner, self.db, self.config)
        # Владелец — вызывается со статистикой
        msg_owner.answer.assert_called_once()
        self.assertIn("Статистика сервиса", msg_owner.answer.call_args[0][0])

    async def test_help_command(self):
        msg = MagicMock()
        msg.answer = AsyncMock()

        await handle_help(msg)
        msg.answer.assert_called_once()
        self.assertIn("Справка", msg.answer.call_args[0][0])

    async def test_claim_lot_flow(self):
        # 1. Создаем лот в outbox
        lot = LotItem(
            lot_key="101:202:500",
            gift_id=101,
            raw_id=1,
            title="Instant Ramen",
            num=42,
            slug="InstantRamen-42",
            price_stars=500,
            seller_id=202,
            username="ramen_seller",
            first_name="Иван",
            last_name="Иванов",
        )
        outbox_id = await self.db.add_to_outbox(lot)

        # 2. Имитируем отправку двум подписчикам
        await self.db.record_outbox_dispatch(outbox_id, chat_id=111, message_id=5001)
        await self.db.record_outbox_dispatch(outbox_id, chat_id=222, message_id=5002)

        bot_mock = MagicMock()
        bot_mock.edit_message_reply_markup = AsyncMock()
        bot_mock.edit_message_text = AsyncMock()

        # 3. Первый подписчик (chat_id=111, @user_one) нажимает "Занять лот"
        cb1 = MagicMock()
        cb1.data = f"claim:{outbox_id}"
        cb1.from_user.id = 111
        cb1.from_user.username = "user_one"
        cb1.from_user.first_name = "User One"
        cb1.from_user.full_name = "User One"
        cb1.answer = AsyncMock()
        cb1.message.edit_reply_markup = AsyncMock()
        cb1.message.edit_text = AsyncMock()
        cb1.message.chat.id = 111

        await handle_claim_lot(cb1, self.db, self.config, bot_mock)

        # Проверяем, что первый победил
        cb1.answer.assert_called_once()
        self.assertIn("успешно заняли", cb1.answer.call_args[0][0])
        cb1.message.edit_reply_markup.assert_called_once()

        # Ждем выполнения фоновой задачи обновления клавиатур и текста
        await asyncio.sleep(0.05)
        # Проверяем, что для второго подписчика (chat_id=222) вызван edit_message_text с пометкой занято и скрытыми данными
        bot_mock.edit_message_text.assert_called_once()
        call_kwargs = bot_mock.edit_message_text.call_args[1]
        self.assertEqual(call_kwargs["chat_id"], 222)
        self.assertEqual(call_kwargs["message_id"], 5002)
        hidden_msg = call_kwargs["text"]
        self.assertIn("Лот занят", hidden_msg)
        self.assertIn("Контакт продавца скрыт", hidden_msg)
        self.assertNotIn("ramen_seller", hidden_msg)
        self.assertNotIn("t.me/nft", hidden_msg)
        self.assertNotIn(f"#{lot.num}", hidden_msg)

        # 4. Второй подписчик пытается нажать "Занять лот" на уже занятом лоте
        cb2 = MagicMock()
        cb2.data = f"claim:{outbox_id}"
        cb2.from_user.id = 222
        cb2.from_user.username = "user_two"
        cb2.from_user.first_name = "User Two"
        cb2.from_user.full_name = "User Two"
        cb2.answer = AsyncMock()
        cb2.message.edit_reply_markup = AsyncMock()
        cb2.message.edit_text = AsyncMock()
        cb2.message.chat.id = 222

        await handle_claim_lot(cb2, self.db, self.config, bot_mock)

        # Второй получает предупреждение, что лот уже занят @user_one
        cb2.answer.assert_called_once()
        self.assertIn("уже занял", cb2.answer.call_args[0][0])
        self.assertIn("@user_one", cb2.answer.call_args[0][0])
        cb2.message.edit_text.assert_called_once()
        cb2_hidden_msg = cb2.message.edit_text.call_args[1]["text"]
        self.assertIn("Контакт продавца скрыт", cb2_hidden_msg)
        self.assertNotIn("ramen_seller", cb2_hidden_msg)
        self.assertNotIn("t.me/nft", cb2_hidden_msg)

    async def test_my_claimed_command(self):
        msg = MagicMock()
        msg.from_user.id = 111
        msg.answer = AsyncMock()

        # Пока ничего не занято
        await handle_my_claimed(msg, self.db, self.config)
        self.assertIn("нет занятых лотов", msg.answer.call_args[0][0])

        # Занимаем лот
        lot = LotItem(
            lot_key="101:202:500",
            gift_id=101,
            raw_id=1,
            title="Instant Ramen",
            num=42,
            slug="InstantRamen-42",
            price_stars=500,
            seller_id=202,
            username="ramen_seller",
        )
        outbox_id = await self.db.add_to_outbox(lot)
        await self.db.claim_lot(outbox_id, lot.lot_key, 111, "user_one", "User One")

        # Вызываем /my снова
        msg.answer.reset_mock()
        await handle_my_claimed(msg, self.db, self.config)
        self.assertEqual(msg.answer.call_count, 2)
        # Заголовок
        self.assertIn("Ваши последние занятые лоты", msg.answer.call_args_list[0][0][0])
        # Карточка лота
        self.assertIn("Instant Ramen #42", msg.answer.call_args_list[1][0][0])

    def test_is_trader_or_scam(self):
        # Трейдеры и ресейлеры должны отсеиваться
        self.assertTrue(is_trader_or_scam("Алекс", "Отзывы", "Канал @repflsit"))
        self.assertTrue(is_trader_or_scam("Trade TON", "VIP", "скупаю подарки"))
        self.assertTrue(is_trader_or_scam("Гарант", "Шмекер", "t.me/shmkr_rep"))
        self.assertTrue(is_trader_or_scam("P2P Exchange", "", "ресейл звезд"))
        self.assertTrue(is_trader_or_scam("Crypto Shop", "", "дешевые звезды"))

        # Обычные пользователи должны проходить
        self.assertFalse(is_trader_or_scam("Иван", "Иванов", "студент, люблю музыку"))
        self.assertFalse(is_trader_or_scam("Мария", "", ""))
        self.assertFalse(is_trader_or_scam("Даниил", "Григорьев", "живу в Москве"))

    async def test_inline_main_menu(self):
        cb = MagicMock()
        cb.message.chat.id = 999111
        cb.from_user.id = 999111
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        await self.db.upsert_subscriber(999111)
        await handle_menu_main_callback(cb, self.db)

        cb.message.edit_text.assert_called_once()
        self.assertIn("Добро пожаловать", cb.message.edit_text.call_args[0][0])
        cb.answer.assert_called_once()

    async def test_inline_price_presets_and_adjust(self):
        chat_id = 999222
        await self.db.upsert_subscriber(chat_id)

        # 1. Preset 0 - 500
        cb_preset = MagicMock()
        cb_preset.data = "price_set:0:500"
        cb_preset.message.chat.id = chat_id
        cb_preset.message.edit_text = AsyncMock()
        cb_preset.answer = AsyncMock()

        await handle_price_set_callback(cb_preset, self.db)
        sub = await self.db.get_subscriber(chat_id)
        self.assertEqual(sub.min_price, 0)
        self.assertEqual(sub.max_price, 500)
        cb_preset.answer.assert_called_once()
        self.assertIn("0 – 500 ★", cb_preset.answer.call_args[0][0])

        # 2. Adjust min +100
        cb_adj = MagicMock()
        cb_adj.data = "price_adj:min:100"
        cb_adj.message.chat.id = chat_id
        cb_adj.message.edit_text = AsyncMock()
        cb_adj.answer = AsyncMock()

        await handle_price_adj_callback(cb_adj, self.db)
        sub2 = await self.db.get_subscriber(chat_id)
        self.assertEqual(sub2.min_price, 100)
        self.assertEqual(sub2.max_price, 500)
        cb_adj.answer.assert_called_once()

    async def test_inline_toggle_active(self):
        chat_id = 999333
        await self.db.upsert_subscriber(chat_id)

        cb = MagicMock()
        cb.message.chat.id = chat_id
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        # По умолчанию активен -> переключаем в неактивен
        await handle_menu_toggle_active_callback(cb, self.db)
        sub = await self.db.get_subscriber(chat_id)
        self.assertFalse(sub.active)
        self.assertIn("приостановлены", cb.answer.call_args[0][0])

        # Переключаем обратно в активен
        await handle_menu_toggle_active_callback(cb, self.db)
        sub2 = await self.db.get_subscriber(chat_id)
        self.assertTrue(sub2.active)
        self.assertIn("включены", cb.answer.call_args[0][0])

    async def test_text_message_handler(self):
        chat_id = 999444
        await self.db.upsert_subscriber(chat_id)

        # 1. Отправка двух чисел ("150 750")
        msg_price = MagicMock()
        msg_price.chat.id = chat_id
        msg_price.text = "150 750"
        msg_price.answer = AsyncMock()

        await handle_text_message(msg_price, self.db, self.config)
        sub = await self.db.get_subscriber(chat_id)
        self.assertEqual(sub.min_price, 150)
        self.assertEqual(sub.max_price, 750)
        msg_price.answer.assert_called_once()
        self.assertIn("успешно обновлён", msg_price.answer.call_args[0][0])

        # 2. Отправка произвольного текста ("Привет")
        msg_text = MagicMock()
        msg_text.chat.id = chat_id
        msg_text.text = "Привет"
        msg_text.answer = AsyncMock()

        await handle_text_message(msg_text, self.db, self.config)
        msg_text.answer.assert_called_once()
        self.assertIn("Добро пожаловать", msg_text.answer.call_args[0][0])

    def test_black_or_onyx_backdrop_filtering(self):
        # Должны приниматься
        self.assertTrue(is_black_or_onyx_backdrop("Black"))
        self.assertTrue(is_black_or_onyx_backdrop("black"))
        self.assertTrue(is_black_or_onyx_backdrop("Onyx Black"))
        self.assertTrue(is_black_or_onyx_backdrop("Оникс Black"))
        self.assertTrue(is_black_or_onyx_backdrop("Onyx"))
        self.assertTrue(is_black_or_onyx_backdrop("Оникс"))
        self.assertTrue(is_black_or_onyx_backdrop("Dark Black Shine"))

        # Должны отсекаться
        self.assertFalse(is_black_or_onyx_backdrop("Pink"))
        self.assertFalse(is_black_or_onyx_backdrop("Blue"))
        self.assertFalse(is_black_or_onyx_backdrop("Gold"))
        self.assertFalse(is_black_or_onyx_backdrop("White"))
        self.assertFalse(is_black_or_onyx_backdrop(None))
        self.assertFalse(is_black_or_onyx_backdrop(""))

    async def test_custom_template_flow(self):
        chat_id = 999888
        await self.db.upsert_subscriber(chat_id)

        state_mock = MagicMock()
        state_mock.clear = AsyncMock()
        state_mock.set_state = AsyncMock()

        # 1. Открытие экрана шаблона
        cb_menu = MagicMock()
        cb_menu.from_user.id = chat_id
        cb_menu.message.chat.id = chat_id
        cb_menu.message.edit_text = AsyncMock()
        cb_menu.answer = AsyncMock()

        await handle_menu_template_callback(cb_menu, self.db, self.config, state_mock)
        cb_menu.message.edit_text.assert_called_once()
        self.assertIn("Настройка шаблона", cb_menu.message.edit_text.call_args[0][0])
        self.assertIn("Стандартный", cb_menu.message.edit_text.call_args[0][0])

        # 2. Нажатие кнопки «Изменить шаблон»
        cb_set = MagicMock()
        cb_set.message.edit_text = AsyncMock()
        cb_set.answer = AsyncMock()

        await handle_template_set_callback(cb_set, state_mock)
        state_mock.set_state.assert_called_with(CustomTemplateState.waiting_for_template)
        self.assertIn("Введите ваш новый шаблон", cb_set.message.edit_text.call_args[0][0])

        # 3. Ввод шаблона с синтаксической ошибкой (неизвестная переменная {bad_var})
        msg_bad = MagicMock()
        msg_bad.chat.id = chat_id
        msg_bad.text = "Привет {bad_var} куплю"
        msg_bad.answer = AsyncMock()

        await handle_custom_template_input(msg_bad, self.db, state_mock)
        msg_bad.answer.assert_called_once()
        self.assertIn("Неизвестная переменная", msg_bad.answer.call_args[0][0])

        # 4. Ввод корректного шаблона
        msg_good = MagicMock()
        msg_good.chat.id = chat_id
        msg_good.text = "Привет! За сколько отдашь {title} #{num}? Заберу за {offer} звезд."
        msg_good.answer = AsyncMock()

        await handle_custom_template_input(msg_good, self.db, state_mock)
        msg_good.answer.assert_called_once()
        self.assertIn("успешно сохранён", msg_good.answer.call_args[0][0])
        state_mock.clear.assert_called()

        # Проверяем, что в базе сохранился индивидуальный шаблон
        saved_tpl = await self.db.get_subscriber_template(chat_id)
        self.assertEqual(saved_tpl, msg_good.text)

        # 5. Проверяем, что в меню отображается статус "Индивидуальный"
        cb_check = MagicMock()
        cb_check.from_user.id = chat_id
        cb_check.message.chat.id = chat_id
        cb_check.message.edit_text = AsyncMock()
        cb_check.answer = AsyncMock()

        await handle_menu_template_callback(cb_check, self.db, self.config, state_mock)
        self.assertIn("Индивидуальный", cb_check.message.edit_text.call_args[0][0])

        # 6. Сброс шаблона
        cb_reset = MagicMock()
        cb_reset.from_user.id = chat_id
        cb_reset.message.chat.id = chat_id
        cb_reset.message.edit_text = AsyncMock()
        cb_reset.answer = AsyncMock()

        await handle_template_reset_callback(cb_reset, self.db, self.config, state_mock)
        cb_reset.answer.assert_called_once()
        self.assertIn("сброшен", cb_reset.answer.call_args[0][0])

        # Проверяем, что в базе теперь None
        saved_tpl_after = await self.db.get_subscriber_template(chat_id)
        self.assertIsNone(saved_tpl_after)

    async def test_claimed_lot_uses_custom_template(self):
        chat_id = 777666
        custom_text = "Здорово! Возьму {title} #{num} прямо сейчас за {offer} звёзд."
        await self.db.upsert_subscriber(chat_id)
        await self.db.update_subscriber_template(chat_id, custom_text)

        lot = LotItem(
            lot_key="303:404:800",
            gift_id=303,
            raw_id=3,
            title="Chill Flame",
            num=99,
            slug="ChillFlame-99",
            price_stars=800,
            seller_id=404,
            username="seller_pro",
            first_name="Игорь",
            last_name="",
        )
        outbox_id = await self.db.add_to_outbox(lot)
        await self.db.record_outbox_dispatch(outbox_id, chat_id=chat_id, message_id=7001)

        bot_mock = MagicMock()
        bot_mock.edit_message_reply_markup = AsyncMock()
        bot_mock.edit_message_text = AsyncMock()

        cb = MagicMock()
        cb.data = f"claim:{outbox_id}"
        cb.from_user.id = chat_id
        cb.from_user.username = "buyer_pro"
        cb.from_user.first_name = "Buyer"
        cb.from_user.full_name = "Buyer Pro"
        cb.answer = AsyncMock()
        cb.message.edit_reply_markup = AsyncMock()
        cb.message.chat.id = chat_id

        await handle_claim_lot(cb, self.db, self.config, bot_mock)

        # Проверяем кнопку "Написать продавцу"
        cb.message.edit_reply_markup.assert_called_once()
        markup = cb.message.edit_reply_markup.call_args[1]["reply_markup"]
        offer_btn = markup.inline_keyboard[0][0]
        self.assertEqual(offer_btn.text, "✉️ Написать продавцу")
        import urllib.parse
        decoded_url = urllib.parse.unquote(offer_btn.url)
        # Проверяем, что в ссылке использовался наш custom_text с подставленными данными!
        self.assertIn("Здорово! Возьму Chill Flame #99", decoded_url)
        self.assertIn("750 звёзд", decoded_url)  # 800 - 50 = 750

    async def test_format_claimed_notification_html_hides_serial_and_contact(self):
        lot_market = LotItem(
            lot_key="101:202:500",
            gift_id=101,
            raw_id=1,
            title="Golden Star",
            num=77777,
            slug="GoldenStar-77777",
            price_stars=150,
            price_ton="0.5",
            model="Rare Gold",
            backdrop="Black",
            pattern="Stars",
            seller_id=987654321,
            username="seller_account",
            event_type="market",
        )
        html_m = lot_market.format_claimed_notification_html("@lucky_buyer")
        self.assertIn("Лот занят (@lucky_buyer)", html_m)
        self.assertIn("<b>Golden Star</b>", html_m)
        self.assertNotIn("#77777", html_m)
        self.assertNotIn("77777", html_m)
        self.assertNotIn("GoldenStar-77777", html_m)
        self.assertNotIn("seller_account", html_m)
        self.assertNotIn("987654321", html_m)

        lot_upgrade = LotItem(
            lot_key="102:203:600",
            gift_id=102,
            raw_id=2,
            title="Onyx Dragon",
            num=8888,
            slug="OnyxDragon-8888",
            price_stars=0,
            model="Dark Wings",
            backdrop="Оникс Black",
            pattern="Fire",
            seller_id=123456789,
            username="upgrade_seller",
            event_type="upgrade",
        )
        html_u = lot_upgrade.format_claimed_notification_html("@lucky_buyer")
        self.assertIn("Апгрейд занят (@lucky_buyer)", html_u)
        self.assertIn("<b>Onyx Dragon</b>", html_u)
        self.assertNotIn("#8888", html_u)
        self.assertNotIn("8888", html_u)
        self.assertNotIn("OnyxDragon-8888", html_u)
        self.assertNotIn("upgrade_seller", html_u)
        self.assertNotIn("123456789", html_u)

    def test_format_datetime_msk_and_time_remaining(self):
        # 13:12:46 UTC -> 16:12:46 МСК
        dt_str = "2026-10-15 13:12:46"
        res = format_datetime_msk(dt_str, with_seconds=True)
        self.assertEqual(res, "15.10.2026 16:12:46 МСК")

        res_short = format_datetime_msk(dt_str, with_seconds=False)
        self.assertEqual(res_short, "15.10.2026 16:12 МСК")

        # Time remaining
        self.assertEqual(format_time_remaining(7200), "2 ч.")
        self.assertEqual(format_time_remaining(7260), "2 ч. 1 мин.")
        self.assertEqual(format_time_remaining(86400 * 2 + 3600 * 5), "2 д. 5 ч.")
        self.assertEqual(format_time_remaining(45), "< 1 мин.")
        self.assertEqual(format_time_remaining(120), "2 мин.")
        self.assertEqual(format_time_remaining(0), "срок истек")

    def test_subscriber_get_status_text_msk_and_remaining(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 10, 15, 20, 0, 0, tzinfo=timezone.utc)
        # Active subscription: expires at 22:30 UTC = 01:30 MSK next day (remaining: 2h 30m)
        future_str = (now + timedelta(hours=2, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        sub = Subscriber(chat_id=123, market_until=future_str, upgrades_until=None)

        status_m = sub.get_status_text("market", now=now)
        self.assertIn("✅ До", status_m)
        self.assertIn("МСК", status_m)
        self.assertIn("осталось 2 ч. 30 мин.", status_m)

        status_u = sub.get_status_text("upgrades", now=now)
        self.assertEqual(status_u, "❌ Не активна")

        # Expired subscription: expired 1 hour ago
        past_str = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        sub_expired = Subscriber(chat_id=123, market_until=past_str)
        status_exp = sub_expired.get_status_text("market", now=now)
        self.assertIn("❌ Истекла", status_exp)
        self.assertIn("МСК", status_exp)


if __name__ == "__main__":
    unittest.main()
