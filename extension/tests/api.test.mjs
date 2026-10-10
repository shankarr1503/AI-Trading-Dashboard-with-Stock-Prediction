import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ACCESS_KEY, ApiClient, ApiError, AUTH_KEY, createTokenStore, errorDetail } from '../lib/api.js';
import { ADMIN, fakeFetch, makeChrome, PAIRING_CODE } from './fake-chrome.mjs';

const BASE = 'http://127.0.0.1:47821';

function pair(n) {
  return { access_token: `access-${n}`, refresh_token: `refresh-${n}`, token_type: 'bearer', expires_in: 1800 };
}

function setup(routes, { now = () => 1_000_000 } = {}) {
  const chrome = makeChrome();
  const tokens = createTokenStore(chrome, now);
  const fetchImpl = fakeFetch(routes);
  const client = new ApiClient({ baseUrl: BASE, tokens, fetchImpl, now, pairingCode: PAIRING_CODE });
  return { chrome, tokens, fetchImpl, client };
}

async function signIn(tokens, n = 1, server = BASE) {
  await tokens.save(server, pair(n));
}

test('requests carry the bearer token; health needs none', async () => {
  const { client, tokens, fetchImpl } = setup((m, p) => (p === '/health' ? { body: { status: 'healthy' } } : { body: { ok: true } }));
  await signIn(tokens);
  assert.deepEqual(await client.health(), { status: 'healthy' });
  await client.botStatus();
  assert.equal(fetchImpl.calls[0].auth, null);
  assert.equal(fetchImpl.calls[1].auth, 'Bearer access-1');
  assert.equal(fetchImpl.calls[1].path, '/api/bot/status');
});

test('a trailing slash in the base URL is tolerated and paths are encoded', async () => {
  const chrome = makeChrome();
  const tokens = createTokenStore(chrome);
  const fetchImpl = fakeFetch(() => ({ body: {} }));
  const client = new ApiClient({ baseUrl: `${BASE}/`, tokens, fetchImpl, pairingCode: PAIRING_CODE });
  await tokens.save(BASE, pair(1));
  await client.analyze('^GSPC');
  await client.fundamentals('BRK-B');
  await client.botDecisions(20);
  assert.deepEqual(fetchImpl.calls.map((c) => c.path), [
    '/api/bot/analyze/%5EGSPC', '/api/research/BRK-B/fundamentals', '/api/bot/decisions?limit=20',
  ]);
});

test('token storage: access token in session storage, refresh token in local storage', async () => {
  const { chrome, client } = setup((m, p) => {
    if (p === '/auth/login') return { body: pair(1) };
    if (p === '/auth/me') return { body: ADMIN };
    return undefined;
  });
  const user = await client.login(' owner@example.com ', 'pw-123456');
  assert.equal(user.username, 'owner');
  const session = chrome.storage.session.data[ACCESS_KEY];
  const local = chrome.storage.local.data[AUTH_KEY];
  assert.equal(session.token, 'access-1');
  assert.equal(session.server, BASE);
  assert.ok(!('token' in local) && !('accessToken' in local), 'access token never persisted to disk');
  assert.equal(local.refreshToken, 'refresh-1');
  assert.equal(local.server, BASE);
  assert.equal(local.user.is_superuser, true);
  assert.ok(!JSON.stringify(chrome.storage.local.data).includes('pw-123456'), 'password is never stored');
  assert.ok(!JSON.stringify(chrome.storage.session.data).includes('refresh-1'));
});

test('login sends trimmed email and the password only to /auth/login', async () => {
  const { client, fetchImpl } = setup((m, p) => (p === '/auth/login' ? { body: pair(1) } : { body: ADMIN }));
  await client.login(' owner@example.com ', 'secret-pw');
  assert.deepEqual(fetchImpl.calls[0].body, { email: 'owner@example.com', password: 'secret-pw' });
  assert.equal(fetchImpl.calls[1].path, '/auth/me');
  assert.equal(fetchImpl.calls[1].body, undefined);
});

test('logout clears both stores', async () => {
  const { chrome, client, tokens } = setup(() => ({ body: {} }));
  await signIn(tokens);
  await client.logout();
  assert.equal(chrome.storage.session.data[ACCESS_KEY], undefined);
  assert.equal(chrome.storage.local.data[AUTH_KEY], undefined);
  assert.equal(await client.isSignedIn(), false);
  await assert.rejects(client.botStatus(), (e) => e instanceof ApiError && e.kind === 'auth');
});

test('401 → one refresh → retry once with the new token', async () => {
  let statusCalls = 0;
  const { client, tokens, fetchImpl } = setup((m, p, call) => {
    if (p === '/auth/refresh') {
      assert.deepEqual(call.body, { refresh_token: 'refresh-1' });
      return { body: pair(2) };
    }
    if (p === '/api/bot/status') {
      statusCalls += 1;
      return call.auth === 'Bearer access-2' ? { body: { enabled: true } } : { status: 401, body: { detail: 'expired' } };
    }
    return undefined;
  });
  await signIn(tokens);
  assert.deepEqual(await client.botStatus(), { enabled: true });
  assert.equal(statusCalls, 2);
  assert.deepEqual(fetchImpl.calls.map((c) => c.path), ['/api/bot/status', '/auth/refresh', '/api/bot/status']);
  const t = await tokens.load(BASE);
  assert.equal(t.accessToken, 'access-2');
  assert.equal(t.refreshToken, 'refresh-2');
});

test('concurrent 401s share a single refresh (single flight)', async () => {
  let refreshes = 0;
  let release;
  const gate = new Promise((r) => { release = r; });
  const { client, tokens, fetchImpl } = setup(async (m, p, call) => {
    if (p === '/auth/refresh') {
      refreshes += 1;
      await gate;
      return { body: pair(2) };
    }
    return call.auth === 'Bearer access-2' ? { body: { path: p } } : { status: 401, body: { detail: 'expired' } };
  });
  await signIn(tokens);
  const pending = Promise.all([client.botStatus(), client.botPositions(), client.me().catch(() => null)]);
  await new Promise((r) => setTimeout(r, 20));
  release();
  const [status, positions] = await pending;
  assert.equal(refreshes, 1);
  assert.deepEqual(status, { path: '/api/bot/status' });
  assert.deepEqual(positions, { path: '/api/bot/positions' });
  assert.equal(fetchImpl.calls.filter((c) => c.path === '/auth/refresh').length, 1);
});

test('a 401 that arrives after another caller refreshed reuses the new token', async () => {
  const { client, tokens, fetchImpl } = setup((m, p, call) => {
    if (p === '/auth/refresh') return { body: pair(3) };
    return call.auth === 'Bearer access-2' ? { body: {} } : { status: 401, body: {} };
  });
  await signIn(tokens, 2);
  // Simulate: our request was sent with access-1, meanwhile access-2 got stored.
  const token = await client.refresh('access-1');
  assert.equal(token, 'access-2');
  assert.equal(fetchImpl.calls.length, 0);
});

test('retry happens only once: a second 401 signs out', async () => {
  const { client, tokens, fetchImpl } = setup((m, p) => (p === '/auth/refresh' ? { body: pair(2) } : { status: 401, body: {} }));
  await signIn(tokens);
  await assert.rejects(client.botStatus(), (e) => e.kind === 'auth' && e.status === 401);
  assert.equal(fetchImpl.calls.length, 3);
  assert.equal(await client.isSignedIn(), false);
});

test('a refused refresh clears the tokens', async () => {
  const { client, tokens } = setup((m, p) => (p === '/auth/refresh'
    ? { status: 401, body: { detail: 'Invalid refresh token' } } : { status: 401, body: {} }));
  await signIn(tokens);
  await assert.rejects(client.botStatus(), (e) => e.kind === 'auth' && /sign in again/i.test(e.message));
  assert.equal(await client.isSignedIn(), false);
});

test('a refresh that fails because the server is down keeps the tokens', async () => {
  const { client, tokens } = setup((m, p) => (p === '/auth/refresh' ? new TypeError('Failed to fetch') : { status: 401, body: {} }));
  await signIn(tokens);
  await assert.rejects(client.botStatus(), (e) => e.kind === 'network');
  assert.equal(await client.isSignedIn(), true);
});

test('an expired or missing access token is refreshed before the request', async () => {
  let t = 1_000_000;
  const now = () => t;
  const { client, tokens, fetchImpl } = setup((m, p) => (p === '/auth/refresh' ? { body: pair(2) } : { body: {} }), { now });
  await signIn(tokens);
  t += 1800 * 1000;   // past expiry (minus skew)
  await client.botStatus();
  assert.deepEqual(fetchImpl.calls.map((c) => [c.path, c.auth]), [
    ['/auth/refresh', null], ['/api/bot/status', 'Bearer access-2'],
  ]);
});

test('browser restart: no session token yet, refresh token in local storage', async () => {
  const { chrome, client, tokens, fetchImpl } = setup((m, p) => (p === '/auth/refresh' ? { body: pair(2) } : { body: {} }));
  await signIn(tokens);
  chrome.storage.session.data = {};   // session storage is memory only
  await client.botStatus();
  assert.equal(fetchImpl.calls[0].path, '/auth/refresh');
  assert.equal(fetchImpl.calls[1].auth, 'Bearer access-2');
});

test('tokens are bound to the server that issued them', async () => {
  const chrome = makeChrome();
  const tokens = createTokenStore(chrome);
  await tokens.save('https://other.example.com', pair(9));
  const fetchImpl = fakeFetch(() => ({ body: {} }));
  const client = new ApiClient({ baseUrl: BASE, tokens, fetchImpl });
  assert.equal(await client.isSignedIn(), false);
  await assert.rejects(client.botStatus(), (e) => e.kind === 'auth');
  assert.equal(fetchImpl.calls.length, 0, 'no token is sent to a different server');
});

test('network errors, timeouts and HTTP errors are distinguished', async () => {
  const chrome = makeChrome();
  const tokens = createTokenStore(chrome);
  const down = new ApiClient({ baseUrl: BASE, tokens, fetchImpl: async () => { throw new TypeError('Failed to fetch'); } });
  await assert.rejects(down.health(), (e) => e.kind === 'network' && /Cannot reach/.test(e.message));

  const hang = (url, init) => new Promise((resolve, reject) => {
    init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
  });
  const slow = new ApiClient({ baseUrl: BASE, tokens, fetchImpl: hang, timeoutMs: 30 });
  await assert.rejects(slow.send('/health'), (e) => e.kind === 'timeout');

  const http = new ApiClient({ baseUrl: BASE, tokens, fetchImpl: fakeFetch(() => ({ status: 409, body: { detail: 'Bot is halted (x)' } })) });
  await assert.rejects(http.send('/api/bot/start', { method: 'POST' }),
    (e) => e.kind === 'http' && e.status === 409 && e.message === 'Bot is halted (x)');
});

test('errorDetail understands FastAPI, validation and slowapi bodies', () => {
  assert.equal(errorDetail({ detail: 'Registration is closed' }, 403), 'Registration is closed');
  assert.equal(errorDetail({ detail: [{ loc: ['body', 'email'], msg: 'value is not a valid email address' }] }, 422),
    'email: value is not a valid email address');
  assert.equal(errorDetail({ error: 'Rate limit exceeded: 10 per 1 minute' }, 429), 'Rate limit exceeded: 10 per 1 minute');
  assert.equal(errorDetail(null, 429), 'Too many requests: try again in a minute');
  assert.equal(errorDetail('<html>Bad gateway</html>', 502), 'Server error (HTTP 502)');
  assert.equal(errorDetail('', 418), 'HTTP 418');
});

test('non-JSON success bodies are returned as text', async () => {
  const chrome = makeChrome();
  const client = new ApiClient({ baseUrl: BASE, tokens: createTokenStore(chrome), fetchImpl: fakeFetch(() => ({ raw: 'pong' })) });
  assert.equal(await client.send('/ping'), 'pong');
});

test('setUser does not rewrite storage when nothing changed', async () => {
  const chrome = makeChrome();
  const tokens = createTokenStore(chrome);
  await tokens.save(BASE, pair(1));
  await tokens.setUser(BASE, ADMIN);
  const writes = chrome.storage.local.writes;
  await tokens.setUser(BASE, { ...ADMIN });
  assert.equal(chrome.storage.local.writes, writes);
  await tokens.setUser('https://elsewhere.example', ADMIN);   // other server: ignored
  assert.equal(chrome.storage.local.writes, writes);
});

test('a refresh keeps the cached user, a new login drops it', async () => {
  const chrome = makeChrome();
  const tokens = createTokenStore(chrome);
  await tokens.save(BASE, pair(1));
  await tokens.setUser(BASE, ADMIN);
  await tokens.save(BASE, pair(2));
  assert.equal((await tokens.load(BASE)).user.id, ADMIN.id);
  await tokens.save(BASE, pair(3), { keepUser: false });
  assert.equal((await tokens.load(BASE)).user, null);
});
