'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { addsDesktopToken, formatPairingCode, readPairingCode } = require('../lib/pairing');

test('the pairing code is read from <data dir>/pairing.key and shown in groups of four', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pairing-'));
  try {
    assert.equal(readPairingCode(dir), null); // not created yet (the sidecar writes it)
    assert.equal(readPairingCode(null), null);
    fs.writeFileSync(path.join(dir, 'pairing.key'), 'abcdefghijklmnopqrst\n');
    assert.equal(readPairingCode(dir), 'ABCDEFGHIJKLMNOPQRST');
    assert.equal(formatPairingCode('ABCDEFGHIJKLMNOPQRST'), 'ABCD-EFGH-IJKL-MNOP-QRST');
    fs.writeFileSync(path.join(dir, 'pairing.key'), 'not a code 0189');
    assert.equal(readPairingCode(dir), null);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('the control token goes only on the app window\'s own POST /auth/register', () => {
  const appUrl = 'http://127.0.0.1:47821';
  const ok = { url: `${appUrl}/auth/register`, method: 'POST', appUrl, fromAppWindow: true };
  assert.equal(addsDesktopToken(ok), true);
  assert.equal(addsDesktopToken({ ...ok, method: 'post' }), true);
  assert.equal(addsDesktopToken({ ...ok, url: `${appUrl}/auth/register?x=1` }), true);
  // Anything else: no token.
  assert.equal(addsDesktopToken({ ...ok, fromAppWindow: false }), false); // another window / web contents
  assert.equal(addsDesktopToken({ ...ok, method: 'GET' }), false);
  assert.equal(addsDesktopToken({ ...ok, url: `${appUrl}/auth/login` }), false);
  assert.equal(addsDesktopToken({ ...ok, url: `${appUrl}/api/desktop/shutdown` }), false);
  assert.equal(addsDesktopToken({ ...ok, url: 'http://127.0.0.1:47822/auth/register' }), false); // another port
  assert.equal(addsDesktopToken({ ...ok, url: 'http://localhost:47821/auth/register' }), false);
  assert.equal(addsDesktopToken({ ...ok, url: 'https://evil.example/auth/register' }), false);
  assert.equal(addsDesktopToken({ ...ok, appUrl: null }), false); // no server running
  assert.equal(addsDesktopToken({ ...ok, url: 'not a url' }), false);
});
