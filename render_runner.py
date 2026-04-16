import os
import subprocess
import threading
import sys
import time

def run_bot():
    """Run Telegram bot in background thread, ignore failures"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or len(token) < 10:
        print("--- Telegram Bot token not configured, skipping ---")
        return
    
    print("--- Launching Telegram Bot (background) ---")
    while True:
        try:
            proc = subprocess.Popen([sys.executable, "telegram_bot.py"])
            proc.wait()
            print("Telegram Bot exited, restarting in 30s...")
        except Exception as e:
            print(f"Telegram Bot error: {e}")
        time.sleep(30)

def run():
    print("Starting Main Process (Bot + WebApp)...")
    
    port = os.environ.get("PORT", "7860")
    print(f"Server will listen on PORT: {port}")

    os.environ["WEBAPP_PORT"] = port
    os.environ["WEBAPP_HOST"] = "0.0.0.0"

    # Start bot in background thread (non-blocking)
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Run webapp directly in main process so Hugging Face sees the port
    print("--- Launching Web App (FastAPI) ---")
    os.execv(sys.executable, [sys.executable, "webapp_server.py"])

if __name__ == "__main__":
    run()
