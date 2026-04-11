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
    
    print("--- Launching Telegram Bot ---")
    bot_proc = subprocess.Popen([sys.executable, "telegram_bot.py"])

    try:
        # Keep the main process alive until one of them dies
        while True:
            if web_proc.poll() is not None:
                print("Web App process died. Exiting...")
                break
            if bot_proc.poll() is not None:
                print("Telegram Bot process died. Exiting...")
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("Stopping processes...")
    finally:
        web_proc.terminate()
        bot_proc.terminate()

if __name__ == "__main__":
    run()
