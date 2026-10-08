import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  dateRange, money, modeLabel, num, parseServerTime, pct, qty, relativeTime, signClass, truncate,
} from '../lib/format.js';
import {
  DEFAULT_SERVER_URL, DESKTOP_PORTS, dashboardUrl, findDesktopServers, hasServerPermission, isLoopbackHost,
  normalizeServerUrl, permissionPatternFor, ServerUrlError, tryNormalizeServerUrl,
} from '../lib/server.js';
import { clampPollMinutes, defaultSettings, getSettings, sanitizeSettings, saveSettings } from '../lib/settings.js';
import { makeChrome } from './fake-chrome.mjs';

test('server URL normalisation', () => {
  assert.equal(normalizeServerUrl('http://127.0.0.1:47821'), 'http://127.0.0.1:47821');
  assert.equal(normalizeServerUrl('  http://127.0.0.1:47821/  '), 'http://127.0.0.1:47821');
  assert.equal(normalizeServerUrl('127.0.0.1:47822'), 'http://127.0.0.1:47822');
  assert.equal(normalizeServerUrl('localhost:47821'), 'http://localhost:47821');
  assert.equal(normalizeServerUrl('HTTP://LOCALHOST:47821'), 'http://localhost:47821');
  assert.equal(normalizeServerUrl('trading.example.com'), 'https://trading.example.com');
  assert.equal(normalizeServerUrl('https://trading.example.com:8443/'), 'https://trading.example.com:8443');
  assert.equal(normalizeServerUrl('https://example.com/bot-api//'), 'https://example.com/bot-api');
  assert.equal(normalizeServerUrl('https://example.com:443'), 'https://example.com');
});

test('plain http is refused for anything but loopback, and junk is rejected', () => {
  const bad = [
    '', '   ', 'http://192.168.1.10:47821', 'http://example.com', 'http://127.0.0.1.nip.io', 'ftp://example.com',
    'javascript:alert(1)', 'https://user:pw@example.com', 'https://example.com/?x=1', 'https://example.com/#a',
    'http://[::1]:47821', 'https://',
  ];
  for (const input of bad) {
    assert.throws(() => normalizeServerUrl(input), ServerUrlError, `should reject ${input}`);
    assert.equal(tryNormalizeServerUrl(input), null);
  }
  assert.throws(() => normalizeServerUrl('http://10.0.0.5:47821'), /https/);
});

test('loopback hosts and host permission patterns', () => {
  assert.ok(isLoopbackHost('127.0.0.1'));
  assert.ok(isLoopbackHost('LOCALHOST'));
  assert.ok(!isLoopbackHost('127.0.0.2'));
  assert.equal(permissionPatternFor('http://127.0.0.1:47821'), null);
  assert.equal(permissionPatternFor('http://localhost:47825'), null);
  assert.equal(permissionPatternFor('https://trading.example.com:8443/api'), 'https://trading.example.com/*');
  assert.equal(permissionPatternFor('https://127.0.0.1:8443'), 'https://127.0.0.1/*');
});

test('hasServerPermission checks the optional permission only for remote servers', async () => {
  const chrome = makeChrome({ granted: ['https://ok.example.com/*'] });
  assert.equal(await hasServerPermission('http://127.0.0.1:47821', chrome), true);
  assert.equal(await hasServerPermission('https://ok.example.com', chrome), true);
  assert.equal(await hasServerPermission('https://nope.example.com', chrome), false);
});

test('dashboard URLs use the trailing-slash export', () => {
  assert.equal(dashboardUrl('http://127.0.0.1:47821', 'bot'), 'http://127.0.0.1:47821/bot/');
  assert.equal(dashboardUrl('https://x.example/app', '/research/'), 'https://x.example/app/research/');
  assert.equal(dashboardUrl('http://127.0.0.1:47821', ''), 'http://127.0.0.1:47821/');
});

test('settings defaults, clamping and sanitising', async () => {
  assert.deepEqual(defaultSettings().serverUrl, DEFAULT_SERVER_URL);
  assert.equal(clampPollMinutes(0), 1);
  assert.equal(clampPollMinutes(99), 15);
  assert.equal(clampPollMinutes('7'), 7);
  assert.equal(clampPollMinutes('x'), 1);
  const s = sanitizeSettings({ serverUrl: 'http://evil.example', pollMinutes: 30, notify: { stale: false, bogus: true, halted: 'no' } });
  assert.equal(s.serverUrl, DEFAULT_SERVER_URL, 'an invalid stored URL falls back to the default');
  assert.equal(s.pollMinutes, 15);
  assert.equal(s.notify.stale, false);
  assert.equal(s.notify.halted, true);
  assert.ok(!('bogus' in s.notify));

  const chrome = makeChrome();
  assert.deepEqual(await getSettings(chrome), defaultSettings());
  await saveSettings({ serverUrl: 'https://bot.example.com/', notify: { failures: false } }, chrome);
  await saveSettings({ pollMinutes: 5, notify: { stale: false } }, chrome);
  const saved = await getSettings(chrome);
  assert.equal(saved.serverUrl, 'https://bot.example.com');
  assert.equal(saved.pollMinutes, 5);
  assert.equal(saved.notify.failures, false, 'notify keys merge instead of being replaced');
  assert.equal(saved.notify.stale, false);
  assert.equal(saved.notify.halted, true);
});

test('server timestamps without an offset are UTC', () => {
  assert.equal(parseServerTime('2026-10-08T12:00:00').toISOString(), '2026-10-08T12:00:00.000Z');
  assert.equal(parseServerTime('2026-10-08T12:00:00.123456').toISOString(), '2026-10-08T12:00:00.123Z');
  assert.equal(parseServerTime('2026-10-08 12:00:00').toISOString(), '2026-10-08T12:00:00.000Z');
  assert.equal(parseServerTime('2026-10-08T12:00:00+02:00').toISOString(), '2026-10-08T10:00:00.000Z');
  assert.equal(parseServerTime('2026-10-08T12:00:00.5Z').toISOString(), '2026-10-08T12:00:00.500Z');
  assert.equal(parseServerTime(null), null);
  assert.equal(parseServerTime('garbage'), null);
  // Snapshot times are epoch milliseconds.
  assert.equal(parseServerTime(Date.parse('2026-10-08T12:00:00Z')).toISOString(), '2026-10-08T12:00:00.000Z');
  assert.equal(parseServerTime(NaN), null);
});

test('relative times', () => {
  const now = Date.parse('2026-10-08T12:00:00Z');
  assert.equal(relativeTime(null, now), 'never');
  assert.equal(relativeTime('2026-10-08T11:59:50', now), 'just now');
  assert.equal(relativeTime('2026-10-08T11:55:00', now), '5 min ago');
  assert.equal(relativeTime('2026-10-08T09:00:00', now), '3 h ago');
  assert.equal(relativeTime('2026-10-05T12:00:00', now), '3 d ago');
  assert.equal(relativeTime('2026-10-08T12:10:00Z', now), 'in 10 min');
  assert.equal(relativeTime(now - 5 * 60_000, now), '5 min ago');
});

test('numbers, money and percentages', () => {
  assert.equal(money(100500), '$100,500.00');
  assert.equal(money(-12.5), '−$12.50');
  assert.equal(money(450.4, { signed: true, digits: 0 }), '+$450');
  assert.equal(money(10, { currency: 'INR' }), '10.00 INR');
  assert.equal(money(null), '—');
  assert.equal(money(NaN), '—');
  assert.equal(num(189.2345), '189.23');
  assert.equal(pct(1.234), '1.23%');
  assert.equal(pct(0.5, { signed: true }), '+0.50%');
  assert.equal(pct(-2, { signed: true, digits: 1 }), '−2.0%');
  assert.equal(pct(undefined), '—');
  assert.equal(qty(100), '100');
  assert.equal(qty(1.23456), '1.2346');
  assert.equal(signClass(2), 'up');
  assert.equal(signClass(-0.1), 'down');
  assert.equal(signClass(0), 'flat');
  assert.equal(signClass(null), 'flat');
  assert.equal(modeLabel('alpaca_live'), 'LIVE');
  assert.equal(modeLabel('paper'), 'PAPER');
  assert.equal(truncate('a  b\n c', 10), 'a b c');
  assert.equal(truncate('x'.repeat(20), 10), `${'x'.repeat(9)}…`);
  assert.equal(dateRange('2026-10-30', '2026-10-30'), 'Oct 30, 2026');
  assert.match(dateRange('2026-10-30', '2026-11-03'), /Oct 30, 2026 – Nov 3, 2026 \(unconfirmed\)/);
  assert.equal(dateRange(null), '—');
});

test('finding running desktop apps on the desktop port range', async () => {
  assert.equal(DESKTOP_PORTS[0], 47821);
  assert.equal(DESKTOP_PORTS[DESKTOP_PORTS.length - 1], 47841);
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push(url);
    assert.equal(init.credentials, 'omit');
    const port = Number(new URL(url).port);
    const reply = (status, body) => ({ ok: status < 300, status, text: async () => (typeof body === 'string' ? body : JSON.stringify(body)) });
    if (port === 47821) return reply(200, { status: 'healthy', service: 'Some other app' });   // not ours
    if (port === 47823) return reply(200, { status: 'healthy', service: 'AI Trading Platform', version: '2.0.0' });
    if (port === 47824) return reply(500, { detail: 'boom' });
    if (port === 47825) return reply(200, 'not json');
    if (port === 47830) return reply(200, { status: 'healthy', service: 'AI Trading Platform' });
    if (port === 47831) {   // hangs until the probe's timeout aborts it, like a real fetch
      return new Promise((resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted'))));
    }
    throw new TypeError('connection refused');
  };
  const found = await findDesktopServers({ fetchImpl, timeoutMs: 50 });
  assert.deepEqual(found, ['http://127.0.0.1:47823', 'http://127.0.0.1:47830']);
  assert.equal(seen.length, 21);
  assert.ok(seen.every((u) => u.startsWith('http://127.0.0.1:') && u.endsWith('/health')));
  assert.deepEqual(await findDesktopServers({ fetchImpl: async () => { throw new TypeError('x'); } }), []);
});
