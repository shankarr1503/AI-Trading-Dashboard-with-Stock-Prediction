'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { browserDataDir, defaultDataDir } = require('../lib/paths');

test('the data folder matches the sidecar default (backend/desktop.py default_data_dir)', () => {
  assert.equal(defaultDataDir('win32', { APPDATA: 'C:\\Users\\me\\AppData\\Roaming' }, 'C:\\Users\\me'), 'C:\\Users\\me\\AppData\\Roaming\\AI Trading Bot');
  assert.equal(defaultDataDir('win32', {}, 'C:\\Users\\me'), 'C:\\Users\\me\\AppData\\Roaming\\AI Trading Bot');
  assert.equal(defaultDataDir('darwin', {}, '/Users/me'), '/Users/me/Library/Application Support/AI Trading Bot');
  assert.equal(defaultDataDir('linux', { XDG_DATA_HOME: '/ignored' }, '/home/me'), '/home/me/.local/share/ai-trading-bot');
});

test('Chromium data goes into a subfolder', () => {
  assert.match(browserDataDir('/home/me/.local/share/ai-trading-bot'), /ai-trading-bot[\\/]browser$/);
});
