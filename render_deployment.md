# Render.com Project Structure
# -------------------------
# 1. Main Bot (Telegram): telegram_bot.py
# 2. Mini App API/Web: webapp_server.py
# -------------------------

# Option A: Run BOTH in one Service (Simplest for Free Tier)
# Build Command: pip install -r requirements.txt
# Start Command: python webapp_server.py & python telegram_bot.py

# Option B: Run separate Web Service (webapp_server.py) and Background Worker (telegram_bot.py)
# -------------------------

# Python requirements:
fastapi
uvicorn[standard]
python-telegram-bot
httpx
python-dotenv
# Add any existing requirements from requirements.txt here
