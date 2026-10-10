'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { DEFAULTS, PreferencesStore, parsePreferences, serializePreferences } = require('../lib/preferences');

test('defaults: keep the computer awake, tray notice not shown yet', () => {
  assert.deepEqual(DEFAULTS, { keepAwake: true, trayNoticeShown: false, lastVersion: '' });
  assert.deepEqual(parsePreferences(''), DEFAULTS);
  assert.deepEqual(parsePreferences('not json'), DEFAULTS);
  assert.deepEqual(parsePreferences('[1]'), DEFAULTS);
  assert.deepEqual(parsePreferences('null'), DEFAULTS);
});

test('parsePreferences keeps valid values and drops unknown or mistyped ones', () => {
  assert.deepEqual(parsePreferences('{"keepAwake": false, "trayNoticeShown": "yes", "lastVersion": "1.0.0", "evil": 1}'), {
    keepAwake: false,
    trayNoticeShown: false,
    lastVersion: '1.0.0',
  });
});

test('serializePreferences writes only known keys', () => {
  assert.deepEqual(JSON.parse(serializePreferences({ keepAwake: false, other: 1 })), {
    keepAwake: false,
    trayNoticeShown: false,
    lastVersion: '',
  });
});

test('PreferencesStore round-trips through the file', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'prefs-'));
  try {
    const file = path.join(dir, 'nested', 'desktop-preferences.json');
    const store = new PreferencesStore(file);
    assert.deepEqual(store.load(), DEFAULTS);
    store.set('keepAwake', false);
    store.set('trayNoticeShown', true);
    const again = new PreferencesStore(file);
    again.load();
    assert.equal(again.get('keepAwake'), false);
    assert.equal(again.get('trayNoticeShown'), true);
    assert.deepEqual(fs.readdirSync(path.dirname(file)), ['desktop-preferences.json']); // no temp files left
    assert.throws(() => store.set('keepAwake', 'no'), TypeError);
    assert.throws(() => store.get('nope'), /unknown/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
