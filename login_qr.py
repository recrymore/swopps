import asyncio
import datetime
import os
import sys
import qrcode
from telethon import TelegramClient, errors, functions, types
from config import load_config
from auth_account import get_client_profile


def print_ascii_qr(url: str) -> None:
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        print("\n" + "=" * 54)
        qr.print_ascii(invert=True)
        print("=" * 54 + "\n")
    except Exception:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        matrix = qr.get_matrix()
        print("\n" + "=" * 54)
        for row in matrix:
            line = "".join("##" if col else "  " for col in row)
            print(line)
        print("=" * 54 + "\n")


async def active_qr_login(idx: int = 4):
    config = load_config()
    acc = config.accounts[idx - 1]
    profile = get_client_profile(idx)

    print("=" * 65)
    print(f" АВТОРИЗАЦИЯ В TELEGRAM ЧЕРЕЗ QR-КОД — АККАУНТ #{idx} ({acc.session_name})")
    print(f" API ID: {acc.api_id}")
    print(f" Клиент: {profile['device_model']} ({profile['app_version']}, {profile['system_version']})")
    print("=" * 65)

    # 1. Проверяем существующую сессию: если уже авторизована — просто выходим
    if os.path.exists(f"{acc.session_name}.session"):
        try:
            test_c = TelegramClient(
                acc.session_name,
                acc.api_id,
                acc.api_hash,
                device_model=profile["device_model"],
                system_version=profile["system_version"],
                app_version=profile["app_version"],
                lang_code=profile["lang_code"],
                system_lang_code=profile["system_lang_code"],
            )
            await asyncio.wait_for(test_c.connect(), timeout=7)
            if await test_c.is_user_authorized():
                me = await test_c.get_me()
                print(f"\n[+] Аккаунт #{idx} '{acc.session_name}' уже успешно авторизован!")
                print(f"    Пользователь: {me.first_name} (@{getattr(me, 'username', 'нет юзернейма')}), ID: {me.id}\n")
                await test_c.disconnect()
                return
            await test_c.disconnect()
        except Exception:
            pass

    # 2. Если сессия не авторизована или битая — удаляем старый файл сессии для чистого старта
    for suffix in ["", "-journal"]:
        p = f"{acc.session_name}.session{suffix}"
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    # 3. Подключаемся с чистого листа
    print("[*] Подключение к серверам Telegram...")
    client = TelegramClient(
        acc.session_name,
        acc.api_id,
        acc.api_hash,
        device_model=profile["device_model"],
        system_version=profile["system_version"],
        app_version=profile["app_version"],
        lang_code=profile["lang_code"],
        system_lang_code=profile["system_lang_code"],
    )
    await client.connect()

    while True:
        try:
            qr = await client.qr_login()
        except errors.SessionPasswordNeededError:
            print("\n[!] На аккаунте требуется двухфакторный пароль (2FA).")
            pwd = input("Введите ваш 2FA пароль: ")
            user = await client.sign_in(password=pwd)
            print(f"\n[🎉] УСПЕШНЫЙ ВХОД! Аккаунт #{idx} авторизован: {user.first_name} (ID: {user.id})")
            print(f"[+] Файл сессии {acc.session_name}.session успешно создан и сохранён!\n")
            await client.disconnect()
            return

        token_url = qr.url
        print("\nИнструкция:")
        print("1. Откройте Telegram на телефоне: Настройки -> Устройства -> Подключить устройство")
        print("2. Наведите камеру на QR-код ниже и подтвердите вход на телефоне:")
        print_ascii_qr(token_url)
        print(f"Прямая ссылка: {token_url}")
        print("[*] Ожидание подтверждения на телефоне...")

        login_success = False

        # Активный опрос подтверждения с поддержкой миграции между дата-центрами
        for _ in range(35):
            await asyncio.sleep(1.2)
            try:
                poll_resp = await client(qr._request)
                if isinstance(poll_resp, types.auth.LoginTokenSuccess):
                    user = poll_resp.authorization.user
                    await client._on_login(user)
                    client.session.save()
                    print(f"\n[🎉] УСПЕШНЫЙ ВХОД! Аккаунт #{idx} авторизован: {user.first_name} (ID: {user.id})")
                    print(f"[+] Файл сессии {acc.session_name}.session создан и сохранён!\n")
                    login_success = True
                    break
                elif isinstance(poll_resp, types.auth.LoginTokenMigrateTo):
                    print(f"[*] Миграция аккаунта на дата-центр DC {poll_resp.dc_id}...")
                    await client._switch_dc(poll_resp.dc_id)
                    try:
                        import_resp = await client(functions.auth.ImportLoginTokenRequest(poll_resp.token))
                        if isinstance(import_resp, types.auth.LoginTokenSuccess):
                            user = import_resp.authorization.user
                            await client._on_login(user)
                            client.session.save()
                            print(f"\n[🎉] УСПЕШНЫЙ ВХОД! Аккаунт #{idx} авторизован: {user.first_name} (ID: {user.id})")
                            print(f"[+] Файл сессии {acc.session_name}.session создан и сохранён!\n")
                            login_success = True
                            break
                    except errors.SessionPasswordNeededError:
                        print("\n[!] На аккаунте включён 2FA (облачный пароль).")
                        pwd = input("Введите ваш 2FA пароль: ")
                        user = await client.sign_in(password=pwd)
                        client.session.save()
                        print(f"\n[🎉] УСПЕШНЫЙ ВХОД! Аккаунт #{idx} авторизован: {user.first_name} (ID: {user.id})")
                        print(f"[+] Файл сессии {acc.session_name}.session создан и сохранён!\n")
                        login_success = True
                        break
            except errors.SessionPasswordNeededError:
                print("\n[!] На аккаунте включён 2FA (облачный пароль).")
                pwd = input("Введите ваш 2FA пароль: ")
                user = await client.sign_in(password=pwd)
                client.session.save()
                print(f"\n[🎉] УСПЕШНЫЙ ВХОД! Аккаунт #{idx} авторизован: {user.first_name} (ID: {user.id})")
                print(f"[+] Файл сессии {acc.session_name}.session создан и сохранён!\n")
                login_success = True
                break
            except Exception:
                pass

        if login_success:
            try:
                import subprocess
                if os.name == "nt":
                    subprocess.run(["scp", f"{acc.session_name}.session", f"bobr@192.168.0.3:~/jolly-raman/{acc.session_name}.session"], check=False)
                    print(f"[+] Файл сессии автоматически скопирован на боевой сервер bobr@192.168.0.3!\n")
            except Exception:
                pass
            break

        print("\n[*] Токен QR-кода истёк (~35 сек). Автоматически обновляем QR-код...")

    await client.disconnect()


if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 4
    try:
        asyncio.run(active_qr_login(target))
    except (KeyboardInterrupt, SystemExit):
        print("\n[*] Процесс входа прерван пользователем.")
