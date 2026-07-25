from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from paper_trading import (
    AutomaticTradingEngine,
    COIN_IDS,
    MarketOverview,
    PaperStore,
    analyze_market,
    build_snapshot,
    format_price,
    format_large_number,
    persian_coin_name,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram-paper-bot")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MARKET_API_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
MARKET_OVERVIEW_API_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}"
ENGINE_INTERVAL_SECONDS = 15 * 60


class TelegramClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        self.base_url = f"https://api.telegram.org/bot{token}"

    def request(self, method: str, payload: dict[str, Any]) -> Any:
        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error in {method}: {body}")
        return body["result"]

    def send_message(self, chat_id: int, text: str) -> None:
        self.request("sendMessage", {"chat_id": chat_id, "text": text})

    def updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 50, "allowed_updates": '["message"]'}
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload)


def fetch_market_data(symbol: str) -> tuple[list[float], list[float]]:
    coin_id = COIN_IDS[symbol]
    url = MARKET_API_URL.format(coin_id=coin_id)
    query = urllib.parse.urlencode(
        {"vs_currency": "usd", "days": "30", "interval": "hourly"}
    )
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": "PersianPaperTradingBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    prices = [float(item[1]) for item in payload.get("prices", [])]
    volumes = [float(item[1]) for item in payload.get("total_volumes", [])]
    if len(prices) < 30 or not volumes:
        raise RuntimeError(f"Insufficient public market data for {symbol}")
    if len(volumes) < len(prices):
        volumes = ([volumes[0]] * (len(prices) - len(volumes))) + volumes
    return prices, volumes[-len(prices) :]


def fetch_market_overview(symbol: str) -> MarketOverview:
    coin_id = COIN_IDS[symbol]
    query = urllib.parse.urlencode(
        {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false",
        }
    )
    request = urllib.request.Request(
        f"{MARKET_OVERVIEW_API_URL.format(coin_id=coin_id)}?{query}",
        headers={"User-Agent": "PersianPaperTradingBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    market_data = payload.get("market_data") or {}
    current_price = float((market_data.get("current_price") or {}).get("usd", 0))
    if current_price <= 0:
        raise RuntimeError(f"Current market price is unavailable for {symbol}")
    return MarketOverview(
        symbol=symbol,
        current_price=current_price,
        price_change_24h=float(market_data.get("price_change_percentage_24h") or 0),
        market_cap_rank=payload.get("market_cap_rank"),
        market_cap=float((market_data.get("market_cap") or {}).get("usd", 0)),
        volume_24h=float((market_data.get("total_volume") or {}).get("usd", 0)),
    )


class BotApplication:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.store = PaperStore()
        self.engine = AutomaticTradingEngine(
            store=self.store,
            market_data=fetch_market_data,
            notify=self.client.send_message,
        )

    def run(self) -> None:
        threading.Thread(
            target=self._engine_loop, name="paper-trading-engine", daemon=True
        ).start()
        offset: int | None = None
        logger.info("Telegram paper-trading bot started")
        while True:
            try:
                for update in self.client.updates(offset):
                    offset = update["update_id"] + 1
                    self._handle_update(update)
            except (urllib.error.URLError, TimeoutError, RuntimeError):
                logger.exception("Telegram polling failed; retrying")
                time.sleep(5)
            except Exception:
                logger.exception("Unexpected polling error; retrying")
                time.sleep(5)

    def _engine_loop(self) -> None:
        time.sleep(5)
        while True:
            try:
                logger.info("Running automatic paper-trading cycle")
                self.engine.run_once()
            except Exception:
                logger.exception("Automatic engine cycle failed")
            time.sleep(ENGINE_INTERVAL_SECONDS)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message or not message.get("text"):
            return
        chat_id = int(message["chat"]["id"])
        user_id = int(message["from"]["id"])
        parts = message["text"].strip().split()
        command = parts[0].split("@")[0].lower()
        argument = parts[1].upper() if len(parts) > 1 else ""
        handlers = {
            "/start": self._start,
            "/help": self._help,
            "/paper_on": self._paper_on,
            "/auto_on": self._auto_on,
            "/auto_off": self._auto_off,
            "/coin": self._coin,
            "/analysis": self._analysis,
            "/status": self._status,
            "/history": self._history,
        }
        handler = handlers.get(command)
        if handler:
            handler(user_id, chat_id, argument)
        else:
            self.client.send_message(
                chat_id,
                "دستور شناخته نشد. برای دیدن فهرست دستورات، /help را بفرستید.",
            )

    def _start(self, user_id: int, chat_id: int, _: str) -> None:
        self.client.send_message(
            chat_id,
            "سلام! به ربات تحلیل و معاملات آزمایشی خوش آمدید.\n\n"
            "برای شروع حساب مجازی خود را با /paper_on فعال کنید.\n"
            "هیچ پول واقعی یا سفارش صرافی در این ربات استفاده نمی‌شود.",
        )

    def _help(self, user_id: int, chat_id: int, _: str) -> None:
        self.client.send_message(
            chat_id,
            "\n".join(
                [
                    "راهنمای ربات معاملات آزمایشی",
                    "",
                    "/paper_on فعال‌سازی حساب معاملات آزمایشی",
                    "/coin BTC انتخاب ارز، نمونه: BTC یا ETH",
                    "/analysis BTC دریافت تحلیل حرفه‌ای بازار",
                    "/auto_on فعال‌سازی معاملات خودکار",
                    "/auto_off غیرفعال‌سازی معاملات خودکار",
                    "/status نمایش وضعیت حساب و معامله باز",
                    "/history نمایش تاریخچه معاملات تکمیل‌شده",
                    "/help نمایش همین راهنما",
                    "",
                    "تحلیل خودکار هر ۱۵ دقیقه انجام می‌شود.",
                ]
            ),
        )

    def _paper_on(self, user_id: int, chat_id: int, _: str) -> None:
        account = self.store.activate_account(user_id, chat_id)
        self.client.send_message(
            chat_id,
            f"✅ حساب معاملات آزمایشی فعال شد.\n"
            f"موجودی آزمایشی: {account['balance']:,.2f} دلار\n"
            f"ارز انتخاب‌شده: {persian_coin_name(account['symbol'])}\n\n"
            "برای شروع معاملات خودکار، /auto_on را بفرستید.",
        )

    def _auto_on(self, user_id: int, chat_id: int, _: str) -> None:
        account = self.store.set_auto_trading(user_id, True)
        if not account or not account["is_active"]:
            self.client.send_message(
                chat_id,
                "ابتدا حساب معاملات آزمایشی را با /paper_on فعال کنید.",
            )
            return
        self.client.send_message(
            chat_id,
            f"✅ معاملات خودکار فعال شد.\n"
            f"ارز انتخاب‌شده: {persian_coin_name(account['symbol'])}\n"
            "ربات هر ۱۵ دقیقه بازار را بررسی می‌کند.",
        )

    def _auto_off(self, user_id: int, chat_id: int, _: str) -> None:
        account = self.store.set_auto_trading(user_id, False)
        if not account or not account["is_active"]:
            self.client.send_message(
                chat_id,
                "حساب فعالی پیدا نشد. برای شروع /paper_on را بفرستید.",
            )
            return
        self.client.send_message(
            chat_id,
            "⏸ معاملات خودکار غیرفعال شد.\n"
            "معامله‌ی باز بدون تغییر باقی می‌ماند و با /auto_on ادامه پیدا می‌کند.",
        )

    def _coin(self, user_id: int, chat_id: int, symbol: str) -> None:
        if symbol not in COIN_IDS:
            self.client.send_message(
                chat_id,
                "این ارز پشتیبانی نمی‌شود.\n"
                "ارزهای قابل انتخاب: BTC، ETH، SOL، BNB، XRP، ADA، DOGE، AVAX، DOT، LINK",
            )
            return
        account = self.store.set_symbol(user_id, symbol)
        if not account or not account["is_active"]:
            self.client.send_message(
                chat_id,
                "ابتدا حساب معاملات آزمایشی را با /paper_on فعال کنید.",
            )
            return
        self.client.send_message(
            chat_id,
            f"✅ ارز انتخاب‌شده به {persian_coin_name(symbol)} تغییر کرد.\n"
            "این انتخاب در چرخه‌ی تحلیل بعدی استفاده می‌شود.",
        )

    def _analysis(self, user_id: int, chat_id: int, symbol: str) -> None:
        if not symbol:
            self.client.send_message(
                chat_id,
                "لطفاً نماد ارز را وارد کنید.\nنمونه: /analysis BTC",
            )
            return
        if symbol not in COIN_IDS:
            self.client.send_message(
                chat_id,
                "این ارز برای تحلیل پشتیبانی نمی‌شود.\n"
                "ارزهای قابل تحلیل: BTC، ETH، SOL، BNB، XRP، ADA، DOGE، AVAX، DOT، LINK",
            )
            return
        try:
            overview = fetch_market_overview(symbol)
            prices, volumes = fetch_market_data(symbol)
            snapshot = build_snapshot(symbol, prices, volumes)
            trend, risk, confidence, recommendation, explanation = analyze_market(
                overview, snapshot
            )
            rank = (
                f"رتبه {overview.market_cap_rank}"
                if overview.market_cap_rank is not None
                else "نامشخص"
            )
            report = "\n".join(
                [
                    f"📊 گزارش تحلیل {persian_coin_name(symbol)}",
                    "",
                    f"💰 قیمت فعلی: {format_price(overview.current_price)} دلار",
                    f"📈 تغییر قیمت ۲۴ ساعت: {overview.price_change_24h:+.2f}٪",
                    f"🏆 رتبه بازار: {rank}",
                    f"🏦 ارزش بازار: {format_large_number(overview.market_cap)} دلار",
                    f"📦 حجم معاملات ۲۴ ساعت: {format_large_number(overview.volume_24h)} دلار",
                    "",
                    f"روند بازار: {trend}",
                    f"سطح ریسک: {risk}",
                    f"شاخص اطمینان: {confidence} از ۱۰۰",
                    f"پیشنهاد: {recommendation}",
                    "",
                    "دلیل تحلیل:",
                    explanation,
                    "",
                    "این گزارش صرفاً تحلیلی است و هیچ معامله‌ای انجام نمی‌دهد.",
                ]
            )
            self.client.send_message(chat_id, report)
        except Exception:
            logger.exception("Market analysis failed for %s", symbol)
            self.client.send_message(
                chat_id,
                "در حال حاضر دریافت داده‌های بازار ممکن نیست. لطفاً کمی بعد دوباره تلاش کنید.",
            )

    def _status(self, user_id: int, chat_id: int, _: str) -> None:
        account = self.store.get_account(user_id)
        if not account or not account["is_active"]:
            self.client.send_message(
                chat_id,
                "حساب فعالی ندارید. برای شروع /paper_on را بفرستید.",
            )
            return
        position = self.store.get_position(user_id)
        lines = [
            "وضعیت حساب معاملات آزمایشی",
            "",
            f"موجودی: {account['balance']:,.2f} دلار",
            f"ارز انتخاب‌شده: {persian_coin_name(account['symbol'])}",
            f"معاملات خودکار: {'فعال' if account['auto_trading_enabled'] else 'غیرفعال'}",
        ]
        if position:
            lines.extend(
                [
                    "",
                    "معامله‌ی باز:",
                    f"قیمت ورود: {format_price(position.entry_price)} دلار",
                    f"حد ضرر: {format_price(position.stop_loss)} دلار",
                    f"حد سود: {format_price(position.take_profit)} دلار",
                ]
            )
        else:
            lines.extend(["", "در حال حاضر معامله‌ی بازی ندارید."])
        self.client.send_message(chat_id, "\n".join(lines))

    def _history(self, user_id: int, chat_id: int, _: str) -> None:
        trades = self.store.history(user_id)
        if not trades:
            self.client.send_message(
                chat_id,
                "هنوز معامله‌ی تکمیل‌شده‌ای در تاریخچه وجود ندارد.",
            )
            return
        lines = ["📚 تاریخچه معاملات", ""]
        for index, trade in enumerate(trades, 1):
            date = (
                trade["exit_time"]
                .replace("T", " ")
                .replace("+00:00", " زمان هماهنگ جهانی")
            )
            lines.extend(
                [
                    f"معامله {index} — {persian_coin_name(trade['symbol'])}",
                    f"ورود: {format_price(trade['entry_price'])} دلار",
                    f"خروج: {format_price(trade['exit_price'])} دلار",
                    f"سود/زیان: {trade['pnl']:+,.2f} دلار",
                    f"درصد سود/زیان: {trade['pnl_percent']:+.2f}٪",
                    f"دلیل خروج: {trade['close_reason']}",
                    f"تاریخ: {date}",
                    "",
                ]
            )
        self.client.send_message(chat_id, "\n".join(lines).strip())


def main() -> None:
    application = BotApplication(TelegramClient(TELEGRAM_BOT_TOKEN))
    application.run()


if __name__ == "__main__":
    main()