"""
market_data.py
===============
Fetches OHLCV candles from OKX with a retry-enabled HTTP session, and
caches each timeframe with its own TTL so the multi-timeframe scan
(1H / 15m / 5m) doesn't triple the number of API calls every loop for
no reason -- the 1H trend doesn't change candle-to-candle.
"""

from __future__ import annotations

import time
from typing import Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from logger import get_logger
from utils import to_okx_inst

log = get_logger(__name__)

OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"

# OKX bar codes differ slightly in casing from common shorthand.
_BAR_MAP = {
    "1H": "1H",
    "1h": "1H",
    "15m": "15m",
    "5m": "5m",
    "1m": "1m",
    "4H": "4H",
    "4h": "4H",
    "1D": "1Dutc",
}

# How long a cached frame for a given timeframe is considered fresh.
_CACHE_TTL_SECONDS = {
    "1H": 15 * 60,
    "15m": 4 * 60,
    "5m": 90,
}

_retry = Retry(
    total=3,
    backoff_factor=1,  # waits 1s, 2s, 4s between attempts
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    raise_on_status=False,
)

session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=_retry))

# (symbol, timeframe) -> (fetched_at_epoch, dataframe)
_cache: dict = {}


def _bar_code(timeframe: str) -> str:
    return _BAR_MAP.get(timeframe, timeframe)


def _ttl_for(timeframe: str) -> int:
    return _CACHE_TTL_SECONDS.get(timeframe, 60)


def fetch_candles(symbol: str, timeframe: str, limit: int = 200) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles for `symbol` on `timeframe`, using a small in-memory
    TTL cache to limit API requests across the multi-timeframe scan.
    Returns None on failure (caller must handle this -- never raises).
    """
    cache_key = (symbol, timeframe, limit)
    now = time.time()
    cached = _cache.get(cache_key)
    if cached is not None:
        fetched_at, df = cached
        if now - fetched_at < _ttl_for(timeframe):
            return df

    params = {"instId": to_okx_inst(symbol), "bar": _bar_code(timeframe), "limit": limit}

    try:
        res = session.get(OKX_CANDLES_URL, params=params, timeout=10)
        data = res.json()
    except Exception as e:
        log.error("Market data fetch failed for %s %s: %s", symbol, timeframe, e)
        return cached[1] if cached else None  # serve stale data over nothing, if we have it

    if data.get("code") != "0" or not data.get("data"):
        log.warning("OKX returned no data for %s %s: %s", symbol, timeframe, data.get("msg"))
        return cached[1] if cached else None

    df = pd.DataFrame(
        data["data"],
        columns=[
            "time", "open", "high", "low", "close",
            "volume", "volCcy", "volCcyQuote", "confirm",
        ],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["time"] = df["time"].astype("int64")
    df = df.sort_values("time").reset_index(drop=True)

    _cache[cache_key] = (now, df)
    return df


def clear_cache() -> None:
    _cache.clear()
