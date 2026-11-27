import asyncio
import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from telethon.tl import types

# Ensure workspace is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import AppConfig, TelegramAccountConfig, load_config
from database import Database
from models import LotItem, Subscriber
from userbot import parse_attributes, parse_prices, has_cyrillic, UserbotMonitor, AccountWorker


class TestGiftMonitor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "test_run_database.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = Database(self.test_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    # ---------------- 1. База данных и подписчики ----------------

    async def test_subscriber_flow(self):
        # Регистрация /start
        sub = await self.db.upsert_subscriber(12345, default_min=0, default_max=1000000)
        self.assertEqual(sub.chat_id, 12345)
        self.assertTrue(sub.active)
        self.assertEqual(sub.min_price, 0)
        self.assertEqual(sub.max_price, 1000000)

        # Обновление цен /price 150 700
        await self.db.update_subscriber_price(12345, 150, 700)
        sub_updated = await self.db.get_subscriber(12345)
        self.assertEqual(sub_updated.min_price, 150)
        self.assertEqual(sub_updated.max_price, 700)
        self.assertTrue(sub_updated.active)

        # Проверка соответствия цены
        # 100 ★ — не подходит (меньше min_price 150)
        matched_100 = await self.db.get_active_subscribers_for_price(100)
        self.assertNotIn(12345, matched_100)

        # 300 ★ — подходит (в диапазоне [150, 700])
        matched_300 = await self.db.get_active_subscribers_for_price(300)
        self.assertIn(12345, matched_300)

        # 800 ★ — не подходит (больше max_price 700)
        matched_800 = await self.db.get_active_subscribers_for_price(800)
        self.assertNotIn(12345, matched_800)

        # Отключение /stop
        await self.db.deactivate_subscriber(12345)
        sub_stopped = await self.db.get_subscriber(12345)
        self.assertFalse(sub_stopped.active)

        # После /stop не должен получать рассылку даже при подходящей цене
        matched_after_stop = await self.db.get_active_subscribers_for_price(300)
        self.assertNotIn(12345, matched_after_stop)

        # Повторный /start восстанавливает active=1
        sub_restarted = await self.db.upsert_subscriber(12345)
        self.assertTrue(sub_restarted.active)
        self.assertEqual(sub_restarted.min_price, 150)  # Сохраняет настройки пользователя

    # ---------------- 2. Очередь Outbox и дедупликация ----------------

    async def test_outbox_and_deduplication(self):
        self.assertTrue(await self.db.is_seen_lots_empty())

        # Первый запуск — пакетное запоминание
        initial_keys = ["101:555:300", "102:555:450"]
        await self.db.mark_lots_seen_bulk(initial_keys)

        self.assertFalse(await self.db.is_seen_lots_empty())
        self.assertTrue(await self.db.has_seen_lot("101:555:300"))
        self.assertFalse(await self.db.has_seen_lot("103:555:500"))

        # Добавление лота в outbox
        lot = LotItem(
            lot_key="103:555:500",
            gift_id=99,
            raw_id=103,
            title="Durov's Cap",
            num=42,
            slug="durovscap-42",
            price_stars=500,
            price_ton="5.25",
            model="Legendary Cap",
            model_rarity="Legendary",
            backdrop="Gold",
            pattern="Telegram",
            seller_id=555,
            username="seller_durov",
            first_name="Павел",
            last_name="Дуров",
            level=1,
            send_paid_messages_stars=10,
            premium=True,
            detected_at="11.10.2026 12:00:00",
        )

        outbox_id = await self.db.add_to_outbox(lot)
        pending = await self.db.get_pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][0], outbox_id)
        self.assertEqual(pending[0][1].title, "Durov's Cap")

        # Обработка
        await self.db.mark_outbox_processing(outbox_id)
        await self.db.mark_outbox_done(outbox_id)

        pending_empty = await self.db.get_pending_outbox()
        self.assertEqual(len(pending_empty), 0)

    # ---------------- 3. Кэш продавцов (TTL) ----------------

    async def test_seller_cache(self):
        seller_id = 999888
        data = {"level": 2, "about": "Привет!", "username": "pavel"}

        await self.db.save_cached_seller(seller_id, data)

        # В пределах 3600 секунд кэш валиден
        cached = await self.db.get_cached_seller(seller_id, max_age_seconds=3600)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["level"], 2)
        self.assertEqual(cached["about"], "Привет!")

        # С истекшим TTL кэш не возвращается
        await asyncio.sleep(0.02)
        expired = await self.db.get_cached_seller(seller_id, max_age_seconds=0.001)
        self.assertIsNone(expired)

    # ---------------- 4. Парсинг цен и атрибутов ----------------

    def test_price_parsing(self):
        # Только Stars
        amt_stars = [types.StarsAmount(amount=500, nanos=0)]
        stars, ton = parse_prices(amt_stars)
        self.assertEqual(stars, 500)
        self.assertIsNone(ton)

        # Stars + TON (в нанотонах 2_500_000_000 = 2.50 TON)
        amt_both = [
            types.StarsAmount(amount=750, nanos=0),
            types.StarsTonAmount(amount=2500000000),
        ]
        stars, ton = parse_prices(amt_both)
        self.assertEqual(stars, 750)
        self.assertEqual(ton, "2.50")

    def test_attribute_parsing(self):
        # Тестирование модели с редкостью в промилле (50 permille = 5%)
        model_attr = types.StarGiftAttributeModel(
            name="Diamond",
            document=None,
            rarity=types.StarGiftAttributeRarity(permille=50),
        )
        backdrop_attr = types.StarGiftAttributeBackdrop(
            name="Midnight Blue",
            backdrop_id=1,
            center_color=0,
            edge_color=0,
            pattern_color=0,
            text_color=0,
            rarity=None,
        )
        pattern_attr = types.StarGiftAttributePattern(
            name="Constellation",
            document=None,
            rarity=None,
        )

        model, rarity, backdrop, pattern = parse_attributes(
            [model_attr, backdrop_attr, pattern_attr]
        )
        self.assertEqual(model, "Diamond")
        self.assertEqual(rarity, "5%")
        self.assertEqual(backdrop, "Midnight Blue")
        self.assertEqual(pattern, "Constellation")

        # Тестирование модели со словесной редкостью (Epic, Rare, Legendary, Uncommon)
        model_epic = types.StarGiftAttributeModel(
            name="Dragon",
            document=None,
            rarity=types.StarGiftAttributeRarityEpic(),
        )
        model, rarity, _, _ = parse_attributes([model_epic])
        self.assertEqual(model, "Dragon")
        self.assertEqual(rarity, "Epic")

    # ---------------- 5. СНГ-эвристика (кириллица) ----------------

    def test_cyrillic_heuristic(self):
        self.assertTrue(has_cyrillic("Иван"))
        self.assertTrue(has_cyrillic("Alex Иванов"))
        self.assertTrue(has_cyrillic("Продаю редкие подарки недорого"))
        self.assertFalse(has_cyrillic("John Doe"))
        self.assertFalse(has_cyrillic("CryptoWhale123"))
        self.assertFalse(has_cyrillic(None))
        self.assertFalse(has_cyrillic(""))

    # ---------------- 6. Форматирование уведомлений и HTML ----------------

    def test_html_formatting_and_escaping(self):
        lot = LotItem(
            lot_key="key-1",
            gift_id=1,
            raw_id=10,
            title="<Special> Gift & More",
            num=7,
            slug="special-gift-7",
            price_stars=300,
            price_ton="1.25",
            model="Laser <Eyes>",
            model_rarity="Rare",
            backdrop="Deep & Dark",
            pattern="Dots & Lines",
            seller_id=111222,
            username="best_trader",
            first_name="Иван",
            last_name="Тест",
            level=2,
            send_paid_messages_stars=0,
            premium=False,
            detected_at="11.10.2026 12:30:00",
        )

        html_text = lot.format_notification_html()

        # Проверка наличия ключевых элементов
        self.assertIn("🎉 <b>Новый листинг</b>", html_text)
        self.assertIn('<a href="https://t.me/nft/special-gift-7">&lt;Special&gt; Gift &amp; More #7</a>', html_text)
        self.assertIn("💰 <b>300 ★</b> · 1.25 TON", html_text)
        self.assertIn("🧩 Модель: Laser &lt;Eyes&gt; (Rare)", html_text)
        self.assertIn("🎨 Фон: Deep &amp; Dark · Узор: Dots &amp; Lines", html_text)
        self.assertIn('<a href="https://t.me/best_trader">@best_trader</a> · <code>111222</code>', html_text)
        self.assertIn("📊 Уровень: 2", html_text)
        self.assertIn("💬 Сообщения: бесплатно", html_text)
        self.assertIn("⭐ без Premium", html_text)
        self.assertIn("🕓 11.10.2026 12:30:00", html_text)

    # ---------------- 7. Ссылка кнопки торга ----------------

    def test_offer_button_url(self):
        lot = LotItem(
            lot_key="key-1",
            gift_id=1,
            raw_id=10,
            title="Plush Pepe",
            num=123,
            slug="pepe-123",
            price_stars=150,
            price_ton=None,
            model="Pepe",
            model_rarity="0.5%",
            backdrop="Green",
            pattern="Frogs",
            seller_id=111222,
            username="pepe_seller",
            first_name="Алексей",
            last_name="",
            level=1,
            send_paid_messages_stars=5,
            premium=True,
            detected_at="11.10.2026 12:30:00",
        )

        template = "Привет! Продаёшь {title} #{num}? Готов купить, могу сразу. Скинешь {discount} ★? Тогда беру за {offer} ★"
        url = lot.get_offer_url(discount=50, template=template)

        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("https://t.me/pepe_seller?text="))
        # Проверяем, что offer = 150 - 50 = 100
        import urllib.parse
        decoded_text = urllib.parse.unquote(url.replace("https://t.me/pepe_seller?text=", ""))
        self.assertIn("Plush Pepe #123", decoded_text)
        self.assertIn("Скинешь 50 ★? Тогда беру за 100 ★", decoded_text)

        # Если нет username — URL не должен генерироваться
        lot.username = None
        self.assertIsNone(lot.get_offer_url(discount=50, template=template))

    # ---------------- 8. Фильтр и отображение NFT-подарков ----------------

    def test_nft_gifts_filtering_and_formatting(self):
        # 1. Лот с 2 NFT (подходит под лимит max_nft_gifts = 4)
        lot_ok = LotItem(
            lot_key="key-ok",
            gift_id=1,
            raw_id=10,
            title="Golden Star",
            num=10,
            slug="star-10",
            price_stars=200,
            price_ton=None,
            model="Star",
            model_rarity="Common",
            backdrop="Blue",
            pattern="Lines",
            seller_id=111,
            username="seller1",
            first_name="Иван",
            last_name="",
            level=1,
            send_paid_messages_stars=0,
            premium=False,
            detected_at="11.10.2026 12:30:00",
            nft_gifts_count=2,
        )

        html_text = lot_ok.format_notification_html()
        self.assertIn("🎁 NFT у продавца: 2\n", html_text)

        # 2. Проверка логики фильтрации max_nft_gifts
        max_limit = 4
        # Продавец с 2 NFT проходит
        self.assertFalse(lot_ok.nft_gifts_count > max_limit)
        # Продавец с 15 NFT (реселлер / кит) отсеивается
        seller_whale_count = 15
        self.assertTrue(seller_whale_count > max_limit)

    def test_lot_item_backward_compatibility(self):
        # Проверка десериализации старых записей outbox (где нет ключа nft_gifts_count)
        old_data = {
            "lot_key": "old-key",
            "gift_id": 1,
            "raw_id": 1,
            "title": "Old Gift",
            "num": 5,
            "slug": "old-5",
            "price_stars": 100,
            "price_ton": None,
            "model": "Old",
            "model_rarity": "Common",
            "backdrop": "Red",
            "pattern": "Dots",
            "seller_id": 999,
            "username": "old_user",
            "first_name": "Старый",
            "last_name": "",
            "level": 0,
            "send_paid_messages_stars": None,
            "premium": False,
            "detected_at": "11.10.2026 10:00:00",
        }
        lot = LotItem.from_dict(old_data)
        self.assertIsNone(lot.nft_gifts_count)
        self.assertFalse(lot.motivated)
        self.assertFalse(lot.liquid)
        self.assertFalse(lot.stale)
        self.assertNotIn("NFT у продавца", lot.format_notification_html())

    # ---------------- 9. Приоритизация и фильтры (listing_history, motivated, liquid, stale) ----------------

    async def test_listing_history_motivated_and_stale(self):
        listing_key = "100:555"
        # Первое обнаружение по 500 ★
        motivated, stale = await self.db.process_listing_history(
            listing_key=listing_key,
            price_stars=500,
            stale_minutes_threshold=30,
        )
        self.assertFalse(motivated)
        self.assertFalse(stale)

        # Снижение цены до 450 ★ -> motivated = True
        motivated, stale = await self.db.process_listing_history(
            listing_key=listing_key,
            price_stars=450,
            stale_minutes_threshold=30,
        )
        self.assertTrue(motivated)
        self.assertFalse(stale)

        # Тест залежавшегося лота (stale):
        stale_key = "200:777"
        # Первое появление по 600 ★
        await self.db.process_listing_history(
            listing_key=stale_key,
            price_stars=600,
            stale_minutes_threshold=30,
        )
        # Искусственно сдвигаем first_seen_at на 35 минут назад
        import aiosqlite, time
        async with aiosqlite.connect(self.test_db_path) as db:
            past_time = time.time() - (35 * 60)
            await db.execute(
                "UPDATE listing_history SET first_seen_at = ?, last_price = 600 WHERE listing_key = ?",
                (past_time, stale_key),
            )
            await db.commit()

        # При той же цене 600 ★ через 35 минут -> stale = True, motivated = False
        motivated, stale = await self.db.process_listing_history(
            listing_key=stale_key,
            price_stars=600,
            stale_minutes_threshold=30,
        )
        self.assertFalse(motivated)
        self.assertTrue(stale)

    def test_lot_priority_sorting(self):
        from bot import lot_priority_key

        def make_lot(title: str, motivated=False, liquid=False, stale=False):
            return LotItem(
                lot_key=f"k_{title}",
                gift_id=1,
                raw_id=1,
                title=title,
                num=1,
                slug="s",
                price_stars=100,
                price_ton=None,
                model="M",
                model_rarity="R",
                backdrop="B",
                pattern="P",
                seller_id=1,
                username=None,
                first_name="",
                last_name="",
                level=1,
                send_paid_messages_stars=0,
                premium=False,
                detected_at="now",
                motivated=motivated,
                liquid=liquid,
                stale=stale,
            )

        lot_other = make_lot("Other")
        lot_stale = make_lot("Stale", stale=True)
        lot_liquid = make_lot("Liquid", liquid=True)
        lot_motivated = make_lot("Motivated", motivated=True)

        batch = [lot_other, lot_stale, lot_liquid, lot_motivated]
        batch.sort(key=lot_priority_key)

        expected_order = ["Motivated", "Liquid", "Stale", "Other"]
        actual_order = [l.title for l in batch]
        self.assertEqual(actual_order, expected_order)

    def test_free_messages_and_level_filter(self):
        # 1. Максимальный уровень продавца: 1
        max_level = 1
        self.assertTrue(1 <= max_level)
        self.assertTrue(0 <= max_level)
        self.assertFalse(2 <= max_level)

        # 2. Сообщения обязательно бесплатные
        # send_paid_messages_stars is None или 0 -> разрешено
        # send_paid_messages_stars > 0 -> запрещено
        def is_free_messages(paid_stars):
            return paid_stars is None or paid_stars == 0

        self.assertTrue(is_free_messages(None))
        self.assertTrue(is_free_messages(0))
        self.assertFalse(is_free_messages(5))
        self.assertFalse(is_free_messages(100))

    # ---------------- 10. Мониторинг апгрейдов и режим /mode ----------------

    async def test_upgrade_tracking_db(self):
        # 1. tracked_gift_models
        await self.db.upsert_tracked_gift_model("ChillFlame", 101, active=True)
        await self.db.upsert_tracked_gift_model("PepeCrown", 102, active=True)
        models = await self.db.get_active_tracked_models()
        self.assertEqual(len(models), 2)
        short_names = [m["short_name"] for m in models]
        self.assertIn("ChillFlame", short_names)
        self.assertIn("PepeCrown", short_names)

        # Деактивация модели
        await self.db.upsert_tracked_gift_model("ChillFlame", 101, active=False)
        active_models = await self.db.get_active_tracked_models()
        self.assertEqual(len(active_models), 1)
        self.assertEqual(active_models[0]["short_name"], "PepeCrown")

        # 2. upgrade_tracking (last_known_num)
        self.assertIsNone(await self.db.get_upgrade_last_num("PepeCrown"))
        await self.db.set_upgrade_last_num("PepeCrown", 55)
        self.assertEqual(await self.db.get_upgrade_last_num("PepeCrown"), 55)

        # Обновление номера
        await self.db.set_upgrade_last_num("PepeCrown", 56)
        self.assertEqual(await self.db.get_upgrade_last_num("PepeCrown"), 56)

    async def test_subscriber_mode(self):
        # Регистрация подписчика - по умолчанию mode = 'market'
        sub = await self.db.upsert_subscriber(777)
        self.assertEqual(sub.mode, "market")

        # Обновление режима на 'upgrades'
        await self.db.update_subscriber_mode(777, "upgrades")
        sub_up = await self.db.get_subscriber(777)
        self.assertEqual(sub_up.mode, "upgrades")

        # Обновление режима на 'both'
        await self.db.update_subscriber_mode(777, "both")
        sub_both = await self.db.get_subscriber(777)
        self.assertEqual(sub_both.mode, "both")

    def test_upgrade_html_formatting_and_no_price(self):
        lot_upgrade = LotItem(
            lot_key="upgrade:chillflame-42",
            gift_id=1,
            raw_id=999,
            title="Chill Flame",
            num=42,
            slug="chillflame-42",
            price_stars=0,
            price_ton=None,
            model="Fire Ice",
            model_rarity="Epic",
            backdrop="Blue Nebula",
            pattern="Frost",
            seller_id=999999999,
            username="cool_upgrader",
            first_name="Алексей",
            last_name="Смирнов",
            level=1,
            send_paid_messages_stars=0,
            premium=True,
            detected_at="11.10.2026 15:45:00",
            event_type="upgrade",
        )

        html_text = lot_upgrade.format_notification_html()

        # 1. Заголовок апгрейда
        self.assertIn("💎 <b>Новый апгрейд</b>", html_text)
        self.assertNotIn("🎉 <b>Новый листинг</b>", html_text)

        # 2. Ссылка на t.me/nft/{slug}
        self.assertIn('<a href="https://t.me/nft/chillflame-42">Chill Flame #42</a>', html_text)

        # 3. НЕТ строки с ценой
        self.assertNotIn("💰", html_text)
        self.assertNotIn("★", html_text.split("💬")[0])  # Никакой цены до блока сообщений

        # 4. Атрибуты и профиль
        self.assertIn("🧩 Модель: Fire Ice (Epic)", html_text)
        self.assertIn("🎨 Фон: Blue Nebula · Узор: Frost", html_text)
        self.assertIn('<a href="https://t.me/cool_upgrader">@cool_upgrader</a> · <code>999999999</code>', html_text)
        self.assertIn("📊 Уровень: 1", html_text)
        self.assertIn("💬 Сообщения: бесплатно", html_text)
        self.assertIn("⭐ с Premium", html_text)
        self.assertIn("🕓 11.10.2026 15:45:00", html_text)

    def test_upgrade_offer_button_url(self):
        lot_upgrade = LotItem(
            lot_key="upgrade:chillflame-42",
            gift_id=1,
            raw_id=999,
            title="Chill Flame",
            num=42,
            slug="chillflame-42",
            price_stars=0,
            price_ton=None,
            model="Fire",
            model_rarity="Common",
            backdrop="Blue",
            pattern="Stars",
            seller_id=123,
            username="happy_owner",
            first_name="",
            last_name="",
            level=1,
            send_paid_messages_stars=0,
            premium=False,
            detected_at="11.10.2026 15:45:00",
            event_type="upgrade",
        )

        template = "Привет! Только что улучшил {title} #{num}? Продашь? Какая цена?"
        url = lot_upgrade.get_upgrade_offer_url(template)

        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("https://t.me/happy_owner?text="))

        import urllib.parse
        decoded = urllib.parse.unquote(url.replace("https://t.me/happy_owner?text=", ""))
        self.assertEqual(decoded, "Привет! Только что улучшил Chill Flame #42? Продашь? Какая цена?")

        # Без юзернейма -> None
        lot_upgrade.username = None
        self.assertIsNone(lot_upgrade.get_upgrade_offer_url(template))

    def test_upgrade_html_formatting_with_nft_count_and_at_username(self):
        lot_upgrade = LotItem(
            lot_key="upgrade:chillflame-43",
            gift_id=1,
            raw_id=1000,
            title="Chill Flame",
            num=43,
            slug="chillflame-43",
            price_stars=0,
            price_ton=None,
            model="Dark Flame",
            model_rarity="Rare",
            backdrop="Black",
            pattern="Frost",
            seller_id=777888999,
            username="@cryptoroman",
            first_name="Роман",
            last_name="Иванов",
            level=1,
            send_paid_messages_stars=0,
            premium=True,
            detected_at="15.10.2026 00:00:00",
            nft_gifts_count=1,
            event_type="upgrade",
        )

        html_text = lot_upgrade.format_notification_html()
        self.assertIn("💎 <b>Новый апгрейд</b>", html_text)
        self.assertIn('<a href="https://t.me/cryptoroman">@cryptoroman</a>', html_text)
        self.assertIn("📊 Уровень: 1", html_text)
        self.assertIn("🎁 NFT у продавца: 1", html_text)
        self.assertIn("💬 Сообщения: бесплатно", html_text)

        # Проверка генерации ссылки без двойных @@
        url = lot_upgrade.get_upgrade_offer_url("Привет!")
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("https://t.me/cryptoroman?text="))

    def test_mode_filtering_logic(self):
        # Проверка соответствия подписчика и события
        def should_receive(sub_mode: str, sub_min: int, sub_max: int, event_type: str, price: int) -> bool:
            if event_type == "upgrade":
                return sub_mode in ("upgrades", "both")
            else:
                return sub_mode in ("market", "both") and (sub_min <= price <= sub_max)

        # 1. Подписчик mode='market', [100, 500]
        self.assertTrue(should_receive("market", 100, 500, "market", 300))
        self.assertFalse(should_receive("market", 100, 500, "market", 50))
        self.assertFalse(should_receive("market", 100, 500, "upgrade", 0))

        # 2. Подписчик mode='upgrades'
        self.assertFalse(should_receive("upgrades", 100, 500, "market", 300))
        self.assertTrue(should_receive("upgrades", 100, 500, "upgrade", 0))

        # 3. Подписчик mode='both', [100, 500]
        self.assertTrue(should_receive("both", 100, 500, "market", 300))
        self.assertFalse(should_receive("both", 100, 500, "market", 600))
        self.assertTrue(should_receive("both", 100, 500, "upgrade", 0))

    def test_new_message_template(self):
        new_template = (
            "привет нфт подарок за рубли/$/звезды/тоны напрямую продашь? "
            "просто 3 недели кд ждать и комку платить не хочу (ботов не хожу, гарантов по ситуации)"
        )
        lot = LotItem(
            lot_key="test-key",
            gift_id=1,
            raw_id=1,
            title="Durov's Cap",
            num=10,
            slug="cap-10",
            price_stars=500,
            price_ton=None,
            model="Cap",
            model_rarity="Rare",
            backdrop="Gold",
            pattern="Stars",
            seller_id=123,
            username="seller_durov",
            first_name="Павел",
            last_name="",
            level=1,
            send_paid_messages_stars=0,
            premium=True,
            detected_at="now",
            event_type="market",
        )
        url = lot.get_offer_url(discount=50, template=new_template)
        import urllib.parse
        decoded = urllib.parse.unquote(url.replace("https://t.me/seller_durov?text=", ""))
        self.assertEqual(decoded, new_template)

        lot.event_type = "upgrade"
        url_up = lot.get_upgrade_offer_url(template=new_template)
        decoded_up = urllib.parse.unquote(url_up.replace("https://t.me/seller_durov?text=", ""))
        self.assertEqual(decoded_up, new_template)

    def test_round_robin_lead_distribution(self):
        # Имитация распределения логов между тремя подписчиками
        subscribers = [1001, 1002, 1003]
        lots = ["Lot1", "Lot2", "Lot3", "Lot4", "Lot5", "Lot6"]
        distributed = {}
        rr_index = 0
        for lot in lots:
            target_sub = subscribers[rr_index % len(subscribers)]
            rr_index += 1
            distributed.setdefault(target_sub, []).append(lot)

        self.assertEqual(distributed[1001], ["Lot1", "Lot4"])
        self.assertEqual(distributed[1002], ["Lot2", "Lot5"])
        self.assertEqual(distributed[1003], ["Lot3", "Lot6"])

    def test_config_require_username_and_max_price(self):
        cfg = AppConfig(require_username=True, max_price_stars=2000)
        self.assertTrue(cfg.require_username)
        self.assertEqual(cfg.max_price_stars, 2000)

        # Проверка логики фильтрации цены
        test_price_low = 800
        test_price_high = 3500
        self.assertFalse(cfg.max_price_stars is not None and test_price_low > cfg.max_price_stars)
        self.assertTrue(cfg.max_price_stars is not None and test_price_high > cfg.max_price_stars)

        # Проверка логики фильтрации юзернейма
        seller_with_user = {"username": "valid_user"}
        seller_no_user = {"username": None}
        self.assertFalse(cfg.require_username and not seller_with_user.get("username"))
        self.assertTrue(cfg.require_username and not seller_no_user.get("username"))

    def test_config_max_collection_floor(self):
        cfg = AppConfig(max_collection_floor=5000)
        self.assertEqual(cfg.max_collection_floor, 5000)

        # Коллекция с флором 1200 ★ проходит
        cheap_floor = 1200
        self.assertFalse(cfg.max_collection_floor is not None and cheap_floor > cfg.max_collection_floor)

        # Коллекция с флором 66666 ★ (Durov's Cap) отсекается
        expensive_floor = 66666
        self.assertTrue(cfg.max_collection_floor is not None and expensive_floor > cfg.max_collection_floor)

    def test_multi_account_config(self):
        acc1 = TelegramAccountConfig(session_name="userbot", api_id=111, api_hash="hash1")
        acc2 = TelegramAccountConfig(session_name="userbot_2", api_id=222, api_hash="hash2")
        cfg = AppConfig(accounts=[acc1, acc2])

        self.assertEqual(len(cfg.accounts), 2)
        self.assertEqual(cfg.accounts[0].session_name, "userbot")
        self.assertEqual(cfg.accounts[0].api_id, 111)
        self.assertEqual(cfg.accounts[1].session_name, "userbot_2")
        self.assertEqual(cfg.accounts[1].api_id, 222)

    def test_userbot_worker_pool_initialization(self):
        acc1 = TelegramAccountConfig(session_name="test_worker_1", api_id=111, api_hash="hash1")
        acc2 = TelegramAccountConfig(session_name="test_worker_2", api_id=222, api_hash="hash2")
        cfg = AppConfig(accounts=[acc1, acc2])
        try:
            monitor = UserbotMonitor(cfg, self.db)
            self.assertEqual(len(monitor.workers), 2)
            self.assertEqual(monitor.workers[0].idx, 1)
            self.assertEqual(monitor.workers[0].session_name, "test_worker_1")
            self.assertEqual(monitor.workers[1].idx, 2)
            self.assertEqual(monitor.workers[1].session_name, "test_worker_2")
            self.assertEqual(monitor.primary_worker, monitor.workers[0])
        finally:
            for s in ("test_worker_1.session", "test_worker_2.session"):
                if os.path.exists(s):
                    try:
                        os.remove(s)
                    except Exception:
                        pass

    def test_collection_sharding_distribution(self):
        # 26 целевых коллекций
        collection_ids = list(range(1001, 1027))
        self.assertEqual(len(collection_ids), 26)

        # 1. Шардинг между 2 воркерами (13 и 13)
        total_workers = 2
        assigned_0 = [gid for i, gid in enumerate(collection_ids) if i % total_workers == 0]
        assigned_1 = [gid for i, gid in enumerate(collection_ids) if i % total_workers == 1]

        self.assertEqual(len(assigned_0), 13)
        self.assertEqual(len(assigned_1), 13)
        # Нет пересечений
        self.assertEqual(len(set(assigned_0).intersection(set(assigned_1))), 0)
        # Полное покрытие всех 26 коллекций
        self.assertEqual(set(assigned_0).union(set(assigned_1)), set(collection_ids))

        # 2. Шардинг между 3 воркерами (9, 9, 8)
        total_workers_3 = 3
        w0 = [gid for i, gid in enumerate(collection_ids) if i % total_workers_3 == 0]
        w1 = [gid for i, gid in enumerate(collection_ids) if i % total_workers_3 == 1]
        w2 = [gid for i, gid in enumerate(collection_ids) if i % total_workers_3 == 2]

        self.assertEqual(len(w0), 9)
        self.assertEqual(len(w1), 9)
        self.assertEqual(len(w2), 8)
        self.assertEqual(len(w0) + len(w1) + len(w2), 26)
    def test_floor_overprice_ratio_cutoff(self):
        floor = 500
        ratio = 1.35
        max_allowed = int(floor * ratio)
        self.assertEqual(max_allowed, 675)

        # Лоты около floor проходят
        self.assertLessEqual(500, max_allowed)
        self.assertLessEqual(550, max_allowed)
        self.assertLessEqual(675, max_allowed)

        # Оверпрайс и неликвид отсекаются
        self.assertGreater(700, max_allowed)
        self.assertGreater(1500, max_allowed)
        self.assertGreater(777000, max_allowed)

    async def test_claim_database_methods(self):
        # 1. Добавляем лот в outbox
        lot = LotItem(
            lot_key="g1:s1:600",
            gift_id=1,
            raw_id=10,
            title="Chill Flame",
            num=1,
            slug="ChillFlame-1",
            price_stars=600,
            seller_id=1001,
            username="seller1",
        )
        outbox_id = await self.db.add_to_outbox(lot)

        # 2. Диспетчеризация сообщений
        await self.db.record_outbox_dispatch(outbox_id, chat_id=100, message_id=10)
        await self.db.record_outbox_dispatch(outbox_id, chat_id=200, message_id=20)

        dispatches = await self.db.get_outbox_dispatches(outbox_id)
        self.assertEqual(len(dispatches), 2)
        self.assertIn((100, 10), dispatches)
        self.assertIn((200, 20), dispatches)

        # 3. Первый клейм успешен
        ok, claim = await self.db.claim_lot(outbox_id, lot.lot_key, 100, "user100", "Name100")
        self.assertTrue(ok)
        self.assertEqual(claim["claimed_by"], 100)

        # 4. Повторный клейм другим юзером отклонен
        ok2, claim2 = await self.db.claim_lot(outbox_id, lot.lot_key, 200, "user200", "Name200")
        self.assertFalse(ok2)
        self.assertEqual(claim2["claimed_by"], 100)
        self.assertEqual(claim2["username"], "user100")

        # 5. get_claim возвращает информацию
        claim_info = await self.db.get_claim(outbox_id)
        self.assertIsNotNone(claim_info)
        self.assertEqual(claim_info["claimed_by"], 100)

        # 6. get_user_claimed_lots
        user_lots = await self.db.get_user_claimed_lots(100, limit=10)
        self.assertEqual(len(user_lots), 1)
        meta, claimed_lot = user_lots[0]
        self.assertEqual(meta["outbox_id"], outbox_id)
        self.assertEqual(claimed_lot.title, "Chill Flame")

    def test_is_trader_or_scam_filters(self):
        from userbot import is_trader_or_scam

        # 1. Тот самый лот из outbox
        self.assertTrue(
            is_trader_or_scam(
                "гасай | эдитор sell физы",
                "",
                "| $price @price3409 | $работы @deyoiw | бесплатно не делаю | $репа @reparw 🎼 | не в км |",
                "usergisiu",
            )
        )

        # 2. Слово "реп" / "репа" / "репы"
        self.assertTrue(is_trader_or_scam("Иван", "", "Мой реп канал: @rep_channel", "ivan"))
        self.assertTrue(is_trader_or_scam("Петр", "", "репа в закрепе", "peter"))

        # 3. Слово "гарант" / "гаранты" / "гаранта"
        self.assertTrue(is_trader_or_scam("Сергей", "", "Работаю только через гаранта!", "serg"))
        self.assertTrue(is_trader_or_scam("Garant", "", "Гарант-сервис", "garant_service"))

        # 4. Слово "продажа" / "sell"
        self.assertTrue(is_trader_or_scam("Алексей", "", "Продажа подарков и звезд", "alex"))
        self.assertTrue(is_trader_or_scam("Sell Stars", "", "sell gifts", "starseller"))

        # 5. Слово "физы" / "физ"
        self.assertTrue(is_trader_or_scam("Максим", "", "покупаю физы дорого", "max"))

        # 6. Запрет @ в имени/фамилии (реклама OTC картелей и ботов, кроме разрешённого RUBUP)
        self.assertFalse(is_trader_or_scam("soccer @RUBUP", "", "", "lezino"))
        self.assertTrue(is_trader_or_scam("Arsalan", "@GIFTSDouble", "", "arsalan1010"))
        self.assertTrue(is_trader_or_scam(". @Snake_Dice", "", "", "borovikqq"))

        # 7. Запрет ссылок на каналы/боты/шопы в био
        self.assertTrue(is_trader_or_scam("Иван", "", "Мой канал: https://t.me/super_gifts", "ivan_gifts"))
        self.assertTrue(is_trader_or_scam("Олег", "", "Снайпер: t.me/gift_satellite_bot", "oleg_sniper"))

        # 8. Картельные ключевые слова (devil metro, rent, gates, etc. - кроме RUBUP)
        self.assertTrue(is_trader_or_scam("МЕНЕДЖЕР", "DEVIL METRO-RENT", "", "devilpik"))
        self.assertFalse(is_trader_or_scam("Кошкомальчик", "", "под защитой rubup", "eblahoid"))

        # 9. Обычные пользователи без признаков скама/трейда
        self.assertFalse(is_trader_or_scam("Алина", "", "", "alina2005"))
        self.assertFalse(
            is_trader_or_scam("Михаил", "Петров", "Учусь в МГУ, люблю музыку", "misha_p")
        )


if __name__ == "__main__":
    unittest.main()


