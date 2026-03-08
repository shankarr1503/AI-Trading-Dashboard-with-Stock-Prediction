/**
 * API client — Axios instance with base URL and auth interceptor.
 */
import axios from 'axios';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
const WS_URL = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000';

export const api = axios.create({
  baseURL: API_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

// Auth interceptor
api.interceptors.request.use((config) => {
  const token = typeof window !== 'undefined' ? localStorage.getItem('access_token') : null;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// Market Data
export const marketApi = {
  getQuote: (symbol: string) => api.get(`/api/market/quote/${symbol}`),
  getHistory: (symbol: string, period = '1y', interval = '1d') =>
    api.get(`/api/market/history/${symbol}?period=${period}&interval=${interval}`),
  getMovers: () => api.get('/api/market/movers'),
  search: (q: string) => api.get(`/api/market/search?q=${q}`),
  getBatch: (symbols: string[]) => api.get(`/api/market/batch?symbols=${symbols.join(',')}`),
};

// Technical Indicators
export const indicatorsApi = {
  get: (symbol: string, period = '6mo', interval = '1d') =>
    api.get(`/api/indicators/${symbol}?period=${period}&interval=${interval}`),
};

// Predictions
export const predictionsApi = {
  predict: (symbol: string) => api.get(`/api/predict/${symbol}`),
};

// Signals
export const signalsApi = {
  getSignal: (symbol: string) => api.get(`/api/signals/${symbol}`),
};

// Portfolio
export const portfolioApi = {
  getSummary: (portfolioId: number) => api.get(`/api/portfolio/${portfolioId}/summary`),
  createPortfolio: (data: any) => api.post('/api/portfolio/', data),
  addHolding: (portfolioId: number, data: any) => api.post(`/api/portfolio/${portfolioId}/holdings`, data),
};

// Alerts
export const alertsApi = {
  list: () => api.get('/api/alerts/'),
  create: (data: any) => api.post('/api/alerts/', data),
  delete: (id: number) => api.delete(`/api/alerts/${id}`),
};

// WebSocket factory
export const createPriceWebSocket = (symbol: string, onMessage: (data: any) => void): WebSocket => {
  const ws = new WebSocket(`${WS_URL}/ws/market/${symbol}`);
  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      onMessage(data);
    } catch {}
  };
  return ws;
};

export { API_URL, WS_URL };
