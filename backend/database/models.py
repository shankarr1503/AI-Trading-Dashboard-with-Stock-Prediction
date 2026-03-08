"""SQLAlchemy ORM models for all database entities."""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text,
    ForeignKey, Enum, Index, UniqueConstraint
)
from sqlalchemy.orm import relationship
import enum
from backend.database.session import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    portfolios = relationship("Portfolio", back_populates="user")
    alerts = relationship("Alert", back_populates="user")


class MarketData(Base):
    __tablename__ = "market_data"
    __table_args__ = (
        Index("ix_market_data_symbol_timestamp", "symbol", "timestamp"),
        UniqueConstraint("symbol", "timestamp", "interval"),
    )

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), index=True, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    interval = Column(String(10), default="1d")
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    adj_close = Column(Float)
    volume = Column(Float)
    vwap = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)


class Portfolio(Base):
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, default="My Portfolio")
    description = Column(Text)
    initial_capital = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="portfolios")
    holdings = relationship("Holding", back_populates="portfolio")
    transactions = relationship("Transaction", back_populates="portfolio")


class Holding(Base):
    __tablename__ = "holdings"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    quantity = Column(Float, nullable=False)
    avg_buy_price = Column(Float, nullable=False)
    sector = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    portfolio = relationship("Portfolio", back_populates="holdings")


class TransactionType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    transaction_type = Column(Enum(TransactionType), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    total_value = Column(Float, nullable=False)
    executed_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)

    portfolio = relationship("Portfolio", back_populates="transactions")


class PredictionResult(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), index=True, nullable=False)
    model_name = Column(String(50), nullable=False)
    predicted_price = Column(Float)
    current_price = Column(Float)
    price_change_pct = Column(Float)
    direction = Column(String(10))  # BULLISH / BEARISH
    confidence = Column(Float)
    horizon = Column(String(20), default="1d")
    predicted_at = Column(DateTime, default=datetime.utcnow)
    features_used = Column(Text)


class SignalType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class TradeSignal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), index=True, nullable=False)
    signal_type = Column(Enum(SignalType), nullable=False)
    confidence = Column(Float)
    entry_price = Column(Float)
    target_price = Column(Float)
    stop_loss = Column(Float)
    risk_reward_ratio = Column(Float)
    indicators_summary = Column(Text)
    ml_score = Column(Float)
    sentiment_score = Column(Float)
    generated_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)


class SentimentScore(Base):
    __tablename__ = "sentiment_scores"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), index=True)
    source = Column(String(50))  # news, twitter, reddit
    positive_score = Column(Float)
    negative_score = Column(Float)
    neutral_score = Column(Float)
    overall_sentiment = Column(String(20))  # BULLISH / BEARISH / NEUTRAL
    headline = Column(Text)
    analyzed_at = Column(DateTime, default=datetime.utcnow)


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
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    alert_type = Column(Enum(AlertType), nullable=False)
    threshold_value = Column(Float)
    is_active = Column(Boolean, default=True)
    is_triggered = Column(Boolean, default=False)
    triggered_at = Column(DateTime)
    notify_email = Column(Boolean, default=True)
    notify_sms = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="alerts")
