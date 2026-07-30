from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    price: float
    ema_fast: float
    ema_slow: float
    rsi: float
    macd: float
    macd_signal: float
    trend_strength: float
    volume_ratio: float
    confidence: int
    ema_20: float = 0.0
    ema_50: float = 0.0
    atr: float = 0.0

    @property
    def bullish(self) -> bool:
        return self.ema_fast > self.ema_slow and self.macd > self.macd_signal

    @property
    def bearish(self) -> bool:
        return self.ema_fast < self.ema_slow and self.macd < self.macd_signal


@dataclass(frozen=True)
class MarketOverview:
    symbol: str
    current_price: float
    price_change_24h: float
    market_cap_rank: int | None
    market_cap: float
    volume_24h: float


@dataclass(frozen=True)
class AnalysisFactor:
    name: str
    status: str
    score: int
    weight: int
    reason: str


@dataclass(frozen=True)
class AnalysisResult:
    trend: str
    risk: str
    factors: tuple[AnalysisFactor, ...]
    confidence: int
    recommendation: str
    explanation: str


@dataclass(frozen=True)
class PaperPosition:
    user_id: int
    chat_id: int
    symbol: str
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    entry_time: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperStore:
    """SQLite persistence for paper accounts, open positions, and trade history."""

    def __init__(self, database_path: str = "telegram_bot/paper_trading.db") -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    auto_trading_enabled INTEGER NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL DEFAULT 'BTC',
                    balance REAL NOT NULL DEFAULT 10000,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_positions (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    indicators_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    pnl REAL NOT NULL,
                    pnl_percent REAL NOT NULL,
                    close_reason TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT NOT NULL,
                    indicators_json TEXT NOT NULL
                );
                """
            )

    def activate_account(self, user_id: int, chat_id: int) -> sqlite3.Row:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_accounts (user_id, chat_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    is_active = 1,
                    updated_at = excluded.updated_at
                """,
                (user_id, chat_id, now, now),
            )
            return connection.execute(
                "SELECT * FROM paper_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()

    def get_account(self, user_id: int) -> sqlite3.Row | None:
        with self._lock, self._connect() as connection:
            return connection.execute(
                "SELECT * FROM paper_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()

    def set_auto_trading(self, user_id: int, enabled: bool) -> sqlite3.Row | None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE paper_accounts
                SET auto_trading_enabled = ?, updated_at = ?
                WHERE user_id = ? AND is_active = 1
                """,
                (int(enabled), utc_now(), user_id),
            )
            return connection.execute(
                "SELECT * FROM paper_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()

    def set_symbol(self, user_id: int, symbol: str) -> sqlite3.Row | None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE paper_accounts
                SET symbol = ?, updated_at = ?
                WHERE user_id = ? AND is_active = 1
                """,
                (symbol, utc_now(), user_id),
            )
            return connection.execute(
                "SELECT * FROM paper_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()

    def active_auto_accounts(self) -> list[sqlite3.Row]:
        with self._lock, self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT DISTINCT a.* FROM paper_accounts a
                    LEFT JOIN paper_positions p ON p.user_id = a.user_id
                    WHERE a.is_active = 1
                      AND (a.auto_trading_enabled = 1 OR p.user_id IS NOT NULL)
                    """
                ).fetchall()
            )

    def get_position(self, user_id: int) -> PaperPosition | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM paper_positions WHERE user_id = ?", (user_id,)
            ).fetchone()
        return self._position_from_row(row) if row else None

    def open_position(
        self,
        user_id: int,
        chat_id: int,
        symbol: str,
        entry_price: float,
        quantity: float,
        stop_loss: float,
        take_profit: float,
        indicators: MarketSnapshot,
    ) -> PaperPosition:
        entry_time = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_positions (
                    user_id, chat_id, symbol, entry_price, quantity, stop_loss,
                    take_profit, entry_time, indicators_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    chat_id,
                    symbol,
                    entry_price,
                    quantity,
                    stop_loss,
                    take_profit,
                    entry_time,
                    json.dumps(asdict(indicators)),
                ),
            )
        return PaperPosition(
            user_id,
            chat_id,
            symbol,
            entry_price,
            quantity,
            stop_loss,
            take_profit,
            entry_time,
        )

    def close_position(
        self,
        position: PaperPosition,
        exit_price: float,
        reason: str,
        indicators: MarketSnapshot,
    ) -> tuple[float, float]:
        pnl = (exit_price - position.entry_price) * position.quantity
        pnl_percent = ((exit_price - position.entry_price) / position.entry_price) * 100
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trade_history (
                    user_id, chat_id, symbol, entry_price, exit_price, quantity,
                    pnl, pnl_percent, close_reason, entry_time, exit_time,
                    indicators_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.user_id,
                    position.chat_id,
                    position.symbol,
                    position.entry_price,
                    exit_price,
                    position.quantity,
                    pnl,
                    pnl_percent,
                    reason,
                    position.entry_time,
                    utc_now(),
                    json.dumps(asdict(indicators)),
                ),
            )
            connection.execute(
                "DELETE FROM paper_positions WHERE user_id = ?", (position.user_id,)
            )
            connection.execute(
                """
                UPDATE paper_accounts
                SET balance = balance + ?, updated_at = ?
                WHERE user_id = ?
                """,
                (pnl, utc_now(), position.user_id),
            )
        return pnl, pnl_percent

    def history(self, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock, self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM trade_history
                    WHERE user_id = ?
                    ORDER BY exit_time DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            )

    @staticmethod
    def _position_from_row(row: sqlite3.Row) -> PaperPosition:
        return PaperPosition(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            symbol=row["symbol"],
            entry_price=row["entry_price"],
            quantity=row["quantity"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            entry_time=row["entry_time"],
        )


def exponential_moving_average(values: list[float], period: int) -> float:
    if not values:
        raise ValueError("Cannot calculate EMA without values")
    multiplier = 2 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = (value - ema) * multiplier + ema
    return ema


def relative_strength_index(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for before, after in zip(values[-period - 1 : -1], values[-period:]):
        change = after - before
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    if average_loss == 0:
        return 100.0 if average_gain else 50.0
    return 100 - (100 / (1 + average_gain / average_loss))


def average_true_range(
    prices: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    period: int = 14,
) -> float:
    """Calculate ATR, falling back to close-to-close ranges when needed."""
    if len(prices) < 2:
        return 0.0
    true_ranges: list[float] = []
    if highs is not None and lows is not None:
        usable = min(len(prices), len(highs), len(lows))
        for index in range(usable):
            previous_close = prices[index - 1] if index else prices[index]
            true_ranges.append(
                max(
                    highs[index] - lows[index],
                    abs(highs[index] - previous_close),
                    abs(lows[index] - previous_close),
                )
            )
    else:
        true_ranges = [abs(after - before) for before, after in zip(prices, prices[1:])]
    if not true_ranges:
        return 0.0
    return sum(true_ranges[-period:]) / len(true_ranges[-period:])


def build_snapshot(
    symbol: str,
    prices: list[float],
    volumes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> MarketSnapshot:
    if len(prices) < 30:
        raise ValueError("Not enough market data")
    ema_fast = exponential_moving_average(prices, 12)
    ema_slow = exponential_moving_average(prices, 26)
    macd_values = []
    for index in range(26, len(prices) + 1):
        window = prices[:index]
        macd_values.append(
            exponential_moving_average(window, 12)
            - exponential_moving_average(window, 26)
        )
    macd = macd_values[-1]
    macd_signal = exponential_moving_average(macd_values, 9)
    price = prices[-1]
    average_volume = sum(volumes[-20:]) / max(len(volumes[-20:]), 1)
    volume_ratio = volumes[-1] / average_volume if average_volume else 1.0
    trend_strength = abs(ema_fast - ema_slow) / price * 100
    rsi = relative_strength_index(prices)
    ema_20 = exponential_moving_average(prices, 20)
    ema_50 = exponential_moving_average(prices, 50)
    atr = average_true_range(prices, highs=highs, lows=lows)

    confidence = 0
    confidence += 25 if ema_fast > ema_slow else 0
    confidence += 25 if macd > macd_signal else 0
    confidence += 20 if 50 <= rsi <= 70 else 0
    confidence += 15 if trend_strength >= 0.3 else 0
    confidence += 15 if volume_ratio >= 0.8 else 0

    return MarketSnapshot(
        symbol=symbol,
        price=price,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi=rsi,
        macd=macd,
        macd_signal=macd_signal,
        trend_strength=trend_strength,
        volume_ratio=volume_ratio,
        confidence=confidence,
        ema_20=ema_20,
        ema_50=ema_50,
        atr=atr,
    )


class RiskManager:
    """Conservative paper-only position sizing and exit levels."""

    @staticmethod
    def levels(price: float, balance: float) -> tuple[float, float, float]:
        stop_loss = price * 0.98
        take_profit = price * 1.04
        risk_amount = max(balance, 0) * 0.01
        risk_per_unit = price - stop_loss
        quantity_by_risk = risk_amount / risk_per_unit if risk_per_unit else 0
        quantity_by_budget = (max(balance, 0) * 0.10) / price if price else 0
        quantity = min(quantity_by_risk, quantity_by_budget)
        return quantity, stop_loss, take_profit


class AutomaticTradingEngine:
    """Evaluates enabled paper accounts and emits notification callbacks."""

    def __init__(
        self,
        store: PaperStore,
        market_data: Callable[[str], tuple[list[float], list[float]]],
        notify: Callable[[int, str], None],
    ) -> None:
        self.store = store
        self.market_data = market_data
        self.notify = notify

    def run_once(self) -> None:
        for account in self.store.active_auto_accounts():
            try:
                prices, volumes = self.market_data(account["symbol"])
                snapshot = build_snapshot(account["symbol"], prices, volumes)
                position = self.store.get_position(account["user_id"])
                if position:
                    self._monitor_position(position, snapshot)
                elif account["auto_trading_enabled"] and self._entry_conditions(snapshot):
                    self._open_trade(account, snapshot)
            except Exception:
                logger.exception(
                    "Automatic analysis failed for user %s", account["user_id"]
                )

    @staticmethod
    def _entry_conditions(snapshot: MarketSnapshot) -> bool:
        return (
            snapshot.ema_fast > snapshot.ema_slow
            and snapshot.macd > snapshot.macd_signal
            and 50 <= snapshot.rsi <= 70
            and snapshot.trend_strength >= 0.3
            and snapshot.volume_ratio >= 0.8
            and snapshot.confidence >= 70
        )

    def _open_trade(self, account: sqlite3.Row, snapshot: MarketSnapshot) -> None:
        quantity, stop_loss, take_profit = RiskManager.levels(
            snapshot.price, account["balance"]
        )
        if quantity <= 0:
            return
        self.store.open_position(
            user_id=account["user_id"],
            chat_id=account["chat_id"],
            symbol=account["symbol"],
            entry_price=snapshot.price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            indicators=snapshot,
        )
        self.notify(
            account["chat_id"],
            "\n".join(
                [
                    "🤖 معامله خودکار انجام شد",
                    "",
                    f"🟢 خرید {persian_coin_name(account['symbol'])}",
                    f"قیمت ورود: {format_price(snapshot.price)} دلار",
                    f"حد ضرر: {format_price(stop_loss)} دلار",
                    f"حد سود: {format_price(take_profit)} دلار",
                    f"امتیاز اطمینان: {snapshot.confidence}٪",
                    "",
                    "این معامله کاملاً آزمایشی است و پول واقعی در آن استفاده نمی‌شود.",
                ]
            ),
        )

    def _monitor_position(
        self, position: PaperPosition, snapshot: MarketSnapshot
    ) -> None:
        reason = self._exit_reason(position, snapshot)
        if not reason:
            return
        pnl, pnl_percent = self.store.close_position(
            position, snapshot.price, reason, snapshot
        )
        result_word = "سود" if pnl >= 0 else "زیان"
        self.notify(
            position.chat_id,
            "\n".join(
                [
                    "🔴 معامله بسته شد",
                    "",
                    f"فروش {persian_coin_name(position.symbol)}",
                    f"قیمت ورود: {format_price(position.entry_price)} دلار",
                    f"قیمت خروج: {format_price(snapshot.price)} دلار",
                    f"{result_word}: {format_price(abs(pnl))} دلار",
                    f"درصد سود/زیان: {pnl_percent:+.2f}٪",
                    f"دلیل خروج: {reason}",
                    "",
                    "این معامله کاملاً آزمایشی بود و پول واقعی در آن استفاده نشد.",
                ]
            ),
        )

    @staticmethod
    def _exit_reason(
        position: PaperPosition, snapshot: MarketSnapshot
    ) -> str | None:
        if snapshot.price <= position.stop_loss:
            return "رسیدن قیمت به حد ضرر"
        if snapshot.price >= position.take_profit:
            return "رسیدن قیمت به حد سود"
        if snapshot.rsi >= 78:
            return "اشباع خرید و فعال شدن شرط خروج"
        if snapshot.bearish and snapshot.trend_strength >= 0.3:
            if snapshot.trend_strength >= 0.7:
                return "بازگشت شدید روند"
            return "فعال شدن شرایط خروج"
        return None


COIN_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "LINK": "chainlink",
}

PERSIAN_COIN_NAMES = {
    "BTC": "بیت‌کوین",
    "ETH": "اتریوم",
    "SOL": "سولانا",
    "BNB": "بایننس‌کوین",
    "XRP": "ریپل",
    "ADA": "کاردانو",
    "DOGE": "دوج‌کوین",
    "AVAX": "آوالانچ",
    "DOT": "پولکادات",
    "LINK": "چین‌لینک",
}


def persian_coin_name(symbol: str) -> str:
    return PERSIAN_COIN_NAMES.get(symbol.upper(), symbol.upper())


def format_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:,.8f}"


def format_large_number(value: float) -> str:
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:,.2f} میلیارد"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.2f} میلیون"
    if abs(value) >= 1_000:
        return f"{value / 1_000:,.2f} هزار"
    return f"{value:,.2f}"


def analyze_market(
    overview: MarketOverview, snapshot: MarketSnapshot
) -> AnalysisResult:
    """Build a Persian, weighted multi-factor market analysis."""

    def factor(
        name: str,
        score: int,
        weight: int,
        reason: str,
    ) -> AnalysisFactor:
        normalized_score = max(0, min(100, score))
        status = (
            "مثبت"
            if normalized_score >= 67
            else "منفی"
            if normalized_score <= 33
            else "خنثی"
        )
        return AnalysisFactor(
            name=name,
            status=status,
            score=normalized_score,
            weight=weight,
            reason=reason,
        )

    if overview.price_change_24h >= 2:
        price_factor = factor(
            "تغییر قیمت ۲۴ ساعت",
            100,
            12,
            f"قیمت در ۲۴ ساعت گذشته {overview.price_change_24h:+.2f}٪ تغییر کرده و شتاب کوتاه‌مدت صعودی است.",
        )
    elif overview.price_change_24h <= -2:
        price_factor = factor(
            "تغییر قیمت ۲۴ ساعت",
            0,
            12,
            f"قیمت در ۲۴ ساعت گذشته {overview.price_change_24h:+.2f}٪ تغییر کرده و فشار فروش کوتاه‌مدت دیده می‌شود.",
        )
    else:
        price_factor = factor(
            "تغییر قیمت ۲۴ ساعت",
            50,
            12,
            f"تغییر قیمت {overview.price_change_24h:+.2f}٪ است و حرکت روزانه هنوز جهت قوی ندارد.",
        )

    if 45 <= snapshot.rsi <= 65:
        rsi_factor = factor(
            "شاخص قدرت نسبی",
            100,
            14,
            f"شاخص قدرت نسبی {snapshot.rsi:.1f} است؛ در محدوده متعادل برای ادامه حرکت قرار دارد.",
        )
    elif 35 <= snapshot.rsi < 45 or 65 < snapshot.rsi <= 70:
        rsi_factor = factor(
            "شاخص قدرت نسبی",
            50,
            14,
            f"شاخص قدرت نسبی {snapshot.rsi:.1f} است؛ از محدوده ایده‌آل فاصله دارد اما هنوز هشدار شدید نیست.",
        )
    else:
        rsi_factor = factor(
            "شاخص قدرت نسبی",
            0,
            14,
            f"شاخص قدرت نسبی {snapshot.rsi:.1f} است؛ اشباع خرید یا فروش، ریسک برگشت قیمت را بالا می‌برد.",
        )

    ema_bullish = snapshot.ema_20 > snapshot.ema_50
    ema_factor = factor(
        "میانگین‌های نمایی",
        100 if ema_bullish else 0 if snapshot.ema_20 < snapshot.ema_50 else 50,
        16,
        (
            f"EMA20 ({format_price(snapshot.ema_20)}) بالاتر از EMA50 "
            f"({format_price(snapshot.ema_50)}) است و روند میان‌مدت صعودی است."
            if ema_bullish
            else f"EMA20 ({format_price(snapshot.ema_20)}) پایین‌تر از EMA50 "
            f"({format_price(snapshot.ema_50)}) است و روند میان‌مدت نزولی است."
            if snapshot.ema_20 < snapshot.ema_50
            else "EMA20 و EMA50 تقریباً برابرند و روند میان‌مدت خنثی است."
        ),
    )

    macd_bullish = snapshot.macd > snapshot.macd_signal
    macd_factor = factor(
        "مکدی",
        100 if macd_bullish else 0 if snapshot.macd < snapshot.macd_signal else 50,
        14,
        (
            "خط مکدی بالاتر از خط سیگنال است و مومنتوم صعودی را تأیید می‌کند."
            if macd_bullish
            else "خط مکدی پایین‌تر از خط سیگنال است و مومنتوم صعودی را تأیید نمی‌کند."
            if snapshot.macd < snapshot.macd_signal
            else "خط مکدی و خط سیگنال نزدیک‌اند و مومنتوم جهت مشخصی ندارد."
        ),
    )

    if snapshot.volume_ratio >= 1:
        volume_factor = factor(
            "حجم معاملات",
            100,
            10,
            f"حجم فعلی {snapshot.volume_ratio * 100:.0f}٪ میانگین اخیر است و حرکت قیمت پشتوانه حجمی دارد.",
        )
    elif snapshot.volume_ratio >= 0.8:
        volume_factor = factor(
            "حجم معاملات",
            50,
            10,
            f"حجم فعلی {snapshot.volume_ratio * 100:.0f}٪ میانگین اخیر است و نزدیک به حد قابل قبول قرار دارد.",
        )
    else:
        volume_factor = factor(
            "حجم معاملات",
            0,
            10,
            f"حجم فعلی فقط {snapshot.volume_ratio * 100:.0f}٪ میانگین اخیر است و حرکت قیمت پشتوانه کافی ندارد.",
        )

    if overview.market_cap_rank is None:
        rank_factor = factor(
            "رتبه ارزش بازار",
            50,
            8,
            "رتبه ارزش بازار از داده‌های فعلی در دسترس نیست؛ این عامل خنثی در نظر گرفته شد.",
        )
    elif overview.market_cap_rank <= 20:
        rank_factor = factor(
            "رتبه ارزش بازار",
            100,
            8,
            f"رتبه بازار {overview.market_cap_rank} است و نقدشوندگی و اعتبار نسبی بالاتری دارد.",
        )
    elif overview.market_cap_rank <= 100:
        rank_factor = factor(
            "رتبه ارزش بازار",
            50,
            8,
            f"رتبه بازار {overview.market_cap_rank} است و در محدوده متوسط بازار قرار دارد.",
        )
    else:
        rank_factor = factor(
            "رتبه ارزش بازار",
            0,
            8,
            f"رتبه بازار {overview.market_cap_rank} است و نسبت به دارایی‌های بزرگ‌تر ریسک نقدشوندگی بیشتری دارد.",
        )

    trend_bullish = snapshot.ema_20 > snapshot.ema_50
    trend_neutral = snapshot.trend_strength < 0.3
    trend_factor = factor(
        "قدرت روند",
        100 if trend_bullish and not trend_neutral else 50 if trend_neutral else 0,
        14,
        (
            f"قدرت روند {snapshot.trend_strength:.2f}٪ است و جهت غالب صعودی تشخیص داده شد."
            if trend_bullish and not trend_neutral
            else f"قدرت روند {snapshot.trend_strength:.2f}٪ است و حرکت بازار قدرت کافی برای یک جهت قطعی ندارد."
            if trend_neutral
            else f"قدرت روند {snapshot.trend_strength:.2f}٪ است اما جهت غالب نزولی تشخیص داده شد."
        ),
    )

    atr_percent = (snapshot.atr / snapshot.price * 100) if snapshot.price else 0
    atr_factor = factor(
        "نوسان ATR",
        100 if atr_percent <= 2 else 50 if atr_percent <= 4 else 0,
        12,
        (
            f"ATR برابر {format_price(snapshot.atr)} دلار، یعنی {atr_percent:.2f}٪ قیمت است و نوسان کنترل‌شده است."
            if atr_percent <= 2
            else f"ATR برابر {format_price(snapshot.atr)} دلار، یعنی {atr_percent:.2f}٪ قیمت است و نوسان متوسط است."
            if atr_percent <= 4
            else f"ATR برابر {format_price(snapshot.atr)} دلار، یعنی {atr_percent:.2f}٪ قیمت است و نوسان بالا ریسک ورود را زیاد می‌کند."
        ),
    )

    factors = (
        price_factor,
        rsi_factor,
        ema_factor,
        macd_factor,
        volume_factor,
        rank_factor,
        trend_factor,
        atr_factor,
    )
    confidence = round(
        sum(item.score * item.weight for item in factors)
        / sum(item.weight for item in factors)
    )
    recommendation = (
        "خرید بسیار قوی"
        if confidence >= 85
        else "خرید"
        if confidence >= 70
        else "نگهداری"
        if confidence >= 55
        else "صبر"
        if confidence >= 40
        else "فروش یا خروج"
    )

    trend = (
        "صعودی"
        if ema_bullish and macd_bullish
        else "نزولی"
        if not ema_bullish and not macd_bullish
        else "خنثی"
    )
    risk = (
        "زیاد"
        if atr_percent > 4 or snapshot.rsi < 30 or snapshot.rsi > 70
        else "متوسط"
        if atr_percent > 2 or abs(overview.price_change_24h) >= 4
        else "کم"
    )
    strongest = sorted(
        factors,
        key=lambda item: abs(item.score - 50) * item.weight,
        reverse=True,
    )[:3]
    explanation = "؛ ".join(
        f"{item.name} با وضعیت {item.status} و وزن {item.weight}٪ بیشترین اثر را داشت: {item.reason}"
        for item in strongest
    )
    return AnalysisResult(
        trend=trend,
        risk=risk,
        factors=factors,
        confidence=confidence,
        recommendation=recommendation,
        explanation=explanation + f" تصمیم نهایی بر اساس امتیاز وزنی {confidence} از ۱۰۰، «{recommendation}» است.",
    )


def entry_conditions(
    snapshot: MarketSnapshot, confidence: int
) -> tuple[tuple[str, bool, str], ...]:
    """Return the six read-only entry checks for the analysis report."""
    trend_is_bullish_or_neutral = (
        snapshot.ema_20 >= snapshot.ema_50 or snapshot.trend_strength < 0.3
    )
    return (
        (
            "EMA20 بالاتر از EMA50",
            snapshot.ema_20 > snapshot.ema_50,
            f"EMA20 برابر {format_price(snapshot.ema_20)} و EMA50 برابر {format_price(snapshot.ema_50)} است.",
        ),
        (
            "مکدی صعودی",
            snapshot.macd > snapshot.macd_signal,
            "خط مکدی بالاتر از خط سیگنال است."
            if snapshot.macd > snapshot.macd_signal
            else "خط مکدی بالاتر از خط سیگنال نیست.",
        ),
        (
            "RSI بین ۴۵ و ۶۵",
            45 <= snapshot.rsi <= 65,
            f"مقدار RSI برابر {snapshot.rsi:.1f} است.",
        ),
        (
            "حجم حداقل ۸۰٪ میانگین اخیر",
            snapshot.volume_ratio >= 0.8,
            f"حجم فعلی {snapshot.volume_ratio * 100:.0f}٪ میانگین اخیر است.",
        ),
        (
            "قدرت روند صعودی یا خنثی",
            trend_is_bullish_or_neutral,
            (
                f"قدرت روند {snapshot.trend_strength:.2f}٪ است و جهت نزولی قوی نیست."
                if trend_is_bullish_or_neutral
                else f"قدرت روند {snapshot.trend_strength:.2f}٪ است و جهت نزولی قوی تشخیص داده شد."
            ),
        ),
        (
            "شاخص اطمینان حداقل ۷۰ از ۱۰۰",
            confidence >= 70,
            f"شاخص اطمینان فعلی {confidence} از ۱۰۰ است.",
        ),
    )