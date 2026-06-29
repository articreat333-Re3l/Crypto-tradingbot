"""
telegram_bot.py
================
All Telegram I/O for Bot V2.

Architecture
------------
Three layers, each with a single responsibility:

  _send_raw(chat_id, text, ...)          ← sole HTTP function; nothing else
  │                                         calls the Telegram API directly.
  ├── send_signal_message(text, ...)     ← trading alerts → signals channel
  └── send_admin_message(text, ...)      ← ops messages  → admin channel

Both public send functions are independent: a failure in one (network
error, wrong chat ID) is caught, logged, and never propagates to the
other.  Scanning always continues.

Backward compatibility
----------------------
send_message() is kept as an alias for send_signal_message().  All
existing call sites (scanner.py, botv2.py, journal hooks) work unchanged.

If only TELEGRAM_CHAT_ID is configured, both channels resolve to the same
destination, so the bot behaves identically to the previous single-channel
design.

Channel resolution (done in config.py at startup):
  Signals: SIGNALS_CHAT_ID → TELEGRAM_CHAT_ID
  Admin:   ADMIN_CHAT_ID   → SIGNALS_CHAT_ID → TELEGRAM_CHAT_ID

Message priorities
------------------
MessagePriority is metadata carried on every admin send.  Currently it is
logged; the design makes it trivial to add per-priority filtering (e.g.
suppress LOW heartbeats during market hours) without structural changes.

Future extensibility
--------------------
_send_raw() accepts any chat_id string.  To add a second signal channel,
a premium subscribers group, or multiple admin users, loop over a list of
IDs and call _send_raw() for each — no other code changes required.
"""

from __future__ import annotations

import csv
import os
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import requests

import dashboard
import journal as journal_mod
import persistence
import stats as stats_module
from config import settings
from logger import get_logger
from models import Direction, Trade
from utils import fmt_price, tradingview_link

log = get_logger(__name__)

API_BASE = f"https://api.telegram.org/bot{settings.telegram_token}"

_last_update_id = 0
_bot_start_time = time.time()


# ---------------------------------------------------------------------------
# Priority enum
# ---------------------------------------------------------------------------

class MessagePriority(str, Enum):
    """
    Semantic priority of an admin message.

    HIGH   — crash, exception, bot going offline
    NORMAL — startup, daily summary, backup complete
    LOW    — heartbeat, CSV export complete

    Priority is currently carried as metadata and logged.  It is designed
    to make future per-priority filtering (suppress LOW during maintenance,
    page on HIGH, etc.) a configuration change rather than a code change.
    """
    HIGH   = "high"
    NORMAL = "normal"
    LOW    = "low"


# ---------------------------------------------------------------------------
# Layer 1: raw HTTP — the ONLY place that calls the Telegram API
# ---------------------------------------------------------------------------

def _send_raw(
    chat_id: str,
    text: str,
    parse_mode: str = "Markdown",
    silent: bool = False,
) -> bool:
    """
    Send a single message to any Telegram chat.

    Returns True on success, False on failure.  Never raises — callers
    must not depend on an exception to detect delivery failure.

    All public send functions route through here so that API call
    formatting, timeout, and error handling live in exactly one place.
    Adding new destinations (premium channel, multiple admin users) is a
    matter of calling this function with additional chat IDs.
    """
    try:
        resp = requests.post(
            f"{API_BASE}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
                "disable_notification": silent,
            },
            timeout=10,
        )
        if not resp.ok:
            log.warning(
                "Telegram rejected message to %s (HTTP %s): %s",
                chat_id, resp.status_code, resp.text[:200],
            )
            return False
        return True
    except Exception as exc:
        log.error("Telegram send to chat %s failed: %s", chat_id, exc)
        return False


# ---------------------------------------------------------------------------
# Layer 2: public send functions
# ---------------------------------------------------------------------------

def send_signal_message(
    text: str,
    parse_mode: str = "Markdown",
) -> None:
    """
    Deliver a trading signal to the signals channel.

    Target: settings.signals_chat_id
    Audience: traders / signal subscribers

    Failure is logged and swallowed — never interrupts scanning.
    """
    _send_raw(settings.signals_chat_id, text, parse_mode)


def send_admin_message(
    text: str,
    parse_mode: str = "Markdown",
    silent: bool = False,
    priority: MessagePriority = MessagePriority.NORMAL,
) -> None:
    """
    Deliver an operational message to the admin channel.

    Target: settings.admin_chat_id  (may equal signals_chat_id when no
            ADMIN_CHAT_ID is configured — backward-compatible)
    Audience: bot owner / operator

    silent=True suppresses the notification sound — suitable for
    heartbeats and low-priority status messages.

    Failure is logged and swallowed — never interrupts scanning.
    The signals channel is unaffected by any admin delivery failure.
    """
    if priority == MessagePriority.LOW:
        log.debug("admin[%s]: %s", priority.value, text[:80])
    else:
        log.info("admin[%s] → %s", priority.value, text[:80])
    _send_raw(settings.admin_chat_id, text, parse_mode, silent=silent)


def send_message(text: str, parse_mode: str = "Markdown") -> None:
    """
    Backward-compatible alias for send_signal_message().

    All existing call sites that call send_message() continue to work
    without modification.  New code should call send_signal_message() or
    send_admin_message() explicitly.
    """
    send_signal_message(text, parse_mode)


# ---------------------------------------------------------------------------
# Cooldowns (unchanged)
# ---------------------------------------------------------------------------

def _cooldown_key(symbol: str, direction: Direction, kind: str) -> str:
    return f"{symbol}:{direction.value}:{kind}"


def cooldown_ok(symbol: str, direction: Direction, kind: str = "signal") -> bool:
    last = persistence.get_cooldown(_cooldown_key(symbol, direction, kind))
    return (time.time() - last) >= settings.alert_cooldown_seconds


def mark_cooldown(symbol: str, direction: Direction, kind: str = "signal") -> None:
    persistence.set_cooldown(_cooldown_key(symbol, direction, kind), time.time())


# ---------------------------------------------------------------------------
# CSV signal log (unchanged)
# ---------------------------------------------------------------------------

def log_signal(trade: Trade, event: str) -> None:
    """Append a human-readable row to the CSV signal log."""
    file_exists = os.path.isfile(settings.signal_log_file)
    try:
        with open(settings.signal_log_file, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "event", "symbol", "direction", "entry",
                    "stop_loss", "take_profit", "rr", "confluence_score", "trade_id",
                ])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                event,
                trade.symbol,
                trade.direction.value,
                trade.entry_price,
                trade.stop_loss,
                trade.take_profit,
                trade.realized_rr if trade.realized_rr is not None else trade.planned_rr,
                trade.confluence_score,
                trade.id,
            ])
    except Exception as exc:
        log.error("Failed to write signal log: %s", exc)


# ---------------------------------------------------------------------------
# Message builders — pure string functions, no I/O
# ---------------------------------------------------------------------------

def build_pending_alert(trade: Trade) -> str:
    arrow = "🟢" if trade.direction is Direction.BULLISH else "🔴"
    return (
        f"🚨 *Breakout Detected — Waiting for Retest*\n\n"
        f"{arrow} *{trade.symbol}* — {trade.direction.value.upper()}\n"
        f"Entry Zone: `{fmt_price(trade.zone_bottom)} - {fmt_price(trade.zone_top)}`\n"
        f"Projected TP: `{fmt_price(trade.take_profit)}`\n"
        f"Projected SL: `{fmt_price(trade.stop_loss)}`\n"
        f"Planned RR: `{trade.planned_rr:.2f}`\n"
        f"Confluence Score: `{trade.confluence_score}/10`\n"
        f"Expires: in {int((trade.expiry_ts - time.time()) / 60)}m\n\n"
        f"[Chart]({tradingview_link(trade.symbol)})"
    )


def build_entry_alert(trade: Trade) -> str:
    arrow = "🟢" if trade.direction is Direction.BULLISH else "🔴"
    tp_display = trade.actual_target if trade.actual_target is not None else trade.take_profit
    lines = [
        f"🔥 *ENTRY TRIGGERED*\n",
        f"{arrow} *{trade.symbol}* — {trade.direction.value.upper()}",
        f"Entry:       `{fmt_price(trade.entry_price)}`",
        f"Stop Loss:   `{fmt_price(trade.stop_loss)}`",
        f"Take Profit: `{fmt_price(tp_display)}`",
        f"ATR:         `{fmt_price(trade.atr)}`",
        f"Planned RR:  `{trade.planned_rr:.2f}`",
    ]
    if trade.slippage is not None and trade.planned_entry is not None:
        sign = "+" if trade.slippage >= 0 else ""
        lines.append(
            f"Slippage:    `{sign}{fmt_price(trade.slippage)}` "
            f"vs planned `{fmt_price(trade.planned_entry)}`"
        )
    lines.append(f"\n[Chart]({tradingview_link(trade.symbol)})")
    return "\n".join(lines)


def _fmt_duration(triggered_ts: Optional[float], closed_ts: Optional[float]) -> str:
    if not triggered_ts or not closed_ts:
        return "—"
    secs = max(0, int(closed_ts - triggered_ts))
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


def build_outcome_alert(trade: Trade) -> str:
    """
    Detailed trade closure message per spec Step 8.
    All values come from actual executed prices stored on the trade.
    realized_rr is always computed from actual_entry → exit_price,
    never from projected or zone-midpoint prices.
    """
    is_tp = (trade.state.value == "tp_hit")
    icon    = "✅" if is_tp else "❌"
    outcome = "TAKE PROFIT HIT" if is_tp else "STOP LOSS HIT"

    entry    = trade.entry_price  or 0.0
    exit_px  = trade.exit_price   or 0.0
    sl       = trade.stop_loss
    rr       = trade.realized_rr
    rr_str   = f"{rr:+.2f}" if rr is not None else "—"
    pnl_str  = f"{rr:+.2f}R" if rr is not None else "—"

    risk_d   = trade.risk_distance
    rew_d    = trade.reward_distance
    risk_str = fmt_price(risk_d)  if risk_d  is not None else "—"
    rew_str  = fmt_price(rew_d)   if rew_d   is not None else "—"
    if not is_tp and rew_d is not None:
        rew_str = f"-{rew_str}"   # adverse move shown as negative on SL

    duration  = _fmt_duration(trade.triggered_ts, trade.closed_ts)
    journal_id = f"#{trade.id[-4:].upper()}"

    lines = [
        f"{icon} *{outcome}*\n",
        f"*{trade.symbol}* | {trade.direction.value.capitalize()}",
        "",
        f"Entry:        `{fmt_price(entry)}`",
        f"Exit:         `{fmt_price(exit_px)}`",
        f"Risk:         `{risk_str}`",
        f"Reward:       `{rew_str}`",
        "",
    ]
    if is_tp:
        lines += [
            f"Planned RR:   `{trade.planned_rr:.2f}`",
            f"Realized RR:  `{rr_str}`",
            f"PnL:          `{pnl_str}`",
        ]
    else:
        lines += [
            f"Realized RR:  `{rr_str}`",
        ]
    lines += [
        "",
        f"Duration:     `{duration}`",
        f"Confluence:   `{trade.confluence_score}/10`",
        f"Journal ID:   `{journal_id}`",
    ]
    return "\n".join(lines)


def build_status_message() -> str:
    open_trades = persistence.get_trades(states=None)
    pending = [t for t in open_trades if t.state.value == "pending"]
    running = [t for t in open_trades if t.state.value == "running"]
    uptime_s = int(time.time() - _bot_start_time)
    hours, rem = divmod(uptime_s, 3600)
    minutes = rem // 60
    lines = [
        "📊 *Bot Status*",
        f"Uptime: {hours}h {minutes}m",
        f"Symbols tracked: {len(settings.symbols)}",
        f"Pending setups: {len(pending)}",
        f"Running trades: {len(running)}",
        f"Signals → `{settings.signals_chat_id}`",
        f"Admin   → `{settings.admin_chat_id}`",
    ]
    for t in running[:10]:
        lines.append(
            f"  • {t.symbol} {t.direction.value} entry {fmt_price(t.entry_price)} "
            f"SL {fmt_price(t.stop_loss)} TP {fmt_price(t.take_profit)}"
        )
    return "\n".join(lines)


def build_stats_message(lookback_days: Optional[int] = None) -> str:
    s = stats_module.compute_stats(lookback_days)
    period = f"last {lookback_days}d" if lookback_days else "all-time"
    lines = [
        f"📈 *Stats ({period})*",
        f"Signals: {s['total_signals']}  |  Decided: {s['total_decided']}",
        f"Win rate: {s['win_rate']:.1f}%  |  Loss rate: {s['loss_rate']:.1f}%",
        f"Avg RR: {s['avg_rr']:.2f}",
        f"Signals/day: {s['signals_per_day']:.1f}",
        f"Avg trade duration: {stats_module.format_duration(s['avg_duration_seconds'])}",
        f"Expired: {s['expired']}  |  Invalidated: {s['invalidated']}",
    ]
    if s["most_profitable_direction"]:
        lines.append(f"Most profitable direction: {s['most_profitable_direction']}")
    if s["best_symbols"]:
        lines.append("\nBest symbols:")
        for symbol, data in s["best_symbols"]:
            lines.append(f"  • {symbol}: {data['total_r']:.2f}R ({data['wins']}W/{data['losses']}L)")
    return "\n".join(lines)


def build_journal_message() -> str:
    d = dashboard.daily_stats()
    lines = [
        "📓 *Journal — Today*",
        f"Signals detected: `{d.get('total_signals', 0)}`",
        f"Completed trades: `{d.get('completed', 0)}`",
        f"Pending: `{d.get('pending', 0)}`  |  Expired: `{d.get('expired', 0)}`",
        f"Invalidated: `{d.get('invalidated', 0)}`",
    ]
    if d.get("total_decided", 0) > 0:
        pf = d.get("profit_factor")
        pf_str = f"{pf:.2f}" if pf is not None else "∞"
        lines += [
            f"Win rate: `{d['win_rate']:.1f}%`",
            f"Profit factor: `{pf_str}`",
            f"Net R: `{d.get('net_r', 0):+.2f}`",
            f"Avg duration: `{int(d.get('avg_duration_minutes', 0))}m`",
        ]
    else:
        lines.append("_No completed trades today yet._")
    return "\n".join(lines)


def build_performance_message() -> str:
    d = dashboard.weekly_stats()
    pending = dashboard.pending_trades()
    active  = dashboard.active_trades()
    recent  = dashboard.recent_completed(limit=5)
    pf = d.get("profit_factor")
    pf_str = f"{pf:.2f}" if pf is not None else "∞"
    lines = [
        "⚡ *Performance (7 days)*",
        f"Win Rate: `{d.get('win_rate', 0):.1f}%`",
        f"Profit Factor: `{pf_str}`",
        f"Net R: `{d.get('net_r', 0):+.2f}`",
        f"Expectancy: `{d.get('expectancy', 0):+.3f}R`",
        f"Avg RR: `{d.get('avg_rr', 0):.2f}`",
        "",
        f"📊 Trades today: `{d.get('completed', 0)}`",
        f"⏳ Pending setups: `{len(pending)}`",
        f"🔥 Running trades: `{len(active)}`",
        f"✅ Decided (7d): `{d.get('total_decided', 0)}`",
    ]
    if recent:
        lines.append("\n_Recent completions:_")
        for t in recent[:5]:
            icon = "✅" if t.get("result") == "Win" else "❌"
            r = t.get("profit_r")
            r_str = f"{r:+.2f}R" if r is not None else "—"
            lines.append(f"  {icon} {t['symbol']} {t['direction']}  {r_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 3: high-level senders — each routed to signal or admin channel
# ---------------------------------------------------------------------------

# -- Signal senders (trading alerts) ----------------------------------------

def send_pending_alert(trade: Trade) -> None:
    """Breakout detected, waiting for retest → signals channel."""
    send_signal_message(build_pending_alert(trade))
    log_signal(trade, "pending")


def send_entry_alert(trade: Trade) -> None:
    """Retest confirmed, entry triggered → signals channel."""
    send_signal_message(build_entry_alert(trade))
    log_signal(trade, "triggered")


def send_outcome_alert(trade: Trade) -> None:
    """TP or SL hit → signals channel."""
    send_signal_message(build_outcome_alert(trade))
    log_signal(trade, trade.state.value)


# -- Admin senders (operational messages) ------------------------------------

def send_startup_alert() -> None:
    """Bot started → admin channel."""
    if not settings.startup_alert:
        return
    send_admin_message(
        f"🤖 *Bot V2 started*\n"
        f"Tracking {len(settings.symbols)} symbols\n"
        f"Retest mode: {settings.retest_mode.value}\n"
        f"Confluence threshold: {settings.confluence_threshold}/10\n"
        f"Signals → `{settings.signals_chat_id}`\n"
        f"Admin   → `{settings.admin_chat_id}`",
        priority=MessagePriority.NORMAL,
    )


def send_heartbeat() -> None:
    """Periodic liveness ping → admin channel, silently."""
    send_admin_message(
        f"💓 Heartbeat — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        silent=True,
        priority=MessagePriority.LOW,
    )


def send_daily_summary() -> None:
    """Daily performance snapshot → admin channel."""
    send_admin_message(
        build_stats_message(lookback_days=1),
        priority=MessagePriority.NORMAL,
    )


def send_crash_report(error: str) -> None:
    """Unhandled exception in main loop → admin channel, high priority."""
    send_admin_message(
        f"⚠️ *Bot crashed and is recovering*\n```\n{error[:500]}\n```",
        priority=MessagePriority.HIGH,
    )


# ---------------------------------------------------------------------------
# Command polling
# ---------------------------------------------------------------------------

def _handle_command(text: str, reply_chat_id: str) -> None:
    """
    Dispatch a bot command and send the reply to the chat it came from.

    Commands are accepted from both the signals channel and the admin
    channel so traders can query signal stats and the admin can trigger
    ops actions from their own channel.  Replies always go back to the
    originating chat so each audience sees responses in context.
    """
    text = text.strip().lower()

    def reply(msg: str) -> None:
        _send_raw(reply_chat_id, msg)

    if text.startswith("/status"):
        reply(build_status_message())

    elif text.startswith("/stats"):
        parts = text.split()
        lookback = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        reply(build_stats_message(lookback_days=lookback))

    elif text.startswith("/journal"):
        reply(build_journal_message())

    elif text.startswith("/performance"):
        reply(build_performance_message())

    elif text.startswith("/backup"):
        try:
            dest = journal_mod.backup_database()
            if dest:
                reply(f"✅ *Backup created*\n`{dest}`")
            else:
                reply("⚠️ Backup failed or journal DB not found yet.")
        except Exception as exc:
            reply(f"⚠️ Backup error: `{exc}`")


def poll_commands_loop() -> None:
    """
    Long-poll Telegram for incoming messages.

    Accepts commands from both the signals channel and the admin channel.
    Replies are routed back to the originating chat.
    Uses exponential backoff on errors (5 s → 10 s → … → 300 s cap).
    """
    global _last_update_id

    # Both channel IDs may be equal when only one chat is configured --
    # that's fine; the set deduplicates naturally.
    authorized_chats = {
        str(settings.signals_chat_id),
        str(settings.admin_chat_id),
    }

    _backoff = 5
    while True:
        try:
            res = requests.get(
                f"{API_BASE}/getUpdates",
                params={"offset": _last_update_id + 1, "timeout": 25},
                timeout=30,
            )
            data = res.json()
            for update in data.get("result", []):
                _last_update_id = max(_last_update_id, update["update_id"])
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text and chat_id in authorized_chats:
                    _handle_command(text, reply_chat_id=chat_id)
            _backoff = 5   # reset on any successful round-trip
        except Exception as exc:
            log.error("Telegram polling error: %s", exc)
            time.sleep(_backoff)
            _backoff = min(_backoff * 2, 300)


def start_polling_thread() -> threading.Thread:
    t = threading.Thread(target=poll_commands_loop, daemon=True)
    t.start()
    return t
