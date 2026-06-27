"""
config.py
=========
All tunables live here, loaded from environment variables with sane
defaults. Nothing else in the project should read os.environ directly --
that keeps every other module testable and keeps "what does this bot do"
answerable by reading one file.

Environment loading order
-------------------------
1. A `.env` file in the project root is loaded first, if present.
   Values already set in the process environment are NOT overwritten,
   so Render's injected variables always take precedence over the file.
2. `os.environ` is then read by the `_env_*` helpers as before.

This means:
  - Local dev: put secrets in `.env`, never commit it (it's in .gitignore).
  - Render:    set vars in the dashboard; `.env` is absent on the dyno,
               so nothing changes for production deployments.
  - CI:        set vars in the CI environment; `.env` is absent, no change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from models import RetestMode


# ---------------------------------------------------------------------------
# .env loader — no third-party dependencies
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path = Path(".env")) -> None:
    """
    Parse a .env file and populate os.environ with any variables it
    contains that are not already set in the environment.

    Rules:
      - Lines starting with # are comments and are skipped.
      - Blank lines are skipped.
      - Keys already present in os.environ are left untouched, so
        real environment variables (e.g. from Render) always win.
      - Values may optionally be quoted with single or double quotes;
        surrounding quotes are stripped.
      - Inline comments (value # comment) are not supported to avoid
        ambiguity with values that legitimately contain #.
    """
    if not path.exists():
        return
    with path.open() as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip()
            # Strip matching surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            # Never overwrite a value already present in the environment
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _env_str(key: str, default: str) -> str:
    val = os.environ.get(key, "").strip()
    return val if val else default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key, "").strip()
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key, "").strip()
    try:
        return float(val) if val else default
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _env_list(key: str, default: List[str]) -> List[str]:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    return [s.strip().upper() for s in val.split(",") if s.strip()]


DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "TRUMPUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
    "TONUSDT", "AAVEUSDT", "TAOUSDT", "SUIUSDT",
]


@dataclass(frozen=True)
class Settings:
    # --- Telegram ---
    telegram_token: str
    telegram_chat_id: str   # legacy field; kept for backward compat
    signals_chat_id: str    # trading signal channel (traders see this)
    admin_chat_id: str      # operational/admin channel (bot owner sees this)
    startup_alert: bool

    # --- Symbols & timeframes ---
    symbols: List[str]
    htf_timeframe: str       # 1H trend
    execution_timeframe: str  # 15m structure / breakout / zones
    confirmation_timeframe: str  # 5m retest confirmation

    # --- Loop timing ---
    loop_interval_seconds: int
    heartbeat_interval_seconds: int
    daily_summary_hour_utc: int
    symbol_delay_seconds: float

    # --- Swing / structure detection ---
    swing_left: int
    swing_right: int
    structure_lookback_candles: int
    sr_touch_tolerance_pct: float
    min_level_touches: int

    # --- Breakout confirmation ---
    min_body_ratio: float          # candle body / candle range
    min_volume_ratio: float        # breakout candle volume / avg volume
    volume_lookback: int
    min_breakout_atr_mult: float   # close must clear level by this many ATR
    min_zone_atr_mult: float       # rejects micro/noise zones

    # --- Confluence scoring ---
    confluence_threshold: int
    ema_period: int

    # --- Risk management ---
    atr_period: int
    atr_sl_buffer_mult: float
    min_risk_reward: float
    default_target_rr: float       # fallback TP when no liquidity/zone target found

    # --- Retest engine ---
    retest_mode: RetestMode
    pending_trade_expiry_seconds: int
    confirmation_window_candles: int  # how many confirmation-tf candles to wait for rejection

    # --- Cooldowns ---
    alert_cooldown_seconds: int

    # --- Persistence ---
    database_url: str
    sqlite_path: str

    # --- Misc ---
    signal_log_file: str = "signals.csv"
    min_candles_required: int = 60


def load_settings() -> Settings:
    telegram_token = _env_str("TELEGRAM_TOKEN", "")

    # --- Chat ID resolution with backward-compatible priority chain ---
    #
    # Signals channel (what traders see):
    #   1. SIGNALS_CHAT_ID
    #   2. TELEGRAM_CHAT_ID  (legacy fallback)
    #
    # Admin channel (what the bot owner sees):
    #   1. ADMIN_CHAT_ID
    #   2. SIGNALS_CHAT_ID
    #   3. TELEGRAM_CHAT_ID  (legacy fallback)
    #
    # A deployment with only TELEGRAM_CHAT_ID set continues to work
    # exactly as before -- both channels resolve to the same value.

    legacy_chat_id  = _env_str("TELEGRAM_CHAT_ID", "")
    signals_chat_id = _env_str("SIGNALS_CHAT_ID", "") or legacy_chat_id
    admin_chat_id   = _env_str("ADMIN_CHAT_ID",   "") or signals_chat_id

    # Safety net: auto-swap if token and chat ID were pasted into the wrong vars.
    if signals_chat_id and ":" in signals_chat_id and ":" not in telegram_token:
        telegram_token, signals_chat_id = signals_chat_id, telegram_token
        admin_chat_id = signals_chat_id   # re-resolve after swap

    if not telegram_token or not signals_chat_id:
        raise RuntimeError(
            "Missing Telegram credentials. Set TELEGRAM_TOKEN and at least one of "
            "SIGNALS_CHAT_ID or TELEGRAM_CHAT_ID."
        )

    # Legacy field kept so any code that still reads settings.telegram_chat_id works.
    telegram_chat_id = signals_chat_id

    retest_mode_raw = _env_str("RETEST_MODE", RetestMode.CONFIRMATION.value).lower()
    try:
        retest_mode = RetestMode(retest_mode_raw)
    except ValueError:
        retest_mode = RetestMode.CONFIRMATION

    database_url = _env_str("DATABASE_URL", "")
    sqlite_path = _env_str("SQLITE_PATH", "botv2.db")

    return Settings(
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        signals_chat_id=signals_chat_id,
        admin_chat_id=admin_chat_id,
        startup_alert=_env_bool("STARTUP_ALERT", True),
        symbols=_env_list("SYMBOLS", DEFAULT_SYMBOLS),
        htf_timeframe=_env_str("HTF_TIMEFRAME", "1H"),
        execution_timeframe=_env_str("EXECUTION_TIMEFRAME", "15m"),
        confirmation_timeframe=_env_str("CONFIRMATION_TIMEFRAME", "5m"),
        loop_interval_seconds=_env_int("LOOP_INTERVAL_SECONDS", 300),
        heartbeat_interval_seconds=_env_int("HEARTBEAT_INTERVAL_SECONDS", 3600),
        daily_summary_hour_utc=_env_int("DAILY_SUMMARY_HOUR_UTC", 8),
        symbol_delay_seconds=_env_float("SYMBOL_DELAY_SECONDS", 0.2),
        swing_left=_env_int("SWING_LEFT", 3),
        swing_right=_env_int("SWING_RIGHT", 3),
        structure_lookback_candles=_env_int("STRUCTURE_LOOKBACK_CANDLES", 150),
        sr_touch_tolerance_pct=_env_float("SR_TOUCH_TOLERANCE_PCT", 0.0015),
        min_level_touches=_env_int("MIN_LEVEL_TOUCHES", 2),
        min_body_ratio=_env_float("MIN_BODY_RATIO", 0.4),
        min_volume_ratio=_env_float("MIN_VOLUME_RATIO", 1.3),
        volume_lookback=_env_int("VOLUME_LOOKBACK", 20),
        min_breakout_atr_mult=_env_float("MIN_BREAKOUT_ATR_MULT", 0.1),
        min_zone_atr_mult=_env_float("MIN_ZONE_ATR_MULT", 0.25),
        confluence_threshold=_env_int("CONFLUENCE_THRESHOLD", 6),
        ema_period=_env_int("EMA_PERIOD", 50),
        atr_period=_env_int("ATR_PERIOD", 14),
        atr_sl_buffer_mult=_env_float("ATR_SL_BUFFER_MULT", 0.25),
        min_risk_reward=_env_float("MIN_RISK_REWARD", 2.0),
        default_target_rr=_env_float("DEFAULT_TARGET_RR", 2.5),
        retest_mode=retest_mode,
        pending_trade_expiry_seconds=_env_int("PENDING_TRADE_EXPIRY_SECONDS", 3 * 3600),
        confirmation_window_candles=_env_int("CONFIRMATION_WINDOW_CANDLES", 3),
        alert_cooldown_seconds=_env_int("ALERT_COOLDOWN_SECONDS", 3 * 3600),
        database_url=database_url,
        sqlite_path=sqlite_path,
        signal_log_file=_env_str("SIGNAL_LOG_FILE", "signals.csv"),
        min_candles_required=_env_int("MIN_CANDLES_REQUIRED", 60),
    )


settings = load_settings()
