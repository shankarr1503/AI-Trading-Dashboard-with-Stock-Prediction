// REST client for the AI Trading Bot server (desktop sidecar or self-hosted).
//
// Tokens: the short-lived access token lives in chrome.storage.session (memory
// only, gone when the browser closes); the refresh token in chrome.storage.local.
// Both are bound to the server they were issued by and are never sent anywhere
// else. A 401 triggers one refresh (shared by every concurrent request: single
// flight) and one retry; if the refresh is refused the user is signed out.

export const ACCESS_KEY = 'access';   // chrome.storage.session
export const AUTH_KEY = 'auth';       // chrome.storage.local
const EXPIRY_SKEW_MS = 30_000;        // refresh a little before the server would say 401
const DEFAULT_TIMEOUT_MS = 15_000;

export class ApiError extends Error {
  /**
   * kind: 'network' (server unreachable or access not granted), 'timeout',
   * 'auth' (not signed in / session ended), 'http' (any other non-2xx answer).
   */
  constructor(message, { status = 0, kind = 'http', detail = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.kind = kind;
    this.detail = detail;
  }
}

/** Human message from a FastAPI / slowapi error body. */
export function errorDetail(body, status) {
  if (body && typeof body === 'object') {
    const d = body.detail;
    if (typeof d === 'string' && d) return d;
    if (Array.isArray(d) && d.length) {
      return d.map((e) => (e && e.msg ? `${(e.loc || []).filter((x) => x !== 'body').join('.') || 'input'}: ${e.msg}` : String(e)))
        .join('; ');
    }
    if (typeof body.error === 'string' && body.error) return body.error;
    if (typeof body.message === 'string' && body.message) return body.message;
  } else if (typeof body === 'string' && body.trim() && body.length < 300 && !/^\s*</.test(body)) {
    return body.trim();
  }
  if (status === 429) return 'Too many requests: try again in a minute';
  if (status === 403) return 'Not allowed';
  if (status === 404) return 'Not found';
  if (status >= 500) return `Server error (HTTP ${status})`;
  return `HTTP ${status}`;
}

/** Token persistence on chrome.storage, bound to one server URL. */
export function createTokenStore(chromeApi = globalThis.chrome, now = () => Date.now()) {
  const session = chromeApi.storage.session;
  const local = chromeApi.storage.local;
  return {
    async load(server) {
      const [s, l] = await Promise.all([session.get(ACCESS_KEY), local.get(AUTH_KEY)]);
      const access = s[ACCESS_KEY];
      const auth = l[AUTH_KEY];
      const sameServer = auth && auth.server === server;
      return {
        accessToken: access && access.server === server && sameServer ? access.token : null,
        accessExpiresAt: access && access.server === server ? access.expiresAt || 0 : 0,
        refreshToken: sameServer ? auth.refreshToken : null,
        user: sameServer ? auth.user || null : null,
        signedInServer: auth ? auth.server : null,
      };
    },
    /** Store a token pair. A refresh keeps the cached user; a new login (keepUser false) drops it. */
    async save(server, tokenResponse, { keepUser = true } = {}) {
      const l = await local.get(AUTH_KEY);
      const prev = l[AUTH_KEY];
      const expiresIn = Number(tokenResponse.expires_in) || 0;
      await session.set({
        [ACCESS_KEY]: {
          server,
          token: tokenResponse.access_token,
          expiresAt: expiresIn ? now() + expiresIn * 1000 - EXPIRY_SKEW_MS : 0,
        },
      });
      await local.set({
        [AUTH_KEY]: {
          server,
          refreshToken: tokenResponse.refresh_token,
          user: keepUser && prev && prev.server === server ? prev.user || null : null,
        },
      });
    },
    async setUser(server, user) {
      const l = await local.get(AUTH_KEY);
      const auth = l[AUTH_KEY];
      if (!auth || auth.server !== server) return;
      const slim = user ? {
        id: user.id, email: user.email, username: user.username, is_superuser: !!user.is_superuser,
      } : null;
      if (JSON.stringify(auth.user || null) === JSON.stringify(slim)) return;   // unchanged: no storage event
      await local.set({ [AUTH_KEY]: { ...auth, user: slim } });
    },
    async clear() {
      await Promise.all([session.remove(ACCESS_KEY), local.remove(AUTH_KEY)]);
    },
  };
}

// One refresh per server at a time, shared by every client in this context.
const refreshInFlight = new Map();

export class ApiClient {
  constructor({ baseUrl, tokens, fetchImpl, timeoutMs = DEFAULT_TIMEOUT_MS, now = () => Date.now() } = {}) {
    if (!baseUrl) throw new Error('ApiClient needs a baseUrl');
    this.baseUrl = String(baseUrl).replace(/\/+$/, '');
    this.tokens = tokens || createTokenStore();
    this.fetch = fetchImpl || globalThis.fetch.bind(globalThis);
    this.timeoutMs = timeoutMs;
    this.now = now;
  }

  url(path) {
    return `${this.baseUrl}${path.startsWith('/') ? '' : '/'}${path}`;
  }

  /** Low-level request: one attempt, no auth handling. Resolves with the parsed body. */
  async send(path, { method = 'GET', body, token = null, timeoutMs = this.timeoutMs } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = `Bearer ${token}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let res;
    try {
      res = await this.fetch(this.url(path), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
      });
    } catch (e) {
      if (controller.signal.aborted) {
        throw new ApiError(`The server did not answer within ${Math.round(timeoutMs / 1000)} s`, { kind: 'timeout' });
      }
      throw new ApiError(`Cannot reach the server at ${this.baseUrl}`, { kind: 'network', detail: String(e && e.message) });
    } finally {
      clearTimeout(timer);
    }
    const text = await res.text().catch(() => '');
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
    if (!res.ok) {
      throw new ApiError(errorDetail(data, res.status), { status: res.status, kind: 'http', detail: data });
    }
    return data;
  }

  /** A valid access token, refreshing first when it is missing or about to expire. */
  async accessToken() {
    const t = await this.tokens.load(this.baseUrl);
    if (t.accessToken && (!t.accessExpiresAt || t.accessExpiresAt > this.now())) return t.accessToken;
    if (!t.refreshToken) throw new ApiError('Not signed in', { status: 401, kind: 'auth' });
    return this.refresh(t.accessToken);
  }

  /**
   * Exchange the refresh token for a new pair. Concurrent callers share one
   * request. `failedToken` is the access token the caller saw rejected: if a
   * newer one is already stored (another caller refreshed), it is used as is.
   */
  async refresh(failedToken = null) {
    const t = await this.tokens.load(this.baseUrl);
    if (t.accessToken && t.accessToken !== failedToken && (!t.accessExpiresAt || t.accessExpiresAt > this.now())) {
      return t.accessToken;
    }
    let pending = refreshInFlight.get(this.baseUrl);
    if (!pending) {
      pending = (async () => {
        if (!t.refreshToken) throw new ApiError('Not signed in', { status: 401, kind: 'auth' });
        let pair;
        try {
          pair = await this.send('/auth/refresh', { method: 'POST', body: { refresh_token: t.refreshToken } });
        } catch (e) {
          if (e.kind === 'http' && [400, 401, 403, 422].includes(e.status)) {
            await this.tokens.clear();
            throw new ApiError('Your session has ended: sign in again', { status: 401, kind: 'auth' });
          }
          throw e;   // server down or rate limited: keep the tokens and try again later
        }
        await this.tokens.save(this.baseUrl, pair);
        return pair.access_token;
      })().finally(() => refreshInFlight.delete(this.baseUrl));
      refreshInFlight.set(this.baseUrl, pending);
    }
    return pending;
  }

  /** Request with auth: Bearer token, one refresh + retry on 401. */
  async request(path, { method = 'GET', body, auth = true, timeoutMs } = {}) {
    if (!auth) return this.send(path, { method, body, timeoutMs });
    let token = await this.accessToken();
    try {
      return await this.send(path, { method, body, token, timeoutMs });
    } catch (e) {
      if (!(e.kind === 'http' && e.status === 401)) throw e;
    }
    token = await this.refresh(token);
    try {
      return await this.send(path, { method, body, token, timeoutMs });
    } catch (e) {
      if (e.kind === 'http' && e.status === 401) {
        await this.tokens.clear();
        throw new ApiError('Your session has ended: sign in again', { status: 401, kind: 'auth' });
      }
      throw e;
    }
  }

  // ─── Auth ──────────────────────────────────────────────────────────────────

  async login(email, password) {
    const pair = await this.send('/auth/login', {
      method: 'POST', body: { email: String(email || '').trim(), password: String(password || '') },
    });
    await this.tokens.save(this.baseUrl, pair, { keepUser: false });
    return this.me();
  }

  async logout() {
    await this.tokens.clear();
  }

  async me() {
    const user = await this.request('/auth/me');
    await this.tokens.setUser(this.baseUrl, user);
    return user;
  }

  async storedUser() {
    return (await this.tokens.load(this.baseUrl)).user;
  }

  async isSignedIn() {
    return !!(await this.tokens.load(this.baseUrl)).refreshToken;
  }

  // ─── Endpoints ─────────────────────────────────────────────────────────────

  health(opts = {}) {
    return this.request('/health', { auth: false, timeoutMs: opts.timeoutMs ?? 8000 });
  }

  botStatus() { return this.request('/api/bot/status'); }
  botPositions() { return this.request('/api/bot/positions', { timeoutMs: 30_000 }); }
  botDecisions(limit = 20) { return this.request(`/api/bot/decisions?limit=${encodeURIComponent(limit)}`); }
  botStart() { return this.request('/api/bot/start', { method: 'POST' }); }
  botStop() { return this.request('/api/bot/stop', { method: 'POST' }); }
  // The server waits up to 60 s for a running cycle before answering "queued".
  botFlatten() { return this.request('/api/bot/flatten', { method: 'POST', timeoutMs: 90_000 }); }

  analyze(symbol) {
    return this.request(`/api/bot/analyze/${encodeURIComponent(symbol)}`, { timeoutMs: 60_000 });
  }

  fundamentals(symbol) {
    return this.request(`/api/research/${encodeURIComponent(symbol)}/fundamentals`, { timeoutMs: 60_000 });
  }

  quote(symbol) {
    return this.request(`/api/market/quote/${encodeURIComponent(symbol)}`, { auth: false, timeoutMs: 20_000 });
  }
}
