/**
 * API client — Axios instance with JWT auth, transparent token refresh,
 * and typed helpers for every backend area.
 */
import axios, { AxiosError, InternalAxiosRequestConfig } from 'axios';

// NEXT_PUBLIC_SAME_ORIGIN=true (the desktop build) means the page is served by the API server
// itself: requests use relative URLs and the WebSocket base is derived from the page's origin
// when a socket is opened. Otherwise the API lives at NEXT_PUBLIC_API_URL / NEXT_PUBLIC_WS_URL.
const SAME_ORIGIN = process.env.NEXT_PUBLIC_SAME_ORIGIN === 'true';
const API_URL = SAME_ORIGIN ? '' : process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
const WS_URL = SAME_ORIGIN ? '' : process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000';

/** WebSocket base URL (no trailing slash): ws(s)://<page host> in same-origin mode, else WS_URL. */
export function wsBaseUrl(): string {
  if (!SAME_ORIGIN || typeof window === 'undefined') return WS_URL;
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${window.location.host}`;
}

const ACCESS_KEY = 'access_token';
const REFRESH_KEY = 'refresh_token';

export const tokens = {
  get access() {
    return typeof window !== 'undefined' ? localStorage.getItem(ACCESS_KEY) : null;
  },
  get refresh() {
    return typeof window !== 'undefined' ? localStorage.getItem(REFRESH_KEY) : null;
  },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

export const api = axios.create({
  baseURL: API_URL,
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' },
});

api.interceptors.request.use((config) => {
  const token = tokens.access;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// On a 401, try one refresh, then replay the original request.
let refreshing: Promise<string | null> | null = null;
api.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as (InternalAxiosRequestConfig & { _retried?: boolean }) | undefined;
    if (error.response?.status !== 401 || !original || original._retried || !tokens.refresh) {
      return Promise.reject(error);
    }
    original._retried = true;
    refreshing =
      refreshing ||
      axios
        .post(`${API_URL}/auth/refresh`, { refresh_token: tokens.refresh })
        .then((r) => {
          tokens.set(r.data.access_token, r.data.refresh_token);
          return r.data.access_token as string;
        })
        .catch(() => {
          tokens.clear();
          return null;
        })
        .finally(() => {
          refreshing = null;
        });
    const fresh = await refreshing;
    if (!fresh) return Promise.reject(error);
    original.headers.Authorization = `Bearer ${fresh}`;
    return api(original);
  },
);

export function errorMessage(e: unknown, fallback = 'Request failed'): string {
  const err = e as AxiosError<{ detail?: unknown }>;
  const detail = err?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  return fallback;
}

const enc = encodeURIComponent;

export interface User {
  id: number;
  email: string;
  username: string;
  full_name: string | null;
  is_active: boolean;
  is_superuser: boolean;
}

// Auth
export const authApi = {
  async login(email: string, password: string) {
    const r = await api.post('/auth/login', { email, password });
    tokens.set(r.data.access_token, r.data.refresh_token);
    return r.data;
  },
  register: (data: { email: string; username: string; password: string; full_name?: string }) =>
    api.post<User>('/auth/register', data),
  me: () => api.get<User>('/auth/me'),
  logout: () => tokens.clear(),
};

// Market Data
export const marketApi = {
  getQuote: (symbol: string) => api.get(`/api/market/quote/${enc(symbol)}`),
  getHistory: (symbol: string, period = '1y', interval = '1d') =>
    api.get(`/api/market/history/${enc(symbol)}`, { params: { period, interval } }),
  getMovers: () => api.get('/api/market/movers'),
  search: (q: string) => api.get('/api/market/search', { params: { q } }),
  getBatch: (symbols: string[]) => api.get('/api/market/batch', { params: { symbols: symbols.join(',') } }),
};

// Technical Indicators
export const indicatorsApi = {
  get: (symbol: string, period = '6mo', interval = '1d') =>
    api.get(`/api/indicators/${enc(symbol)}`, { params: { period, interval } }),
};

// Predictions & sentiment
export const predictionsApi = {
  predict: (symbol: string) => api.get(`/api/predict/${enc(symbol)}`),
  sentiment: (symbol: string) => api.get(`/api/predict/${enc(symbol)}/sentiment`),
};

// Signals
export const signalsApi = {
  getSignal: (symbol: string) => api.get(`/api/signals/${enc(symbol)}`),
};

// Portfolio
export const portfolioApi = {
  list: () => api.get('/api/portfolio/'),
  getSummary: (portfolioId: number) => api.get(`/api/portfolio/${portfolioId}/summary`),
  createPortfolio: (data: { name: string; description?: string; initial_capital?: number }) =>
    api.post('/api/portfolio/', data),
  addHolding: (portfolioId: number, data: { symbol: string; quantity: number; avg_buy_price: number; sector?: string }) =>
    api.post(`/api/portfolio/${portfolioId}/holdings`, data),
  deleteHolding: (portfolioId: number, holdingId: number) =>
    api.delete(`/api/portfolio/${portfolioId}/holdings/${holdingId}`),
};

// Alerts
export const alertsApi = {
  list: () => api.get('/api/alerts/'),
  create: (data: { symbol: string; alert_type: string; threshold_value?: number }) => api.post('/api/alerts/', data),
  delete: (id: number) => api.delete(`/api/alerts/${id}`),
  check: () => api.post('/api/alerts/check'),
};

// Trading bot
export const botApi = {
  status: () => api.get('/api/bot/status'),
  start: () => api.post('/api/bot/start'),
  stop: () => api.post('/api/bot/stop'),
  runOnce: () => api.post('/api/bot/run-once'),
  flatten: () => api.post('/api/bot/flatten'),
  resetHalt: () => api.post('/api/bot/reset-halt'),
  calibrate: () => api.post('/api/bot/calibrate'),
  updateConfig: (data: { risk?: Record<string, number>; universe?: string[] }) => api.put('/api/bot/config', data),
  positions: () => api.get('/api/bot/positions'),
  trades: (limit = 50) => api.get('/api/bot/trades', { params: { limit } }),
  decisions: (limit = 100) => api.get('/api/bot/decisions', { params: { limit } }),
  equity: () => api.get('/api/bot/equity'),
  performance: () => api.get('/api/bot/performance'),
  analyze: (symbol: string) => api.get(`/api/bot/analyze/${enc(symbol)}`),
  backtest: (data: { symbols: string[]; period?: string; walk_forward?: boolean; folds?: number; initial_capital?: number }) =>
    api.post('/api/bot/backtest', data, { timeout: 300000 }),
};

// Equity research
export const researchApi = {
  fundamentals: (symbol: string) => api.get(`/api/research/${enc(symbol)}/fundamentals`),
  // Reads the latest stored report (or a free rules-based one); never runs the paid AI analyst.
  report: (symbol: string) => api.get(`/api/research/${enc(symbol)}/report`),
  // Explicitly generates a new report: the paid AI analyst for administrators, rules-based otherwise.
  // A paid run can outlast this timeout: the server still finishes and stores it. If one finished minutes
  // ago the server returns it (`reused_recent`) instead of paying again, unless `force`.
  generateReport: (symbol: string, force = false) =>
    api.post(`/api/research/${enc(symbol)}/report`, null, { params: force ? { force: true } : undefined, timeout: 300000 }),
  reports: () => api.get('/api/research/reports'),
  universes: () => api.get('/api/research/universes'),
  screen: (body: { symbols?: string[]; universe?: string }) => api.post('/api/research/screen', body, { timeout: 300000 }),
};

// WebSocket factory
export const createPriceWebSocket = (symbol: string, onMessage: (data: any) => void): WebSocket => {
  const ws = new WebSocket(`${wsBaseUrl()}/ws/market/${enc(symbol)}?token=${enc(tokens.access ?? '')}`);
  ws.onmessage = (event) => {
    try {
      onMessage(JSON.parse(event.data));
    } catch {
      /* ignore malformed frames */
    }
  };
  return ws;
};

// Both are '' in same-origin mode: prefix paths with API_URL, and build socket URLs with wsBaseUrl().
export { API_URL, WS_URL };
