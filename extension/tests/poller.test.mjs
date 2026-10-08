import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createTokenStore } from '../lib/api.js';
import { ALARM_NAME, ensureAlarm, lastSnapshot, MONITOR_KEY, runPoll } from '../lib/poller.js';
import { saveSettings } from '../lib/settings.js';
import { ADMIN, botStatus, fakeFetch, makeChrome, USER } from './fake-chrome.mjs';

const BASE = 'http://127.0.0.1:47821';
const now = () => Date.parse('2026-10-08T12:05:00Z');

function server({ user = ADMIN, bot = () => botStatus(), up = () => true } = {}) {
  return fakeFetch((method, path) => {
    if (!up()) return new TypeError('Failed to fetch');
    if (path === '/health') return { body: { status: 'healthy', service: 'AI Trading Platform', version: '2.0.0' } };
    if (path === '/auth/me') return { body: user };
    if (path === '/api/bot/status') return user.is_superuser ? { body: bot() } : { status: 403, body: { detail: 'Admin only' } };
    return undefined;
  });
}

async function signedIn(chrome, server_ = BASE) {
  await createTokenStore(chrome, now).save(server_, { access_token: 'a', refresh_token: 'r', expires_in: 1800 });
}

test('server down: DOWN badge, no notifications, nothing thrown', async () => {
  const chrome = makeChrome();
  await signedIn(chrome);
  const r = await runPoll({ chromeApi: chrome, fetchImpl: server({ up: () => false }), now });
  assert.equal(r.snapshot.phase, 'disconnected');
  assert.equal(chrome.badge.text, 'DOWN');
  assert.match(chrome.badge.title, /Server not reachable at http:\/\/127\.0\.0\.1:47821/);
  assert.equal(chrome.sent.length, 0);
  assert.equal((await lastSnapshot(chrome)).phase, 'disconnected');
  assert.equal(r.snapshot.error, null, 'the generic network error adds nothing to "not reachable"');
});

test('server answering with an error: disconnected, with the reason', async () => {
  const chrome = makeChrome();
  const fetchImpl = fakeFetch(() => ({ status: 502, raw: '<html>Bad gateway</html>' }));
  const r = await runPoll({ chromeApi: chrome, fetchImpl, now });
  assert.equal(r.snapshot.phase, 'disconnected');
  assert.equal(r.snapshot.error, 'Server error (HTTP 502)');
  assert.match(chrome.badge.title, /Server error \(HTTP 502\)/);
});

test('not signed in: "?" badge and no authenticated calls', async () => {
  const chrome = makeChrome();
  const fetchImpl = server();
  const r = await runPoll({ chromeApi: chrome, fetchImpl, now });
  assert.equal(r.snapshot.phase, 'signed_out');
  assert.equal(chrome.badge.text, '?');
  assert.deepEqual(fetchImpl.calls.map((c) => c.path), ['/health']);
});

test('non-admin: connectivity only, the bot status is never requested', async () => {
  const chrome = makeChrome();
  await signedIn(chrome);
  const fetchImpl = server({ user: USER });
  const r = await runPoll({ chromeApi: chrome, fetchImpl, now });
  assert.equal(r.snapshot.phase, 'user');
  assert.equal(chrome.badge.text, '');
  assert.deepEqual(fetchImpl.calls.map((c) => c.path), ['/health', '/auth/me']);
});

test('admin who lost the role (403) is shown as a plain user', async () => {
  const chrome = makeChrome();
  await signedIn(chrome);
  const fetchImpl = fakeFetch((m, p) => {
    if (p === '/health') return { body: {} };
    if (p === '/auth/me') return { body: ADMIN };
    return { status: 403, body: { detail: 'Not enough permissions' } };
  });
  const r = await runPoll({ chromeApi: chrome, fetchImpl, now });
  assert.equal(r.snapshot.phase, 'user');
});

test('remote server without the host permission: "!" and no request at all', async () => {
  const chrome = makeChrome();
  await saveSettings({ serverUrl: 'https://bot.example.com' }, chrome);
  const fetchImpl = fakeFetch(() => ({ body: {} }), { base: 'https://bot.example.com' });
  const r = await runPoll({ chromeApi: chrome, fetchImpl, now });
  assert.equal(r.snapshot.phase, 'no_access');
  assert.equal(chrome.badge.text, '!');
  assert.equal(fetchImpl.calls.length, 0);
});

test('remote server with the permission is polled', async () => {
  const chrome = makeChrome({ granted: ['https://bot.example.com/*'] });
  await saveSettings({ serverUrl: 'https://bot.example.com' }, chrome);
  const fetchImpl = fakeFetch((m, p) => (p === '/health' ? { body: {} } : undefined), { base: 'https://bot.example.com' });
  const r = await runPoll({ chromeApi: chrome, fetchImpl, now });
  assert.equal(r.snapshot.phase, 'signed_out');
  assert.equal(fetchImpl.calls[0].path, '/health');
});

test('admin: badge follows the bot and notifications fire once per transition', async () => {
  const chrome = makeChrome();
  await signedIn(chrome);
  let state = botStatus({ enabled: true });
  let up = true;
  const fetchImpl = server({ bot: () => state, up: () => up });
  const poll = () => runPoll({ chromeApi: chrome, fetchImpl, now });

  await poll();
  assert.equal(chrome.badge.text, 'RUN');
  assert.equal(chrome.badge.color, '#00d4aa');
  assert.equal(chrome.badge.textColor, '#0a0b0d');
  assert.equal(chrome.sent.length, 0);

  state = botStatus({ halted: true, halt_reason: 'KILL: drawdown 10.4% ≥ 10%' });
  const r = await poll();
  assert.equal(chrome.badge.text, 'HALT');
  assert.equal(r.notifications.length, 1);
  assert.equal(chrome.sent.length, 1);
  assert.equal(chrome.sent[0].id, 'tradebot-halted');
  assert.equal(chrome.sent[0].iconUrl, 'chrome-extension://test-id/icons/icon128.png');
  assert.equal(chrome.sent[0].requireInteraction, true);

  await poll();
  await poll();
  assert.equal(chrome.sent.length, 1, 'no repeat on later polls');

  up = false;   // server outage while halted
  await poll();
  assert.equal(chrome.badge.text, 'DOWN');
  up = true;
  await poll();
  assert.equal(chrome.badge.text, 'HALT');
  assert.equal(chrome.sent.length, 1, 'coming back after an outage does not repeat the alert');
  assert.deepEqual(chrome.storage.local.data[MONITOR_KEY].conditions,
    { halted: true, flatten: false, stale: false, failures: false, dataFaults: false });
});

test('alert memory is per server and account', async () => {
  const chrome = makeChrome();
  await signedIn(chrome);
  const halted = botStatus({ halted: true });
  await runPoll({ chromeApi: chrome, fetchImpl: server({ bot: () => halted }), now });
  assert.equal(chrome.sent.length, 1);
  // Another administrator account on the same server: its first observation reports the halt too.
  await runPoll({ chromeApi: chrome, fetchImpl: server({ user: { ...ADMIN, id: 7 }, bot: () => halted }), now });
  assert.equal(chrome.sent.length, 2);
});

test('blocked notifications do not break the poll and are not retried every minute', async () => {
  const chrome = makeChrome({ notificationsFail: true });
  await signedIn(chrome);
  const fetchImpl = server({ bot: () => botStatus({ halted: true }) });
  const r1 = await runPoll({ chromeApi: chrome, fetchImpl, now });
  const r2 = await runPoll({ chromeApi: chrome, fetchImpl, now });
  assert.equal(chrome.badge.text, 'HALT');
  assert.equal(r1.notifications.length, 0);
  assert.equal(r2.notifications.length, 0);
});

test('notification preferences are honoured', async () => {
  const chrome = makeChrome();
  await signedIn(chrome);
  await saveSettings({ notify: { halted: false } }, chrome);
  await runPoll({ chromeApi: chrome, fetchImpl: server({ bot: () => botStatus({ halted: true }) }), now });
  assert.equal(chrome.badge.text, 'HALT');
  assert.equal(chrome.sent.length, 0);
});

test('ensureAlarm creates the alarm and follows the interval', async () => {
  const chrome = makeChrome();
  assert.equal(await ensureAlarm(chrome, 1), true);
  assert.deepEqual(chrome.alarmMap.get(ALARM_NAME), { name: ALARM_NAME, periodInMinutes: 1, delayInMinutes: 1 });
  assert.equal(await ensureAlarm(chrome, 1), false, 'unchanged: left alone');
  assert.equal(await ensureAlarm(chrome, 5), true);
  assert.equal(chrome.alarmMap.get(ALARM_NAME).periodInMinutes, 5);
});
