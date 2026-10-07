"""SQLAlchemy ORM models for all database entities."""
import enum

from sqlalchemy import (
    JSON, BigInteger, Boolean, Column, DateTime, Enum, Float, ForeignKey, Index,
    Integer, Numeric, String, Text, UniqueConstraint, false as sa_false,
)
from sqlalchemy.orm import relationship

from backend.database.session import Base, utcnow

# Money and quantities are stored as exact decimals in the database but surfaced
# to Python as floats (asdecimal=False) so arithmetic in services stays simple.
Money = Numeric(18, 6, asdecimal=False)
Qty = Numeric(18, 6, asdecimal=False)
TZDateTime = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    username = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255))
    is_active = Column(Boolean, default=True, nullable=False)
    is_superuser = Column(Boolean, default=False, nullable=False)
    # Bumped to revoke every outstanding token (logout everywhere / compromise).
    token_version = Column(Integer, default=0, server_default="0", nullable=False)
    created_at = Column(TZDateTime, default=utcnow)
    updated_at = Column(TZDateTime, default=utcnow, onupdate=utcnow)

    portfolios = relationship("Portfolio", back_populates="user", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="user", cascade="all, delete-orphan")


class MarketData(Base):
    __tablename__ = "market_data"
    __table_args__ = (
        Index("ix_market_data_symbol_timestamp", "symbol", "timestamp"),
        UniqueConstraint("symbol", "timestamp", "interval", name="uq_market_data"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    timestamp = Column(TZDateTime, nullable=False)
    interval = Column(String(10), default="1d", nullable=False)
    open = Column(Money)
    high = Column(Money)
    low = Column(Money)
    close = Column(Money)
    adj_close = Column(Money)
    volume = Column(BigInteger)
    vwap = Column(Money)
    created_at = Column(TZDateTime, default=utcnow)


class Portfolio(Base):
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), default="My Portfolio")
    description = Column(Text)
    initial_capital = Column(Money, default=0.0)
    created_at = Column(TZDateTime, default=utcnow)

    user = relationship("User", back_populates="portfolios")
    holdings = relationship("Holding", back_populates="portfolio", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="portfolio", cascade="all, delete-orphan")


class Holding(Base):
    __tablename__ = "holdings"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    quantity = Column(Qty, nullable=False)
    avg_buy_price = Column(Money, nullable=False)
    sector = Column(String(100))
    updated_at = Column(TZDateTime, default=utcnow, onupdate=utcnow)

    portfolio = relationship("Portfolio", back_populates="holdings")


class TransactionType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    transaction_type = Column(Enum(TransactionType, name="transaction_type"), nullable=False)
    quantity = Column(Qty, nullable=False)
    price = Column(Money, nullable=False)
    total_value = Column(Money, nullable=False)
    executed_at = Column(TZDateTime, default=utcnow)
    notes = Column(Text)

    portfolio = relationship("Portfolio", back_populates="transactions")


class AlertType(str, enum.Enum):
    PRICE_ABOVE = "PRICE_ABOVE"
    PRICE_BELOW = "PRICE_BELOW"
    VOLUME_SPIKE = "VOLUME_SPIKE"
    RSI_OVERBOUGHT = "RSI_OVERBOUGHT"
    RSI_OVERSOLD = "RSI_OVERSOLD"
    SIGNAL_BUY = "SIGNAL_BUY"
    SIGNAL_SELL = "SIGNAL_SELL"


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    alert_type = Column(Enum(AlertType, name="alert_type"), nullable=False)
    threshold_value = Column(Float)
    is_active = Column(Boolean, default=True, nullable=False)
    is_triggered = Column(Boolean, default=False, nullable=False)
    triggered_at = Column(TZDateTime)
    notify_email = Column(Boolean, default=True)
    notify_sms = Column(Boolean, default=False)
    created_at = Column(TZDateTime, default=utcnow)

    user = relationship("User", back_populates="alerts")


# ─── Trading bot ──────────────────────────────────────────────────────────────

class BotState(Base):
    """Singleton row (id=1) holding the bot's switches, circuit-breaker state and lease."""
    __tablename__ = "bot_state"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False, nullable=False)
    halted = Column(Boolean, default=False, nullable=False)
    halt_reason = Column(Text)
    halted_at = Column(TZDateTime)
    high_water_mark = Column(Money)
    day_start_equity = Column(Money)
    day_start_date = Column(String(10))
    consecutive_losses = Column(Integer, default=0, nullable=False)
    cooldown_until = Column(TZDateTime)
    last_cycle_at = Column(TZDateTime)
    last_cycle_summary = Column(JSON)
    lease_owner = Column(String(64))
    lease_until = Column(TZDateTime)
    config_overrides = Column(JSON)
    # Panic button: set by /flatten, executed by whichever process holds the lease.
    flatten_requested = Column(Boolean, default=False, server_default=sa_false(), nullable=False)
    consecutive_failures = Column(Integer, default=0, server_default="0", nullable=False)
    consecutive_data_faults = Column(Integer, default=0, server_default="0", nullable=False)
    last_error = Column(Text)
    last_success_at = Column(TZDateTime)
    cashflow_checked_at = Column(TZDateTime)
    updated_at = Column(TZDateTime, default=utcnow, onupdate=utcnow)


class PaperAccount(Base):
    """Cash ledger of the built-in simulated broker (singleton id=1)."""
    __tablename__ = "paper_account"

    id = Column(Integer, primary_key=True)
    cash = Column(Money, nullable=False)
    initial_capital = Column(Money, nullable=False)
    created_at = Column(TZDateTime, default=utcnow)


class PaperPosition(Base):
    """Positions held at the built-in simulated broker."""
    __tablename__ = "paper_positions"

    symbol = Column(String(20), primary_key=True)
    qty = Column(Qty, nullable=False)
    avg_price = Column(Money, nullable=False)
    updated_at = Column(TZDateTime, default=utcnow, onupdate=utcnow)


class BotPosition(Base):
    """Bot-side risk metadata for an open position (stops, targets, trailing state)."""
    __tablename__ = "bot_positions"

    symbol = Column(String(20), primary_key=True)
    qty = Column(Qty, nullable=False)
    entry_price = Column(Money, nullable=False)
    stop_price = Column(Money, nullable=False)
    initial_stop = Column(Money, nullable=False)
    target_price = Column(Money)
    highest_price = Column(Money)
    entry_atr = Column(Money)
    entry_score = Column(Float)
    entry_costs = Column(Money, default=0.0)
    opened_at = Column(TZDateTime, default=utcnow)
    meta = Column(JSON)


class BotOrder(Base):
    __tablename__ = "bot_orders"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(4), nullable=False)  # BUY / SELL
    qty = Column(Qty, nullable=False)
    ref_price = Column(Money)
    fill_price = Column(Money)
    commission = Column(Money, default=0.0)
    status = Column(String(20), nullable=False)  # filled / submitted / rejected
    reason = Column(Text)
    broker = Column(String(20))
    broker_order_id = Column(String(64))
    created_at = Column(TZDateTime, default=utcnow, index=True)


class BotTrade(Base):
    """A completed round trip (entry → exit)."""
    __tablename__ = "bot_trades"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    qty = Column(Qty, nullable=False)
    entry_price = Column(Money, nullable=False)
    exit_price = Column(Money, nullable=False)
    entry_time = Column(TZDateTime)
    exit_time = Column(TZDateTime, default=utcnow, index=True)
    pnl = Column(Money, nullable=False)
    pnl_pct = Column(Float)
    r_multiple = Column(Float)
    costs = Column(Money, default=0.0)
    entry_score = Column(Float)
    exit_reason = Column(String(50))


class BotDecision(Base):
    """Audit journal: every evaluation the agent made, including rejections."""
    __tablename__ = "bot_decisions"

    id = Column(Integer, primary_key=True)
    cycle_id = Column(String(32), index=True)
    symbol = Column(String(20), index=True)
    action = Column(String(20), nullable=False)  # BUY / SELL / HOLD / SKIP / HALT
    score = Column(Float)
    regime = Column(String(20))
    expected_edge_pct = Column(Float)
    cost_pct = Column(Float)
    qty = Column(Qty)
    reasons = Column(JSON)
    llm_verdict = Column(JSON)
    created_at = Column(TZDateTime, default=utcnow, index=True)


class EquitySnapshot(Base):
    __tablename__ = "bot_equity"

    id = Column(Integer, primary_key=True)
    timestamp = Column(TZDateTime, default=utcnow, index=True)
    equity = Column(Money, nullable=False)
    cash = Column(Money, nullable=False)
    exposure = Column(Money, nullable=False)
    drawdown_pct = Column(Float, nullable=False)


class BotCalibration(Base):
    """Per-symbol win-rate / payoff statistics learned from walk-forward backtests."""
    __tablename__ = "bot_calibration"

    symbol = Column(String(20), primary_key=True)
    stats = Column(JSON, nullable=False)
    updated_at = Column(TZDateTime, default=utcnow, onupdate=utcnow)


# ─── Equity research ──────────────────────────────────────────────────────────

class ResearchReport(Base):
    """A stored analyst report (Claude or rules-based) with its headline numbers."""
    __tablename__ = "research_reports"
    __table_args__ = (Index("ix_research_reports_symbol_created", "symbol", "created_at"),)

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    created_at = Column(TZDateTime, default=utcnow, nullable=False)
    source = Column(String(20), nullable=False)          # claude | quant_model
    model = Column(String(64))
    rating = Column(String(12), nullable=False)
    conviction = Column(Integer)
    price = Column(Money)
    expected_price = Column(Money)
    expected_return_pct = Column(Float)
    composite_score = Column(Float)
    fair_value = Column(Money)
    report = Column(JSON, nullable=False)
    usage = Column(JSON)
    requested_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
