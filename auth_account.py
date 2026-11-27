import asyncio
import os
import sys

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from telethon import TelegramClient
from config import load_config

DEVICE_PROFILES = [
    {
        "device_model": "Desktop",
        "system_version": "Windows 11 Home 64bit",
        "app_version": "5.6.3 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "Desktop",
        "system_version": "Windows 10 Pro 64bit",
        "app_version": "5.6.1 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "Desktop",
        "system_version": "Windows 11 Pro 64bit",
        "app_version": "5.5.5 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "Desktop",
        "system_version": "Windows 10 Enterprise 64bit",
        "app_version": "5.6.0 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
]

def get_client_profile(idx: int) -> dict:
    return DEVICE_PROFILES[(idx - 1) % len(DEVICE_PROFILES)]


async def auth_account(session_name: str, api_id: int, api_hash: str, idx: int = 1) -> bool:
    print("=" * 60)
    print(f"[*] Авторизация аккаунта #{idx}: {session_name}")
    print(f"    API ID: {api_id}")
    profile = get_client_profile(idx)
    print(f"    Клиент: {profile['device_model']} ({profile['app_version']}, {profile['system_version']})")
    print("=" * 60)

    client = TelegramClient(
        session_name,
        api_id,
        api_hash,
        device_model=profile["device_model"],
        system_version=profile["system_version"],
        app_version=profile["app_version"],
        lang_code=profile["lang_code"],
        system_lang_code=profile["system_lang_code"],
    )
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[+] Аккаунт '{session_name}' уже авторизован!")
        print(f"    Пользователь: {me.first_name} (@{me.username or 'без_юзернейма'}), ID: {me.id}\n")
        await client.disconnect()
        return True

    print(f"[!] Аккаунт '{session_name}' не авторизован. Запуск процесса входа...")
    try:
        def _get_phone():
            val = input("Введите номер телефона аккаунта (например, +79991234567): ").strip()
            return val

        def _get_code():
            val = input("Введите код подтверждения из Telegram: ").strip()
            return val

        def _get_password():
            val = input("Введите ваш 2FA пароль (если не включён, нажмите Enter): ").strip()
            return val

        await client.start(
            phone=_get_phone,
            code_callback=_get_code,
            password=_get_password,
        )
        print("[*] Завершение авторизации и получение данных профиля...")
        me = await client.get_me()
        print(f"\n[+] УСПЕШНО! Аккаунт '{session_name}' авторизован!")
        print(f"    Пользователь: {getattr(me, 'first_name', '')} (@{getattr(me, 'username', '') or 'без_юзернейма'}), ID: {me.id}")
        print(f"    Файл сессии сохранён: {session_name}.session\n")
        await client.disconnect()
        return True
    except Exception as e:
        print(f"[-] Ошибка авторизации {session_name}: {e}\n")
        await client.disconnect()
        return False


async def main():
    config = load_config()
    if not config.accounts:
        print("[-] В .env не найдено ни одного аккаунта Telegram (TELEGRAM_API_ID / TELEGRAM_API_HASH)")
        return

    target_idx = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        target_idx = int(sys.argv[1])

    if target_idx is not None:
        if target_idx < 1 or target_idx > len(config.accounts):
            print(f"[-] Неверный номер аккаунта: {target_idx}. Всего настроено: {len(config.accounts)}")
            return
        acc = config.accounts[target_idx - 1]
        await auth_account(acc.session_name, acc.api_id, acc.api_hash, idx=target_idx)
    else:
        print(f"[*] Найдено аккаунтов в конфигурации: {len(config.accounts)}")
        for i, acc in enumerate(config.accounts, start=1):
            print(f"\n--- Проверка аккаунта #{i} ({acc.session_name}) ---")
            await auth_account(acc.session_name, acc.api_id, acc.api_hash, idx=i)


if __name__ == "__main__":
    asyncio.run(main())
