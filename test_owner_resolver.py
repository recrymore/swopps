import asyncio
import os
import sys
import unittest
from telethon.tl import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Database, has_gift_on_profile, find_best_profile_gift
from userbot import UserbotMonitor, AccountWorker
from config import AppConfig


class DummyObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class DummyWorker:
    def __init__(self):
        self.idx = 1
        self.session_name = "dummy"
        self._flood_wait_until = 0.0
        self._last_call_time = 0.0
        self._last_market_call_time = 0.0
        self._full_user_lock = asyncio.Lock()
        self._market_throttle_lock = asyncio.Lock()


class TestOwnerResolver(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "test_resolver_db.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = Database(self.test_db_path)
        await self.db.init_db()

        self.config = AppConfig(
            telegram_api_id=12345,
            telegram_api_hash="abcdef",
            bot_token="test_token",
            max_level=1,
            max_nft_gifts=4,
            require_username=True,
            require_cyrillic=True,
        )

        self.monitor = UserbotMonitor(self.config, self.db)
        self.dummy_worker = DummyWorker()

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    async def test_layer1_gift_registry(self):
        """Layer 1: Resolving owner from gift_registry by slug."""
        seller_data = {
            "seller_id": 999111,
            "username": "ivan_ivanov",
            "first_name": "Иван",
            "level": 1,
            "nft_gifts_count": 2,
        }
        await self.db.save_gift_registry("snake-1234", 999111, seller_data)

        gift = DummyObject(slug="snake-1234", owner_name="Иван", owner_id=None, attributes=[])
        users_map = {}

        sid, seller, peer = await self.monitor.resolve_gift_owner(
            gift, users_map, worker=self.dummy_worker, is_upgrade=False
        )

        self.assertEqual(sid, 999111)
        self.assertIsNotNone(seller)
        self.assertEqual(seller["username"], "ivan_ivanov")
        self.assertEqual(peer.user_id, 999111)

    async def test_layer2_direct_owner_id(self):
        """Layer 2: Resolving owner when owner_id is provided directly by Telegram."""
        owner_peer = types.PeerUser(user_id=777888)
        gift = DummyObject(slug="durov-10", owner_name="Сергей", owner_id=owner_peer, attributes=[])
        user_obj = DummyObject(id=777888, username="sergey_user", first_name="Сергей", last_name="")
        users_map = {777888: user_obj}

        # Mock get_seller_profile
        async def mock_profile(uid, peer, user_obj=None, worker=None):
            return {
                "seller_id": uid,
                "username": user_obj.username,
                "first_name": user_obj.first_name,
                "level": 0,
                "nft_gifts_count": 1,
            }
        self.monitor.get_seller_profile = mock_profile

        sid, seller, peer = await self.monitor.resolve_gift_owner(
            gift, users_map, worker=self.dummy_worker, is_upgrade=False
        )

        self.assertEqual(sid, 777888)
        self.assertEqual(seller["username"], "sergey_user")
        self.assertEqual(peer, owner_peer)

        # Verify saved to gift_registry
        reg = await self.db.get_gift_registry("durov-10")
        self.assertIsNotNone(reg)
        self.assertEqual(reg["seller_id"], 777888)

    async def test_layer3_original_details_upgrade(self):
        """Layer 3: Resolving owner from StarGiftAttributeOriginalDetails upon upgrade."""
        recipient_peer = types.PeerUser(user_id=555444)
        attr = DummyObject(recipient_id=recipient_peer)
        setattr(attr, "__class__", type("StarGiftAttributeOriginalDetails", (), {}))

        gift = DummyObject(slug="pepe-77", owner_name="Алексей", owner_id=None, attributes=[attr])
        user_obj = DummyObject(id=555444, username="alexey_simple", first_name="Алексей", last_name="")
        users_map = {555444: user_obj}

        async def mock_profile(uid, peer, user_obj=None, worker=None):
            return {
                "seller_id": uid,
                "username": user_obj.username,
                "first_name": user_obj.first_name,
                "level": 1,
                "nft_gifts_count": 0,
            }
        self.monitor.get_seller_profile = mock_profile

        sid, seller, peer = await self.monitor.resolve_gift_owner(
            gift, users_map, worker=self.dummy_worker, is_upgrade=True
        )

        self.assertEqual(sid, 555444)
        self.assertEqual(seller["username"], "alexey_simple")
        self.assertEqual(peer.user_id, 555444)

    async def test_layer4_seller_cache_by_name(self):
        """Layer 4: Resolving owner from seller_cache (preloaded into RAM) by owner_name."""
        cached_user = {
            "seller_id": 333222,
            "username": "dmitry_dev",
            "first_name": "Дмитрий",
            "last_name": "Смирнов",
            "level": 0,
            "nft_gifts_count": 3,
            "profile_gifts": [{"slug": "star-1", "num": 1, "title": "Star"}],
        }
        await self.db.save_cached_seller(333222, cached_user)

        single_name_user = {
            "seller_id": 444333,
            "username": "persik_seller",
            "first_name": "PERSIKSKIF",
            "last_name": "",
            "level": 1,
            "nft_gifts_count": 2,
            "profile_gifts": [{"slug": "star-2", "num": 2, "title": "Star"}],
        }
        await self.db.save_cached_seller(444333, single_name_user)

        # User with same name who has OTHER sellable gifts on profile (should be accepted when require_exact=False)
        diff_gift_user = {
            "seller_id": 555666,
            "username": "ivan_seller",
            "first_name": "Иван Смирнов",
            "last_name": "",
            "level": 1,
            "nft_gifts_count": 1,
            "profile_gifts": [{"slug": "other-999", "num": 999, "title": "Other"}],
        }
        await self.db.save_cached_seller(555666, diff_gift_user)

        # User with same name whose gifts are HIDDEN / empty (should be rejected)
        empty_profile_user = {
            "seller_id": 666777,
            "username": "empty_user",
            "first_name": "Сергей Смирнов",
            "last_name": "",
            "level": 1,
            "nft_gifts_count": 0,
            "profile_gifts": [],
        }
        await self.db.save_cached_seller(666777, empty_profile_user)

        async def mock_profile(uid, peer, user_obj=None, worker=None):
            return await self.db.get_cached_seller(uid, max_age_seconds=86400)
        self.monitor.get_seller_profile = mock_profile

        # Test full name match with matching gift on profile -> accepted
        gift1 = DummyObject(slug="star-1", owner_name="Дмитрий Смирнов", owner_id=None, attributes=[], num=1, title="Star")
        sid1, seller1, peer1 = await self.monitor.resolve_gift_owner(
            gift1, {}, worker=self.dummy_worker, is_upgrade=False
        )
        self.assertEqual(sid1, 333222)
        self.assertEqual(seller1["username"], "dmitry_dev")

        # Test single unique name match with matching gift on profile -> accepted
        gift2 = DummyObject(slug="star-2", owner_name="PERSIKSKIF", owner_id=None, attributes=[], num=2, title="Star")
        sid2, seller2, peer2 = await self.monitor.resolve_gift_owner(
            gift2, {}, worker=self.dummy_worker, is_upgrade=False
        )
        self.assertEqual(sid2, 444333)
        self.assertEqual(seller2["username"], "persik_seller")

        # Test candidate who has OTHER sellable gifts on profile -> ACCEPTED under require_exact=False!
        gift_other = DummyObject(slug="star-99", owner_name="Иван Смирнов", owner_id=None, attributes=[], num=99, title="Star")
        sid_oth, seller_oth, peer_oth = await self.monitor.resolve_gift_owner(
            gift_other, {}, worker=self.dummy_worker, is_upgrade=False
        )
        self.assertEqual(sid_oth, 555666)
        self.assertEqual(seller_oth["username"], "ivan_seller")

        # Test candidate with HIDDEN / 0 gifts on profile -> REJECTED!
        gift_empty = DummyObject(slug="star-100", owner_name="Сергей Смирнов", owner_id=None, attributes=[], num=100, title="Star")
        sid_emp, seller_emp, peer_emp = await self.monitor.resolve_gift_owner(
            gift_empty, {}, worker=self.dummy_worker, is_upgrade=False
        )
        self.assertEqual(sid_emp, 0)
        self.assertIsNone(seller_emp)

        # Test strict rejection when name does NOT match (e.g. gift says Данил Зайцев, but candidate is Дмитрий)
        gift3 = DummyObject(slug="star-3", owner_name="Данил Зайцев", owner_id=None, attributes=[], num=3, title="Star")
        sid3, seller3, peer3 = await self.monitor.resolve_gift_owner(
            gift3, {}, worker=self.dummy_worker, is_upgrade=False
        )
        self.assertEqual(sid3, 0)
        self.assertIsNone(seller3)

    async def test_unresolved_owner_returns_zero(self):
        """Unresolved owner returns (0, None, None) so lot is dropped (no anonymous / no regex)."""
        gift = DummyObject(slug="random-999", owner_name="Неизвестный", owner_id=None, attributes=[])
        sid, seller, peer = await self.monitor.resolve_gift_owner(
            gift, {}, worker=self.dummy_worker, is_upgrade=False
        )
        self.assertEqual(sid, 0)
        self.assertIsNone(seller)
        self.assertIsNone(peer)


    async def test_scanned_upgrades_table(self):
        """Verify automatic recording and reading from scanned_upgrades table."""
        count_before = await self.db.get_scanned_upgrades_count()
        self.assertEqual(count_before, 0)

        await self.db.save_scanned_upgrade(
            slug="chillflame-100",
            short_name="Chill Flame",
            num=100,
            model="Dark Flame",
            model_rarity="Rare",
            backdrop="Black",
            pattern="Cross",
            owner_id=777111,
            owner_name="Михаил",
            username="mikhail_upgrade",
            first_name="Михаил",
            level=1,
            nft_gifts_count=2,
            raw_data='{"seller_id": 777111}',
        )

        count_after = await self.db.get_scanned_upgrades_count()
        self.assertEqual(count_after, 1)

        saved = await self.db.get_scanned_upgrade("chillflame-100")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["slug"], "chillflame-100")
        self.assertEqual(saved["short_name"], "Chill Flame")
        self.assertEqual(saved["num"], 100)
        self.assertEqual(saved["backdrop"], "Black")
        self.assertEqual(saved["owner_id"], 777111)
        self.assertEqual(saved["username"], "mikhail_upgrade")

        stats = await self.db.get_stats()
        self.assertEqual(stats["scanned_upgrades"], 1)

    def test_has_gift_on_profile(self):
        """Test has_gift_on_profile matching logic."""
        sample_profile = [
            {"slug": "FaithAmulet-62944", "num": 62944, "title": "Faith Amulet"},
            {"slug": "StarNotepad-1847", "num": 1847, "title": "Star Notepad"},
        ]

        # 1. Non-exact mode (require_exact=False): any sellable gift on profile is accepted
        self.assertTrue(has_gift_on_profile(sample_profile, require_exact=False))
        self.assertTrue(has_gift_on_profile(sample_profile, slug="RandomSlug-123", require_exact=False))
        self.assertFalse(has_gift_on_profile([], require_exact=False))
        self.assertFalse(has_gift_on_profile(None, require_exact=False))

        # 2. Exact mode (require_exact=True)
        # 2.1 Exact slug match
        self.assertTrue(has_gift_on_profile(sample_profile, slug="FaithAmulet-62944", require_exact=True))
        self.assertTrue(has_gift_on_profile(sample_profile, slug="faithamulet-62944", require_exact=True))

        # 2.2 Number + title match
        self.assertTrue(has_gift_on_profile(sample_profile, num=1847, title="Star Notepad", require_exact=True))
        self.assertTrue(has_gift_on_profile(sample_profile, num=1847, title="star notepad", require_exact=True))

        # 2.3 Mismatches in exact mode
        self.assertFalse(has_gift_on_profile(sample_profile, slug="RandomSlug-123", require_exact=True))
        self.assertFalse(has_gift_on_profile(sample_profile, num=99999, title="Faith Amulet", require_exact=True))
        self.assertFalse(has_gift_on_profile(sample_profile, num=62944, title="Different Collection", require_exact=True))
        self.assertFalse(has_gift_on_profile([], slug="FaithAmulet-62944", require_exact=True))
        self.assertFalse(has_gift_on_profile(None, slug="FaithAmulet-62944", require_exact=True))

    def test_find_best_profile_gift(self):
        """Test find_best_profile_gift logic with sale preferences and fallback."""
        profile_with_sales = [
            {
                "slug": "Showcase-1",
                "num": 1,
                "title": "Showcase",
                "on_sale": False,
                "price_stars": None,
            },
            {
                "slug": "CookieHeart-112429",
                "num": 112429,
                "title": "Cookie Heart",
                "on_sale": True,
                "price_stars": 1900,
            },
            {
                "slug": "ChillFlame-500",
                "num": 500,
                "title": "Chill Flame",
                "on_sale": True,
                "price_stars": 3500,
            },
        ]

        # 1. Exact match on sale
        g = find_best_profile_gift(profile_with_sales, slug="ChillFlame-500")
        self.assertIsNotNone(g)
        self.assertEqual(g["slug"], "ChillFlame-500")
        self.assertEqual(g["price_stars"], 3500)

        # 2. Scanned lot does not match, but another gift is on sale -> returns selling gift!
        g_diff = find_best_profile_gift(profile_with_sales, slug="RandomMarketLot-999")
        self.assertIsNotNone(g_diff)
        # Should pick cheapest selling gift (CookieHeart-112429 for 1900 stars)
        self.assertEqual(g_diff["slug"], "CookieHeart-112429")
        self.assertEqual(g_diff["price_stars"], 1900)

        # 3. Budget limit check: max_price_stars = 2000
        g_budget = find_best_profile_gift(profile_with_sales, slug="RandomLot-1", max_price_stars=2000)
        self.assertIsNotNone(g_budget)
        self.assertEqual(g_budget["slug"], "CookieHeart-112429")

        # 4. Profile without active sales -> fallback to first gift
        profile_no_sales = [
            {"slug": "EasterEgg-13", "num": 13, "title": "Easter Egg", "on_sale": False, "price_stars": None},
        ]
        g_fallback = find_best_profile_gift(profile_no_sales, slug="RandomMarketLot-777")
        self.assertIsNotNone(g_fallback)
        self.assertEqual(g_fallback["slug"], "EasterEgg-13")

        # 5. Empty profile -> None
        self.assertIsNone(find_best_profile_gift([], slug="Random"))
        self.assertIsNone(find_best_profile_gift(None, slug="Random"))


if __name__ == "__main__":
    unittest.main()
