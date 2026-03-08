-- ============================================================
-- AI Trading Platform — PostgreSQL Database Schema
-- ============================================================

-- ─── Extensions ──────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ─── Users ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    email           VARCHAR(255) UNIQUE NOT NULL,
    username        VARCHAR(100) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    full_name       VARCHAR(255),
    is_active       BOOLEAN DEFAULT TRUE,
    is_superuser    BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_users_email ON users(email);

-- ─── Market Data (OHLCV) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_data (
    id          BIGSERIAL PRIMARY KEY,
    symbol      VARCHAR(20) NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    interval    VARCHAR(10) DEFAULT '1d',
    open        DECIMAL(18,6),
    high        DECIMAL(18,6),
    low         DECIMAL(18,6),
    close       DECIMAL(18,6),
    adj_close   DECIMAL(18,6),
    volume      BIGINT,
    vwap        DECIMAL(18,6),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_market_data UNIQUE (symbol, timestamp, interval)
);

CREATE INDEX idx_market_data_symbol ON market_data(symbol);
CREATE INDEX idx_market_data_symbol_ts ON market_data(symbol, timestamp DESC);

-- ─── Portfolios ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS portfolios (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name            VARCHAR(255) DEFAULT 'My Portfolio',
    description     TEXT,
    initial_capital DECIMAL(18,2) DEFAULT 0.00,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Holdings ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS holdings (
    id              SERIAL PRIMARY KEY,
    portfolio_id    INTEGER REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol          VARCHAR(20) NOT NULL,
    quantity        DECIMAL(18,6) NOT NULL,
    avg_buy_price   DECIMAL(18,6) NOT NULL,
    sector          VARCHAR(100),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Transactions ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transactions (
    id               SERIAL PRIMARY KEY,
    portfolio_id     INTEGER REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol           VARCHAR(20) NOT NULL,
    transaction_type VARCHAR(10) CHECK (transaction_type IN ('BUY', 'SELL')),
    quantity         DECIMAL(18,6) NOT NULL,
    price            DECIMAL(18,6) NOT NULL,
    total_value      DECIMAL(18,2) NOT NULL,
    executed_at      TIMESTAMPTZ DEFAULT NOW(),
    notes            TEXT
);

CREATE INDEX idx_transactions_portfolio ON transactions(portfolio_id);
CREATE INDEX idx_transactions_symbol ON transactions(symbol);

-- ─── AI Predictions ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS predictions (
    id               SERIAL PRIMARY KEY,
    symbol           VARCHAR(20) NOT NULL,
    model_name       VARCHAR(100) NOT NULL,
    predicted_price  DECIMAL(18,6),
    current_price    DECIMAL(18,6),
    price_change_pct DECIMAL(10,4),
    direction        VARCHAR(20),
    confidence       DECIMAL(5,4),
    horizon          VARCHAR(20) DEFAULT '1d',
    predicted_at     TIMESTAMPTZ DEFAULT NOW(),
    features_used    TEXT
);

CREATE INDEX idx_predictions_symbol ON predictions(symbol);
CREATE INDEX idx_predictions_symbol_at ON predictions(symbol, predicted_at DESC);

-- ─── Trade Signals ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id                 SERIAL PRIMARY KEY,
    symbol             VARCHAR(20) NOT NULL,
    signal_type        VARCHAR(10) CHECK (signal_type IN ('BUY', 'SELL', 'HOLD')),
    confidence         DECIMAL(5,4),
    entry_price        DECIMAL(18,6),
    target_price       DECIMAL(18,6),
    stop_loss          DECIMAL(18,6),
    risk_reward_ratio  DECIMAL(8,4),
    indicators_summary TEXT,
    ml_score           DECIMAL(5,4),
    sentiment_score    DECIMAL(5,4),
    generated_at       TIMESTAMPTZ DEFAULT NOW(),
    expires_at         TIMESTAMPTZ
);

CREATE INDEX idx_signals_symbol ON signals(symbol);
CREATE INDEX idx_signals_generated ON signals(generated_at DESC);

-- ─── Sentiment Scores ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sentiment_scores (
    id               SERIAL PRIMARY KEY,
    symbol           VARCHAR(20),
    source           VARCHAR(50),
    positive_score   DECIMAL(5,4),
    negative_score   DECIMAL(5,4),
    neutral_score    DECIMAL(5,4),
    overall_sentiment VARCHAR(20),
    headline         TEXT,
    analyzed_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_sentiment_symbol ON sentiment_scores(symbol);

-- ─── Alerts ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    symbol          VARCHAR(20) NOT NULL,
    alert_type      VARCHAR(50) NOT NULL,
    threshold_value DECIMAL(18,6),
    is_active       BOOLEAN DEFAULT TRUE,
    is_triggered    BOOLEAN DEFAULT FALSE,
    triggered_at    TIMESTAMPTZ,
    notify_email    BOOLEAN DEFAULT TRUE,
    notify_sms      BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_alerts_user ON alerts(user_id);
CREATE INDEX idx_alerts_symbol ON alerts(symbol);
CREATE INDEX idx_alerts_active ON alerts(is_active) WHERE is_active = TRUE;

-- ─── Sample Data ─────────────────────────────────────────────
-- Demo user (password: 'password123' — bcrypt hashed)
INSERT INTO users (email, username, hashed_password, full_name) VALUES
  ('demo@aitradingplatform.com', 'demo', '$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW', 'Demo User')
ON CONFLICT DO NOTHING;

-- Demo portfolio
INSERT INTO portfolios (user_id, name, description, initial_capital) VALUES
  (1, 'Tech Growth Portfolio', 'Long-term technology sector portfolio', 100000.00)
ON CONFLICT DO NOTHING;

-- Demo holdings
INSERT INTO holdings (portfolio_id, symbol, quantity, avg_buy_price, sector) VALUES
  (1, 'AAPL',  50,  170.25, 'Technology'),
  (1, 'MSFT',  30,  380.50, 'Technology'),
  (1, 'GOOGL', 20,  140.00, 'Communication Services'),
  (1, 'NVDA',  25,  450.00, 'Technology'),
  (1, 'JPM',   40,  185.00, 'Financials')
ON CONFLICT DO NOTHING;
