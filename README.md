# AI Trading Dashboard, Equity Research & Agentic Trading Bot

Market dashboard (live quotes, charts, indicators, ML forecasts, news sentiment, portfolio analytics), an
**equity research engine** (fundamentals, valuation, factor scorecards and AI analyst reports), and an
**agentic, risk-managed trading bot** that paper-trades by default.

## ⚠️ Read this first

- **No trading system can guarantee profits or prevent losses.** This bot is engineered to *limit* losses and to
  *refuse* trades whose expected edge doesn't clear transaction costs, not to promise gains. Losing trades and
  losing periods will happen.
- The bot runs in **paper mode** (simulated money) unless you explicitly configure otherwise. Live trading
  requires `TRADING_MODE=alpaca_live` **and** `ALLOW_LIVE_TRADING=true`.
- Backtests (including walk-forward) are simulations. Real fills, outages and regime changes will differ. Paper-trade
  for weeks and review the decision journal before risking real money.
- Nothing here is financial advice.

---

## Equity research (analyst-style)

Two tiers, the way research desks work:

1. **Screen everything, cheaply** (`POST /api/research/screen`, `/research` page). Every symbol gets a
   deterministic dossier from its financial statements:
   - **Fundamentals**: revenue/EPS/FCF growth, gross/operating/net/FCF margins and their trend, ROIC, ROE,
     leverage (net debt/EBITDA, interest coverage), liquidity, cash conversion, accruals, dilution, shareholder yield,
     **Piotroski F-score**, **Altman Z-score**, and red flags.
   - **Valuation**: P/E, EV/EBITDA, EV/Sales, FCF yield, P/B; cost of capital (CAPM); a two-stage **DCF with
     bear/base/bull scenarios** and a probability-weighted fair value; a **reverse DCF** (the growth rate the market
     is pricing in); justified P/B for banks and insurers.
   - **Factor scorecard** (0–100): value, quality, growth, momentum, low risk, street sentiment (analyst
     targets, recommendation trend, earnings surprises), with cross-sectional percentiles when screening.
2. **Deep-dive the shortlist** (`GET /api/research/{symbol}/report`, the dashboard "Research" tab). A Claude analyst
   agent (`claude-opus-5-5`) investigates with tools that expose the dossier, street data, technicals and news
   (optionally live web search) and writes a structured report: rating, conviction, bear/base/bull 12-month targets
   with probabilities, thesis, catalysts, risks, moat, and "what would change our mind". Every number must come from
   tool data; reports are validated (probabilities, target ordering, rating vs. expected return). Without an API key,
   or for non-admin users, a free rules-based report is produced from the same dossier.

AI reports cost API credits (roughly $0.50–$2 each), so only administrators trigger new ones and results are cached
(`RESEARCH_REPORT_MAX_AGE_HOURS`). The bot uses research only as a **filter**: it skips financially distressed
companies (Altman distress zone, Piotroski ≤ 2) and avoids opening positions in the days before earnings.

## How the bot decides

Each cycle (default every 15 minutes, `python -m backend.trading.runner`):

```
observe ──► analyse ──► estimate edge ──► risk manager ──► (Claude review) ──► execute ──► journal
```

1. **Observe**: account, positions and quotes from the broker. Positions are reconciled with the bot's own
   stop/target records, including exits and partial fills that happened at the broker between cycles (booked at
   the real fill price). Positions opened elsewhere are ignored unless `BOT_ADOPT_EXTERNAL_POSITIONS=true`, which
   adopts them with a protective stop.
2. **Protect first**: every cycle (even when paused) enforces hard stops, take-profits, trailing stops
   (breakeven + costs after +1.5 ATR, then a 3-ATR chandelier trail), signal-reversal exits and a 40-day time stop.
3. **Analyse** (`backend/trading/strategy.py`) on **completed daily bars only**: trend (EMA 20/50/200 + MACD,
   scaled by ADX), momentum (vol-normalised 3-month and 1-month returns), regime-aware mean reversion and OBV volume
   flow, weighted by the detected **market regime** (bull / bear / range / high volatility). This *technical score*
   is exactly what the backtest and the calibration see. ML, sentiment and fundamentals are **overlays that can only
   block** a trade (never create one), because they cannot be replayed point-in-time in a backtest.
4. **Estimate edge** (`calibration.py`): a score is not a probability. Walk-forward backtests learn win rate and
   average win/loss in R-multiples per score bucket. These estimates are shrunk toward a conservative prior
   (Beta-binomial), so a few lucky trades can't inflate confidence.
5. **Risk manager** (`risk.py`), the final authority that nothing can bypass:
   - risk ≤ 1% of equity per trade (quarter-Kelly, capped), ≤ 15% per position, ≤ 90% gross exposure, no leverage
   - **cost gate**: expected edge must be ≥ 2× round-trip costs (spread + slippage + commission + fees)
   - minimum expectancy (0.10R) and reward:risk (1.5)
   - correlation limit vs. existing positions (including ones opened earlier in the same cycle), max 8 positions
   - size halves in high-volatility regimes, halves again until the edge is calibrated, and shrinks as drawdown grows
   - no re-entry for 24h after a stop-out, nor on the same day after any exit; skip if price gapped > 2 ATR from
     the signal
   - **circuit breakers**: −2% day → no new entries; 4 consecutive losses → 24h cooldown;
     −10% from the high-water mark → **kill switch** (flatten everything, halt and pause until an admin resets the
     halt *and* restarts the bot). Deposits/withdrawals are excluded from these numbers (each broker cash activity
     is counted once; if they can't be read, the breakers are frozen for that cycle rather than guessing).
   - with a real broker, no entries at all until `/calibrate` has stored ≥ 30 out-of-sample trades with positive
     expectancy
6. **Claude review (optional)**: with `LLM_REVIEW_ENABLED=true`, Claude (`claude-opus-5-5`) investigates each
   approved trade using read-only tools (price history, headlines, portfolio). It looks for things price signals
   miss, such as earnings inside the holding period, fraud probes or halts. It can only **approve, shrink or veto**,
   never enlarge a trade. If the API fails or a review takes longer than 90 s, the default is to veto.
7. **Execute**: paper broker (fills at the live quote ± spread/slippage/fees), or Alpaca with **GTC bracket
   orders** whose fills are confirmed by polling. Every cycle verifies each position has a broker-side stop and
   re-arms it if missing; exits cancel every bracket leg first and re-protect the position if the close fails.
8. **Journal**: every evaluation, including trades *not* taken and why, is stored and shown on the `/bot` page.

### Operational safety

- **One trader**: a DB lease with a unique token per cycle, renewed and re-checked before every order.
- **Durable**: each order is journaled as `pending` before it is sent; its outcome is committed together with the
  resulting position or trade. If the outcome is lost (network timeout, crash, a cancel that wasn't confirmed), the
  next cycle looks the order up at the broker by its unique client order id: a fill becomes a managed position with
  the stop and target it was sent with, never an orphan. One failing symbol cannot roll back the others.
- **Fail safe on bad data**: a held position without a trustworthy price freezes equity/high-water-mark/breakers,
  blocks entries and alerts. A price more than 30% away from the last accepted mark is not used for equity, the kill
  switch, exits or paper fills until a split explains it (the split feed is re-read without its cache, and at Alpaca
  the position's own split adjustment is followed) or a second observation confirms it.
- **Panic button** (`/flatten`): durable halt + flatten request. Every entry is fenced atomically against the flag
  right before it is sent, so nothing new is sent after the press; a cycle that is already running flattens before
  it finishes, otherwise the request closes positions immediately. Positions in closed markets are closed at the
  next open.
- **Alerts** to `ALERT_WEBHOOK_URL` (kill switch, flatten, data faults, failed cycles, rejected orders, missing broker
  stops) and a heartbeat (`GET /api/bot/health`, container healthcheck).

### Validate before trusting it

```bash
# Walk-forward, out-of-sample backtest of the exact live logic (needs internet for Yahoo data)
curl -X POST localhost:8000/api/bot/backtest -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"symbols":["AAPL","MSFT","NVDA","JPM","WMT"],"period":"5y","walk_forward":true}'
```

Results include costs paid, max drawdown, Sharpe/Sortino, expectancy and an equal-weight buy-and-hold benchmark.
`POST /api/bot/calibrate` (admin) stores the learned edge statistics that the live bot uses.

Backtest assumptions are conservative: signals execute at the **next bar's open**, every fill pays costs,
gaps through a stop fill at the worse open price, and if a bar touches both stop and target, the stop is assumed hit.

---

## Architecture

```
frontend (Next.js) ──► nginx ──► backend (FastAPI) ──► PostgreSQL / Redis
                                    │    ▲
                                    │    └── bot runner (same image, one process)
                                    └──► ml_service (XGBoost + ARIMA [+ LSTM], FinBERT/lexicon sentiment)
                                    └──► broker: paper simulator | Alpaca
                                    └──► Claude API (optional reviewer)
```

| Path | What |
|---|---|
| `shared/indicators.py` | Pure pandas/numpy indicators (RSI, MACD, BB, ATR, ADX, Stoch, OBV, VWAP…), causal by construction |
| `backend/trading/` | `strategy`, `regime`, `calibration`, `risk`, `costs`, `backtest`, `broker`, `llm_reviewer`, `agent`, `runner`, `router` |
| `backend/` | API: auth (JWT), market data, indicators, predictions, signals, portfolio, alerts |
| `ml/` | Per-symbol models predicting next-day **returns**, weighted by holdout skill; no train/test leakage |
| `tests/` | Offline suite on synthetic data: indicators, risk, strategy, backtests, agent cycles, API, ML |

---

## Quick start

### Docker (full stack)

```bash
cp .env.example .env
# set POSTGRES_PASSWORD and JWT_SECRET_KEY (openssl rand -hex 32)
docker compose up --build
```

Open http://localhost (published on loopback only; see `HTTP_BIND`). Create the administrator (bot controls)
with the CLI, since registration never grants admin rights outside development:

```bash
docker compose exec backend python -m backend.manage create-admin --email you@example.com --username admin
```

### Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements-dev.txt
cp .env.example .env                      # SQLite by default, no Postgres needed
alembic upgrade head
uvicorn backend.main:app --reload         # API on :8000, docs at /docs
python -m backend.trading.runner          # the bot (separate terminal)

# optional ML service
pip install -r ml/requirements.txt
uvicorn ml.api.server:app --port 8001
python -m ml.training.train --symbol AAPL,MSFT --period 5y

# frontend
cd frontend && npm ci && npm run dev      # http://localhost:3000
```

### Tests

```bash
python -m pytest            # backend, bot, API (offline)
cd frontend && npm run lint && npm run typecheck && npm run build
```

---

## Bot API (`/api/bot`)

| Method | Path | Who | |
|---|---|---|---|
| GET | `/status` `/positions` `/trades` `/orders` `/decisions` `/equity` `/performance` | admin | State and journal |
| GET | `/analyze/{symbol}` | user | What the bot thinks now (no order placed) |
| POST | `/backtest` | admin | Backtest / walk-forward |
| POST | `/start` `/stop` | admin | Enable entries / pause entries (stops stay enforced) |
| POST | `/run-once` | admin | Run one full cycle now |
| POST | `/flatten` | admin | Panic button: close everything and pause |
| POST | `/reset-halt` | admin | Acknowledge a circuit-breaker halt (the bot stays paused until `/start`) |
| PUT | `/config` | admin | Adjust risk limits (within hard bounds) and the universe |
| POST | `/calibrate` | admin | Walk-forward calibrate the live edge estimates |

## Configuration

See `.env.example`. Key settings: `TRADING_MODE`, `ALLOW_LIVE_TRADING`, `BOT_UNIVERSE`, `BOT_CYCLE_MINUTES`,
`LLM_REVIEW_ENABLED`, `ANTHROPIC_API_KEY`, `JWT_SECRET_KEY` (required, ≥32 chars, outside development).

## Production checklist

- `JWT_SECRET_KEY` = `openssl rand -hex 32` (the Docker stack refuses to start without a strong secret).
- Create the admin with `python -m backend.manage create-admin`, then set `REGISTRATION_OPEN=false` if nobody else
  should sign up. `POST /auth/logout-all` (or `manage revoke-tokens`) revokes every session.
- Put the stack behind **HTTPS** (e.g. Caddy or a load balancer with a certificate). nginx publishes plain HTTP on
  `HTTP_BIND` (default `127.0.0.1`); never expose that port to the internet.
- Set `ALERT_WEBHOOK_URL` and point an uptime monitor at `/api/bot/health`.
- Use a **dedicated broker account** for the bot (`BOT_ADOPT_EXTERNAL_POSITIONS=false` ignores other holdings).

## When the bot halts (runbook)

1. Read `GET /api/bot/status` (`halt_reason`, `last_error`, `flatten_requested`) and the latest `/decisions`.
2. Kill switch or `/flatten`: positions are closed as markets open; `flatten_requested` clears once nothing the bot
   owns is left. Check the broker directly if alerts mention `exit_failed` or `protection_failed`.
3. `DATA_FAULT` / `degraded`: a held symbol has no trustworthy price, or deposits/withdrawals couldn't be read.
   Stops keep being enforced at the broker (Alpaca); fix the data source, nothing needs resetting.
4. `RECOVER` decisions: an order's confirmation was lost and has been reconciled; verify it against the broker.
5. When you understand what happened: `POST /reset-halt` (re-bases the high-water mark), then `POST /start`.

## Before risking real money

1. Run `POST /api/bot/calibrate` and read the out-of-sample result. If expectancy is not clearly positive after
   costs, do not go further.
2. Paper-trade (`TRADING_MODE=paper`, then `alpaca_paper`) for several weeks. Read the decision journal and compare
   realised results with the backtest.
3. Only then consider `alpaca_live` with a small allocation and tight limits (`PUT /api/bot/config`).

## Known limitations

- Daily-bar strategy; it is not a high-frequency system. Paper mode detects holidays from Yahoo data on reference
  instruments (SPY/QQQ/DIA, NIFTY) but not early closes; Alpaca modes use the broker clock.
- Paper splits rely on Yahoo's split feed (re-read without the cache whenever a held price jumps). If Yahoo has no
  record of a split at all, the move is treated as a real one after a second observation.
- Backtests and calibration use today's symbols (survivorship bias) and a flat cost model that is not liquidity-aware,
  so results are optimistic for small or illiquid names. ML and sentiment are not backtested (hence veto-only).
- The walk-forward resets the high-water mark per fold; the daily-loss window follows the New York calendar date.
- Live `costs` in the trade journal count commissions and fees; backtest `costs` also include spread and slippage.
- Long-only. Shorting is intentionally not implemented (unbounded loss).
- Yahoo Finance data is free but unofficial and can be delayed or rate-limited.
- Alpaca supports US equities only; NSE symbols work in the dashboard, backtests and the paper broker.
