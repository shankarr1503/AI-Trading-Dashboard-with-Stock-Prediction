// Pairing with the desktop app (SEC-2): no password and no token goes to a server on
// this computer that cannot prove it knows the app's pairing code.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ApiClient, createTokenStore } from '../lib/api.js';
import {
  ensurePaired, forgetPairings, normalizePairingCode, PAIR_TTL_MS, pairingProof, pairingRequired, sameText,
  tryNormalizePairingCode, verifyServer,
} from '../lib/pairing.js';
import { runPoll } from '../lib/poller.js';
import { describe } from '../lib/status.js';
import { ADMIN, botStatus, fakeFetch, makeChrome, PAIRING_CODE } from './fake-chrome.mjs';

const APP = 'http://127.0.0.1:47821';
const NONCE = '0123456789abcdef'.repeat(4);

function pair(n = 1) {
  return { access_token: `access-${n}`, refresh_token: `refresh-${n}`, token_type: 'bearer', expires_in: 1800 };
}

/** The real app's API (login, refresh, bot status) behind fakeFetch, answering pairing for `pairing`. */
function server({ pairing = PAIRING_CODE, base = APP } = {}) {
  return fakeFetch((method, path) => {
    if (path === '/health') return { body: { status: 'healthy', service: 'AI Trading Platform' } };
    if (path === '/auth/login') return { body: pair(1) };
    if (path === '/auth/refresh') return { body: pair(2) };
    if (path === '/auth/me') return { body: ADMIN };
    if (path === '/api/bot/status') return { body: botStatus() };
    return undefined;
  }, { pairing, base });
}

function client(fetchImpl, { code = PAIRING_CODE, baseUrl = APP, chrome = makeChrome(), now } = {}) {
  return new ApiClient({ baseUrl, tokens: createTokenStore(chrome, now), fetchImpl, pairingCode: code, now });
}

test('the proof is the backend\'s HMAC-SHA256 and is bound to the server URL', async () => {
  // Same vectors as backend.main.pairing_proof (tests/test_desktop.py checks that side).
  assert.equal(await pairingProof(PAIRING_CODE, APP, NONCE),
    '197b5533b9f0f97cbb5bc87c0117a643e468e6358c90bab93caa28466c078efd');
  assert.equal(await pairingProof(PAIRING_CODE, 'http://127.0.0.1:47822', NONCE),
    'ae28557286e0b6b162e990f268863cf8b969f08294f026324e41f448290c2aa0');
  assert.ok(sameText('abc', 'abc') && !sameText('abc', 'abd') && !sameText('abc', 'abcd'));
});

test('pairing codes are accepted as copied from the tray (dashes, spaces, lower case)', () => {
  assert.equal(normalizePairingCode('abcd-efgh-ijkl-mnop-qrst'), PAIRING_CODE);
  assert.equal(normalizePairingCode('  ABCD EFGH IJKL MNOP QRST '), PAIRING_CODE);
  for (const bad of ['', 'short', 'ABCD-EFGH-IJKL-MNOP-QRS1', 'not a pairing code at all!!']) {
    assert.throws(() => normalizePairingCode(bad), /not a pairing code/);
    assert.equal(tryNormalizePairingCode(bad), '');
  }
});

test('which servers need the pairing check', () => {
  assert.equal(pairingRequired(APP, ''), true);                         // the desktop app's port range
  assert.equal(pairingRequired('http://127.0.0.1:47841', ''), true);
  assert.equal(pairingRequired('http://localhost:47830', ''), true);
  assert.equal(pairingRequired('http://127.0.0.1:8000', ''), false);      // another local server, no code
  assert.equal(pairingRequired('http://127.0.0.1:8000', PAIRING_CODE), true); // once a code is set: every local server
  assert.equal(pairingRequired('https://bot.example.com', PAIRING_CODE), false); // TLS authenticates it
  assert.equal(pairingRequired('garbage', PAIRING_CODE), false);
});

test('verifyServer: the paired app passes; anything else is refused with a reason', async () => {
  assert.equal(await verifyServer({ baseUrl: APP, code: PAIRING_CODE, fetchImpl: server() }), true);
  const refused = async (fetchImpl, kind, pattern, code = PAIRING_CODE) => {
    await assert.rejects(verifyServer({ baseUrl: APP, code, fetchImpl }), (e) => {
      assert.equal(e.kind, kind);
      assert.match(e.message, pattern);
      return true;
    });
  };
  await refused(server({ pairing: 'ZZZZZZZZZZZZZZZZZZZZ' }), 'mismatch', /could not prove.*wrong pairing code.*Nothing was sent/);
  await refused(server({ pairing: null }), 'mismatch', /it has no pairing check/);           // 404: not the app
  await refused(fakeFetch(() => ({ body: { proof: 'x' } }), { pairing: null }), 'mismatch', /answers for another address/);
  await refused(fakeFetch(() => ({ raw: 'hello' }), { pairing: null }), 'mismatch', /unexpected answer/);
  await refused(async () => { throw new TypeError('Failed to fetch'); }, 'network', /Cannot reach/);
  await refused(server(), 'unpaired', /Pair the extension/, '');
});

test('a relayed challenge fails: the real app on 47822 proves 47822, not the impostor\'s 47821', async () => {
  const realApp = server({ base: 'http://127.0.0.1:47822' });
  // The impostor on 47821 forwards the extension's request to the real app and returns its answer.
  const relay = async (url, init) => realApp(url.replace(':47821', ':47822'), init);
  await assert.rejects(verifyServer({ baseUrl: APP, code: PAIRING_CODE, fetchImpl: relay }),
    (e) => e.kind === 'mismatch' && /it is the app at http:\/\/127\.0\.0\.1:47822, use that address/.test(e.message));
});

test('an impostor never receives the password or a token', async () => {
  forgetPairings();
  const chrome = makeChrome();
  const impostor = server({ pairing: 'ZZZZZZZZZZZZZZZZZZZZ' });
  const c = client(impostor, { chrome });
  await assert.rejects(c.login('owner@example.com', 'the-password'), (e) => e.kind === 'pairing' && e.detail === 'mismatch');
  // Signed in earlier (tokens stored for this URL), then something else took the port:
  await createTokenStore(chrome).save(APP, pair(1));
  await assert.rejects(c.botStatus(), (e) => e.kind === 'pairing');
  await createTokenStore(chrome).save(APP, { ...pair(1), expires_in: 0 });  // expired: would need a refresh
  await assert.rejects(c.me(), (e) => e.kind === 'pairing');
  assert.deepEqual(impostor.calls, [], 'no request other than the pairing check reached it');
  assert.ok(impostor.pairCalls.length >= 3 && impostor.pairCalls.every((p) => p.auth === null && !p.body));

  // Without a pairing code nothing is sent at all (not even the check).
  const unpaired = server();
  await assert.rejects(client(unpaired, { code: '' }).login('owner@example.com', 'pw'),
    (e) => e.kind === 'pairing' && e.detail === 'unpaired');
  assert.deepEqual([unpaired.calls, unpaired.pairCalls], [[], []]);
});

test('the paired app: sign-in checks every time, requests at least once a minute', async () => {
  forgetPairings();
  let t = 1_000_000;
  const now = () => t;
  const app = server();
  const c = client(app, { now });
  assert.equal((await c.login('owner@example.com', 'pw')).email, ADMIN.email);
  assert.equal(app.pairCalls.length, 1);
  await c.botStatus();
  await c.botStatus();
  assert.equal(app.pairCalls.length, 1, 'reused within PAIR_TTL_MS');
  t += PAIR_TTL_MS + 1;
  await c.botStatus();
  assert.equal(app.pairCalls.length, 2, 'checked again after a minute');
  await c.login('owner@example.com', 'pw');
  assert.equal(app.pairCalls.length, 3, 'a sign-in always checks first');
  assert.equal(app.calls.filter((x) => x.path === '/auth/login').length, 2);
});

test('servers that need no pairing are not challenged', async () => {
  const local = server({ base: 'http://127.0.0.1:8000', pairing: null });
  const c = client(local, { baseUrl: 'http://127.0.0.1:8000', code: '' });
  assert.equal((await c.login('owner@example.com', 'pw')).email, ADMIN.email);
  assert.equal(await ensurePaired({ baseUrl: 'https://bot.example.com', code: PAIRING_CODE }), 'not-required');
});

test('poller: not paired → PAIR badge; an impostor → "!!" badge, and neither gets a token', async () => {
  const chrome = makeChrome();
  await createTokenStore(chrome).save(APP, pair(1));
  let r = await runPoll({ chromeApi: chrome, fetchImpl: server() });
  assert.equal(r.snapshot.phase, 'unpaired');
  assert.equal(chrome.badge.text, 'PAIR');
  assert.match(chrome.badge.title, /NOT PAIRED[\s\S]*pairing code/);

  chrome.storage.local.data.settings = { pairingCode: PAIRING_CODE };
  forgetPairings();
  const impostor = server({ pairing: 'ZZZZZZZZZZZZZZZZZZZZ' });
  r = await runPoll({ chromeApi: chrome, fetchImpl: impostor });
  assert.equal(r.snapshot.phase, 'untrusted');
  assert.equal(chrome.badge.text, '!!');
  assert.match(chrome.badge.title, /UNVERIFIED SERVER[\s\S]*could not prove/);
  assert.deepEqual(impostor.calls.map((x) => x.path), ['/health']);
  assert.ok(impostor.calls.every((x) => x.auth === null));

  r = await runPoll({ chromeApi: chrome, fetchImpl: server() });
  assert.equal(r.snapshot.phase, 'admin');
  assert.equal(describe(r.snapshot).key, 'paused');
});
