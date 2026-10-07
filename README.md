# AI Trading Dashboard & Agentic Trading Bot

Market dashboard (live quotes, charts, indicators, ML forecasts, news sentiment, portfolio analytics) plus an
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

## How the bot decides

Each cycle (default every 15 minutes, `python -m backend.trading.runner`):

```
observe ──► analyse ──► estimate edge ──► risk manager ──► (Claude review) ──► execute ──► journal
```

1. **Observe**: account, positions and quotes from the broker. Positions are reconciled with the bot's own
   stop/target records; positions opened elsewhere are adopted with a protective stop.
2. **Protect first**: every cycle (even when paused) enforces hard stops, take-profits, trailing stops
   (breakeven + costs after +1.5 ATR, then a 3-ATR chandelier trail), signal-reversal exits and a 40-day time stop.
3. **Analyse** (`backend/trading/strategy.py`): six evidence sources scored in [-1, 1] and weighted by the
   detected **market regime** (bull trend / bear trend / range / high volatility):
   trend (EMA 20/50/200 + MACD, scaled by ADX), momentum (vol-normalised 3-month and 1-month returns),
   regime-aware mean reversion, OBV volume flow, the ML ensemble's P(up), and news sentiment.
4. **Estimate edge** (`calibration.py`): a score is not a probability. Walk-forward backtests learn win rate and
   average win/loss in R-multiples per score bucket. These estimates are shrunk toward a conservative prior
   (Beta-binomial), so a few lucky trades can't inflate confidence.
5. **Risk manager** (`risk.py`), the final authority that nothing can bypass:
   - risk ≤ 1% of equity per trade (quarter-Kelly, capped), ≤ 15% per position, ≤ 90% gross exposure, no leverage
   - **cost gate**: expected edge must be ≥ 2× round-trip costs (spread + slippage + commission + fees)
   - minimum expectancy (0.10R) and reward:risk (1.5)
   - correlation limit vs. existing positions, max 8 open positions
   - size halves in high-volatility regimes and shrinks as drawdown grows
   - **circuit breakers**: −2% day → no new entries; 4 consecutive losses → 24h cooldown;
     −10% from the high-water mark → **kill switch** (flatten everything and halt until an admin resets it)
6. **Claude review (optional)**: with `LLM_REVIEW_ENABLED=true`, Claude (`claude-opus-5-5`) investigates each
   approved trade using read-only tools (price history, headlines, portfolio). It looks for things price signals
   miss, such as earnings inside the holding period, fraud probes or halts. It can only **approve, shrink or veto**,
   never enlarge a trade. If the API fails, the default is to veto.
7. **Execute**: paper broker (fills at the live quote ± spread/slippage/fees), or Alpaca with **bracket orders**,
   so a broker-side stop protects each position even if the bot process dies.
8. **Journal**: every evaluation, including trades *not* taken and why, is stored and shown on the `/bot` page.

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

Open http://localhost. The **first account you register becomes the administrator** (bot controls).

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
| GET | `/status` `/positions` `/trades` `/orders` `/decisions` `/equity` `/performance` | user | State and journal |
| GET | `/analyze/{symbol}` | user | What the bot thinks now (no order placed) |
| POST | `/backtest` | user | Backtest / walk-forward |
| POST | `/start` `/stop` | admin | Enable entries / pause entries (stops stay enforced) |
| POST | `/run-once` | admin | Run one full cycle now |
| POST | `/flatten` | admin | Panic button: close everything and pause |
| POST | `/reset-halt` | admin | Acknowledge a circuit-breaker halt |
| PUT | `/config` | admin | Adjust risk limits (within hard bounds) and the universe |
| POST | `/calibrate` | admin | Walk-forward calibrate the live edge estimates |

## Configuration

See `.env.example`. Key settings: `TRADING_MODE`, `ALLOW_LIVE_TRADING`, `BOT_UNIVERSE`, `BOT_CYCLE_MINUTES`,
`LLM_REVIEW_ENABLED`, `ANTHROPIC_API_KEY`, `JWT_SECRET_KEY` (required, ≥32 chars, outside development).

## Known limitations

- Daily-bar strategy; it is not a high-frequency system. Exchange holidays are not modelled (Alpaca's clock is used in Alpaca modes).
- Long-only. Shorting is intentionally not implemented (unbounded loss).
- Yahoo Finance data is free but unofficial and can be delayed or rate-limited.
- Alpaca supports US equities only; NSE symbols work in the dashboard, backtests and the paper broker.
