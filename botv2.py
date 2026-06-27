"""
botv2.py
========
Entry point. Run with:  python botv2.py

Boot sequence:
  1. load_settings() (validates env vars, raises early if Telegram creds missing)
  2. init_db()
  3. Send startup Telegram alert
  4. Start Telegram command-polling thread (daemon)
  5. Main loop:
       - run_once(symbols) -- the full scanner pass
       - Heartbeat check
       - Daily summary check
       - sleep(loop_interval_seconds)
     Wrapped in try/except: log crash + send Telegram alert + continue.
     Process never exits on scanner errors -- Render Background Workers
     already handle process-level restarts; crashing from Python exceptions
     would cause runaway restart loops.
"""

from __future__ import annotations

import signal
import sys
import time
import traceback
from datetime import datetime, timezone

import persistence
import scanner
import journal
import telegram_bot as tg
from config import settings
from logger import get_logger
from models import TradeState

log = get_logger("botv2")

_shutdown_requested = False


def _handle_sigterm(signum, frame):
    global _shutdown_requested
    log.info("SIGTERM received, shutting down cleanly after current loop iteration")
    _shutdown_requested = True


def _should_send_daily_summary(last_summary_date: str) -> bool:
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    return (
        now_utc.hour == settings.daily_summary_hour_utc
        and today != last_summary_date
    )


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)

    log.info("Bot V2 starting up")
    log.info("Symbols: %s", ", ".join(settings.symbols))
    log.info("Execution TF: %s | HTF: %s | Confirmation TF: %s",
             settings.execution_timeframe, settings.htf_timeframe, settings.confirmation_timeframe)
    log.info("Retest mode: %s | Confluence threshold: %d/10",
             settings.retest_mode.value, settings.confluence_threshold)

    persistence.init_db()
    journal.initialize_database()

    tg.send_startup_alert()
    tg.start_polling_thread()

    last_heartbeat = time.time()
    last_summary_date = persistence.get_kv("last_summary_date", default="")
    last_backup_date = persistence.get_kv("last_backup_date", default="")
    heartbeat_sent_this_loop = False

    log.info("Entering main loop. Loop interval: %ds", settings.loop_interval_seconds)

    while not _shutdown_requested:
        loop_start = time.time()
        heartbeat_sent_this_loop = False

        try:
            scanner.run_once(settings.symbols)
        except KeyboardInterrupt:
            log.info("Keyboard interrupt, shutting down")
            break
        except Exception as e:
            err_text = traceback.format_exc()
            log.error("Main loop crashed: %s", e)
            try:
                tg.send_crash_report(err_text)
            except Exception:
                pass
            try:
                journal.record_error(None, "botv2", "main_loop", e, err_text)
            except Exception:
                pass

        now = time.time()

        if now - last_heartbeat >= settings.heartbeat_interval_seconds:
            try:
                tg.send_heartbeat()
                heartbeat_sent_this_loop = True
            except Exception as e:
                log.error("Heartbeat send failed: %s", e)
            last_heartbeat = now

        if _should_send_daily_summary(last_summary_date):
            try:
                tg.send_daily_summary()
                last_summary_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                persistence.set_kv("last_summary_date", last_summary_date)
            except Exception as e:
                log.error("Daily summary failed: %s", e)

        # Daily backup + CSV export (journal)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today_str != last_backup_date:
            try:
                journal.backup_database()
                journal.export_csv()
                last_backup_date = today_str
                persistence.set_kv("last_backup_date", today_str)
            except Exception as e:
                log.error("Journal backup/export failed: %s", e)

        # Health record -- captures scan cycle metrics
        try:
            all_open = persistence.get_trades(states=[TradeState.PENDING, TradeState.TRIGGERED, TradeState.RUNNING])
            pending_n = sum(1 for t in all_open if t.state is TradeState.PENDING)
            running_n = sum(1 for t in all_open if t.state is TradeState.RUNNING)
            journal.record_health(
                symbols_scanned=len(settings.symbols),
                pending_trades=pending_n,
                active_trades=running_n,
                scan_duration_ms=int((time.time() - loop_start) * 1000),
                heartbeat_sent=heartbeat_sent_this_loop,
            )
        except Exception as e:
            log.debug("journal.record_health non-fatal: %s", e)

        elapsed = time.time() - loop_start
        sleep_for = max(0, settings.loop_interval_seconds - elapsed)
        if sleep_for > 0:
            log.debug("Loop took %.1fs, sleeping %.1fs", elapsed, sleep_for)
            time.sleep(sleep_for)

    log.info("Bot shut down cleanly")


if __name__ == "__main__":
    main()
