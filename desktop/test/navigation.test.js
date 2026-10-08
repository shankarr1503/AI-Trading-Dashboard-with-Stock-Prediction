'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { classifyNavigation, isSafeExternalUrl, originOf } = require('../lib/navigation');

const APP = 'http://127.0.0.1:47821';

test('pages on the local server stay in the app', () => {
  for (const url of [`${APP}/`, `${APP}/dashboard/`, `${APP}/bot/?tab=1#x`, 'http://127.0.0.1:47821']) {
    assert.equal(classifyNavigation(url, APP), 'allow', url);
  }
});

test('other http(s) pages open in the system browser', () => {
  for (const url of [
    'https://example.com/',
    'http://example.com/',
    'http://127.0.0.1:47822/', // another port is another origin
    'http://localhost:47821/', // another host name is another origin
    'https://127.0.0.1:47821/',
    'https://finance.yahoo.com/quote/AAPL',
  ]) {
    assert.equal(classifyNavigation(url, APP), 'external', url);
  }
});

test('everything else is refused', () => {
  for (const url of [
    'file:///etc/passwd',
    'javascript:alert(1)',
    'data:text/html,<script>1</script>',
    'chrome://settings',
    'devtools://devtools/bundled/inspector.html',
    'ftp://example.com/',
    'mailto:me@example.com',
    'about:blank',
    'not a url',
    '',
    'http://user:pass@127.0.0.1:47821/',
    'https://user@example.com/',
  ]) {
    assert.equal(classifyNavigation(url, APP), 'deny', url);
  }
});

test('before the server is ready nothing counts as the app', () => {
  assert.equal(classifyNavigation(`${APP}/dashboard/`, null), 'external');
  assert.equal(classifyNavigation('file:///x', null), 'deny');
});

test('originOf', () => {
  assert.equal(originOf('http://127.0.0.1:47821/dashboard/'), APP);
  assert.equal(originOf('file:///x'), null);
  assert.equal(originOf('nonsense'), null);
  assert.equal(originOf(undefined), null);
});

test('isSafeExternalUrl only accepts plain http(s)', () => {
  assert.equal(isSafeExternalUrl('https://example.com/a?b=c'), true);
  assert.equal(isSafeExternalUrl('http://example.com'), true);
  for (const url of ['file:///C:/Windows/System32/calc.exe', 'smb://host/share', 'ms-settings:', 'https://u:p@example.com', 'x']) {
    assert.equal(isSafeExternalUrl(url), false, url);
  }
});
