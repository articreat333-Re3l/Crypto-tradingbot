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
# Fill in TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, and optionally DATABASE_URL
python botv2.py
```

---

## Deploy to Render

### Option A — Blueprint (recommended)

1. Push the repo root (not the folder itself) to GitHub
2. Render Dashboard → **New → Blueprint** → connect repo
   `render.yaml` provisions the Background Worker + free Postgres automatically
3. Set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in the worker's Environment tab
4. Deploy

### Option B — Manual

1. Render Dashboard → **New → Background Worker**
2. Build command: `pip install -r requirements.txt`
3. Start command: `python botv2.py`
4. Create a **New → PostgreSQL** (free tier), paste the External URL into `DATABASE_URL`
5. Add `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`

> ⚠️ `DATABASE_URL` controls the **live trading state** (pending trades, cooldowns).
> The **research journal** (`data/journal.db`) is a separate SQLite file.
> On Render's ephemeral filesystem it resets on redeploy unless you attach a
> Render persistent disk and set `SQLITE_PATH` / mount at `data/`.
> Daily CSV exports (`exports/`) are written each loop and can be pulled via
> the `/backup` Telegram command before a redeploy.

---

## Environment Variables

See `.env.example` for the full list. Required:

| Variable          | Description                                   |
|-------------------|-----------------------------------------------|
| `TELEGRAM_TOKEN`  | BotFather token                               |
| `TELEGRAM_CHAT_ID`| Your Telegram user/group ID                   |
| `DATABASE_URL`    | Render Postgres URL (recommended for live DB) |

---

## Telegram Commands

| Command        | Description                                               |
|----------------|-----------------------------------------------------------|
| `/status`      | Pending/running trades, uptime                            |
| `/stats`       | All-time win rate, RR, best symbols (from persistence DB) |
| `/stats 30`    | Same, last 30 days                                        |
| `/journal`     | Today's journal stats (from research journal)             |
| `/performance` | 7-day win rate, PF, expectancy, recent completions        |
| `/backup`      | Create an immediate journal.db backup                     |

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
confluence.py     Scoring engine (HTF, EMA, OB, FVG, sweep, level quality)
risk_manager.py   SL/TP/RR calculation
trade_manager.py  Retest engine, trade lifecycle state machine
persistence.py    SQLAlchemy live-state DB (trades, cooldowns, kv_state)
telegram_bot.py   Alerts, commands, heartbeat, daily summary
stats.py          Live stats from persistence DB
market_data.py    OKX candle fetching + TTL cache
models.py         Shared dataclasses and enums
config.py         All settings loaded from env vars
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

### Database — `data/journal.db`

Four tables, auto-created on first launch:

| Table     | Purpose                                          |
|-----------|--------------------------------------------------|
| `signals` | Every detected setup with all confluence factors |
| `trades`  | Entry/exit details for triggered signals         |
| `health`  | Scan cycle metrics (duration, pending count)     |
| `errors`  | Every caught exception with full traceback       |

### Automatic Exports

Every 24 hours the bot writes:
```
exports/signals.csv
exports/trades.csv
exports/health.csv
exports/errors.csv
```

Trigger immediately from Telegram: `/backup`
Or from Python: `python -c "import journal; journal.export_all()"`

### Backups

Every 24 hours:
```
data/backups/journal_YYYYMMDD.db
```
Capped at 30 backups. Oldest pruned automatically.

---

## Analytics

```bash
# Full all-time report
python analyze.py

# Last 30 days
python analyze.py --days 30

# Single symbol
python analyze.py --symbol BTCUSDT
```

Report includes: win rate, profit factor, expectancy, max drawdown,
streaks, session breakdown, confluence band analysis, symbol ranking.

---

## Parameter Optimisation

```bash
# Optimise confluence threshold and min RR from journal history
python optimizer.py

# Filter to last 30 days
python optimizer.py --days 30
```

Tests confluence thresholds 4–9 and RR filters 1.5–3.0 against historical
outcomes. Prints ranked tables and highlights best combinations.

For ATR multiplier / pending expiry optimisation (requires backtesting):

```bash
python backtest.py --symbol BTCUSDT --tf 15m --lookback 800 --optimize
```

---

## Backtest

```bash
# Basic single-timeframe
python backtest.py --symbol BTCUSDT --tf 15m --lookback 500 --rr 2.0

# Multi-timeframe with confirmation candle
python backtest.py --symbol ETHUSDT --tf 15m --lookback 800 --htf 1H --confirm

# Export trade log and equity curve
python backtest.py --symbol BTCUSDT --tf 15m --lookback 500 --csv trades.csv --equity equity.csv

# Full parameter grid search
python backtest.py --symbol BTCUSDT --tf 15m --lookback 500 --optimize
```

---

## Key Design Decisions

**Dual database architecture**
`persistence.py` (SQLAlchemy, Postgres/SQLite) holds the live trading state machine — pending trades, cooldowns, bot state. `journal.py` (raw sqlite3) holds the research journal — every signal, outcome, health tick, and error. They're intentionally separate so a journal problem can never affect trading.

**Confluence scoring over hard AND-gates**
Every filter (HTF trend, EMA, OB, FVG, volume, ATR, sweep) contributes points to a score. A configurable threshold passes the signal. This keeps every factor meaningful without compounding to near-zero signal frequency.

**Zone-based stop loss**
SL is placed beyond the zone's far edge plus an ATR buffer, not at the breakout candle's wick. The wick stop places the SL inside the zone — exactly where price is expected to trade during the retest.

**Stateless retest engine**
Pending trade monitoring re-examines a trailing window of candles each loop rather than tracking "have we touched the zone" in memory. A restart mid-retest picks up exactly where it left off from the persisted trade state.
