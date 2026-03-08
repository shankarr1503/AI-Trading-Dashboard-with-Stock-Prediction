# 🚀 AI-Powered Trading Analytics Platform

> A production-grade, full-scale trading analytics platform with real-time market data, interactive charts, technical indicators, AI-based stock predictions, portfolio analytics, and market sentiment analysis.

![Platform Banner](docs/banner.png)

## ⚠️ DISCLAIMER

> **Stock market predictions cannot guarantee accuracy.** Financial markets are influenced by a complex combination of economic, political, psychological, and random factors. This platform is designed for **educational and analytical purposes only**. Do **NOT** make financial decisions based solely on AI predictions from this system.

---

## 📋 Features

| Feature | Description |
|---------|-------------|
| 📈 **Real-Time Market Data** | Live OHLCV quotes via Yahoo Finance, Alpha Vantage, Polygon |
| 🕯️ **Interactive Charts** | Candlestick, line, volume overlays with zoom/pan |
| 📊 **Technical Indicators** | SMA, EMA, RSI, MACD, Bollinger Bands, VWAP, ATR, Stochastic |
| 🤖 **AI Stock Prediction** | LSTM, XGBoost, ARIMA, Random Forest, Ensemble models |
| 💬 **Sentiment Analysis** | FinBERT NLP on financial news and social media |
| 🔔 **Trade Signals** | AI-driven BUY/SELL/HOLD with entry/target/stop-loss |
| 💼 **Portfolio Analytics** | P&L, Sharpe ratio, drawdown, sector allocation |
| 🔔 **Smart Alerts** | Price thresholds, signal triggers, volume spikes |
| 🔐 **Authentication** | JWT-based secure user auth |
| 🐳 **Containerized** | Docker + Kubernetes + CI/CD ready |

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     FRONTEND (Next.js 14)                     │
│  Dashboard │ Charts │ Predictions │ Portfolio │ Sentiment     │
└─────────────────────────┬────────────────────────────────────┘
                          │ REST API + WebSocket
┌─────────────────────────▼────────────────────────────────────┐
│                  BACKEND (FastAPI)                             │
│  Market Data │ Indicators │ Auth │ Portfolio │ Alerts         │
└──────┬───────────────┬───────────────┬────────────────────────┘
       │               │               │
┌──────▼──────┐ ┌──────▼──────┐ ┌─────▼──────┐
│  ML Service │ │  PostgreSQL  │ │   Redis    │
│ (LSTM/XGB)  │ │  (Storage)  │ │  (Cache)   │
└─────────────┘ └─────────────┘ └────────────┘
```

---

## 🛠️ Tech Stack

### Backend
- **FastAPI** — High-performance async Python API
- **WebSockets** — Real-time price streaming
- **SQLAlchemy + Alembic** — ORM + migrations
- **PostgreSQL** — Primary database
- **Redis** — Caching layer
- **JWT (python-jose)** — Authentication

### Machine Learning
- **TensorFlow/Keras** — LSTM deep learning
- **scikit-learn** — Random Forest, preprocessing
- **XGBoost** — Gradient boosting
- **statsmodels** — ARIMA time-series
- **pandas-ta** — Technical indicator computation
- **transformers (HuggingFace)** — FinBERT sentiment

### Frontend
- **Next.js 14** — React framework with App Router
- **Tailwind CSS** — Utility-first styling
- **TradingView Lightweight Charts** — Professional charting
- **Recharts** — Portfolio visualizations
- **Zustand** — Global state management
- **SWR** — Data fetching with revalidation

### Data Sources
- Yahoo Finance (`yfinance`)
- Alpha Vantage API
- Polygon.io API
- Finnhub API
- NSE India API

---

## 🚀 Quick Start

### Prerequisites
- Docker & Docker Compose
- Node.js 18+
- Python 3.11+

### 1. Clone & Configure
```bash
git clone https://github.com/your-org/ai-trading-platform.git
cd ai-trading-platform
cp .env.example .env
# Edit .env with your API keys
```

### 2. Start with Docker Compose
```bash
docker-compose up --build
```

Services will start at:
- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:8000
- **ML Service**: http://localhost:8001
- **API Docs**: http://localhost:8000/docs

### 3. Local Development

**Backend:**
```bash
cd backend
pip install -r requirements.txt
alembic upgrade head
uvicorn main:app --reload --port 8000
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

**ML Service:**
```bash
cd ml
pip install -r requirements.txt
python api/server.py
```

---

## 📁 Project Structure

```
ai-trading-platform/
├── backend/                  # FastAPI backend
│   ├── main.py
│   ├── config.py
│   ├── auth/                 # JWT authentication
│   ├── market_data/          # Real-time market data
│   ├── indicators/           # Technical indicator engine
│   ├── predictions/          # ML prediction service
│   ├── signals/              # AI signal generator
│   ├── portfolio/            # Portfolio analytics
│   ├── alerts/               # Alert system
│   └── database/             # DB models & migrations
├── frontend/                 # Next.js 14 frontend
│   ├── src/
│   │   ├── app/              # App router pages
│   │   ├── components/       # UI components
│   │   └── lib/              # Utilities, API, store
│   └── public/
├── ml/                       # Machine learning
│   ├── models/               # LSTM, XGBoost, ARIMA
│   ├── ensemble/             # Ensemble predictor
│   ├── sentiment/            # FinBERT NLP
│   ├── feature_engineering/
│   ├── training/
│   ├── evaluation/
│   └── api/                  # ML FastAPI server
├── data_pipeline/            # Data ingestion & processing
├── database/                 # SQL schema & migrations
├── deployment/               # Docker, K8s, CI/CD
└── tests/                    # Unit & integration tests
```

---

## 🔑 Environment Variables

See `.env.example` for all required variables. Key ones:

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection URL |
| `JWT_SECRET_KEY` | Secret for JWT signing |
| `ALPHA_VANTAGE_API_KEY` | Alpha Vantage API key |
| `POLYGON_API_KEY` | Polygon.io API key |
| `FINNHUB_API_KEY` | Finnhub API key |

---

## 📡 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/register` | Register new user |
| POST | `/auth/login` | Login & get JWT |
| GET | `/api/market/quote/{symbol}` | Live quote |
| GET | `/api/market/history/{symbol}` | Historical OHLCV |
| GET | `/api/indicators/{symbol}` | Technical indicators |
| GET | `/api/predict/{symbol}` | ML price prediction |
| GET | `/api/signals/{symbol}` | AI trade signal |
| GET | `/api/portfolio/` | Portfolio summary |
| WS | `/ws/market/{symbol}` | Real-time price stream |

---

## 🧪 Testing

```bash
# Backend tests
cd backend && pytest tests/ -v

# Frontend tests
cd frontend && npm test
```

---

## 🚢 Deployment

See `deployment/` directory for:
- **Docker**: `Dockerfile.backend`, `Dockerfile.frontend`, `Dockerfile.ml`
- **Kubernetes**: `deployment/k8s/`
- **CI/CD**: `deployment/github-actions/ci-cd.yml`

---

## 📜 License

MIT License. See [LICENSE](LICENSE) for details.

---

## 🤝 Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request
