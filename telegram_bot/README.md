# Persian Telegram Paper Trading Bot

This bot is paper-trading only. It reads public market data from CoinGecko,
calculates EMA, RSI, MACD, trend strength, volume ratio, and a confidence
score, then evaluates enabled paper accounts every 15 minutes.

The bot never connects to an exchange and never submits real orders.

## Commands

- `/paper_on` activates a virtual account with a 10,000 USD balance.
- `/coin BTC` selects the asset to analyze.
- `/auto_on` and `/auto_off` control automatic trading.
- `/status` shows the account and open paper position.
- `/history` shows completed paper trades.
- `/help` shows the Persian command guide.