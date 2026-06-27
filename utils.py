"""
utils.py
========
Small, stateless helpers shared by multiple modules. Nothing in here should
import from scanner.py / trade_manager.py / telegram_bot.py -- keep it leaf-level
so it can't create circular imports.
"""

from __future__ import annotations

import pandas as pd


def fmt_price(p: float) -> str:
    """Human-friendly price formatting that scales with magnitude."""
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def tradingview_link(symbol: str, interval: str = "15") -> str:
    return f"https://www.tradingview.com/chart/?symbol=OKX%3A{symbol}.P&interval={interval}"


def to_okx_inst(symbol: str) -> str:
    """Convert e.g. BTCUSDT -> BTC-USDT-SWAP (OKX perpetual swap instrument id)."""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}-USDT-SWAP"
    return symbol


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range -- a measure of recent volatility per candle."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def ema(df: pd.DataFrame, period: int = 50, column: str = "close") -> pd.Series:
    return df[column].ewm(span=period, adjust=False).mean()


def last_closed_candle(df: pd.DataFrame):
    """
    Return (candle, integer_position) for the most recent CLOSED candle.
    OKX includes the still-forming candle as the last row with confirm="0".
    """
    if "confirm" in df.columns and len(df) >= 2:
        try:
            if str(df["confirm"].iloc[-1]) == "0":
                return df.iloc[-2], len(df) - 2
        except Exception:
            pass
    return df.iloc[-1], len(df) - 1


def pct_change(a: float, b: float) -> float:
    """Percentage change from a to b."""
    if a == 0:
        return 0.0
    return (b - a) / a * 100
