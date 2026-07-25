# Persian Crypto Paper Trader

یک ربات تلگرام فارسی برای تحلیل بازار و معاملات کاملاً آزمایشی (Paper Trading).

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `python3 telegram_bot/telegram_bot.py` — run the Telegram paper-trading bot
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `telegram_bot/telegram_bot.py` — Telegram polling, Persian commands, and 15-minute scheduler.
- `telegram_bot/paper_trading.py` — SQLite store, indicators, risk management, and automatic engine.
- `telegram_bot/paper_trading.db` — local runtime state (ignored from version control).

## Architecture decisions

- Market data is read from CoinGecko's public market-data API; no exchange API or order endpoint is used.
- SQLite stores accounts, the single open position per user, and completed trade history.
- Automatic evaluation runs in a background thread every 15 minutes while Telegram long polling runs in the main thread.
- All Telegram-facing text is Persian; source code identifiers and documentation remain English.

## Product

- Persian Telegram commands for activating paper accounts, selecting supported coins, and toggling automatic trading.
- EMA, RSI, MACD, trend strength, volume ratio, and confidence-score based entries.
- Automatic stop-loss, take-profit, exit-condition, and strong-trend-reversal handling with immediate notifications.

## User preferences

- Keep all code in English.
- Keep every message shown to Telegram users completely in Persian.
- Keep the system Paper Trading only; do not connect to exchanges or use real money.

## Gotchas

- `TELEGRAM_BOT_TOKEN` must be stored as a secret before the bot workflow can start.
- CoinGecko market data can be temporarily rate-limited; the automatic cycle logs the failure and retries at the next 15-minute interval.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
