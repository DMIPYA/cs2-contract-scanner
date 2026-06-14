# Crafty — CS2 Trade-Up Contract Scanner

Crafty is a Telegram bot + Mini App that scans thousands of CS2 trade-up combinations to find profitable contracts. It calculates expected value (EV), ROI, outcome probabilities, float optimization, and auto-detects jackpot opportunities.

## Features

- **Contract Hunting** — Scans all possible trades-up across collections, ranks them by ROI / EV / EV-per-cost
- **Contract Details** — Full breakdown: outcome probabilities, float ranges, wear leaps, price estimates
- **Simulated Crafting** — Roll the dice with an actual float simulation
- **Jackpot Detection** — Flags high-variance contracts where one top outcome alone justifies the risk
- **Float Optimization** — Picks input floats to hit the target wear while minimizing cost
- **Multi-Source Pricing** — Market.CSGO, CSFloat, DMarket with configurable fallback chain
- **Bid Mode** — Finds contracts profitable via buy orders instead of direct purchases
- **Favorites** — Per-user saved contracts (server-side)
- **Telegram Mini App** — Web UI embedded inside Telegram for interactive browsing

## Tech Stack

- **Python 3.11+** — core language
- **python-telegram-bot** — Telegram bot framework
- **FastAPI + Uvicorn** — web server for the Mini App
- **Market.CSGO / CSFloat / DMarket APIs** — price data
- **Docker** — deployment

## Quick Start

### Prerequisites

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- API keys for at least one price source

### Setup

```bash
git clone https://github.com/DMIPYA/crafty
cd crafty
cp .env.example .env
```

Edit `.env` with your keys:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `MARKET_API_KEY` | Market.CSGO API key |
| `CSFLOAT_API_KEY` | CSFloat API key |
| `DMARKET_PUBLIC_KEY` | DMarket public key |
| `DMARKET_SECRET_KEY` | DMarket secret key |
| `WEBAPP_URL` | Public HTTPS URL for the Mini App |

### Install & Run

```bash
pip install -r requirements.txt
python telegram_bot.py        # Telegram bot
# or
python webapp_server.py       # Web app only
# or
python render_runner.py       # Both together
```

### Docker

```bash
docker build -t crafty .
docker run -p 7860:7860 crafty
```

### Mini App

In @BotFather → Bot Settings → Mini App, set the URL to your `WEBAPP_URL`.

## Project Structure

```
├── telegram_bot.py          # Telegram bot entry point
├── webapp_server.py         # FastAPI web server
├── bot_service.py           # Core orchestration service
├── calculator.py            # Trade-up math (EV, probabilities, float)
├── calculator_price_lookup.py  # Price lookup for calculator
├── api_client.py            # Price API clients + caching
├── database.py              # CS2 collections & skins database
├── render_runner.py         # Launches bot + web app together
├── index.html               # Mini App frontend
├── collections.json         # CS2 collection data
├── skins.json               # CS2 skin data
├── .env.example             # Environment variable template
├── Dockerfile               # Docker build
└── requirements.txt         # Python dependencies
```

## License

MIT
