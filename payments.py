import asyncio
import base64
import decimal
from decimal import Decimal, ROUND_UP
import logging
import math
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import aiohttp

logger = logging.getLogger("payments")

# Официальный мастер-контракт USDT в сети TON
DEFAULT_USDT_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"


def normalize_ton_address(addr: Optional[str]) -> Optional[str]:
    """
    Приводит любой адрес TON (bounceable EQ..., non-bounceable UQ..., raw 0:hex, testnet)
    к каноническому формату 'workchain:64_hex_lowercase'.
    """
    if not addr or not isinstance(addr, str):
        return None
    addr = addr.strip()
    if not addr:
        return None

    # Уже в raw-формате workchain:hex
    if ":" in addr:
        parts = addr.split(":", 1)
        try:
            wc = int(parts[0])
            hex_part = parts[1].lower().zfill(64)
            if len(hex_part) == 64:
                return f"{wc}:{hex_part}"
        except ValueError:
            pass

    # Base64url декодирование (EQ / UQ / etc.)
    try:
        s = addr.replace("-", "+").replace("_", "/")
        pad = len(s) % 4
        if pad:
            s += "=" * (4 - pad)
        raw = base64.b64decode(s)
        # 1 byte flags + 1 byte workchain + 32 bytes account + 2 bytes crc
        if len(raw) == 36:
            wc = raw[1]
            if wc > 127:
                wc = wc - 256
            account_hex = raw[2:34].hex().lower()
            return f"{wc}:{account_hex}"
    except Exception:
        pass

    return addr.lower()


def is_same_address(addr1: Optional[str], addr2: Optional[str]) -> bool:
    """Сравнивает два TON-адреса с учётом всех возможных форматов (EQ/UQ/raw)."""
    if not addr1 or not addr2:
        return False
    norm1 = normalize_ton_address(addr1)
    norm2 = normalize_ton_address(addr2)
    if norm1 and norm2:
        return norm1 == norm2
    return addr1.strip().lower() == addr2.strip().lower()


def is_valid_ton_address(addr: str) -> bool:
    """Проверяет валидность строки адреса TON."""
    return normalize_ton_address(addr) is not None


def generate_order_comment() -> str:
    """Генерирует уникальный комментарий для заявки вида 'gp-xxxxxx'."""
    code = secrets.token_hex(3)  # ровно 6 hex-символов
    return f"gp-{code}"


def parse_comment_from_payload(payload_str_or_hex: str) -> str:
    """Извлекает текстовый комментарий из forward_payload или hex."""
    if not payload_str_or_hex:
        return ""
    s = payload_str_or_hex.strip()
    # Если строка уже читаемый текст
    if s.startswith("gp-"):
        return s
    # Если это hex (например 0000000067702d...)
    try:
        raw = bytes.fromhex(s)
        if raw.startswith(b"\x00\x00\x00\x00"):
            return raw[4:].decode("utf-8", errors="ignore").strip()
        decoded = raw.decode("utf-8", errors="ignore").strip()
        if decoded.startswith("gp-"):
            return decoded
    except Exception:
        pass
    return s


@dataclass
class TransferItem:
    tx_hash: str
    currency: str  # 'TON' или 'USDT'
    amount: int  # в минимальных единицах: нанотоны (1e9) или микро-USDT (1e6)
    comment: str
    sender: Optional[str] = None
    recipient: Optional[str] = None


class CurrencyConverter:
    """
    Получение и кэширование курса TON/USDT с автоматическим переключением источников
    (TonAPI -> Binance) и расчет цен в USDT с округлением вверх до 2 знаков.
    """

    def __init__(self, cache_ttl: float = 60.0, max_stale_seconds: float = 900.0):
        self._rate: Optional[float] = None
        self._last_updated: float = 0.0
        self._cache_ttl = cache_ttl
        self._max_stale = max_stale_seconds  # 15 минут

    async def get_ton_usdt_rate(
        self, session: Optional[aiohttp.ClientSession] = None
    ) -> Optional[float]:
        now = time.time()
        # Если кэш свежий (< 60 сек), отдаем из памяти
        if self._rate is not None and (now - self._last_updated) < self._cache_ttl:
            return self._rate

        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True

        rate = None
        try:
            # 1. Основной источник: TonAPI
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                async with session.get(
                    "https://tonapi.io/v2/rates?tokens=ton&currencies=usd",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        val = (
                            data.get("rates", {})
                            .get("TON", {})
                            .get("prices", {})
                            .get("USD")
                        )
                        if val and float(val) > 0:
                            rate = float(val)
                            logger.debug(f"Курс TON/USDT получен из TonAPI: {rate}")
            except Exception as e:
                logger.debug(f"Ошибка запроса курса TonAPI: {e}")

            # 2. Резервный источник: Binance TONUSDT
            if rate is None:
                try:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    async with session.get(
                        "https://api.binance.com/api/v3/ticker/price?symbol=TONUSDT",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            p = data.get("price")
                            if p and float(p) > 0:
                                rate = float(p)
                                logger.debug(f"Курс TON/USDT получен из Binance: {rate}")
                except Exception as e:
                    logger.debug(f"Ошибка запроса курса Binance: {e}")

            if rate is not None:
                self._rate = rate
                self._last_updated = now
                return self._rate

            # Если оба источника недоступны, проверяем возраст последнего кэша
            if self._rate is not None and (now - self._last_updated) <= self._max_stale:
                logger.warning(
                    f"Курс TON/USDT устарел ({int(now - self._last_updated)} сек), используется последний кэш: {self._rate}"
                )
                return self._rate

            logger.error("Не удалось получить курс TON/USDT (оба источника недоступны, кэш старше 15 минут)")
            return None

        finally:
            if close_session:
                await session.close()

    def calculate_usdt_amount(
        self, price_ton: float, rate: float
    ) -> Tuple[float, int]:
        """
        Считает цену в USDT:
        цена в TON * текущий курс TON/USDT, округление вверх до 2 знаков (ROUND_UP).
        Возвращает (usdt_float, micro_usdt_int).
        """
        price_ton_dec = Decimal(str(price_ton))
        rate_dec = Decimal(str(rate))
        usdt_dec = (price_ton_dec * rate_dec).quantize(
            Decimal("0.01"), rounding=ROUND_UP
        )
        micro_usdt = int(usdt_dec * Decimal("1000000"))
        return float(usdt_dec), micro_usdt

    def calculate_ton_amount(self, price_ton: float) -> int:
        """Переводит TON во флоат/строке в нанотоны (целые 10^9)."""
        price_dec = Decimal(str(price_ton))
        return int(price_dec * Decimal("1000000000"))


class PaymentVerifier:
    """
    Модуль взаимодействия с блокчейн API для проверки входящих платежей TON и USDT.
    """

    def __init__(
        self,
        owner_address: str,
        ton_network: str = "mainnet",
        tonapi_key: Optional[str] = None,
        usdt_master: str = DEFAULT_USDT_MASTER,
    ):
        self.owner_address = owner_address
        self.ton_network = ton_network.lower()
        self.tonapi_key = tonapi_key
        self.usdt_master = usdt_master
        self.base_url = (
            "https://testnet.tonapi.io/v2"
            if self.ton_network == "testnet"
            else "https://tonapi.io/v2"
        )
        self.toncenter_base_url = (
            "https://testnet.toncenter.com/api/v3"
            if self.ton_network == "testnet"
            else "https://toncenter.com/api/v3"
        )

    async def fetch_incoming_transfers(
        self, session: Optional[aiohttp.ClientSession] = None
    ) -> List[TransferItem]:
        """
        Запрашивает последние входящие события/транзакции для OWNER_ADDRESS
        через TonAPI (с fallback на Toncenter v3).
        Возвращает список TransferItem с проверенным мастер-контрактом для USDT.
        """
        if not self.owner_address:
            return []

        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True

        transfers: List[TransferItem] = []
        try:
            # 1. Попытка через TonAPI Events
            url = f"{self.base_url}/accounts/{self.owner_address}/events?limit=30"
            headers = {"User-Agent": "TelegramBot/1.0"}
            if self.tonapi_key:
                headers["Authorization"] = f"Bearer {self.tonapi_key}"

            try:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        events = data.get("events", [])
                        for event in events:
                            if event.get("in_progress", False):
                                continue
                            event_id = event.get("event_id") or ""
                            actions = event.get("actions", [])
                            for action in actions:
                                if action.get("status") != "ok":
                                    continue
                                act_type = action.get("type")

                                # TON Transfer
                                if act_type == "TonTransfer":
                                    tt = action.get("TonTransfer", {})
                                    recipient = tt.get("recipient", {}).get("address")
                                    if not is_same_address(recipient, self.owner_address):
                                        continue
                                    amount = int(tt.get("amount", 0))
                                    comment = str(tt.get("comment") or "").strip()
                                    sender = tt.get("sender", {}).get("address")
                                    tx_id = f"{event_id}:ton:{amount}" if event_id else f"ton_{amount}_{int(time.time())}"
                                    transfers.append(
                                        TransferItem(
                                            tx_hash=event_id or tx_id,
                                            currency="TON",
                                            amount=amount,
                                            comment=comment,
                                            sender=sender,
                                            recipient=recipient,
                                        )
                                    )

                                # Jetton Transfer (USDT)
                                elif act_type == "JettonTransfer":
                                    jt = action.get("JettonTransfer", {})
                                    recipient = jt.get("recipient", {}).get("address")
                                    if not is_same_address(recipient, self.owner_address):
                                        continue

                                    # Строгая проверка мастер-контракта: EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs
                                    jetton_addr = jt.get("jetton", {}).get("address")
                                    if not is_same_address(jetton_addr, self.usdt_master):
                                        logger.warning(
                                            f"Игнорирован Jetton-перевод с неизвестным мастер-контрактом: {jetton_addr}"
                                        )
                                        continue

                                    try:
                                        amount = int(jt.get("amount", 0))
                                    except (ValueError, TypeError):
                                        amount = 0

                                    raw_comment = jt.get("comment") or jt.get("forward_payload") or ""
                                    comment = parse_comment_from_payload(str(raw_comment))
                                    sender = jt.get("sender", {}).get("address")
                                    tx_id = f"{event_id}:usdt:{amount}" if event_id else f"usdt_{amount}_{int(time.time())}"
                                    transfers.append(
                                        TransferItem(
                                            tx_hash=event_id or tx_id,
                                            currency="USDT",
                                            amount=amount,
                                            comment=comment,
                                            sender=sender,
                                            recipient=recipient,
                                        )
                                    )

                        return transfers
            except Exception as e:
                logger.debug(f"Ошибка запроса TonAPI events: {e}")

            # 2. Fallback: Toncenter v3
            try:
                tc_url = f"{self.toncenter_base_url}/transactions?account={self.owner_address}&limit=25"
                async with session.get(
                    tc_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        tc_data = await resp.json()
                        for tx in tc_data.get("transactions", []):
                            tx_hash = tx.get("hash") or ""
                            in_msg = tx.get("in_msg", {})
                            if not in_msg:
                                continue
                            dest = in_msg.get("destination")
                            if not is_same_address(dest, self.owner_address):
                                continue

                            # Текстовый комментарий
                            content = in_msg.get("message_content", {})
                            decoded = content.get("decoded", {})
                            comment = ""
                            if isinstance(decoded, dict):
                                comment = decoded.get("comment", "")
                            elif in_msg.get("body"):
                                comment = parse_comment_from_payload(in_msg.get("body"))

                            val = int(in_msg.get("value", 0))
                            if val > 0:
                                transfers.append(
                                    TransferItem(
                                        tx_hash=tx_hash,
                                        currency="TON",
                                        amount=val,
                                        comment=comment.strip(),
                                        sender=in_msg.get("source"),
                                        recipient=dest,
                                    )
                                )
            except Exception as tc_err:
                logger.debug(f"Ошибка fallback Toncenter: {tc_err}")

            return transfers

        finally:
            if close_session:
                await session.close()
