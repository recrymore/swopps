import argparse
import os
import signal
import subprocess
import sys
import time

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def run_single(script_name: str) -> None:
    """Запускает один скрипт напрямую в текущем процессе (интерактивный ввод работает)."""
    python_exe = sys.executable
    print(f"[*] Запуск {script_name}...")
    try:
        subprocess.run([python_exe, script_name], check=True)
    except KeyboardInterrupt:
        print(f"\n[*] {script_name} остановлен пользователем.")


def run_both() -> None:
    """Запускает userbot.py и bot.py в параллельных подпроцессах."""
    python_exe = sys.executable

    # Проверяем наличие любого активного файла сессии Telethon
    session_files = ["userbot.session", "userbot_2.session", "userbot_3.session"]
    if not any(os.path.exists(f) for f in session_files):
        print("=" * 70)
        print("ВНИМАНИЕ: Файлы сессий Telegram не обнаружены!")
        print("Для первого входа в аккаунт Telegram (ввод номера и кода авторизации)")
        print("рекомендуется сначала запустить только userbot:")
        print("    python userbot.py   ИЛИ   python run.py --userbot")
        print("После успешного входа вы сможете запускать оба процесса вместе.")
        print("=" * 70)
        time.sleep(2)

    print("[*] Запуск сервиса из двух процессов (userbot.py и bot.py)...")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    p_userbot = subprocess.Popen([python_exe, "userbot.py"], env=env)
    p_bot = subprocess.Popen([python_exe, "bot.py"], env=env)

    processes = {"Userbot": p_userbot, "Bot": p_bot}
    script_map = {"Userbot": "userbot.py", "Bot": "bot.py"}

    def _sigterm_handler(signum, frame):
        print("\n[*] Получен сигнал завершения (SIGTERM)...")
        for p in processes.values():
            if p.poll() is None:
                p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        while True:
            for name in list(processes.keys()):
                proc = processes[name]
                ret = proc.poll()
                if ret is not None:
                    if name == "Userbot" and ret == 2:
                        print("\n" + "=" * 70)
                        print("[!] ОШИБКА АВТОРИЗАЦИИ: Ни один аккаунт Telegram не авторизован!")
                        print("Для работы мониторинга требуется авторизовать аккаунты:")
                        print("  1) По номеру телефона: py auth_account.py 1 (и py auth_account.py 2)")
                        print("  2) Или через QR-код:   py login_qr.py 1   (и py login_qr.py 2)")
                        print("После успешного входа перезапустите сервис: py run.py")
                        print("=" * 70)
                        for p in processes.values():
                            if p.poll() is None:
                                p.terminate()
                        return
                    print(
                        f"[!] Процесс {name} неожиданно завершился с кодом {ret}. "
                        f"Автоматический перезапуск через 3 сек..."
                    )
                    time.sleep(3)
                    processes[name] = subprocess.Popen(
                        [python_exe, script_map[name]], env=env
                    )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Получен сигнал остановки. Завершение обоих процессов...")
        for name, proc in processes.items():
            if proc.poll() is None:
                proc.terminate()
        for name, proc in processes.items():
            proc.wait()
        print("[*] Сервис успешно остановлен.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Запуск сервиса мониторинга подарков Telegram"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--qr",
        nargs="?",
        const=1,
        type=int,
        metavar="ACCOUNT_NUM",
        help="Войти в Telegram через QR-код (без смс и кодов). Пример: --qr 1 или --qr 2",
    )
    group.add_argument(
        "--userbot",
        action="store_true",
        help="Запустить только userbot.py (для первого входа по номеру)",
    )
    group.add_argument(
        "--bot",
        action="store_true",
        help="Запустить только публичного бота bot.py",
    )
    group.add_argument(
        "--auth",
        type=int,
        metavar="ACCOUNT_NUM",
        help="Авторизовать конкретный аккаунт из пула (например: --auth 2)",
    )

    args = parser.parse_args()

    if args.qr is not None:
        python_exe = sys.executable
        subprocess.run([python_exe, "login_qr.py", str(args.qr)])
    elif args.userbot:
        run_single("userbot.py")
    elif args.bot:
        run_single("bot.py")
    elif args.auth is not None:
        python_exe = sys.executable
        subprocess.run([python_exe, "auth_account.py", str(args.auth)])
    else:
        run_both()


if __name__ == "__main__":
    main()
