import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_UP
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import AppConfig
from database import Database
from models import (
    LotItem,
    Subscriber,
    Order,
    compute_extension,
    is_sub_active,
    format_hours_ru,
)
from payments import (
    CurrencyConverter,
    PaymentVerifier,
    TransferItem,
    generate_order_comment,
    is_same_address,
    normalize_ton_address,
    DEFAULT_USDT_MASTER,
)
from aiogram import types
from bot import verify_and_process_payments, notify_owner
from admin import is_admin_authorized, admin_router


class TestSubscriptionModels(unittest.TestCase):
    def test_compute_extension_none(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # 6 hours
        res_6h = compute_extension(None, "6h", now)
        self.assertEqual(res_6h, "2026-01-01 18:00:00")
        # 1 day
        res_1d = compute_extension(None, "1d", now)
        self.assertEqual(res_1d, "2026-01-02 12:00:00")
        # 3 days
        res_3d = compute_extension(None, "3d", now)
        self.assertEqual(res_3d, "2026-01-04 12:00:00")
        # 7 days
        res_7d = compute_extension(None, "7d", now)
        self.assertEqual(res_7d, "2026-01-08 12:00:00")
        # 1 month (30 days)
        res_1m = compute_extension(None, "1m", now)
        self.assertEqual(res_1m, "2026-01-31 12:00:00")
        # forever
        res_forever = compute_extension(None, "forever", now)
        self.assertEqual(res_forever, "forever")

    def test_compute_extension_active_subscription(self):
        # Already active until Jan 10
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        curr = "2026-01-10 12:00:00"
        # Extending by 3 days -> Jan 13
        res = compute_extension(curr, "3d", now)
        self.assertEqual(res, "2026-01-13 12:00:00")

    def test_compute_extension_expired_subscription(self):
        # Expired on Dec 20
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        curr = "2025-12-20 12:00:00"
        # Extending by 1 day -> starts from now (Jan 1) -> Jan 2
        res = compute_extension(curr, "1d", now)
        self.assertEqual(res, "2026-01-02 12:00:00")

    def test_compute_extension_forever_stays_forever(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        res = compute_extension("forever", "1m", now)
        self.assertEqual(res, "forever")
        res2 = compute_extension("2026-01-10 12:00:00", "forever", now)
        self.assertEqual(res2, "forever")

    def test_subscriber_access_checks(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Empty subscriber
        sub_none = Subscriber(chat_id=101)
        self.assertFalse(sub_none.has_market_access(now))
        self.assertFalse(sub_none.has_upgrades_access(now))
        self.assertFalse(sub_none.has_any_access(now))

        # Active market, expired upgrades
        sub_active = Subscriber(
            chat_id=102,
            market_until="2026-01-05 12:00:00",
            upgrades_until="2025-12-31 12:00:00",
        )
        self.assertTrue(sub_active.has_market_access(now))
        self.assertFalse(sub_active.has_upgrades_access(now))
        self.assertTrue(sub_active.has_any_access(now))

        # Forever upgrades
        sub_forever = Subscriber(chat_id=103, upgrades_until="forever")
        self.assertFalse(sub_forever.has_market_access(now))
        self.assertTrue(sub_forever.has_upgrades_access(now))
        self.assertTrue(sub_forever.has_any_access(now))
        self.assertTrue(sub_forever.is_forever("upgrades"))
        self.assertFalse(sub_forever.is_forever("market"))

    def test_compute_extension_custom_hours(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # 1 hour
        res_1h = compute_extension(None, "1h", now)
        self.assertEqual(res_1h, "2026-01-01 13:00:00")
        # 12 hours
        res_12h = compute_extension(None, "12h", now)
        self.assertEqual(res_12h, "2026-01-02 00:00:00")
        # 48 hours
        res_48h = compute_extension(None, "48h", now)
        self.assertEqual(res_48h, "2026-01-03 12:00:00")
        # plain digits as hours
        res_24 = compute_extension(None, "24", now)
        self.assertEqual(res_24, "2026-01-02 12:00:00")

        # Extending active subscription with custom hours
        curr = "2026-01-10 10:00:00"
        res_ext = compute_extension(curr, "5h", now)
        self.assertEqual(res_ext, "2026-01-10 15:00:00")

        # Extending expired subscription with custom hours (starts from now)
        expired = "2025-12-01 10:00:00"
        res_exp = compute_extension(expired, "5h", now)
        self.assertEqual(res_exp, "2026-01-01 17:00:00")

    def test_format_hours_ru(self):
        self.assertEqual(format_hours_ru(1), "1 час")
        self.assertEqual(format_hours_ru(2), "2 часа")
        self.assertEqual(format_hours_ru(4), "4 часа")
        self.assertEqual(format_hours_ru(5), "5 часов")
        self.assertEqual(format_hours_ru(11), "11 часов")
        self.assertEqual(format_hours_ru(12), "12 часов")
        self.assertEqual(format_hours_ru(14), "14 часов")
        self.assertEqual(format_hours_ru(21), "21 час")
        self.assertEqual(format_hours_ru(22), "22 часа")
        self.assertEqual(format_hours_ru(25), "25 часов")
        self.assertEqual(format_hours_ru(48), "48 часов")

    def test_is_sub_active(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(is_sub_active(None, now))
        self.assertFalse(is_sub_active("", now))
        self.assertTrue(is_sub_active("forever", now))
        self.assertTrue(is_sub_active("2026-01-01 12:00:00", now))
        self.assertTrue(is_sub_active("2026-01-01 12:00:01", now))
        self.assertFalse(is_sub_active("2026-01-01 11:59:59", now))
        self.assertFalse(is_sub_active("2025-12-31 00:00:00", now))

    def test_order_duration_title_custom_hours(self):
        ord_obj = Order(
            id=1,
            user_id=123,
            sub_type="market",
            duration="12h",
            currency="TON",
            amount=1000000000,
            rate=1.0,
            comment="c",
            status="paid",
            created_at="",
            expires_at="",
        )
        self.assertEqual(ord_obj.duration_title, "12 часов")
        ord_obj.duration = "1h"
        self.assertEqual(ord_obj.duration_title, "1 час")
        ord_obj.duration = "3d"
        self.assertEqual(ord_obj.duration_title, "3 дня")


class TestCurrencyAndPayments(unittest.TestCase):
    def test_address_normalization_and_comparison(self):
        # Bounceable / Non-bounceable / Raw addresses
        bounceable = "EQDDOVmz4YWWZgHEWZocsce6VmWbsnnUi8pslfpJI-Qhvp-q"
        raw = normalize_ton_address(bounceable)
        self.assertTrue(is_same_address(raw, bounceable))
        self.assertTrue(is_same_address(bounceable, bounceable))
        self.assertFalse(is_same_address(raw, "0:1111111111111111111111111111111111111111111111111111111111111111"))

    def test_currency_converter_rounding(self):
        conv = CurrencyConverter()
        # 0.2 TON * 5.1234 = 1.02468 -> Rounded up to 1.03 USDT
        usdt_float, micro_usdt = conv.calculate_usdt_amount(0.2, 5.1234)
        self.assertEqual(usdt_float, 1.03)
        self.assertEqual(micro_usdt, 1030000)

        # Exact decimal: 0.5 TON * 2.0 = 1.00 USDT
        usdt_float, micro_usdt = conv.calculate_usdt_amount(0.5, 2.0)
        self.assertEqual(usdt_float, 1.0)
        self.assertEqual(micro_usdt, 1000000)

        # NanoTON calculation: 0.2 TON -> 200,000,000 nanoTON
        nano_ton = conv.calculate_ton_amount(0.2)
        self.assertEqual(nano_ton, 200000000)

    def test_order_comment_generation(self):
        c1 = generate_order_comment()
        c2 = generate_order_comment()
        self.assertTrue(c1.startswith("gp-"))
        self.assertEqual(len(c1), 9)  # "gp-" + 6 hex chars
        self.assertNotEqual(c1, c2)


class TestSubscriptionDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "test_sub_db.db"
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass
        self.db = Database(self.test_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    async def test_trial_period_activation(self):
        chat_id = 555001
        await self.db.upsert_subscriber(chat_id)

        # Activate trial for 24 hours
        success, until_date = await self.db.activate_trial(chat_id, 24)
        self.assertTrue(success)
        self.assertIsNotNone(until_date)

        sub = await self.db.get_subscriber(chat_id)
        self.assertTrue(sub.has_used_trial)
        self.assertTrue(sub.has_market_access())
        self.assertTrue(sub.has_upgrades_access())
        self.assertEqual(sub.mode, "both")

        # Second attempt must fail
        success2, _ = await self.db.activate_trial(chat_id, 24)
        self.assertFalse(success2)

    async def test_order_creation_and_retrieval(self):
        chat_id = 555002
        order = await self.db.create_order(
            user_id=chat_id,
            sub_type="market",
            duration="7d",
            currency="TON",
            amount=1900000000,
            rate=None,
        )
        self.assertIsNotNone(order.id)
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.currency, "TON")
        self.assertEqual(order.amount, 1900000000)
        self.assertTrue(order.comment.startswith("gp-"))

        # Retrieve by ID
        fetched = await self.db.get_order(order.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.comment, order.comment)

        # Retrieve by Comment
        by_comment = await self.db.get_order_by_comment(order.comment)
        self.assertIsNotNone(by_comment)
        self.assertEqual(by_comment.id, order.id)

    async def test_payment_processing_atomic_and_double_spend(self):
        chat_id = 555003
        await self.db.upsert_subscriber(chat_id)
        order = await self.db.create_order(
            user_id=chat_id,
            sub_type="upgrades",
            duration="3d",
            currency="TON",
            amount=1000000000,
        )

        tx_hash = "tx_test_hash_12345"
        success, msg, updated_sub = await self.db.process_successful_payment(
            order_id=order.id,
            tx_hash=tx_hash,
            actual_amount=1000000000,
            currency="TON",
        )
        self.assertTrue(success)
        self.assertIsNotNone(updated_sub)
        self.assertTrue(updated_sub.has_upgrades_access())

        # Check order status updated
        paid_order = await self.db.get_order(order.id)
        self.assertEqual(paid_order.status, "paid")
        self.assertEqual(paid_order.tx_hash, tx_hash)

        # Verify tx is marked processed
        self.assertTrue(await self.db.is_tx_processed(tx_hash))

        # Attempt to process duplicate payment with same tx_hash -> must be rejected
        dup_order = await self.db.create_order(
            user_id=chat_id,
            sub_type="upgrades",
            duration="3d",
            currency="TON",
            amount=1000000000,
        )
        success2, msg2, _ = await self.db.process_successful_payment(
            order_id=dup_order.id,
            tx_hash=tx_hash,
            actual_amount=1000000000,
            currency="TON",
        )
        self.assertFalse(success2)
        self.assertIn("уже был зачислен", msg2)

    async def test_underpayment_and_currency_mismatch(self):
        chat_id = 555004
        await self.db.upsert_subscriber(chat_id)
        order = await self.db.create_order(
            user_id=chat_id,
            sub_type="market",
            duration="1d",
            currency="TON",
            amount=500000000,  # 0.5 TON
        )

        # Underpayment (0.2 TON instead of 0.5)
        success_under, msg_under, _ = await self.db.process_successful_payment(
            order_id=order.id,
            tx_hash="tx_under_1",
            actual_amount=200000000,
            currency="TON",
        )
        self.assertFalse(success_under)
        self.assertIn("Недоплата", msg_under)

        # Currency mismatch (USDT instead of TON)
        success_curr, msg_curr, _ = await self.db.process_successful_payment(
            order_id=order.id,
            tx_hash="tx_mismatch_1",
            actual_amount=500000000,
            currency="USDT",
        )
        self.assertFalse(success_curr)
        self.assertIn("Несовпадение валюты", msg_curr)

    async def test_reminders_and_expiry_queries(self):
        chat_id = 555005
        now = datetime.now(timezone.utc)
        # Expiring in 10 hours
        exp_soon = (now + timedelta(hours=10)).strftime("%Y-%m-%d %H:%M:%S")
        await self.db.upsert_subscriber(chat_id)

        async with self.db._connect() if hasattr(self.db, "_connect") else __import__("aiosqlite").connect(self.test_db_path) as conn:
            await conn.execute(
                "UPDATE subscribers SET market_until = ?, market_reminder_sent = 0 WHERE chat_id = ?",
                (exp_soon, chat_id),
            )
            await conn.commit()

        # Should be returned in needing reminder
        needing = await self.db.get_subscriptions_needing_reminder()
        matching = [item for item in needing if item[0] == chat_id and item[1] == "market"]
        self.assertEqual(len(matching), 1)

        # Mark reminder sent
        await self.db.mark_reminder_sent(chat_id, "market")
        needing_after = await self.db.get_subscriptions_needing_reminder()
        matching_after = [item for item in needing_after if item[0] == chat_id and item[1] == "market"]
        self.assertEqual(len(matching_after), 0)

        # Now test newly expired query
        past_exp = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        async with __import__("aiosqlite").connect(self.test_db_path) as conn:
            await conn.execute(
                "UPDATE subscribers SET market_until = ?, market_expired_sent = 0 WHERE chat_id = ?",
                (past_exp, chat_id),
            )
            await conn.commit()

        expired_list = await self.db.get_newly_expired_subscriptions()
        matching_exp = [item for item in expired_list if item[0] == chat_id and item[1] == "market"]
        self.assertEqual(len(matching_exp), 1)

        await self.db.mark_expired_sent(chat_id, "market")
        expired_list_after = await self.db.get_newly_expired_subscriptions()
        matching_exp_after = [item for item in expired_list_after if item[0] == chat_id and item[1] == "market"]
        self.assertEqual(len(matching_exp_after), 0)


class TestPaymentVerificationFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "test_verifier_db.db"
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass
        self.db = Database(self.test_db_path)
        await self.db.init_db()
        self.config = AppConfig(
            owner_address="EQDDOVmz4YWWZgHEWZocsce6VmWbsnnUi8pslfpJI-Qhvp-q",
            owner_chat_id=1234567,
            usdt_master_address=DEFAULT_USDT_MASTER,
        )

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    async def test_full_successful_ton_flow(self):
        user_id = 999001
        await self.db.upsert_subscriber(user_id)
        order = await self.db.create_order(
            user_id=user_id,
            sub_type="both",
            duration="1m",
            currency="TON",
            amount=6000000000,  # 6 TON
        )

        mock_verifier = MagicMock(spec=PaymentVerifier)
        mock_verifier.fetch_incoming_transfers = AsyncMock(
            return_value=[
                TransferItem(
                    tx_hash="tx_valid_ton_hash_999",
                    currency="TON",
                    amount=6000000000,
                    comment=order.comment,
                    sender="EQSender...",
                    recipient=self.config.owner_address,
                )
            ]
        )

        mock_bot = AsyncMock()
        processed = await verify_and_process_payments(
            bot=mock_bot, db=self.db, config=self.config, verifier=mock_verifier
        )
        self.assertEqual(processed, 1)

        # Order should now be paid
        order_after = await self.db.get_order(order.id)
        self.assertEqual(order_after.status, "paid")
        self.assertEqual(order_after.tx_hash, "tx_valid_ton_hash_999")

        # Subscriber should have access to both modes
        sub = await self.db.get_subscriber(user_id)
        self.assertTrue(sub.has_market_access())
        self.assertTrue(sub.has_upgrades_access())

        # Notification sent to user
        mock_bot.send_message.assert_called()

    async def test_fake_usdt_master_rejected_by_verifier(self):
        # Test PaymentVerifier directly filters out rogue USDT contracts
        verifier = PaymentVerifier(
            owner_address=self.config.owner_address,
            usdt_master=DEFAULT_USDT_MASTER,
        )

        # Mock TonAPI events payload
        fake_event = {
            "events": [
                {
                    "event_id": "fake_usdt_event_1",
                    "in_progress": False,
                    "actions": [
                        {
                            "type": "JettonTransfer",
                            "status": "ok",
                            "JettonTransfer": {
                                "recipient": {"address": self.config.owner_address},
                                "jetton": {"address": "EQ_FAKE_SCAM_JETTON_ADDRESS"},
                                "amount": 10000000,
                                "comment": "gp-123456",
                            },
                        }
                    ],
                }
            ]
        }

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=fake_event)

        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__.return_value = mock_response

        transfers = await verifier.fetch_incoming_transfers(mock_session)
        # Rogue USDT should be filtered out!
        self.assertEqual(len(transfers), 0)

    async def test_late_payment_after_order_expired(self):
        user_id = 999002
        await self.db.upsert_subscriber(user_id)
        order = await self.db.create_order(
            user_id=user_id,
            sub_type="market",
            duration="1d",
            currency="TON",
            amount=500000000,
        )

        # Artificially expire the order in database
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with __import__("aiosqlite").connect(self.test_db_path) as conn:
            await conn.execute("UPDATE orders SET expires_at = ? WHERE id = ?", (past_iso, order.id))
            await conn.commit()

        mock_verifier = MagicMock(spec=PaymentVerifier)
        mock_verifier.fetch_incoming_transfers = AsyncMock(
            return_value=[
                TransferItem(
                    tx_hash="tx_late_ton_999",
                    currency="TON",
                    amount=500000000,
                    comment=order.comment,
                    sender="EQSender...",
                    recipient=self.config.owner_address,
                )
            ]
        )

        mock_bot = AsyncMock()
        processed = await verify_and_process_payments(
            bot=mock_bot, db=self.db, config=self.config, verifier=mock_verifier
        )
        self.assertEqual(processed, 0)

        # Order must be marked expired
        order_after = await self.db.get_order(order.id)
        self.assertEqual(order_after.status, "expired")

        # Transfer must be recorded in unmatched_payments
        self.assertTrue(await self.db.is_tx_processed("tx_late_ton_999"))


class TestAdminPanelAuthAndOperations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "test_admin_db.db"
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass
        self.db = Database(self.test_db_path)
        await self.db.init_db()
        self.config = AppConfig(
            admin_username="super_boss",  # will test with @Super_Boss
            owner_chat_id=1234567,
        )

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    async def test_admin_auth_first_login_and_subsequent_id_check(self):
        # 1. Unknown user with wrong username
        u_wrong = types.User(id=8881, is_bot=False, first_name="Random", username="random_guy")
        is_adm = await is_admin_authorized(u_wrong, self.db, self.config)
        self.assertFalse(is_adm)

        # 2. First login with configured admin username (case-insensitive with @)
        u_boss = types.User(id=999999, is_bot=False, first_name="Boss", username="Super_Boss")
        is_adm = await is_admin_authorized(u_boss, self.db, self.config)
        self.assertTrue(is_adm)

        # Verify admin_id was saved to DB
        saved_id = await self.db.get_setting("admin_id")
        self.assertEqual(saved_id, "999999")

        # 3. Subsequent check with different username or no username, but matching saved ID
        u_changed = types.User(id=999999, is_bot=False, first_name="Boss", username="changed_name")
        is_adm_by_id = await is_admin_authorized(u_changed, self.db, self.config)
        self.assertTrue(is_adm_by_id)

    async def test_admin_grant_and_revoke_subscription(self):
        target_user = 777001
        await self.db.upsert_subscriber(target_user)

        # Grant 7 days market
        success = await self.db.admin_grant_subscription(target_user, "market", "7d")
        self.assertTrue(success)

        sub = await self.db.get_subscriber(target_user)
        self.assertTrue(sub.has_market_access())
        self.assertFalse(sub.has_upgrades_access())

        # Grant forever both
        success_both = await self.db.admin_grant_subscription(target_user, "both", "forever")
        self.assertTrue(success_both)
        sub2 = await self.db.get_subscriber(target_user)
        self.assertEqual(sub2.market_until, "forever")
        self.assertEqual(sub2.upgrades_until, "forever")

        # Revoke market
        await self.db.admin_revoke_subscription(target_user, "market")
        sub3 = await self.db.get_subscriber(target_user)
        self.assertIsNone(sub3.market_until)
        self.assertEqual(sub3.upgrades_until, "forever")

    async def test_admin_statistics_calculation(self):
        # Create users, orders and payments
        u1 = 801
        u2 = 802
        await self.db.upsert_subscriber(u1)
        await self.db.upsert_subscriber(u2)

        # u1 pays 4 TON
        o1 = await self.db.create_order(u1, "market", "1m", "TON", 4000000000)
        await self.db.process_successful_payment(o1.id, "tx_stats_ton", 4000000000, "TON")

        # u2 pays 5 USDT
        o2 = await self.db.create_order(u2, "upgrades", "1m", "USDT", 5000000)
        await self.db.process_successful_payment(o2.id, "tx_stats_usdt", 5000000, "USDT")

        stats = await self.db.admin_get_stats()
        self.assertEqual(stats["total_users"], 2)
        self.assertEqual(stats["active_subscribers"], 2)
        self.assertEqual(stats["total_orders"], 2)
        self.assertEqual(stats["paid_orders"], 2)
        self.assertAlmostEqual(stats["revenue_ton"], 4.0)
        self.assertAlmostEqual(stats["revenue_usdt"], 5.0)

    async def test_admin_user_list_shows_inactive_for_expired_subscriptions(self):
        from admin import handle_admin_users

        # User 1: Active market subscription
        u1 = 811
        await self.db.upsert_subscriber(u1, username="active_user")
        await self.db.admin_grant_subscription(u1, "market", "7d")

        # User 2: Expired market subscription
        u2 = 812
        await self.db.upsert_subscriber(u2, username="expired_user")
        past_iso = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        async with __import__("aiosqlite").connect(self.test_db_path) as conn:
            await conn.execute("UPDATE subscribers SET market_until = ? WHERE chat_id = ?", (past_iso, u2))
            await conn.commit()

        # User 3: Never subscribed
        u3 = 813
        await self.db.upsert_subscriber(u3, username="no_sub_user")

        admin_user = types.User(id=999999, is_bot=False, first_name="Boss", username="Super_Boss")

        cb = MagicMock()
        cb.data = "admin_users:1"
        cb.from_user = admin_user
        cb.message.edit_text = AsyncMock()
        cb.message.answer = AsyncMock()
        cb.answer = AsyncMock()

        await handle_admin_users(cb, self.db, self.config)

        # Inspect the markup generated
        edit_args = cb.message.edit_text.call_args
        markup = edit_args[1]["reply_markup"]
        btn_texts = [btn.text for row in markup.inline_keyboard for btn in row]

        # active_user should have 📦▫️
        active_btn = [t for t in btn_texts if "@active_user" in t][0]
        self.assertIn("📦▫️", active_btn)

        # expired_user MUST have ▫️▫️ (NOT 📦!)
        expired_btn = [t for t in btn_texts if "@expired_user" in t][0]
        self.assertIn("▫️▫️", expired_btn)
        self.assertNotIn("📦", expired_btn)

        # no_sub_user should have ▫️▫️
        no_sub_btn = [t for t in btn_texts if "@no_sub_user" in t][0]
        self.assertIn("▫️▫️", no_sub_btn)

    async def test_admin_grant_custom_hours_flow(self):
        from admin import (
            handle_admin_grant_custom,
            handle_admin_text_inputs,
            _admin_states,
        )

        admin_user = types.User(id=999999, is_bot=False, first_name="Boss", username="Super_Boss")
        admin_id = admin_user.id
        target_id = 821
        await self.db.upsert_subscriber(target_id, username="target_user")

        # 1. Admin clicks "Ввести любое кол-во часов"
        cb = MagicMock()
        cb.data = f"admin_grant_custom:{target_id}:market:1"
        cb.from_user = admin_user
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        await handle_admin_grant_custom(cb, self.db, self.config)
        self.assertIn(admin_id, _admin_states)
        self.assertEqual(_admin_states[admin_id]["action"], "grant_custom_hours")
        self.assertEqual(_admin_states[admin_id]["target_user_id"], target_id)

        # 2. Admin enters invalid number
        msg_invalid = MagicMock()
        msg_invalid.from_user = admin_user
        msg_invalid.text = "invalid"
        msg_invalid.answer = AsyncMock()
        msg_invalid.bot = AsyncMock()

        await handle_admin_text_inputs(msg_invalid, self.db, self.config)
        msg_invalid.answer.assert_called_once()
        self.assertIn("Неверный формат", msg_invalid.answer.call_args[0][0])
        self.assertIn(admin_id, _admin_states)  # State remains

        # 3. Admin enters valid number of hours (e.g. 15 hours)
        msg_valid = MagicMock()
        msg_valid.from_user = admin_user
        msg_valid.text = "15"
        msg_valid.answer = AsyncMock()
        mock_bot = AsyncMock()
        msg_valid.bot = mock_bot

        await handle_admin_text_inputs(msg_valid, self.db, self.config)
        self.assertNotIn(admin_id, _admin_states)  # State cleared

        # User must receive subscription for ~15 hours
        sub = await self.db.get_subscriber(target_id)
        self.assertTrue(sub.has_market_access())
        self.assertFalse(sub.has_upgrades_access())

        # Check notification sent to user
        mock_bot.send_message.assert_called_once()
        self.assertEqual(mock_bot.send_message.call_args[1]["chat_id"], target_id)
        self.assertIn("15 часов", mock_bot.send_message.call_args[1]["text"])

    async def test_admin_pinned_message_broadcast_and_unpin(self):
        from admin import (
            get_admin_main_keyboard,
            handle_admin_pin_prompt,
            handle_admin_pin_confirm,
            handle_admin_text_inputs,
            handle_admin_unpin_all_confirm,
            _admin_states,
        )

        # 1. Проверяем наличие кнопки в клавиатуре
        kb = get_admin_main_keyboard()
        buttons = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("admin_pin_prompt", buttons)

        # 2. Создаем 2 пользователей в БД (один активный, один неактивный)
        u1 = 811
        u2 = 812
        await self.db.upsert_subscriber(u1)
        await self.db.upsert_subscriber(u2)
        await self.db.deactivate_subscriber(u2)

        # Проверяем get_all_subscribers
        all_subs = await self.db.get_all_subscribers()
        all_ids = [s.chat_id for s in all_subs]
        self.assertIn(u1, all_ids)
        self.assertIn(u2, all_ids)

        # 3. Админ запрашивает ввод текста закрепленного сообщения
        admin_user = types.User(id=999999, is_bot=False, first_name="Boss", username="Super_Boss")
        cb = MagicMock()
        cb.from_user = admin_user
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        await handle_admin_pin_prompt(cb, self.db, self.config)
        self.assertEqual(_admin_states[admin_user.id]["action"], "pin_broadcast_wait_text")
        cb.message.edit_text.assert_called_once()
        self.assertIn("закрепл", cb.message.edit_text.call_args[0][0].lower())

        # 4. Админ отправляет текст сообщения
        msg = MagicMock()
        msg.from_user = admin_user
        msg.text = "Важное объявление для всех!"
        msg.caption = None
        msg.answer = AsyncMock()

        await handle_admin_text_inputs(msg, self.db, self.config)
        self.assertEqual(_admin_states[admin_user.id]["action"], "pin_broadcast_confirm")
        self.assertEqual(_admin_states[admin_user.id]["text"], "Важное объявление для всех!")
        msg.answer.assert_called_once()
        preview_text = msg.answer.call_args[0][0]
        self.assertIn("Предпросмотр", preview_text)
        self.assertIn("Важное объявление для всех!", preview_text)

        # 5. Админ подтверждает отправку с закрепом
        bot_mock = MagicMock()
        sent_mock_1 = MagicMock(message_id=101)
        sent_mock_2 = MagicMock(message_id=102)
        bot_mock.send_message = AsyncMock(side_effect=[sent_mock_1, sent_mock_2, MagicMock()])
        bot_mock.pin_chat_message = AsyncMock()

        cb_confirm = MagicMock()
        cb_confirm.from_user = admin_user
        cb_confirm.message = MagicMock()
        cb_confirm.message.edit_text = AsyncMock()
        cb_confirm.answer = AsyncMock()

        await handle_admin_pin_confirm(cb_confirm, self.db, self.config, bot_mock)
        # Ждем выполнения фоновой задачи рассылки
        await asyncio.sleep(0.15)

        # Проверяем, что сообщения были отправлены обоим пользователям и закреплены
        self.assertEqual(bot_mock.send_message.call_count, 3)  # 2 пользователям + 1 админу с отчетом
        self.assertEqual(bot_mock.pin_chat_message.call_count, 2)
        pinned_chats = [c[1]["chat_id"] for c in bot_mock.pin_chat_message.call_args_list]
        self.assertIn(u1, pinned_chats)
        self.assertIn(u2, pinned_chats)

        # 6. Проверяем функцию открепления всех сообщений
        bot_mock.unpin_all_chat_messages = AsyncMock()
        cb_unpin = MagicMock()
        cb_unpin.from_user = admin_user
        cb_unpin.message = MagicMock()
        cb_unpin.message.edit_text = AsyncMock()
        cb_unpin.answer = AsyncMock()

        await handle_admin_unpin_all_confirm(cb_unpin, self.db, self.config, bot_mock)
        await asyncio.sleep(0.15)

        self.assertEqual(bot_mock.unpin_all_chat_messages.call_count, 2)
        unpinned_chats = [c[1]["chat_id"] for c in bot_mock.unpin_all_chat_messages.call_args_list]
        self.assertIn(u1, unpinned_chats)
        self.assertIn(u2, unpinned_chats)


class TestOutboxAndModeRestrictions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "test_mode_outbox_db.db"
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass
        self.db = Database(self.test_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    async def test_outbox_filtering_by_subscription_type(self):
        # 1. No subscription
        s_none = await self.db.upsert_subscriber(1001)
        # 2. Market only
        s_m = await self.db.upsert_subscriber(1002)
        await self.db.admin_grant_subscription(1002, "market", "7d")
        # 3. Upgrades only
        s_u = await self.db.upsert_subscriber(1003)
        await self.db.admin_grant_subscription(1003, "upgrades", "7d")
        # 4. Both
        s_b = await self.db.upsert_subscriber(1004)
        await self.db.admin_grant_subscription(1004, "both", "7d")

        active_subs = await self.db.get_active_subscribers()

        # Market lot
        market_eligible = [
            sub for sub in active_subs
            if sub.mode in ("market", "both") and sub.has_market_access() and sub.min_price <= 500 <= sub.max_price
        ]
        market_cids = {s.chat_id for s in market_eligible}
        self.assertIn(1002, market_cids)
        self.assertIn(1004, market_cids)
        self.assertNotIn(1001, market_cids)
        self.assertNotIn(1003, market_cids)

        # Upgrade lot
        upgrade_eligible = [
            sub for sub in active_subs
            if sub.mode in ("upgrades", "both") and sub.has_upgrades_access()
        ]
        upgrade_cids = {s.chat_id for s in upgrade_eligible}
        self.assertIn(1003, upgrade_cids)
        self.assertIn(1004, upgrade_cids)
        self.assertNotIn(1001, upgrade_cids)
        self.assertNotIn(1002, upgrade_cids)

    async def test_mode_command_blocks_unsubscribed_users(self):
        from bot import handle_mode, handle_mode_callback

        user_id = 2001
        msg = MagicMock()
        msg.chat.id = user_id
        msg.answer = AsyncMock()

        # User has no subscription
        await handle_mode(msg, self.db)
        msg.answer.assert_called_once()
        sent_text = msg.answer.call_args[0][0]
        self.assertIn("Доступ ограничен", sent_text)

        # Attempt to change mode via callback without subscription
        cb = MagicMock()
        cb.data = "set_mode:market"
        cb.from_user.id = user_id
        cb.message.chat.id = user_id
        cb.message.answer = AsyncMock()
        cb.answer = AsyncMock()

        await handle_mode_callback(cb, self.db)
        cb.answer.assert_called_with("🔒 У вас нет активной подписки! Ознакомьтесь с тарифами.", show_alert=True)

        # Grant only upgrades subscription
        await self.db.admin_grant_subscription(user_id, "upgrades", "7d")

        # Trying to select market should be blocked
        cb.data = "set_mode:market"
        await handle_mode_callback(cb, self.db)
        cb.answer.assert_called_with("⚠️ Требуется активная подписка на «Маркет»!", show_alert=True)

        # Selecting upgrades should succeed
        cb.data = "set_mode:upgrades"
        cb.message.edit_text = AsyncMock()
        await handle_mode_callback(cb, self.db)
        sub_after = await self.db.get_subscriber(user_id)
        self.assertEqual(sub_after.mode, "upgrades")

    async def test_upgrade_backdrop_filter_and_dispatch(self):
        from bot import handle_toggle_upgrade_filter
        from models import LotItem, is_black_or_onyx_backdrop, get_upgrade_filter_title

        # Check helper
        self.assertTrue(is_black_or_onyx_backdrop("Black"))
        self.assertTrue(is_black_or_onyx_backdrop("Onyx Black"))
        self.assertTrue(is_black_or_onyx_backdrop("Оникс Black"))
        self.assertFalse(is_black_or_onyx_backdrop("Neon Green"))
        self.assertFalse(is_black_or_onyx_backdrop("Sun Yellow"))
        self.assertFalse(is_black_or_onyx_backdrop(None))

        # Check titles
        self.assertIn("Black", get_upgrade_filter_title("black_onyx"))
        self.assertIn("Все фоны", get_upgrade_filter_title("all"))

        # User without upgrades subscription tries to toggle filter
        user_id = 3001
        await self.db.upsert_subscriber(user_id)
        cb = MagicMock()
        cb.data = "toggle_upgrade_filter"
        cb.from_user.id = user_id
        cb.message.chat.id = user_id
        cb.answer = AsyncMock()
        cb.message.edit_text = AsyncMock()

        await handle_toggle_upgrade_filter(cb, self.db)
        cb.answer.assert_called_with(
            "⚠️ Фильтр улучшений доступен при активной подписке на «Улучшения» или «Всё вместе»!",
            show_alert=True,
        )

        # Grant upgrades subscription
        await self.db.admin_grant_subscription(user_id, "upgrades", "7d")
        sub = await self.db.get_subscriber(user_id)
        self.assertEqual(sub.upgrade_filter, "all")
        self.assertTrue(sub.allows_upgrade_backdrop("Neon Green"))

        # Toggle to black_onyx
        await handle_toggle_upgrade_filter(cb, self.db)
        cb.answer.assert_called_with("🖤 Включен фильтр: только Black и Оникс Black")
        sub_after = await self.db.get_subscriber(user_id)
        self.assertEqual(sub_after.upgrade_filter, "black_onyx")
        self.assertTrue(sub_after.allows_upgrade_backdrop("Black"))
        self.assertTrue(sub_after.allows_upgrade_backdrop("Onyx Black"))
        self.assertFalse(sub_after.allows_upgrade_backdrop("Neon Green"))
        self.assertFalse(sub_after.allows_upgrade_backdrop("Sun Yellow"))

        # Toggle back to all
        await handle_toggle_upgrade_filter(cb, self.db)
        cb.answer.assert_called_with("🌌 Включен фильтр: все фоны улучшений (все вместе)")
        sub_after2 = await self.db.get_subscriber(user_id)
        self.assertEqual(sub_after2.upgrade_filter, "all")
        self.assertTrue(sub_after2.allows_upgrade_backdrop("Neon Green"))

        # Check eligible filtering in outbox dispatch for two users:
        # User 1: upgrade_filter = 'all'
        # User 2: upgrade_filter = 'black_onyx'
        u1 = 3010
        u2 = 3020
        await self.db.upsert_subscriber(u1)
        await self.db.admin_grant_subscription(u1, "upgrades", "7d")
        await self.db.update_subscriber_mode(u1, "upgrades")
        await self.db.update_subscriber_upgrade_filter(u1, "all")

        await self.db.upsert_subscriber(u2)
        await self.db.admin_grant_subscription(u2, "upgrades", "7d")
        await self.db.update_subscriber_mode(u2, "upgrades")
        await self.db.update_subscriber_upgrade_filter(u2, "black_onyx")

        active_subs = await self.db.get_active_subscribers()

        lot_black = LotItem(
            lot_key="u_1", gift_id=1, raw_id=1, title="Test", num=1, slug="t1",
            price_stars=100, backdrop="Black", event_type="upgrade"
        )
        lot_neon = LotItem(
            lot_key="u_2", gift_id=2, raw_id=2, title="Test", num=2, slug="t2",
            price_stars=100, backdrop="Neon Blue", event_type="upgrade"
        )

        # For lot_black: both u1 and u2 eligible
        eligible_black = [
            s for s in active_subs
            if s.chat_id in (u1, u2)
            and s.mode in ("upgrades", "both")
            and s.has_upgrades_access()
            and s.allows_upgrade_backdrop(getattr(lot_black, "backdrop", None))
        ]
        self.assertEqual({s.chat_id for s in eligible_black}, {u1, u2})

        # For lot_neon: only u1 eligible
        eligible_neon = [
            s for s in active_subs
            if s.chat_id in (u1, u2)
            and s.mode in ("upgrades", "both")
            and s.has_upgrades_access()
            and s.allows_upgrade_backdrop(getattr(lot_neon, "backdrop", None))
        ]
        self.assertEqual({s.chat_id for s in eligible_neon}, {u1})

    async def test_send_notification_rate_limit_and_429_protection(self):
        from bot import send_notification_to_user, _chat_cooldowns
        from aiogram.exceptions import TelegramRetryAfter

        bot_mock = MagicMock()
        bot_mock.send_message = AsyncMock()

        # 1. Normal send succeeds
        bot_mock.send_message.return_value = MagicMock(message_id=999)
        msg = await send_notification_to_user(bot_mock, 4001, "Hello", None, self.db)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_id, 999)

        # 2. Telegram returns 429 RetryAfter
        # send_notification_to_user should NOT raise, should NOT block for 25s,
        # but should set _chat_cooldowns[4002] and return None immediately!
        err = TelegramRetryAfter(method=MagicMock(), message="Too many requests", retry_after=25)
        bot_mock.send_message = AsyncMock(side_effect=err)

        start_t = asyncio.get_running_loop().time()
        res_429 = await send_notification_to_user(bot_mock, 4002, "Flood", None, self.db)
        duration = asyncio.get_running_loop().time() - start_t

        self.assertIsNone(res_429)
        # Verify it returned immediately without waiting 25 seconds
        self.assertLess(duration, 0.5)
        # Verify chat 4002 is on cooldown
        self.assertGreater(_chat_cooldowns.get(4002, 0.0), start_t + 20)

        # 3. Subsequent send to 4002 is immediately skipped without hitting Bot API
        bot_mock.send_message.reset_mock()
        res_skip = await send_notification_to_user(bot_mock, 4002, "Another", None, self.db)
        self.assertIsNone(res_skip)
        bot_mock.send_message.assert_not_called()

        # 4. Another user 4003 is NOT affected by 4002's cooldown!
        bot_mock.send_message = AsyncMock(return_value=MagicMock(message_id=1001))
        res_other = await send_notification_to_user(bot_mock, 4003, "For other user", None, self.db)
        self.assertIsNotNone(res_other)
        self.assertEqual(res_other.message_id, 1001)


if __name__ == "__main__":
    unittest.main()


