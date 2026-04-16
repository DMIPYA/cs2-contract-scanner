import os
import subprocess
import time
import sys

def run():
    print("Starting Main Process (Bot + WebApp)...")
    
    # Render provides PORT variable
    port = os.environ.get("PORT", "10000")
    print(f"Server will listen on PORT: {port}")

    # Set webapp host/port via env for webapp_server.py
    os.environ["WEBAPP_PORT"] = port
    os.environ["WEBAPP_HOST"] = "0.0.0.0"

    print("--- Launching Web App (FastAPI) ---")
    # Using uvicorn directly if webapp_server has an 'app' object
    # Or just run the script if it starts uvicorn internally
    # Let's run webapp_server.py and telegram_bot.py as subprocesses
    
    web_proc = subprocess.Popen([sys.executable, "webapp_server.py"])
    
    # Wait a bit for the web server to bind to the port
    time.sleep(5)
    
    # Check if Telegram token is valid before starting bot
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    bot_proc = None
    
    if telegram_token and len(telegram_token) > 10:
        print("--- Launching Telegram Bot ---")
        try:
            bot_proc = subprocess.Popen([sys.executable, "telegram_bot.py"])
        except Exception as e:
            print(f"Failed to start Telegram Bot: {e}")
            print("Continuing with Web App only...")
    else:
        print("--- Telegram Bot token not configured, skipping bot ---")
        print("Web App will run standalone")

    try:
        # Keep the main process alive
        # If bot fails, continue with web app only
        while True:
            if web_proc.poll() is not None:
                print("Web App process died. Exiting...")
                break
            
            # Check bot status but don't exit if it dies
            if bot_proc and bot_proc.poll() is not None:
                print("Telegram Bot process died. Continuing with Web App only...")
                bot_proc = None  # Clear reference
                
            time.sleep(5)
    except KeyboardInterrupt:
        print("Stopping processes...")
    finally:
        web_proc.terminate()
        if bot_proc:
            bot_proc.terminate()

if __name__ == "__main__":
    run()
