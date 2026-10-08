// In-memory stand-ins for the chrome.* APIs and fetch used by lib/*.

function clone(v) {
  return v === undefined ? undefined : JSON.parse(JSON.stringify(v));
}

export class FakeStorageArea {
  constructor(name, onChange) {
    this.name = name;
    this.data = {};
    this.onChange = onChange;
    this.writes = 0;
  }

  async get(keys) {
    if (keys === null || keys === undefined) return clone(this.data);
    const list = typeof keys === 'string' ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys);
    const out = {};
    for (const k of list) {
      if (k in this.data) out[k] = clone(this.data[k]);
      else if (keys && typeof keys === 'object' && !Array.isArray(keys)) out[k] = keys[k];
    }
    return out;
  }

  async set(items) {
    this.writes += 1;
    const changes = {};
    for (const [k, v] of Object.entries(items)) {
      changes[k] = { oldValue: clone(this.data[k]), newValue: clone(v) };
      this.data[k] = clone(v);
    }
    this.onChange(changes, this.name);
  }

  async remove(keys) {
    const list = typeof keys === 'string' ? [keys] : keys;
    const changes = {};
    for (const k of list) {
      if (k in this.data) {
        changes[k] = { oldValue: clone(this.data[k]) };
        delete this.data[k];
      }
    }
    if (Object.keys(changes).length) this.onChange(changes, this.name);
  }
}

/**
 * A fake `chrome` with storage (local/session + onChanged), action (badge),
 * notifications, alarms, permissions and runtime. Recorded state is exposed as
 * .badge, .sent (notifications) and .alarmMap.
 */
export function makeChrome({ granted = [], notificationsFail = false } = {}) {
  const listeners = [];
  const onChange = (changes, area) => listeners.forEach((l) => l(changes, area));
  const badge = { text: null, color: null, textColor: null, title: null };
  const sent = [];
  const granted_ = new Set(granted);
  const alarmMap = new Map();
  return {
    badge,
    sent,
    alarmMap,
    storage: {
      local: new FakeStorageArea('local', onChange),
      session: new FakeStorageArea('session', onChange),
      onChanged: { addListener: (l) => listeners.push(l) },
    },
    action: {
      async setBadgeText({ text }) { badge.text = text; },
      async setBadgeBackgroundColor({ color }) { badge.color = color; },
      async setBadgeTextColor({ color }) { badge.textColor = color; },
      async setTitle({ title }) { badge.title = title; },
    },
    notifications: {
      async create(id, options) {
        if (notificationsFail) throw new Error('Notifications are blocked');
        sent.push({ id, ...options });
        return id;
      },
    },
    alarms: {
      async get(name) { return alarmMap.get(name); },
      async clear(name) { return alarmMap.delete(name); },
      async create(name, info) { alarmMap.set(name, { name, ...info }); },
    },
    permissions: {
      async contains({ origins }) { return origins.every((o) => granted_.has(o)); },
    },
    runtime: { id: 'test-id', getURL: (p) => `chrome-extension://test-id/${p}` },
  };
}

/**
 * A scripted fetch: routes is a function (method, path, request) → {status, body} | Error.
 * Records every call in .calls ({method, path, auth, body}).
 */
export function fakeFetch(routes, { base = 'http://127.0.0.1:47821' } = {}) {
  const calls = [];
  const impl = async (url, init = {}) => {
    const u = new URL(url);
    const base0 = new URL(base);
    if (u.origin !== base0.origin) throw new TypeError(`unexpected origin ${u.origin}`);
    const path = u.pathname.slice(base0.pathname.replace(/\/$/, '').length) + u.search;
    const auth = (init.headers && init.headers.Authorization) || null;
    const body = init.body ? JSON.parse(init.body) : undefined;
    const call = { method: init.method || 'GET', path, auth, body };
    calls.push(call);
    let r = await routes(call.method, path, call);
    if (r instanceof Error) throw r;
    if (r === undefined) r = { status: 404, body: { detail: 'Not Found' } };
    const status = r.status ?? 200;
    const text = r.raw !== undefined ? r.raw : r.body === undefined ? '' : JSON.stringify(r.body);
    return {
      ok: status >= 200 && status < 300,
      status,
      async text() { return text; },
    };
  };
  impl.calls = calls;
  return impl;
}

export const ADMIN = { id: 1, email: 'owner@example.com', username: 'owner', full_name: null, is_active: true, is_superuser: true };
export const USER = { id: 2, email: 'user@example.com', username: 'user', full_name: null, is_active: true, is_superuser: false };

export function botStatus(overrides = {}) {
  return {
    enabled: false,
    halted: false,
    halt_reason: null,
    flatten_requested: false,
    stale: false,
    consecutive_failures: 0,
    consecutive_data_faults: 0,
    last_error: null,
    config_error: null,
    mode: 'paper',
    live_trading: false,
    cycle_minutes: 5,
    last_cycle_at: '2026-10-08T12:00:00',
    last_cycle_summary: { status: 'ok', daily_pnl_pct: 0.5, equity: 100500 },
    equity: 100500,
    cash: 50000,
    drawdown_pct: 1.25,
    open_positions: 0,
    ...overrides,
  };
}
