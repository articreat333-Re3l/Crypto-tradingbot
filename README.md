# Crypto Scanner Bot V2

Smart Money / retest-based crypto scanner for OKX perpetual swaps, with a
research-grade trading journal, analytics reports, parameter optimiser, and
dashboard data layer.

## How it works

```
Structure (swing HH/HL/LH/LL)
   → Auto S/R levels (repeated swing clusters)
      → Breakout confirmation (body + volume + ATR + sweep rejection)
         → Zone construction (origin of the impulsive move)
            → Confluence scoring (HTF trend, EMA, OB, FVG, sweep, level quality)
               → Pending trade (waits for retest)
                  → Entry trigger (rejection candle inside zone, configurable)
                     → TP/SL monitor → outcome tracking → journal → stats
```

---

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in TELEGRAM_TOKEN plus SIGNALS_CHAT_ID (and optionally ADMIN_CHAT_ID)
python botv2.py
```

---

## Database

The project uses **SQLite exclusively** — no external database required.

| File | Purpose |
|---|---|
| `botv2.db` | Live trading state (trades, cooldowns, bot state) — SQLAlchemy Core |
| `data/journal.db` | Research journal (signals, health, errors) — raw sqlite3 |

Both files are created automatically on first run.

### Persistence on Render

Render's filesystem resets on each redeploy. To preserve data across deploys,
attach a **Render Persistent Disk** to the worker and point both database files at it:

```
SQLITE_PATH=/data/botv2.db
```

Set `journal.DB_PATH` to `/data/journal.db` in the same way, or accept that the
journal resets on redeploy while the trading state (cooldowns, pending trades) is preserved.

---

## Deploy to Render

### Option A — Blueprint (recommended)

1. Push the repo root to GitHub
2. Render Dashboard → **New → Blueprint** → connect repo
   `render.yaml` provisions the Background Worker automatically
3. Set `TELEGRAM_TOKEN`, `SIGNALS_CHAT_ID`, and `ADMIN_CHAT_ID` in the
   worker's Environment tab
4. (Optional) Attach a Persistent Disk and set `SQLITE_PATH=/data/botv2.db`
5. Deploy

### Option B — Manual

1. Render Dashboard → **New → Background Worker**
2. Build command: `pip install -r requirements.txt`
3. Start command: `python botv2.py`
4. Set environment variables (see `.env.example`)

---

## Environment Variables

See `.env.example` for the full list. Required:

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | BotFather token |
| `SIGNALS_CHAT_ID` | Trading signal channel (traders see this) |
| `ADMIN_CHAT_ID` | Admin/ops channel — falls back to `SIGNALS_CHAT_ID` |

Backward-compatible: `TELEGRAM_CHAT_ID` still works if you haven't migrated.

---

## Telegram Channels

Two independent channels with independent failure handling:

| Channel | Messages |
|---|---|
| `SIGNALS_CHAT_ID` | Breakout detected, entry triggered, TP/SL hit |
| `ADMIN_CHAT_ID` | Startup, heartbeat, daily summary, crash reports, backups |

---

## Telegram Commands

| Command | Description |
|---|---|
| `/status` | Pending/running trades, uptime |
| `/stats` | All-time win rate, RR, best symbols |
| `/stats 30` | Same, last 30 days |
| `/journal` | Today's journal stats |
| `/performance` | 7-day win rate, PF, expectancy, recent completions |
| `/backup` | Create an immediate journal.db backup |

---

## File Structure

```
botv2.py          Entry point, main loop, scheduler
scanner.py        Per-symbol orchestration + journal hooks
structure.py      Swing detection, trend, S/R levels
breakout.py       Breakout confirmation
zones.py          Supply/demand zone construction
order_blocks.py   Order block detection
fvg.py            Fair value gap detection
confluence.py     Scoring engine
risk_manager.py   SL/TP/RR calculation
trade_manager.py  Retest engine, trade lifecycle state machine
persistence.py    SQLite live-state DB via SQLAlchemy Core
telegram_bot.py   Signal + admin channels, commands, heartbeat
stats.py          Live stats from persistence DB
market_data.py    OKX candle fetching + TTL cache
models.py         Shared dataclasses and enums
config.py         All settings from env vars + .env file
utils.py          Shared helpers

journal.py        Research journal (sqlite3, data/journal.db)
dashboard.py      Data-access layer for a future web UI
analyze.py        Standalone analytics report
optimizer.py      Parameter optimisation from journal history
backtest.py       Historical replay harness

data/
  journal.db      Research journal (auto-created on first run)
  backups/        Daily journal backups (30-day rolling window)
exports/          Daily CSV exports: signals, trades, health, errors
logs/             Log output directory
```

---

## Journal System

### Automatic Exports

Every 24 hours the bot writes:
```
exports/signals.csv
exports/trades.csv
exports/health.csv
exports/errors.csv
```

Trigger immediately: `/backup` via Telegram, or:
```bash
python -c "import journal; journal.export_all()"
```

---

## Analytics

```bash
python analyze.py              # full all-time report
python analyze.py --days 30    # last 30 days
python analyze.py --symbol BTCUSDT
```

---

## Parameter Optimisation

```bash
python optimizer.py            # optimise from journal history
python optimizer.py --days 30

# For ATR/expiry optimisation (requires backtesting):
python backtest.py --symbol BTCUSDT --tf 15m --lookback 800 --optimize
```

---

## Backtest

```bash
python backtest.py --symbol BTCUSDT --tf 15m --lookback 500 --rr 2.0
python backtest.py --symbol ETHUSDT --tf 15m --lookback 800 --htf 1H --confirm
python backtest.py --symbol BTCUSDT --tf 15m --lookback 500 --csv trades.csv --equity equity.csv
python backtest.py --symbol BTCUSDT --tf 15m --lookback 500 --optimize
```

---

## Key Design Decisions

**SQLite-only, two files**
`botv2.db` (SQLAlchemy Core) holds live trading state. `data/journal.db` (raw sqlite3)
holds the research journal. Both are SQLite. No external database service required.
WAL mode is enabled on both so the Telegram polling thread and scanner loop never block each other.

**Dual Telegram channels**
`send_signal_message()` and `send_admin_message()` are fully independent — a delivery
failure to one channel never affects the other. Both route through a single `_send_raw()`
function so HTTP logic lives in exactly one place.

**Confluence scoring over hard AND-gates**
Every filter contributes points to a score. A configurable threshold passes the signal.
This keeps every factor meaningful without compounding to near-zero signal frequency.

**Zone-based stop loss**
SL is placed beyond the zone's far edge plus an ATR buffer, not at the breakout candle wick.

**Stateless retest engine**
Pending trade monitoring re-examines a trailing candle window each loop. A restart
mid-retest picks up exactly where it left off from the persisted trade state.

**.env for local development**
Copy `.env.example` to `.env` for local runs. On Render, variables are set in the
dashboard and the file doesn't exist — no conflict possible.
