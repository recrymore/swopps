import argparse
import asyncio
import json
import os
import sys

from telethon import TelegramClient, errors
from config import load_config

# Ensure UTF-8 output
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

STATE_FILE = "auth_state.json"


async def request_code(phone: str) -> None:
    config = load_config()
    client = TelegramClient(config.session_name, config.telegram_api_id, config.telegram_api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"ALREADY_AUTHORIZED:{me.first_name}:{me.id}")
        await client.disconnect()
        return

    try:
        sent = await client.send_code_request(phone)
        state = {
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "type": str(type(sent.type).__name__),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
        print(f"CODE_SENT:{sent.phone_code_hash}:TYPE={type(sent.type).__name__}:TIMEOUT={getattr(sent, 'timeout', None)}")
    except errors.FloodWaitError as e:
        print(f"FLOOD_WAIT:{e.seconds}")
    except Exception as e:
        print(f"ERROR:{e}")
    finally:
        await client.disconnect()


async def verify_code(code: str, password: str | None = None) -> None:
    if not os.path.exists(STATE_FILE):
        print("ERROR:state_not_found")
        return

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    phone = state["phone"]
    phone_code_hash = state["phone_code_hash"]

    config = load_config()
    client = TelegramClient(config.session_name, config.telegram_api_id, config.telegram_api_hash)
    await client.connect()

    try:
        try:
            user = await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            print(f"SUCCESS:{user.first_name}:{user.id}")
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except errors.SessionPasswordNeededError:
            if password:
                user = await client.sign_in(password=password)
                print(f"SUCCESS:{user.first_name}:{user.id}")
                if os.path.exists(STATE_FILE):
                    os.remove(STATE_FILE)
            else:
                print("PASSWORD_NEEDED")
    except errors.PhoneCodeInvalidError:
        print("ERROR:invalid_code")
    except errors.PhoneCodeExpiredError:
        print("ERROR:code_expired")
    except errors.PasswordHashInvalidError:
        print("ERROR:invalid_password")
    except Exception as e:
        print(f"ERROR:{e}")
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", type=str)
    parser.add_argument("--code", type=str)
    parser.add_argument("--password", type=str)
    args = parser.parse_args()

    if args.phone:
        asyncio.run(request_code(args.phone))
    elif args.code:
        asyncio.run(verify_code(args.code, args.password))
    else:
        print("ERROR:no_action")


if __name__ == "__main__":
    main()
