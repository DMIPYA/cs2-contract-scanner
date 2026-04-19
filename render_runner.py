import os
import subprocess
import time
import sys

BOT_RESTART_DELAY = 15   # seconds to wait before restarting bot after ConflictError
BOT_MAX_RESTARTS = 10    # give up after this many restarts in a row
CHECK_INTERVAL = 5


def run():
    print("Starting Main Process (Bot + WebApp)...")

    port = os.environ.get("PORT", "10000")
    print(f"Server will listen on PORT: {port}")

    os.environ["WEBAPP_PORT"] = port
    os.environ["WEBAPP_HOST"] = "0.0.0.0"

    print("--- Launching Web App (FastAPI) ---")
    web_proc = subprocess.Popen([sys.executable, "webapp_server.py"])

    # Wait for web server to bind
    time.sleep(5)

    print("--- Launching Telegram Bot ---")
    bot_proc = subprocess.Popen([sys.executable, "telegram_bot.py"])

    bot_restarts = 0

    try:
        while True:
            time.sleep(CHECK_INTERVAL)

            # Web app died — nothing to do without it, exit
            if web_proc.poll() is not None:
                print(f"Web App process died (exit={web_proc.returncode}). Exiting.")
                break

            # Bot died — try to restart unless too many failures
            if bot_proc.poll() is not None:
                code = bot_proc.returncode
                bot_restarts += 1
                print(f"Telegram Bot process died (exit={code}, restart #{bot_restarts}).")

                if bot_restarts > BOT_MAX_RESTARTS:
                    print("Too many bot restarts. Exiting.")
                    break

                # ConflictError causes sys.exit(1) — wait longer so old instance releases the token
                delay = BOT_RESTART_DELAY if code == 1 else 5
                print(f"Restarting bot in {delay}s...")
                time.sleep(delay)

                bot_proc = subprocess.Popen([sys.executable, "telegram_bot.py"])
                print("Bot restarted.")
            else:
                # Bot is alive — reset restart counter
                bot_restarts = 0

    except KeyboardInterrupt:
        print("Stopping processes...")
    finally:
        try:
            web_proc.terminate()
        except Exception:
            pass
        try:
            bot_proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    run()
